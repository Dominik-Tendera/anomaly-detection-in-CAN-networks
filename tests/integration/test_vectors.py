"""Cross language check of the record format.

The C encoder writes a binary stream and a CSV listing the exact field values it
put on the wire. This test decodes the stream with the Python receiver and
compares every field. If the two implementations ever drift apart, the firmware
would keep transmitting records the analysis side silently misreads, which is
the failure mode this test exists to prevent.

Usage:
    gcc -std=c11 -O2 -I can_logger/Core/Inc tests/can_logger/test_can_stream_codec.c \
        can_logger/Core/Src/can_stream_codec.c -o tests/can_logger/test_codec.exe
    tests/can_logger/test_codec.exe --emit-vectors vectors.bin vectors.csv
    python tests/integration/test_vectors.py vectors.bin vectors.csv
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'raspberry_pi/rpi_receiver'))

import can_stream_protocol as proto  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)


def compare_session(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_session(record)
    check(got is not None, "session did not decode")
    if got is None:
        return
    fields = [
        ("proto_version", 2), ("flags", 3), ("max_record_len", 4),
        ("ts64", 5), ("can1_bitrate", 6), ("can2_bitrate", 7),
        ("can1_btr", 8), ("can2_btr", 9), ("uart_baudrate", 10),
    ]
    for name, index in fields:
        check(got[name] == int(expected[index]),
              f"session {name}: {got[name]} != {expected[index]}")
    check(got["rtc_valid"] == bool(int(expected[11])), "session rtc_valid")
    rtc = (f"20{int(expected[12]):02d}-{int(expected[13]):02d}-"
           f"{int(expected[14]):02d} {int(expected[15]):02d}:"
           f"{int(expected[16]):02d}:{int(expected[17]):02d}")
    check(got["rtc"] == rtc, f"session rtc: {got['rtc']} != {rtc}")


def compare_frame(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_frame(record)
    check(got is not None, "frame did not decode")
    if got is None:
        return
    check(got.seq == int(expected[1]), f"frame seq {got.seq}")
    check(got.channel == int(expected[2]), f"frame channel {got.channel}")
    check(got.can_id == int(expected[3]), f"frame id {got.can_id}")
    check(got.dlc == int(expected[4]), f"frame dlc {got.dlc}")
    check(got.data.hex().upper() == expected[5],
          f"frame data {got.data.hex().upper()} != {expected[5]}")
    check(got.ts32 == int(expected[6]), f"frame ts32 {got.ts32}")


def compare_event(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_event(record)
    check(got is not None, "event did not decode")
    if got is None:
        return
    check(got["channel"] == int(expected[2]), "event channel")
    check(got["reason"] == int(expected[3]), "event reason")
    check(got["state"] == int(expected[4]), "event state")
    check(got["frame_id"] == int(expected[5]), "event frame_id")
    check(got["error_code"] == int(expected[6]), "event error_code")
    check(got["suppressed"] == int(expected[7]), "event suppressed")
    check(got["ts64"] == int(expected[8]), "event ts64")


def compare_bus_stats(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_bus_stats(record)
    check(got is not None, "bus stats did not decode")
    if got is None:
        return
    check(got["channel"] == int(expected[2]), "bus stats channel")
    check(got["ts64"] == int(expected[3]), "bus stats ts64")
    for offset, name in enumerate(proto.BUS_STATS_COUNTERS):
        want = int(expected[4 + offset])
        check(got[name] == want, f"bus stats {name}: {got[name]} != {want}")
    tail = ["fifo_max_fill", "current_rec", "current_tec", "max_rec",
            "max_tec", "state"]
    base = 4 + len(proto.BUS_STATS_COUNTERS)
    for offset, name in enumerate(tail):
        want = int(expected[base + offset])
        check(got[name] == want, f"bus stats {name}: {got[name]} != {want}")


def compare_logger_stats(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_logger_stats(record)
    check(got is not None, "logger stats did not decode")
    if got is None:
        return
    names = ["ts64", "ring_drop_total", "tx_records_sent", "tx_bytes_sent",
             "tx_records_dropped", "tx_bytes_dropped", "event_queue_dropped",
             "loop_max_us", "ring_peak", "ring_capacity", "tx_buffer_peak",
             "tx_buffer_capacity", "flags"]
    for offset, name in enumerate(names):
        want = int(expected[2 + offset])
        check(got[name] == want, f"logger stats {name}: {got[name]} != {want}")


def compare_ack(expected: list[str], record: proto.Record) -> None:
    got = proto.decode_ack(record)
    check(got is not None, "ack did not decode")
    if got is None:
        return
    check(got["status"] == int(expected[2]), "ack status")
    check(got["command"] == int(expected[3]), "ack command")
    check(got["value"] == int(expected[4]), "ack value")
    check(got["ts64"] == int(expected[5]), "ack ts64")


COMPARATORS = {
    "SESSION": (proto.REC_SESSION, compare_session),
    "TIMESYNC": (proto.REC_TIME_SYNC, None),
    "FRAME": (proto.REC_FRAME, compare_frame),
    "EVENT": (proto.REC_EVENT, compare_event),
    "BUSSTATS": (proto.REC_BUS_STATS, compare_bus_stats),
    "LOGGERSTATS": (proto.REC_LOGGER_STATS, compare_logger_stats),
    "ACK": (proto.REC_ACK, compare_ack),
}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    stream = Path(sys.argv[1]).read_bytes()
    expected_lines = [line for line in
                      Path(sys.argv[2]).read_text().splitlines() if line]

    decoder = proto.StreamDecoder()
    records = list(decoder.feed(stream))

    print(f"decoded {len(records)} records from {len(stream)} bytes")
    print(f"expected {len(expected_lines)} records")
    print(f"counters: {decoder.counters.as_dict()}")

    check(len(records) == len(expected_lines),
          f"record count {len(records)} != {len(expected_lines)}")
    check(decoder.counters.crc_errors == 0, "unexpected crc errors")
    # The vectors contain an intentional garbage burst.
    check(decoder.counters.cobs_errors + decoder.counters.short_records > 0,
          "injected garbage was not rejected")

    sync_ts: int | None = None
    frames_with_time = 0
    tracker = proto.SequenceTracker()

    for line, record in zip(expected_lines, records):
        fields = line.split(";")
        kind = fields[0]
        check(kind in COMPARATORS, f"unknown expected record kind {kind}")
        if kind not in COMPARATORS:
            continue
        want_type, comparator = COMPARATORS[kind]
        check(record.type == want_type,
              f"{kind}: type {record.type} != {want_type}")
        check(record.seq == int(fields[1]),
              f"{kind}: seq {record.seq} != {fields[1]}")

        if kind == "TIMESYNC":
            sync_ts = proto.decode_time_sync(record)
            check(sync_ts == int(fields[2]), "time sync value")
        elif comparator is not None:
            comparator(fields, record)

        if kind == "FRAME" and sync_ts is not None:
            frame = proto.decode_frame(record)
            if frame is not None:
                restored = proto.restore_time(sync_ts, frame.ts32)
                # Every emitted frame is stamped at or after the sync record.
                check(restored >= sync_ts,
                      f"restored time {restored} precedes sync {sync_ts}")
                check((restored & 0xFFFFFFFF) == frame.ts32,
                      "restored time inconsistent with the 32-bit field")
                frames_with_time += 1

        tracker.observe(record.seq, None)

    check(tracker.missing_total == 0,
          f"sequence gaps in a complete stream: {tracker.gaps}")
    print(f"frames with reconstructed time: {frames_with_time}")

    print(f"\nchecks: {CHECKS}, failures: {len(FAILURES)}")
    for message in FAILURES[:20]:
        print(f"FAIL: {message}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
