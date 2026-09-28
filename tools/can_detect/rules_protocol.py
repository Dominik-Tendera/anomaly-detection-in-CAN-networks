"""Protocol rules that compare observed CAN IDs with DBC/profile context.

An identifier is known when it is present in either the loaded DBC or the
baseline profile.  The profile-only case is intentional: a reference trace
may contain an identifier that is not present in the current DBC, and the
requirements treat that identifier as known for this rule.

Unknown-ID alarms are episode based.  Consecutive frames with the same
unknown ID on one channel form one episode, so a burst does not produce one
alarm per frame.  A known frame, a different unknown ID, or a session restart
ends the current episode on that channel.  Non-frame records do not interrupt
an episode because they are not traffic from a different CAN ID.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Optional

from . import dbc, trace

UNKNOWN_IDENTIFIER_EVENT = "unknown_identifier"
DLC_MISMATCH_EVENT = "dlc_mismatch"
SIGNAL_RANGE_EVENT = "signal_range_exceeded"
DBC_RANGE_SOURCE = "dbc"
BASELINE_RANGE_SOURCE = "baseline_profile"


def _known_ids(database: dbc.DbcDatabase | Mapping[int, object],
               profile: Optional[Mapping[int, object]]) -> frozenset[int]:
    """Return IDs known by either the DBC or the baseline profile."""
    if isinstance(database, dbc.DbcDatabase):
        dbc_ids = database.messages_by_id.keys()
    else:
        dbc_ids = database.keys()
    profile_ids = () if profile is None else profile.keys()
    return frozenset(int(can_id) for can_id in (*dbc_ids, *profile_ids))


def _event(record: trace.TraceRecord, can_id: int) -> dict[str, object]:
    """Create the stable event shape used by the reporting layer."""
    channel = int(record.frame.channel) if record.frame is not None else 0
    return {
        "type": UNKNOWN_IDENTIFIER_EVENT,
        "method": "protocol",
        "ts64": record.ts64,
        "channel": channel,
        "can_id": can_id,
        # Keep the generic frame_id spelling used by protocol-layer events as
        # well as the domain-specific CAN-ID spelling used by this rule.
        "frame_id": can_id,
        "frame_seq": record.seq,
    }


def _message_data_length(
        database: dbc.DbcDatabase | Mapping[int, object],
        can_id: int,
) -> Optional[int]:
    """Return the DBC payload length for ``can_id`` when it is available."""
    if isinstance(database, dbc.DbcDatabase):
        message = database.message_for_id(can_id)
    else:
        message = database.get(can_id)
        if message is None:
            message = database.get(str(can_id))  # type: ignore[arg-type]
    if message is None:
        return None
    length = getattr(message, "data_length", None)
    try:
        length = int(length)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _dlc_event(record: trace.TraceRecord, can_id: int,
               measured_dlc: int, expected_dlc: int) -> dict[str, object]:
    """Build a DLC mismatch event with both values explicit."""
    return {
        "type": DLC_MISMATCH_EVENT,
        "method": "protocol",
        "ts64": record.ts64,
        "channel": int(record.frame.channel),
        "can_id": can_id,
        "frame_id": can_id,
        "frame_seq": record.seq,
        "measured_dlc": measured_dlc,
        "expected_dlc": expected_dlc,
        "measured": measured_dlc,
        "expected": expected_dlc,
    }


def _profile_signal_range(profile: Optional[Mapping[int, object]], can_id: int,
                          signal_name: str) -> Optional[Mapping[str, object]]:
    """Return a baseline signal range, accepting JSON-style string IDs."""
    if profile is None:
        return None
    entry = profile.get(can_id)
    if entry is None:
        entry = profile.get(str(can_id))  # type: ignore[arg-type]
    if not isinstance(entry, Mapping):
        return None
    ranges = entry.get("signal_ranges")
    if not isinstance(ranges, Mapping):
        return None
    value = ranges.get(signal_name)
    return value if isinstance(value, Mapping) else None


def _range_event(record: trace.TraceRecord, can_id: int, signal: dbc.SignalDefinition,
                 value: float, source: str, limits: Mapping[str, object]
                 ) -> dict[str, object]:
    """Create a range alarm with the source and exceeded limits explicit."""
    minimum = float(limits["min"])
    maximum = float(limits["max"])
    return {
        "type": SIGNAL_RANGE_EVENT,
        "method": "protocol",
        "ts64": record.ts64,
        "channel": int(record.frame.channel),
        "can_id": can_id,
        "frame_id": can_id,
        "frame_seq": record.seq,
        "signal": signal.name,
        "signal_name": signal.name,
        "value": value,
        "measured_value": value,
        "minimum": minimum,
        "maximum": maximum,
        "unit": signal.unit,
        "range_source": source,
        "source": source,
    }


def _outside(value: float, limits: Mapping[str, object]) -> bool:
    """Return whether a value is outside a valid min/max range."""
    try:
        minimum = float(limits["min"])
        maximum = float(limits["max"])
    except (KeyError, TypeError, ValueError):
        return False
    return value < minimum or value > maximum


@dataclass
class ProtocolRulesDetector:
    """Incrementally detect protocol mismatches against DBC/profile IDs."""

    database: dbc.DbcDatabase | Mapping[int, object]
    profile: Optional[Mapping[int, object]] = None
    _known_ids: frozenset[int] = field(init=False, repr=False)
    _last_unknown: dict[int, tuple[int, int]] = field(default_factory=dict,
                                                       init=False,
                                                       repr=False)
    events: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._known_ids = _known_ids(self.database, self.profile)

    def _dlc_events(self, record: trace.TraceRecord,
                    can_id: int) -> list[dict[str, object]]:
        """Check a known CAN ID against its DBC payload length."""
        frame = record.frame
        if frame is None:
            return []
        expected_dlc = _message_data_length(self.database, can_id)
        if expected_dlc is None or int(frame.dlc) == expected_dlc:
            return []
        return [_dlc_event(record, can_id, int(frame.dlc), expected_dlc)]

    def _signal_range_events(self, record: trace.TraceRecord,
                             can_id: int) -> list[dict[str, object]]:
        """Check decoded signal values against DBC and baseline ranges.

        ``decode_signals`` omits signals requiring bits beyond the payload, so
        short-DLC frames are intentionally skipped for this rule.
        """
        if not isinstance(self.database, dbc.DbcDatabase):
            return []
        message = self.database.message_for_id(can_id)
        frame = record.frame
        if message is None or frame is None:
            return []
        decoded = self.database.decode_signals(can_id, frame.data)
        definitions = {signal.name: signal for signal in message.signals}
        generated: list[dict[str, object]] = []
        for name, raw_value in decoded.items():
            signal = definitions[name]
            value = float(raw_value)
            if signal.has_reference_range:
                limits = {"min": signal.minimum, "max": signal.maximum}
                if _outside(value, limits):
                    generated.append(_range_event(
                        record, can_id, signal, value, DBC_RANGE_SOURCE, limits))

            baseline_limits = _profile_signal_range(self.profile, can_id, name)
            if (baseline_limits is not None
                    and _outside(value, baseline_limits)):
                generated.append(_range_event(
                    record, can_id, signal, value, BASELINE_RANGE_SOURCE,
                    baseline_limits))
        return generated

    def process(self, record: trace.TraceRecord) -> list[dict[str, object]]:
        """Process one trace record and return alarms generated by it."""
        frame = record.frame
        if frame is None:
            return []

        channel = int(frame.channel)
        can_id = int(frame.can_id)
        previous = self._last_unknown.get(channel)
        current = (int(record.session_index), can_id)

        if can_id in self._known_ids:
            self._last_unknown.pop(channel, None)
            generated = self._dlc_events(record, can_id)
            generated.extend(self._signal_range_events(record, can_id))
            self.events.extend(generated)
            return generated

        self._last_unknown[channel] = current
        if previous == current:
            return []

        generated = [_event(record, can_id)]
        self.events.extend(generated)
        return generated

    def process_all(self, records: Iterable[trace.TraceRecord]
                    ) -> Iterator[dict[str, object]]:
        """Yield alarms while consuming records in trace order."""
        for record in records:
            yield from self.process(record)


def detect_dlc_mismatches(
    records: Iterable[trace.TraceRecord],
    database: dbc.DbcDatabase | Mapping[int, object],
    profile: Optional[Mapping[int, object]] = None,
) -> list[dict[str, object]]:
    """Return one event for every frame whose DLC differs from the DBC."""
    detector = ProtocolRulesDetector(database, profile)
    events: list[dict[str, object]] = []
    for record in records:
        frame = record.frame
        if frame is None:
            continue
        events.extend(detector._dlc_events(record, int(frame.can_id)))
    return events


def detect_signal_ranges(
    records: Iterable[trace.TraceRecord],
    database: dbc.DbcDatabase,
    profile: Optional[Mapping[int, object]] = None,
) -> list[dict[str, object]]:
    """Return DBC and baseline signal-range events for trace frames."""
    detector = ProtocolRulesDetector(database, profile)
    events: list[dict[str, object]] = []
    for record in records:
        frame = record.frame
        if frame is None or database.message_for_id(int(frame.can_id)) is None:
            continue
        events.extend(detector._signal_range_events(record, int(frame.can_id)))
    return events


def detect_unknown_identifiers(
    records: Iterable[trace.TraceRecord],
    database: dbc.DbcDatabase | Mapping[int, object],
    profile: Optional[Mapping[int, object]] = None,
) -> list[dict[str, object]]:
    """Return one unknown-identifier event per contiguous ID episode.

    ``database`` and ``profile`` are deliberately explicit inputs.  No list of
    expected IDs is inferred from the observed trace, because doing so would
    make an injected unknown ID appear normal.
    """
    detector = ProtocolRulesDetector(database, profile)
    return list(detector.process_all(records))


# Short aliases are useful to callers that use the requirement's wording
# (unknown CAN IDs) rather than the full event name.
detect_unknown_ids = detect_unknown_identifiers
ProtocolDetector = ProtocolRulesDetector


__all__ = [
    "UNKNOWN_IDENTIFIER_EVENT",
    "DLC_MISMATCH_EVENT",
    "SIGNAL_RANGE_EVENT",
    "DBC_RANGE_SOURCE",
    "BASELINE_RANGE_SOURCE",
    "ProtocolDetector",
    "ProtocolRulesDetector",
    "detect_dlc_mismatches",
    "detect_signal_ranges",
    "detect_unknown_identifiers",
    "detect_unknown_ids",
]


MISSING_EXPECTED_FRAME_EVENT = "missing_expected_frame"


def _profile_period(entry: object) -> Optional[float]:
    """Return an observed positive period from one baseline entry.

    Profiles with ``period_status=no_period`` or a null period deliberately do
    not participate: the reference trace did not contain enough intervals to
    make a missing-frame claim defensible.
    """
    if not isinstance(entry, Mapping):
        return None
    period = entry.get("period_us")
    if isinstance(period, Mapping):
        period = period.get("value")
    try:
        value = float(period)
    except (TypeError, ValueError):
        return None
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        return None
    if entry.get("period_status") == "no_period":
        return None
    return value


def _missing_event(record: trace.TraceRecord, channel: int, can_id: int,
                   last_ts64: int, period_us: float,
                   multiplier: float) -> dict[str, object]:
    """Build a deterministic missing-frame event."""
    threshold_us = period_us * multiplier
    return {
        "type": MISSING_EXPECTED_FRAME_EVENT,
        "method": "protocol",
        "ts64": int(record.ts64),
        "channel": channel,
        "can_id": can_id,
        "frame_id": can_id,
        "frame_seq": record.seq,
        "last_observation_ts64": last_ts64,
        "period_us": period_us,
        "threshold_us": threshold_us,
        "threshold_multiplier": multiplier,
    }


@dataclass
class MissingFrameDetector:
    """Detect gaps longer than a profiled period threshold.

    The detector is driven by records, not by callbacks or a wall clock. Every
    record carrying an eligible device timestamp advances the detector, so a
    gap is reported even when the next record is a statistics or event record
    rather than another CAN frame. A reported gap remains one episode until the
    corresponding frame is observed again.
    """

    profile: Mapping[int, object]
    multiplier: float
    _periods: dict[int, float] = field(init=False, repr=False)
    _last_observed: dict[tuple[int, int], int] = field(default_factory=dict,
                                                        init=False, repr=False)
    _reported: set[tuple[int, int]] = field(default_factory=set,
                                             init=False, repr=False)
    _last_ts64: Optional[int] = field(default=None, init=False, repr=False)
    _session_index: Optional[int] = field(default=None, init=False, repr=False)
    events: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        try:
            multiplier = float(self.multiplier)
        except (TypeError, ValueError) as exc:
            raise ValueError("missing-frame multiplier must be a finite positive number") from exc
        if multiplier <= 0 or not math.isfinite(multiplier):
            raise ValueError("missing-frame multiplier must be a finite positive number")
        self.multiplier = multiplier
        self._periods = {
            int(can_id): period
            for can_id, entry in self.profile.items()
            if (period := _profile_period(entry)) is not None
        }

    def _reset_timeline(self, session_index: int, timestamp: int) -> None:
        self._last_observed.clear()
        self._reported.clear()
        self._session_index = session_index
        self._last_ts64 = timestamp

    def process(self, record: trace.TraceRecord) -> list[dict[str, object]]:
        """Process one record and return missing-frame alarms generated by it."""
        if not record.timing_eligible:
            return []
        timestamp = int(record.ts64)
        session_index = int(record.session_index)
        if (self._session_index is None
                or session_index != self._session_index
                or (self._last_ts64 is not None and timestamp < self._last_ts64)):
            self._reset_timeline(session_index, timestamp)

        generated: list[dict[str, object]] = []
        # Check before recording the current frame, so a delayed expected frame
        # both closes the outage and still reports the missed interval.
        for (channel, can_id), last_ts64 in tuple(self._last_observed.items()):
            period_us = self._periods.get(can_id)
            if period_us is None or (channel, can_id) in self._reported:
                continue
            if timestamp - last_ts64 > period_us * self.multiplier:
                event = _missing_event(record, channel, can_id, last_ts64,
                                       period_us, self.multiplier)
                generated.append(event)
                self.events.append(event)
                self._reported.add((channel, can_id))

        frame = record.frame
        if frame is not None and frame.can_id in self._periods:
            key = (int(frame.channel), int(frame.can_id))
            self._last_observed[key] = timestamp
            self._reported.discard(key)
        self._last_ts64 = timestamp
        return generated

    def process_all(self, records: Iterable[trace.TraceRecord]
                    ) -> Iterator[dict[str, object]]:
        """Yield alarms while consuming records in device-record order."""
        for record in records:
            yield from self.process(record)


def detect_missing_frames(
    records: Iterable[trace.TraceRecord],
    profile: Mapping[int, object],
    multiplier: float,
) -> list[dict[str, object]]:
    """Return one event per profiled-ID outage beyond ``period * multiplier``."""
    detector = MissingFrameDetector(profile, multiplier)
    return list(detector.process_all(records))


# Naming aliases keep the rule discoverable for callers using either the
# requirement wording or the shorter detector terminology.
detect_missing_expected_frames = detect_missing_frames


__all__ += [
    "MISSING_EXPECTED_FRAME_EVENT", "MissingFrameDetector",
    "detect_missing_frames", "detect_missing_expected_frames",
]
