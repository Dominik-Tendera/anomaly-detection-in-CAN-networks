#!/usr/bin/env python3
"""Access to a record trace: one iterator of enriched records for both modes.

Everything downstream of this module, the baseline configurator, the rules and
the evaluation, sees the trace only through the iterator produced here. That is
the whole point of the module: reading a stored `.canbin` and reading a live
serial port have to yield records that are indistinguishable field by field,
because criterion 9.2 asks the replay of a session to reproduce the live result
exactly. Both paths therefore go through one enrichment engine, `TraceReader`,
and differ only in where the bytes come from.

The decoder itself is not here. `rpi_receiver/can_stream_protocol.py` mirrors
`can_logger/Core/Src/can_stream_codec.c` field by field, and that agreement is checked by
`rpi_receiver/test_vectors.py` against a stream produced by the C encoder. A
second copy of the framing, the CRC or the record layouts would drift away from
the firmware without any test noticing, so this module imports that one and adds
nothing to it.

What enrichment means, and why each piece is needed.

A frame record carries only the low 32 bits of the device time, because eight
bytes of timestamp per record would eat a large part of the link budget. The full
value is restored from the newest time synchronisation record, and the difference
of the two is read as a *signed* number (criterion 2.1). The sign is not a
detail: the firmware stamps a frame in the receive interrupt and emits the
synchronisation record from the main loop, so a frame stamped a few microseconds
before the synchronisation record can reach the link after it. Read unsigned,
that frame would land almost 4295 s in the future and every timing rule looking
at it would be wrong. The same signed difference is what makes the wrap of the
32-bit field harmless.

The time base for everything downstream is this device time, never the clock of
the analysing machine. Statistics records arrive every 100 ms, so device time
moves forward even in a stretch with no frames on the bus.

Absolute time is a separate question and it is answered once per session, from
the session record, not per record (criterion 2.8). It exists so that a trace can
be tied to a bench notebook entry; no rule depends on it. On the present bench
the RTC of the logger has never been set and reads 2000-01-01, so the session is
marked as carrying no trustworthy absolute time and the device time stands alone.

The frame paths retain records without a reconstructed time for protocol rules,
carry an explicit unavailable time state, and count them. The `iter_timed_frames`
filter and `timing_eligible` predicate keep those records out of time-dependent
rules. The reader also computes modulo-2^32 diagnostic-counter increments and
classifies intervals bounded by bus-statistics records as complete or
incomplete when device losses or serial-link sequence gaps are present. Session
boundaries and the identifier-projection note remain separate metadata.

Usage:
    from can_detect.trace import TraceReader, iter_timed_frames, iter_trace_file

    for item in iter_timed_frames(
            iter_trace_file(Path("captures/20250307_140902.canbin"))):
        ...

    # live, from a serial port, same iterator
    reader = TraceReader()
    for item in reader.iter_chunks(iter(lambda: port.read(65536), b"")):
        ...

    python3 -m can_detect.trace captures/20250307_140902.canbin
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

_RECEIVER_DIR = Path(__file__).resolve().parents[2] / "raspberry_pi/rpi_receiver"
if str(_RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_RECEIVER_DIR))

import can_stream_protocol as proto  # noqa: E402

DEFAULT_CHUNK_BYTES = 65536

# The RTC of the STM32 comes up at this date when it has never been set, which is
# the state of the logger on the bench. A session record carrying it is treated
# as having no absolute time at all, even though the record says the RTC is
# valid, because a plausible looking date is worse than an admitted absence.
RTC_UNSET_DATE = (2000, 1, 1)

# Where the device time of a record comes from.
TIME_FROM_RECORD = "record"          # the record carries the full 64-bit value
TIME_RECONSTRUCTED = "reconstructed"  # rebuilt from a 32-bit field and a sync

# Whether the device time may be consumed by downstream time-dependent rules.
# ``time_source`` remains the provenance of a timestamp; ``time_state`` is the
# independent usability verdict. Keeping both avoids making callers infer
# eligibility from a nullable timestamp or from the source string.
TIME_STATE_AVAILABLE = "available"
TIME_STATE_UNAVAILABLE = "unavailable"
TIME_STATE_NOT_APPLICABLE = "not_applicable"

# Reasons a frame timestamp cannot be used by time-dependent rules.
TIME_UNAVAILABLE_NO_SYNC = "no_synchronization_record"
TIME_UNAVAILABLE_AMBIGUOUS = "ambiguous_reconstruction"

# The low 32-bit timestamp has two equally close candidates at this delta. In
# that one case the signed-difference convention cannot choose a unique time.
_TIME32_AMBIGUOUS_DELTA = 1 << 31
_TIME32_MASK = 0xFFFFFFFF

# Why a session has no trustworthy absolute time.
NO_RTC_FLAG = "rtc_marked_invalid"
RTC_NEVER_SET = "rtc_reads_the_unset_default"
RTC_UNREADABLE = "rtc_fields_are_not_a_date"
NO_SESSION_RECORD = "no_session_record_in_the_trace"
SESSION_RESTART_REASON = "device_restart"

DecodedPayload = Union[proto.Frame, dict, None]

UINT32_MASK = 0xFFFFFFFF
UINT32_MODULUS = 1 << 32

# The final two fields in BUS_STATS_COUNTERS are snapshots, not cumulative
# counters.  They must not be treated as deltas between statistics records.
BUS_COUNTER_FIELDS = tuple(
    name for name in proto.BUS_STATS_COUNTERS
    if name not in ("last_error_code", "last_ring_drop_id")
)

# These counters describe records lost inside the CAN input path.  Rejected
# extended, remote and invalid-DLC frames remain diagnostic events, but their
# counters do not by themselves make an otherwise complete trace interval
# unusable for replay.
BUS_LOSS_COUNTER_FIELDS = (
    "rx_read_errors",
    "fifo_full",
    "fifo_overrun",
    "ring_dropped",
)


def counter_delta_modulo_u32(previous: int, current: int) -> tuple[int, bool]:
    """Return a cumulative 32-bit counter increment and its wrap flag.

    A decrease is interpreted as the documented modulo-2^32 wrap.  The flag is
    retained in the interval report so a wrapped value is distinguishable from
    an ordinary increment.
    """
    previous_u32 = int(previous) & UINT32_MASK
    current_u32 = int(current) & UINT32_MASK
    return ((current_u32 - previous_u32) & UINT32_MASK,
            current_u32 < previous_u32)


IDENTIFIER_PROJECTION_NOTE = (
    "The trace contains a nonzero extended_frames counter. Frame identifiers "
    "were projected from 29 bits to 11 bits, so an extended frame and a "
    "standard frame with the same projected identifier are indistinguishable."
)


def projected_identifier_report(
        extended_frames_by_channel: dict[int, int]) -> Optional[dict]:
    """Build the report annotation from observed device statistics.

    The diagnostic counter is the only evidence used here. The function does
    not inspect frame identifiers and does not infer a transmitting ECU. A
    value is retained per channel so the report shows where the evidence came
    from without claiming that any particular CAN ID was extended.
    """
    observed = {
        str(channel): int(count)
        for channel, count in sorted(extended_frames_by_channel.items())
        if count > 0
    }
    if not observed:
        return None
    return {
        "detected": True,
        "source_identifier_bits": 29,
        "stored_identifier_bits": 11,
        "extended_frames_by_channel": observed,
        "indistinguishable_with_standard_frames": True,
        "note": IDENTIFIER_PROJECTION_NOTE,
    }


@dataclass(frozen=True)
class SessionClock:
    """The tie between device time and wall clock time, once per session.

    `device_us` is the device time at which the logger read its RTC, so absolute
    time of any later record is the RTC reading plus the elapsed device time.
    `trustworthy` says whether that reading may be used at all; when it is false
    `absolute` returns None for every record rather than a date that would look
    usable in a report.
    """

    session: dict
    device_us: int
    rtc_text: str
    rtc_datetime: Optional[datetime]
    trustworthy: bool
    reason: Optional[str]

    def absolute(self, ts64: Optional[int]) -> Optional[datetime]:
        """Wall clock time of a record, or None when there is no such time."""
        if not self.trustworthy or self.rtc_datetime is None or ts64 is None:
            return None
        return self.rtc_datetime + timedelta(microseconds=ts64 - self.device_us)

    def as_dict(self) -> dict:
        return {
            "device_us_at_rtc_read": self.device_us,
            "rtc": self.rtc_text,
            "absolute_time_trustworthy": self.trustworthy,
            "reason": self.reason,
            "measurement_mode": self.session.get("measurement_mode"),
            "stream_enabled": self.session.get("stream_enabled"),
            "usb_logging": self.session.get("usb_logging"),
            "can1_bitrate": self.session.get("can1_bitrate"),
            "can2_bitrate": self.session.get("can2_bitrate"),
            "uart_baudrate": self.session.get("uart_baudrate"),
        }


@dataclass(frozen=True)
class SessionBoundary:
    """A device restart separating two partial sessions in one trace.

    The session record itself is the boundary marker.  ``previous_seq`` and
    ``restart_seq`` describe the intentional break in the link sequence; the
    record is not a missing-record gap.  ``session_index`` is carried by every
    :class:`TraceRecord`, so consumers calculating frame intervals can reject a
    pair that crosses this boundary without guessing from the device time,
    which starts again after a reset.
    """

    previous_session_index: int
    session_index: int
    previous_seq: Optional[int]
    restart_seq: int
    device_ts64: Optional[int]
    reason: str = SESSION_RESTART_REASON

    @property
    def before_seq(self) -> Optional[int]:
        """Alias naming the last sequence number before the boundary."""
        return self.previous_seq

    @property
    def after_seq(self) -> int:
        """Alias naming the sequence number of the restart record."""
        return self.restart_seq

    def as_dict(self) -> dict:
        return {
            "reason": self.reason,
            "previous_session_index": self.previous_session_index,
            "session_index": self.session_index,
            "previous_seq": self.previous_seq,
            "restart_seq": self.restart_seq,
            "device_ts64": self.device_ts64,
        }


@dataclass
class TraceRecord:
    """One record of the trace with its device time and its decoded body.

    This is the unit the detection pipeline consumes. It holds the raw record as
    decoded by the protocol module, so nothing is lost, plus the two things every
    rule needs and no rule should have to work out for itself: the full 64-bit
    device time in microseconds, and the payload already decoded into the
    structure belonging to its type.
    """

    raw: proto.Record
    ts64: Optional[int]
    time_source: Optional[str]
    decoded: DecodedPayload
    session_index: int = 0
    session_boundary: Optional[SessionBoundary] = None
    # Appended after the fields introduced by the trace-boundary work so the
    # existing positional constructor remains compatible.
    time_state: str = TIME_STATE_NOT_APPLICABLE
    time_unavailable_reason: Optional[str] = None

    @property
    def type(self) -> int:
        return self.raw.type

    @property
    def seq(self) -> int:
        return self.raw.seq

    @property
    def known(self) -> bool:
        return self.raw.known

    @property
    def frame(self) -> Optional[proto.Frame]:
        """The decoded frame, or None when this record is not a frame."""
        return self.decoded if isinstance(self.decoded, proto.Frame) else None

    @property
    def has_time(self) -> bool:
        """Whether a device timestamp is present, preserving the old API."""
        return self.ts64 is not None

    @property
    def timing_eligible(self) -> bool:
        """Whether downstream time-dependent rules may use this record.

        A frame without a synchronization reference deliberately has
        ``ts64=None`` and is marked unavailable. Consumers of timing rules
        should use this predicate rather than checking only that the payload is
        a frame, so an unavailable frame cannot enter interval or window
        calculations accidentally.
        """
        return (self.ts64 is not None
                and self.time_state == TIME_STATE_AVAILABLE)

    @property
    def time_usable_for_timing(self) -> bool:
        """Semantic alias for callers that describe rules rather than state."""
        return self.timing_eligible


def frame_interval_us(previous: TraceRecord,
                      current: TraceRecord) -> Optional[int]:
    """Return a same-ID frame interval, or ``None`` when it is not valid.

    A restart resets the device clock and the sequence number.  The explicit
    session index therefore takes precedence over the numeric timestamp: even
    if the two values happen to look ordered, frames from different partial
    sessions are never paired.  Missing timestamps and different CAN streams
    are also not an interval.  Incomplete-link intervals are handled by the
    later trace-counter task and are deliberately outside this helper.
    """
    if previous.session_index != current.session_index:
        return None
    first = previous.frame
    second = current.frame
    if first is None or second is None:
        return None
    if (first.channel, first.can_id) != (second.channel, second.can_id):
        return None
    if not previous.timing_eligible or not current.timing_eligible:
        return None
    return current.ts64 - previous.ts64


@dataclass
class TraceCounters:
    """What the reader saw, for the session report.

    The decoder keeps its own counters for bytes, CRC and framing; these are
    about the records that survived it.  The completeness fields describe the
    intervals bounded by consecutive bus-statistics records and the sequence
    gaps observed in the same trace.
    """

    records: int = 0
    frames: int = 0
    frames_without_time: int = 0
    time_syncs: int = 0
    session_records: int = 0
    undecodable_payloads: int = 0
    unknown_types: int = 0
    bus_stats_records: int = 0
    counter_intervals: int = 0
    complete_intervals: int = 0
    incomplete_intervals: int = 0
    counter_wraps: int = 0
    sequence_gaps: int = 0
    records_missing_on_link: int = 0

    def as_dict(self) -> dict:
        return {
            "records": self.records,
            "frames": self.frames,
            "frames_without_time": self.frames_without_time,
            "time_syncs": self.time_syncs,
            "session_records": self.session_records,
            "undecodable_payloads": self.undecodable_payloads,
            "unknown_types": self.unknown_types,
            "bus_stats_records": self.bus_stats_records,
            "counter_intervals": self.counter_intervals,
            "complete_intervals": self.complete_intervals,
            "incomplete_intervals": self.incomplete_intervals,
            "counter_wraps": self.counter_wraps,
            "sequence_gaps": self.sequence_gaps,
            "records_missing_on_link": self.records_missing_on_link,
        }


# Record types whose body carries the full 64-bit device time already. Frames and
# synchronisation records are handled on their own: a frame has to be dated, and
# a synchronisation record is the reference the dating uses.
_FULL_TIME_DECODERS = {
    proto.REC_SESSION: proto.decode_session,
    proto.REC_EVENT: proto.decode_event,
    proto.REC_BUS_STATS: proto.decode_bus_stats,
    proto.REC_LOGGER_STATS: proto.decode_logger_stats,
    proto.REC_ACK: proto.decode_ack,
}


class TraceReader:
    """Turns bytes of the record stream into enriched records.

    Stateful and incremental, because the enrichment needs history: a frame is
    dated against the newest synchronisation record seen so far, and absolute
    time comes from the session record that opened the session. Feeding the same
    bytes in one chunk or in many gives the same records, which is what lets the
    live mode and the replay mode share this class.
    """

    def __init__(self) -> None:
        self.decoder = proto.StreamDecoder()
        self.counters = TraceCounters()
        self.sync_ts64: Optional[int] = None
        self.session_clocks: list[SessionClock] = []
        self.session_boundaries: list[SessionBoundary] = []
        self.session_index = 0
        self._has_session_record = False
        self._last_seq: Optional[int] = None
        self._expected_seq: Optional[int] = None
        self._last_record_ts64: Optional[int] = None
        self._bus_stats_by_channel: dict[int, dict] = {}
        self.counter_intervals: list[dict] = []
        self.complete_intervals: list[dict] = []
        self.incomplete_intervals: list[dict] = []
        self._sequence_gaps: list[dict] = []
        self._pending_sequence_gaps: list[dict] = []
        self.first_frame_ts64: Optional[int] = None
        self.last_frame_ts64: Optional[int] = None
        # Device statistics are cumulative. Keep the highest nonzero value
        # observed on each channel, which is enough to establish that this
        # trace contains projected extended identifiers.
        self.extended_frames_by_channel: dict[int, int] = {}

    @property
    def session_clock(self) -> Optional[SessionClock]:
        """The clock of the session in progress, the newest one seen."""
        return self.session_clocks[-1] if self.session_clocks else None

    @property
    def absolute_time_trustworthy(self) -> bool:
        clock = self.session_clock
        return clock is not None and clock.trustworthy

    def absolute_time(self, ts64: Optional[int]) -> Optional[datetime]:
        """Map device time to the session's absolute time, when trustworthy.

        The session record is the only origin of wall-clock time in a trace. A
        missing, invalid, or known-unset RTC therefore returns ``None`` rather
        than exposing a plausible-looking timestamp to downstream reporting.
        """
        clock = self.session_clock
        return clock.absolute(ts64) if clock is not None else None

    def absolute_time_note(self) -> dict:
        """Verdict on absolute time, as it goes into the session report."""
        clock = self.session_clock
        if clock is None:
            return {
                "absolute_time_trustworthy": False,
                "reason": NO_SESSION_RECORD,
                "session_clock": None,
            }
        return {
            "absolute_time_trustworthy": clock.trustworthy,
            "reason": clock.reason,
            "session_clock": clock.as_dict(),
        }

    @property
    def identifier_projection_note(self) -> Optional[dict]:
        """Report whether stats prove that 29-bit IDs were stored as 11-bit."""
        return projected_identifier_report(self.extended_frames_by_channel)

    def feed(self, chunk: bytes) -> Iterator[TraceRecord]:
        """Enrich every record completed by this chunk of bytes."""
        for record in self.decoder.feed(chunk):
            yield self._enrich(record)

    def iter_chunks(self, chunks: Iterable[bytes]) -> Iterator[TraceRecord]:
        """Enrich a whole source of bytes: a file, a port, a memory buffer."""
        for chunk in chunks:
            if not chunk:
                continue
            yield from self.feed(chunk)

    def _trace_record(self, raw: proto.Record, ts64: Optional[int],
                      time_source: Optional[str], decoded: DecodedPayload,
                      session_boundary: Optional[SessionBoundary] = None,
                      time_state: Optional[str] = None,
                      time_unavailable_reason: Optional[str] = None
                      ) -> TraceRecord:
        """Create a record carrying active session and time metadata.

        A missing state is derived for non-frame records to keep existing
        call-sites compatible. Frame paths that cannot be reconstructed pass
        ``TIME_STATE_UNAVAILABLE`` explicitly, which prevents a downstream
        timing rule from treating a plain ``None`` timestamp as an accidental
        default.
        """
        if time_state is None:
            time_state = (TIME_STATE_AVAILABLE
                          if ts64 is not None
                          else TIME_STATE_NOT_APPLICABLE)
        return TraceRecord(
            raw=raw,
            ts64=ts64,
            time_source=time_source,
            decoded=decoded,
            session_index=self.session_index,
            session_boundary=session_boundary,
            time_state=time_state,
            time_unavailable_reason=time_unavailable_reason,
        )

    @property
    def sequence_gaps(self) -> list[dict]:
        """Sequence gaps, with the record-time interval they affect."""
        return self._sequence_gaps

    @property
    def records_missing_on_link(self) -> int:
        """Total valid records missing between sequence numbers."""
        return sum(int(gap["missing"]) for gap in self._sequence_gaps)

    def _observe_sequence(self, seq: int,
                          ts64: Optional[int] = None) -> None:
        """Track a sequence gap and retain it for interval classification.

        A backwards jump larger than half the 16-bit range is left to the
        partial-session handling already present in this reader.  It is not a
        serial-link loss, so it must not become a false gap in this task.
        """
        expected = self._expected_seq
        if expected is not None:
            missing = (seq - expected) & 0xFFFF
            if missing != 0 and missing <= 0x8000:
                gap = {
                    "after_seq": (expected - 1) & 0xFFFF,
                    "next_seq": seq,
                    "missing": missing,
                    "ts64": ts64,
                    "start_ts64": self._last_record_ts64,
                    "end_ts64": ts64,
                    "reason": "sequence_gap",
                }
                self._sequence_gaps.append(gap)
                self._pending_sequence_gaps.append(gap)
                self.counters.sequence_gaps += 1
                self.counters.records_missing_on_link += missing

        self._last_seq = seq
        self._expected_seq = (seq + 1) & 0xFFFF
        if ts64 is not None:
            self._last_record_ts64 = ts64

    def _finish_record(self, item: TraceRecord) -> TraceRecord:
        """Apply sequence accounting and bus-statistics enrichment."""
        self._observe_sequence(item.seq, item.ts64)
        if (item.type == proto.REC_BUS_STATS
                and isinstance(item.decoded, dict)):
            self._note_bus_stats(item.decoded)
        return item

    def _enrich(self, record: proto.Record) -> TraceRecord:
        self.counters.records += 1

        # A valid session record is handled before ordinary sequence tracking:
        # its sequence number belongs to the new device boot and must not be
        # interpreted as a serial-link gap from the previous partial session.
        if record.type == proto.REC_SESSION:
            decoded = proto.decode_session(record)
            if decoded is None:
                self._observe_sequence(record.seq)
                self.counters.undecodable_payloads += 1
                return self._trace_record(record, None, None, None)
            boundary = self._note_session(record.seq, decoded)
            return self._trace_record(
                record, decoded["ts64"], TIME_FROM_RECORD, decoded, boundary)

        if not record.known:
            self.counters.unknown_types += 1
            return self._finish_record(
                self._trace_record(record, None, None, None))

        if record.type == proto.REC_FRAME:
            return self._finish_record(self._enrich_frame(record))
        if record.type == proto.REC_TIME_SYNC:
            return self._finish_record(self._enrich_time_sync(record))

        decoded = _FULL_TIME_DECODERS[record.type](record)
        if decoded is None:
            self.counters.undecodable_payloads += 1
            return self._finish_record(
                self._trace_record(record, None, None, None))

        return self._finish_record(
            self._trace_record(record, decoded["ts64"],
                               TIME_FROM_RECORD, decoded))

    def _enrich_frame(self, record: proto.Record) -> TraceRecord:
        frame = proto.decode_frame(record)
        if frame is None:
            self.counters.undecodable_payloads += 1
            return self._trace_record(record, None, None, None)

        self.counters.frames += 1
        if self.sync_ts64 is None:
            # No synchronisation record yet, so the 32-bit field cannot be
            # placed on the device time line. Keep the decoded frame for
            # protocol rules, but make its exclusion from timing rules
            # explicit instead of relying only on a null timestamp.
            self.counters.frames_without_time += 1
            return self._trace_record(
                record, None, None, frame,
                time_state=TIME_STATE_UNAVAILABLE,
                time_unavailable_reason=TIME_UNAVAILABLE_NO_SYNC)

        # Signed difference, see the module docstring: this is what dates a frame
        # stamped shortly before the synchronisation record correctly, and what
        # makes the wrap of the 32-bit field a non event. At exactly half of the
        # 32-bit range there are two equally close candidates, so no unique time
        # can be reconstructed.
        delta = (frame.ts32 - (self.sync_ts64 & _TIME32_MASK)) & _TIME32_MASK
        if delta == _TIME32_AMBIGUOUS_DELTA:
            self.counters.frames_without_time += 1
            return self._trace_record(
                record, None, None, frame,
                time_state=TIME_STATE_UNAVAILABLE,
                time_unavailable_reason=TIME_UNAVAILABLE_AMBIGUOUS)

        frame.ts64 = proto.restore_time(self.sync_ts64, frame.ts32)
        if self.first_frame_ts64 is None:
            self.first_frame_ts64 = frame.ts64
        self.last_frame_ts64 = frame.ts64
        return self._trace_record(
            record, frame.ts64, TIME_RECONSTRUCTED, frame,
            time_state=TIME_STATE_AVAILABLE)

    def _enrich_time_sync(self, record: proto.Record) -> TraceRecord:
        ts64 = proto.decode_time_sync(record)
        if ts64 is None:
            self.counters.undecodable_payloads += 1
            return self._trace_record(record, None, None, None)
        self.counters.time_syncs += 1
        self.sync_ts64 = ts64
        return self._trace_record(
            record, ts64, TIME_FROM_RECORD, {"ts64": ts64})

    def _take_sequence_gaps(self, start_ts64: Optional[int],
                            end_ts64: Optional[int]) -> list[dict]:
        """Take gaps belonging to the current statistics interval.

        Sequence numbering is global while bus statistics are per channel.  A
        gap is therefore assigned to the next statistics interval that bounds
        it; this keeps the report conservative without claiming that a missing
        record belonged to a particular CAN channel.
        """
        if end_ts64 is None:
            return []

        selected: list[dict] = []
        remaining: list[dict] = []
        for gap in self._pending_sequence_gaps:
            gap_end = gap.get("end_ts64")
            if gap_end is None:
                if start_ts64 is not None:
                    gap["start_ts64"] = start_ts64
                gap["end_ts64"] = end_ts64
                gap["ts64"] = end_ts64
                selected.append(gap)
                continue

            if start_ts64 is None:
                if gap_end <= end_ts64:
                    selected.append(gap)
                else:
                    remaining.append(gap)
            elif start_ts64 < gap_end <= end_ts64:
                selected.append(gap)
            elif gap_end > end_ts64:
                remaining.append(gap)
            # A gap at or before the lower boundary has already been
            # accounted for by an earlier interval.

        self._pending_sequence_gaps = remaining
        return selected

    def _note_bus_stats(self, stats: dict) -> None:
        """Compute counter increments and classify the preceding interval."""
        self.counters.bus_stats_records += 1
        channel = int(stats["channel"])
        extended_frames = int(stats["extended_frames"])
        if extended_frames > self.extended_frames_by_channel.get(channel, 0):
            self.extended_frames_by_channel[channel] = extended_frames

        previous = self._bus_stats_by_channel.get(channel)
        if previous is None:
            # The first sample establishes the cumulative-counter baseline; it
            # does not describe an interval yet.
            stats["counter_increments"] = None
            stats["counter_wraps"] = []
            stats["loss_increments"] = {}
            stats["sequence_gaps"] = []
            stats["interval"] = None
            self._bus_stats_by_channel[channel] = dict(stats)
            # A gap before the first sample cannot be assigned to a bounded
            # statistics interval. It remains visible in sequence_gaps.
            self._take_sequence_gaps(None, stats.get("ts64"))
            return

        increments: dict[str, int] = {}
        wrapped: list[str] = []
        for name in BUS_COUNTER_FIELDS:
            increment, did_wrap = counter_delta_modulo_u32(
                previous.get(name, 0), stats.get(name, 0))
            increments[name] = increment
            if did_wrap:
                wrapped.append(name)

        start_ts64 = previous.get("ts64")
        end_ts64 = stats.get("ts64")
        sequence_gaps = self._take_sequence_gaps(start_ts64, end_ts64)
        loss_increments = {
            name: increments[name]
            for name in BUS_LOSS_COUNTER_FIELDS
            if increments.get(name, 0) > 0
        }
        reasons: list[str] = []
        if loss_increments:
            reasons.append("device_loss")
        if sequence_gaps:
            reasons.append("sequence_gap")

        interval = {
            "kind": "bus_stats",
            "channel": channel,
            "start_ts64": start_ts64,
            "end_ts64": end_ts64,
            "duration_us": (end_ts64 - start_ts64
                            if start_ts64 is not None and end_ts64 is not None
                            else None),
            "complete": not reasons,
            "reasons": reasons,
            "counter_increments": increments,
            "counter_wraps": wrapped,
            "loss_increments": loss_increments,
            "losses": loss_increments,
            "sequence_gaps": [dict(gap) for gap in sequence_gaps],
        }
        stats["counter_increments"] = increments
        stats["counter_wraps"] = wrapped
        stats["loss_increments"] = loss_increments
        stats["sequence_gaps"] = [dict(gap) for gap in sequence_gaps]
        stats["interval"] = interval

        self.counter_intervals.append(interval)
        self.counters.counter_intervals += 1
        self.counters.counter_wraps += len(wrapped)
        if interval["complete"]:
            self.complete_intervals.append(interval)
            self.counters.complete_intervals += 1
        else:
            self.incomplete_intervals.append(interval)
            self.counters.incomplete_intervals += 1
        self._bus_stats_by_channel[channel] = dict(stats)

    def _note_session(self, seq: int, session: dict
                      ) -> Optional[SessionBoundary]:
        self.counters.session_records += 1
        boundary: Optional[SessionBoundary] = None
        if self._has_session_record:
            previous_session_index = self.session_index
            self.session_index += 1
            boundary = SessionBoundary(
                previous_session_index=previous_session_index,
                session_index=self.session_index,
                previous_seq=self._last_seq,
                restart_seq=seq,
                device_ts64=int(session.get("ts64", 0)),
            )
            self.session_boundaries.append(boundary)

        self._has_session_record = True
        # The sequence and device clock both start a new epoch at this record.
        # Do not carry either state from the previous partial session.
        self._observe_sequence(seq)
        self.session_clocks.append(build_session_clock(session))
        self.sync_ts64 = None
        return boundary


def build_session_clock(session: dict) -> SessionClock:
    """Read the RTC of a session record and decide whether it may be used."""
    rtc_text = session.get("rtc", "")
    parsed: Optional[datetime]
    try:
        parsed = datetime.strptime(rtc_text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        parsed = None

    if parsed is None:
        trustworthy, reason = False, RTC_UNREADABLE
    elif not session.get("rtc_valid", False):
        trustworthy, reason = False, NO_RTC_FLAG
    elif (parsed.year, parsed.month, parsed.day) == RTC_UNSET_DATE:
        trustworthy, reason = False, RTC_NEVER_SET
    else:
        trustworthy, reason = True, None

    return SessionClock(
        session=session,
        device_us=int(session.get("ts64", 0)),
        rtc_text=rtc_text,
        rtc_datetime=parsed,
        trustworthy=trustworthy,
        reason=reason,
    )


TRACE_HEADER_MAGIC = b"CANTRACE\\x01"
TRACE_HEADER_VERSION = 1
_TRACE_HEADER_PREFIX = len(TRACE_HEADER_MAGIC) + 4


@dataclass(frozen=True)
class TraceHeader:
    """Checksums captured when a trace is written.

    The envelope is deliberately small JSON followed by the unchanged wire
    records.  Older raw ``.canbin`` files remain readable, but are reported as
    lacking comparability metadata.
    """

    checksums: dict[str, str]
    version: int = TRACE_HEADER_VERSION

    def as_dict(self) -> dict[str, object]:
        return {"version": self.version, "checksums": dict(self.checksums)}

    def encode(self) -> bytes:
        payload = json.dumps(self.as_dict(), sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return (TRACE_HEADER_MAGIC + struct.pack("<I", len(payload)) + payload)

    @classmethod
    def from_config(cls, config) -> "TraceHeader":
        """Build a header from a validated session configuration."""
        return cls(_header_checksums_from_config(config))

    @classmethod
    def decode(cls, payload: bytes) -> "TraceHeader":
        """Decode and validate the JSON metadata envelope."""
        try:
            document = json.loads(payload.decode("utf-8"))
            checksums = document["checksums"]
            version = int(document["version"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError,
                TypeError, ValueError) as exc:
            raise ValueError("invalid CAN trace header") from exc
        if version != TRACE_HEADER_VERSION or not isinstance(checksums, dict):
            raise ValueError("unsupported CAN trace header")
        if any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in checksums.items()):
            raise ValueError("trace header checksums must be strings")
        return cls(dict(checksums), version)


def _read_header(handle) -> Optional[TraceHeader]:
    prefix = handle.read(_TRACE_HEADER_PREFIX)
    if not prefix:
        return None
    if not prefix.startswith(TRACE_HEADER_MAGIC):
        handle.seek(0)
        return None
    if len(prefix) != _TRACE_HEADER_PREFIX:
        raise ValueError("truncated CAN trace header")
    size = struct.unpack("<I", prefix[len(TRACE_HEADER_MAGIC):])[0]
    if size > 1_048_576:
        raise ValueError("CAN trace header is unreasonably large")
    payload = handle.read(size)
    if len(payload) != size:
        raise ValueError("truncated CAN trace header payload")
    return TraceHeader.decode(payload)


def read_trace_header(path: Path) -> Optional[TraceHeader]:
    """Return the persisted metadata header, or ``None`` for legacy traces."""
    with Path(path).open("rb") as handle:
        return _read_header(handle)


def write_trace_file(path: Path, records: bytes, header: TraceHeader) -> Path:
    """Write a checksum-bearing trace without altering its record bytes."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(header.encode() + bytes(records))
    return destination


