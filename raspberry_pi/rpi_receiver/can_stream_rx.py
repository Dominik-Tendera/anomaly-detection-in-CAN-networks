#!/usr/bin/env python3
"""Reference receiver for the STM32 CAN record stream, for a Raspberry Pi 4B.

Scope: this program stores the trace and measures the link. It deliberately does
no anomaly detection. The detection pipeline is a separate piece of work and it
will consume the files written here.

What it writes, all named after the session identifier:
  <session>.canbin   every byte received, untouched, so the whole session can be
                     replayed offline and any analysis repeated on identical data
  <session>.log      frames in the text format v2 used by the logger on the USB
                     drive, so the existing CAN_PARSER and its Vector ASC export
                     can be used without a second decoder
  <session>.report.json  counters, sequence gaps, throughput and the device
                     statistics needed to judge whether the trace is complete

Serial port on a Raspberry Pi 4B: a PL011 is required. The mini UART, uart1 and
/dev/ttyS0, derives its clock from the core frequency and is not suitable here.
The simplest correct setup puts the primary PL011 on GPIO 14 and 15:

  /boot/firmware/config.txt   (Bookworm; /boot/config.txt on Bullseye and older)
      enable_uart=1
      dtoverlay=disable-bt

and the serial console has to be removed from the kernel command line, otherwise
getty holds the port. The device is then /dev/ttyAMA0.

Examples:
  python3 can_stream_rx.py --port /dev/ttyAMA0 --baud 2000000 --seconds 60
  python3 can_stream_rx.py --replay session.canbin
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))

import can_stream_protocol as proto  # noqa: E402
from can_detect.performance import PerformanceMetrics  # noqa: E402
from can_detect.report import write_session_snapshot  # noqa: E402
from record_buffer import RecordBuffer  # noqa: E402

MICROSECONDS_PER_DAY = 24 * 60 * 60 * 1_000_000


@dataclass
class SessionState:
    """Everything the receiver learns about the running device."""

    session: Optional[dict] = None
    sync_ts64: Optional[int] = None
    rtc_start_us: Optional[int] = None
    first_frame_ts64: Optional[int] = None
    last_frame_ts64: Optional[int] = None
    frames: int = 0
    frames_without_time: int = 0
    per_channel: dict = field(default_factory=dict)
    bus_stats: dict = field(default_factory=dict)
    logger_stats: Optional[dict] = None
    events: list = field(default_factory=list)
    acks: list = field(default_factory=list)
    session_restarts: int = 0
    unknown_record_types: dict[str, int] = field(default_factory=dict)

    def note_session(self, session: dict) -> None:
        if self.session is not None:
            self.session_restarts += 1
        self.session = session
        if session["rtc_valid"]:
            # Reproduce the device scale: microseconds since midnight, exactly
            # what gettime() computes for the file on the USB drive.
            hour, minute, second = (
                int(part) for part in session["rtc"].split(" ")[1].split(":"))
            seconds_of_day = hour * 3600 + minute * 60 + second
            self.rtc_start_us = (seconds_of_day * 1_000_000
                                 - session["ts64"])

    def log_time_us(self, ts64: int) -> Optional[int]:
        if self.rtc_start_us is None:
            return None
        return (self.rtc_start_us + ts64) % MICROSECONDS_PER_DAY


class Receiver:
    def __init__(self, session_id: str, output_dir: Path,
                 write_text_log: bool = True,
                 record_consumer: Optional[Callable[[proto.Record], None]] = None,
                 record_buffer: Optional[RecordBuffer[proto.Record]] = None
                 ) -> None:
        """Create a receiver and optionally hand valid records to a consumer.

        The consumer is called only after the raw chunk containing its records
        has been written and flushed.  This ordering makes the binary trace the
        durable input of live detection: a detector failure cannot remove a
        record from the replayable measurement.  Unknown record types are still
        delivered, while their bytes remain untouched in ``.canbin``.
        """
        self.session_id = session_id
        self.output_dir = output_dir
        if record_buffer is not None:
            if record_consumer is not None:
                raise ValueError("record_consumer and record_buffer are mutually exclusive")
            record_consumer = record_buffer.put
        self.record_buffer = record_buffer
        self.record_consumer = record_consumer
        self.decoder = proto.StreamDecoder()
        self.tracker = proto.SequenceTracker()
        self.state = SessionState()
        self.started = time.monotonic()
        self.bytes_in = 0
        self.per_second: list[int] = []
        self._second_start = self.started
        self._second_records = 0
        self.status = "running"
        self.termination_reason: Optional[str] = None
        self.trace_write_error: Optional[dict] = None
        self.report_path = output_dir / f"{session_id}.report.json"
        self.performance = PerformanceMetrics()

        output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = output_dir / f"{session_id}.canbin"
        self.raw = self.raw_path.open("wb")
        self.text_path = output_dir / f"{session_id}.log"
        # newline="" keeps the CRLF the logger itself writes, otherwise Windows
        # would translate it into CR CR LF and the record layout would break.
        self.text = (self.text_path.open("w", newline="")
                     if write_text_log else None)

    def _write_raw(self, chunk: bytes) -> bool:
        """Persist one raw chunk and turn filesystem errors into termination.

        Chunks accepted before the failing write remain in the trace.  The
        receiver deliberately does not hand a failed chunk to a consumer,
        because its durable input is no longer complete.
        """
        try:
            written = self.raw.write(chunk)
            if written is not None and written != len(chunk):
                raise OSError(
                    f"short trace write: {written} of {len(chunk)} bytes")
            self.raw.flush()
            return True
        except (OSError, IOError) as error:
            self.status = "failed"
            self.termination_reason = "trace_write_error"
            self.trace_write_error = {
                "operation": "write",
                "path": str(self.raw_path),
                "error_type": type(error).__name__,
                "error": str(error),
                "bytes_received_before_error": self.bytes_in,
            }
            return False

    def interrupt(self, reason: str = "user_interrupt") -> Path:
        """Stop a partial session and persist all results collected so far."""
        if self.status == "running":
            self.status = "interrupted"
            self.termination_reason = reason
        return self.write_report()

    def write_report(self, **extra: object) -> Path:
        """Write the current report without discarding prior session results."""
        snapshot = self.report()
        snapshot.update(extra)
        return write_session_snapshot(self.report_path, snapshot)

    def close(self) -> None:
        self.raw.close()
        if self.text is not None:
            self.text.close()

    def feed(self, chunk: bytes, store_raw: bool = True) -> bool:
        if self.status != "running":
            return False
        if store_raw and not self._write_raw(chunk):
            return False
        # _write_raw flushes before any consumer is called, so the detector
        # never observes a record that is not durable in the trace.
        self.bytes_in += len(chunk)

        for record in self.decoder.feed(chunk):
            self._second_records += 1
            self.performance.start_record()
            try:
                if not record.known:
                    type_key = f"0x{record.type:02X}"
                    self.state.unknown_record_types[type_key] = (
                        self.state.unknown_record_types.get(type_key, 0) + 1)
                if self.record_consumer is not None:
                    # This happens after raw.write/flush and before any detector
                    # handoff, including for unknown but CRC-valid record types.
                    self.record_consumer(record)
                if self.record_buffer is not None:
                    self.performance.observe_queue_backlog(
                        self.record_buffer.qsize())
                self._handle(record)
            finally:
                self.performance.finish_record()

        now = time.monotonic()
        if now - self._second_start >= 1.0:
            self.per_second.append(self._second_records)
            self._second_records = 0
            self._second_start = now
        return True

    def _handle(self, record: proto.Record) -> None:
        state = self.state

        if record.type == proto.REC_SESSION:
            session = proto.decode_session(record)
            if session is not None:
                if session["proto_version"] != proto.PROTO_VERSION:
                    raise SystemExit(
                        f"unsupported protocol version "
                        f"{session['proto_version']}, this receiver "
                        f"implements {proto.PROTO_VERSION}")
                state.note_session(session)
                # A restart resets the device sequence numbering.
                self.tracker = proto.SequenceTracker()
        elif record.type == proto.REC_TIME_SYNC:
            ts = proto.decode_time_sync(record)
            if ts is not None:
                state.sync_ts64 = ts
        elif record.type == proto.REC_FRAME:
            self._handle_frame(record)
        elif record.type == proto.REC_EVENT:
            event = proto.decode_event(record)
            if event is not None:
                state.events.append(event)
        elif record.type == proto.REC_BUS_STATS:
            stats = proto.decode_bus_stats(record)
            if stats is not None:
                state.bus_stats[stats["channel"]] = stats
        elif record.type == proto.REC_LOGGER_STATS:
            stats = proto.decode_logger_stats(record)
            if stats is not None:
                state.logger_stats = stats
        elif record.type == proto.REC_ACK:
            ack = proto.decode_ack(record)
            if ack is not None:
                state.acks.append(ack)

        ts_for_gap = state.last_frame_ts64
        self.tracker.observe(record.seq, ts_for_gap)

    def _handle_frame(self, record: proto.Record) -> None:
        state = self.state
        frame = proto.decode_frame(record)
        if frame is None:
            return

        if state.sync_ts64 is None:
            state.frames_without_time += 1
        else:
            frame.ts64 = proto.restore_time(state.sync_ts64, frame.ts32)
            if state.first_frame_ts64 is None:
                state.first_frame_ts64 = frame.ts64
            state.last_frame_ts64 = frame.ts64

        state.frames += 1
        state.per_channel[frame.channel] = (
            state.per_channel.get(frame.channel, 0) + 1)

        if self.text is not None and frame.ts64 is not None:
            log_time = state.log_time_us(frame.ts64)
            if log_time is not None:
                data = frame.data.hex().upper() if frame.dlc else "0"
                self.text.write(f"{log_time:011d} {frame.can_id:04X} "
                                f"{data} {frame.channel}\r\n")

    def report(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        counters = self.decoder.counters.as_dict()
        state = self.state

        if self._second_records:
            # Do not lose the trailing partial second of the measurement.
            self.per_second.append(self._second_records)
            self._second_records = 0

        device_losses = {}
        for channel, stats in state.bus_stats.items():
            device_losses[f"can{channel}"] = {
                "rx_frames": stats["rx_frames"],
                "valid_frames": stats["valid_frames"],
                "buffered_frames": stats["buffered_frames"],
                "ring_dropped": stats["ring_dropped"],
                "fifo_full": stats["fifo_full"],
                "fifo_overrun": stats["fifo_overrun"],
                "rx_read_errors": stats["rx_read_errors"],
                "extended_frames": stats["extended_frames"],
                "remote_frames": stats["remote_frames"],
                "invalid_dlc": stats["invalid_dlc"],
                "bus_off_entries": stats["bus_off_entries"],
                "passive_entries": stats["passive_entries"],
                "warning_entries": stats["warning_entries"],
                "max_rec": stats["max_rec"],
                "max_tec": stats["max_tec"],
                "state": stats["state_name"],
            }

        device_total_dropped = sum(
            entry["ring_dropped"] + entry["fifo_overrun"]
            for entry in device_losses.values())
        link_lost = self.tracker.missing_total
        lossless = (link_lost == 0
                    and counters["crc_errors"] == 0
                    and counters["cobs_errors"] == 0
                    and counters["sync_losses"] == 0
                    and device_total_dropped == 0
                    and (state.logger_stats or {}).get(
                        "tx_records_dropped", 0) == 0)

        return {
            "session_id": self.session_id,
            "status": self.status,
            "termination_reason": self.termination_reason,
            "trace_write_error": self.trace_write_error,
            "elapsed_s": round(elapsed, 3),
            "bytes_received": self.bytes_in,
            "bytes_per_second_mean": round(self.bytes_in / elapsed, 1),
            "records_per_second": {
                "samples": self.per_second,
                "max": max(self.per_second) if self.per_second else 0,
                "mean": (round(sum(self.per_second) / len(self.per_second), 1)
                         if self.per_second else 0),
            },
            "decoder": counters,
            "unknown_record_types": dict(sorted(state.unknown_record_types.items())),
            "frames": state.frames,
            "frames_per_channel": state.per_channel,
            "frames_without_time": state.frames_without_time,
            "sequence": {
                "records_missing_on_link": link_lost,
                "gaps": self.tracker.gaps[:100],
                "gap_count": len(self.tracker.gaps),
            },
            "session_record": state.session,
            "session_restarts": state.session_restarts,
            "device_counters": device_losses,
            "logger_stats": state.logger_stats,
            "events": state.events[-200:],
            "event_count": len(state.events),
            "performance": self.performance.snapshot(),
            "acks": state.acks,
            "detector_buffer": (self.record_buffer.report()
                                 if self.record_buffer is not None else None),
            "lossless": lossless,
            "files": {
                "raw": str(self.raw_path),
                "text_log": str(self.text_path) if self.text else None,
            },
            "notes": [
                "A gap in the sequence number means records lost on the serial "
                "link. Frames dropped inside the logger never consume a "
                "sequence number, they appear in ring_dropped and in ring drop "
                "events, so the two causes are distinguishable.",
                "Utilisation of the link should be read against the configured "
                "baud rate divided by ten bits per byte.",
            ],
        }


def utilisation(report: dict, baud: int) -> Optional[float]:
    if not baud:
        return None
    capacity = baud / 10.0
    return round(100.0 * report["bytes_per_second_mean"] / capacity, 2)


def run_serial(args: argparse.Namespace) -> int:
    try:
        import serial  # type: ignore
    except ImportError:
        print("pyserial is missing: pip install pyserial", file=sys.stderr)
        return 2

    session_id = args.session or time.strftime("%Y%m%d_%H%M%S")
    receiver = Receiver(session_id, Path(args.output),
                        write_text_log=not args.no_text_log)

    stop = {"requested": False}

    def handle_signal(signum, frame):  # noqa: ANN001, ARG001
        stop["requested"] = True

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (AttributeError, ValueError):
        pass

    print(f"session {session_id}: {args.port} at {args.baud} baud")
    print(f"writing to {receiver.raw_path}")

    deadline = None if not args.seconds else time.monotonic() + args.seconds

    with serial.Serial(args.port, args.baud, timeout=0.05) as port:
        while not stop["requested"]:
            chunk = port.read(65536)
            if chunk and not receiver.feed(chunk):
                print(f"trace write failed: {receiver.trace_write_error}",
                      file=sys.stderr)
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

    receiver.close()
    if stop["requested"] and receiver.status == "running":
        receiver.status = "interrupted"
        receiver.termination_reason = "user_interrupt"
    report = receiver.report()
    report["baudrate"] = args.baud
    report["link_utilisation_percent"] = utilisation(report, args.baud)

    report_path = receiver.write_report(
        baudrate=args.baud,
        link_utilisation_percent=utilisation(report, args.baud),
    )

    print_summary(report, report_path)
    return (0 if report["lossless"] and report["status"] != "failed"
            else 1)


def run_replay(args: argparse.Namespace) -> int:
    source = Path(args.replay)
    session_id = args.session or f"replay_{source.stem}"
    receiver = Receiver(session_id, Path(args.output),
                        write_text_log=not args.no_text_log)

    data = source.read_bytes()
    # Feed in chunks so the per second sampling stays meaningful.
    step = 65536
    for offset in range(0, len(data), step):
        receiver.feed(data[offset:offset + step], store_raw=False)

    receiver.close()
    receiver.raw_path.unlink(missing_ok=True)

    report = receiver.report()
    report["replayed_from"] = str(source)
    report["files"]["raw"] = str(source)

    report_path = Path(args.output) / f"{session_id}.report.json"
    report_path.write_text(json.dumps(report, indent=2))

    print_summary(report, report_path)
    return 0 if report["lossless"] else 1


def print_summary(report: dict, report_path: Path) -> None:
    print()
    print(f"frames decoded          : {report['frames']}")
    print(f"frames per channel      : {report['frames_per_channel']}")
    print(f"records per second, max : {report['records_per_second']['max']}")
    print(f"bytes received          : {report['bytes_received']}")
    if report.get("link_utilisation_percent") is not None:
        print(f"link utilisation        : "
              f"{report['link_utilisation_percent']} %")
    print(f"decoder counters        : {report['decoder']}")
    print(f"records lost on link    : "
          f"{report['sequence']['records_missing_on_link']} "
          f"in {report['sequence']['gap_count']} gaps")
    for channel, counters in report["device_counters"].items():
        print(f"{channel}: rx={counters['rx_frames']} "
              f"valid={counters['valid_frames']} "
              f"buffered={counters['buffered_frames']} "
              f"ring_dropped={counters['ring_dropped']} "
              f"fifo_ovr={counters['fifo_overrun']} "
              f"state={counters['state']}")
    if report["logger_stats"]:
        stats = report["logger_stats"]
        print(f"logger: tx_records={stats['tx_records_sent']} "
              f"tx_dropped={stats['tx_records_dropped']} "
              f"ring_peak={stats['ring_peak']}/{stats['ring_capacity']} "
              f"tx_peak={stats['tx_buffer_peak']}/"
              f"{stats['tx_buffer_capacity']} "
              f"loop_max={stats['loop_max_us']} us")
    print(f"event count             : {report['event_count']}")
    print(f"lossless                : {report['lossless']}")
    print(f"report                  : {report_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reference receiver for the CAN record stream")
    parser.add_argument("--port", default="/dev/ttyAMA0",
                        help="serial device, must be a PL011, not the mini "
                             "UART /dev/ttyS0")
    parser.add_argument("--baud", type=int, default=2000000)
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this many seconds, 0 means run until "
                             "interrupted")
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "captures_raw"))
    parser.add_argument("--session", default=None,
                        help="session identifier, defaults to the start time")
    parser.add_argument("--no-text-log", action="store_true",
                        help="skip the text format v2 export")
    parser.add_argument("--replay", default=None,
                        help="decode a stored .canbin instead of a serial port")
    args = parser.parse_args()

    if args.replay:
        return run_replay(args)
    return run_serial(args)


if __name__ == "__main__":
    sys.exit(main())
