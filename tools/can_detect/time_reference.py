"""Resolve the generator clock against device timestamps in a CAN trace."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Mapping

from can_generate.time_reference import (
    DEFAULT_MARKER_CAN_ID,
    MARKER_END,
    MARKER_START,
    decode_marker,
)


class TimeReferenceError(ValueError):
    """The trace does not contain enough valid marker frames."""


@dataclass(frozen=True)
class TimeScaleReference:
    marker_can_id: int
    offset_us: float
    markers: tuple[dict, ...]
    method: str = "can_marker_mean_device_minus_generator_us"

    def generator_to_device_us(self, generator_time_us: float) -> float:
        return float(generator_time_us) + self.offset_us

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "marker_can_id": self.marker_can_id,
            "generator_to_device_offset_us": self.offset_us,
            "markers": [dict(marker) for marker in self.markers],
        }


def find_time_markers(records: Iterable[object],
                      marker_can_id: int = DEFAULT_MARKER_CAN_ID) -> list[dict]:
    """Find valid start/end marker frames in enriched trace records.

    Records may be ``TraceRecord`` instances or compatible objects exposing
    ``frame`` and ``ts64``. Frames without a reconstructed device time are
    ignored because they cannot establish a common time scale.
    """
    found: list[dict] = []
    for record in records:
        frame = getattr(record, "frame", None)
        device_time = getattr(record, "ts64", None)
        if frame is None or device_time is None or frame.can_id != marker_can_id:
            continue
        marker = decode_marker(frame.data)
        if marker is None:
            continue
        found.append({
            "kind": marker.name,
            "kind_code": marker.kind,
            "generator_time_us": marker.generator_time_us,
            "device_time_us": int(device_time),
            "offset_us": int(device_time) - marker.generator_time_us,
            "sequence": getattr(record, "seq", None),
        })
    return found


def compute_time_scale_reference(
        records: Iterable[object],
        marker_can_id: int = DEFAULT_MARKER_CAN_ID,
) -> TimeScaleReference:
    """Compute the generator-to-device offset from start and end markers."""
    markers = find_time_markers(records, marker_can_id)
    by_kind = {marker["kind_code"]: marker for marker in markers}
    missing = [name for code, name in ((MARKER_START, "start"), (MARKER_END, "end"))
               if code not in by_kind]
    if missing:
        raise TimeReferenceError(
            "trace is missing valid time marker(s): " + ", ".join(missing))
    offsets = [by_kind[MARKER_START]["offset_us"], by_kind[MARKER_END]["offset_us"]]
    return TimeScaleReference(marker_can_id, mean(offsets), tuple(markers))


def record_time_reference(ground_truth: Mapping[str, object],
                          reference: TimeScaleReference) -> dict:
    """Return ground truth with the alignment method recorded immutably."""
    result = dict(ground_truth)
    result["time_reference"] = reference.as_dict()
    return result


__all__ = [
    "TimeReferenceError", "TimeScaleReference", "compute_time_scale_reference",
    "find_time_markers", "record_time_reference",
]