def _header_checksums_from_config(config) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for digest in config.checksums():
        if digest.parameter == "config":
            checksums["config_sha256"] = digest.digest
        elif digest.parameter == "baseline_path":
            checksums["profile_sha256"] = digest.digest
        elif digest.parameter == "dbc_path":
            index = sum(1 for key in checksums if key.startswith("dbc_sha256"))
            checksums["dbc_sha256" if index == 0 else f"dbc_sha256_{index}"] = digest.digest
    return checksums


def trace_header_from_config(config) -> TraceHeader:
    """Build the header for a :class:`SessionConfig` without importing it."""
    return TraceHeader(_header_checksums_from_config(config))


def validate_trace_header(header: Optional[TraceHeader], config) -> list[str]:
    """Return explicit checksum mismatches; an empty list means comparable."""
    if header is None:
        return ["trace_header_missing"]
    expected = _header_checksums_from_config(config)
    mismatches = []
    for name, actual in expected.items():
        recorded = header.checksums.get(name)
        if recorded != actual:
            mismatches.append(f"{name}: trace={recorded!r}, current={actual!r}")
    for name in sorted(set(header.checksums) - set(expected)):
        mismatches.append(f"{name}: trace={header.checksums[name]!r}, current=<absent>")
    return mismatches


def _payload_chunks(path: Path, chunk_bytes: int) -> Iterator[bytes]:
    with Path(path).open("rb") as handle:
        _read_header(handle)
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            yield chunk


