"""CAN marker frames used to align generator and logger time scales.

The marker is deliberately self-describing and fits in one standard CAN data
frame: two magic bytes, a marker kind, and the generator clock in microseconds.
The marker identifier is kept outside the DBC traffic configuration because it
is an evaluation aid, not a signal from the simulated vehicle network.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct

DEFAULT_MARKER_CAN_ID = 0x7FE
MARKER_MAGIC = b"TR"
MARKER_START = 1
MARKER_END = 2
_MARKER_FORMAT = "<2sBI"
MARKER_DLC = struct.calcsize(_MARKER_FORMAT)


class TimeMarkerError(ValueError):
    """A marker identifier or payload is invalid."""


@dataclass(frozen=True)
class TimeMarker:
    kind: int
    generator_time_us: int

    @property
    def name(self) -> str:
        return {MARKER_START: "start", MARKER_END: "end"}.get(
            self.kind, f"unknown_{self.kind}")


def validate_marker_id(can_id: int) -> int:
    can_id = int(can_id)
    if not 0 <= can_id <= 0x7FF:
        raise TimeMarkerError("marker CAN ID must be an 11-bit identifier")
    return can_id


def encode_marker(kind: int, generator_time_us: int) -> bytes:
    """Encode a session marker as a standard CAN data payload."""
    if kind not in (MARKER_START, MARKER_END):
        raise TimeMarkerError("marker kind must be MARKER_START or MARKER_END")
    timestamp = int(generator_time_us)
    if not 0 <= timestamp <= 0xFFFFFFFF:
        raise TimeMarkerError("generator marker time must fit in an unsigned 32-bit value")
    return struct.pack(_MARKER_FORMAT, MARKER_MAGIC, kind, timestamp)


def decode_marker(data: bytes | bytearray, *, expected_kind: int | None = None) -> TimeMarker | None:
    """Decode a marker payload, returning ``None`` for an ordinary frame."""
    raw = bytes(data)
    if len(raw) != MARKER_DLC:
        return None
    magic, kind, timestamp = struct.unpack(_MARKER_FORMAT, raw)
    if magic != MARKER_MAGIC or kind not in (MARKER_START, MARKER_END):
        return None
    if expected_kind is not None and kind != expected_kind:
        return None
    return TimeMarker(kind, timestamp)


__all__ = [
    "DEFAULT_MARKER_CAN_ID", "MARKER_DLC", "MARKER_END", "MARKER_MAGIC",
    "MARKER_START", "TimeMarker", "TimeMarkerError", "decode_marker",
    "encode_marker", "validate_marker_id",
]