def file_chunks(path: Path,
                chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> Iterator[bytes]:
    """Read record bytes in chunks, transparently skipping a trace header."""
    yield from _payload_chunks(path, chunk_bytes)


def iter_trace_file(path: Path, reader: Optional[TraceReader] = None,
                    chunk_bytes: int = DEFAULT_CHUNK_BYTES
                    ) -> Iterator[TraceRecord]:
    """Enriched records of a stored trace.

    Pass a reader in when the counters and the session clock are needed after
    the iteration; otherwise one is created internally.
    """
    reader = reader if reader is not None else TraceReader()
    yield from reader.iter_chunks(file_chunks(path, chunk_bytes))


def iter_trace_bytes(data: bytes, reader: Optional[TraceReader] = None,
                     chunk_bytes: int = DEFAULT_CHUNK_BYTES
                     ) -> Iterator[TraceRecord]:
    """Enriched records of a trace already in memory, for tests and replay."""
    if data.startswith(TRACE_HEADER_MAGIC):
        if len(data) < _TRACE_HEADER_PREFIX:
            raise ValueError("truncated CAN trace header")
        size = struct.unpack("<I", data[len(TRACE_HEADER_MAGIC):_TRACE_HEADER_PREFIX])[0]
        header_end = _TRACE_HEADER_PREFIX + size
        TraceHeader.decode(data[_TRACE_HEADER_PREFIX:header_end])
        data = data[header_end:]
    reader = reader if reader is not None else TraceReader()
    chunks = (data[offset:offset + chunk_bytes]
              for offset in range(0, len(data), chunk_bytes))
    yield from reader.iter_chunks(chunks)


def iter_timed_frames(records: Iterable[TraceRecord]
                      ) -> Iterator[TraceRecord]:
    """Yield only frames whose device time is safe for timing rules.

    This is the shared downstream boundary for interval and window-based
    processing. Protocol rules can still inspect every ``TraceRecord`` and its
    decoded frame, while time-dependent rules cannot accidentally consume a
    frame marked with ``TIME_STATE_UNAVAILABLE``.
    """
    for record in records:
        if record.frame is not None and record.timing_eligible:
            yield record


def read_trace(path: Path,
               chunk_bytes: int = DEFAULT_CHUNK_BYTES
               ) -> tuple[list[TraceRecord], TraceReader]:
    """Whole trace in memory, for short traces and for tests.

    The streaming iterators above are the production path; this helper exists for
    the cases where a test wants to look at the records twice.
    """
    reader = TraceReader()
    records = list(iter_trace_file(path, reader=reader,
                                   chunk_bytes=chunk_bytes))
    return records, reader


def summary(reader: TraceReader) -> dict:
    """What the reader learned about a trace, for the session report."""
    span_us = None
    if (reader.first_frame_ts64 is not None
            and reader.last_frame_ts64 is not None):
        span_us = reader.last_frame_ts64 - reader.first_frame_ts64
    return {
        "decoder": reader.decoder.counters.as_dict(),
        "records": reader.counters.as_dict(),
        "device_time": {
            "first_frame_us": reader.first_frame_ts64,
            "last_frame_us": reader.last_frame_ts64,
            "span_us": span_us,
        },
        "absolute_time": reader.absolute_time_note(),
        "identifier_projection": reader.identifier_projection_note,
        "completeness": {
            "records_missing_on_link": reader.records_missing_on_link,
            "sequence_gaps": [dict(gap) for gap in reader.sequence_gaps],
            "counter_intervals": reader.counter_intervals,
            "complete_intervals": reader.complete_intervals,
            "incomplete_intervals": reader.incomplete_intervals,
        },
        "session_boundaries": [
            boundary.as_dict() for boundary in reader.session_boundaries
        ],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read a stored trace and report what the reader found: "
                    "record counts, the device time span and the verdict on "
                    "absolute time.")
    parser.add_argument("trace", type=Path, help="trace file, .canbin")
    args = parser.parse_args(argv)

    reader = TraceReader()
    for _ in iter_trace_file(args.trace, reader=reader):
        pass
    print(json.dumps(summary(reader), indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
