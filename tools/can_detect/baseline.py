"""Build a per-CAN-ID baseline from an enriched trace.

The baseline deliberately consumes :class:`can_detect.trace.TraceRecord` objects
rather than raw protocol records.  This keeps timestamp reconstruction and DBC
signal decoding in their respective modules and makes the calculation usable
for both replayed and live record iterators.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping, Optional

from . import dbc, trace


class ProfileError(ValueError):
    """The profile is malformed or cannot be used with the selected DBC."""


class DbcChecksumMismatch(ProfileError):
    """The profile was built against different DBC bytes."""

    def __init__(self, expected: object, actual: object, path: Path) -> None:
        self.expected = expected
        self.actual = actual
        self.path = path
        super().__init__(
            f"DBC checksum mismatch for {path}: profile={expected!r}, "
            f"loaded={actual!r}")


@dataclass(frozen=True)
class ProfileDiscrepancies:
    """Identifiers that make the profile and DBC non-identical."""

    profile_only_ids: tuple[int, ...]
    dbc_only_ids: tuple[int, ...]
    manual_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, list[int]]:
        return {
            "profile_only_ids": list(self.profile_only_ids),
            "dbc_only_ids": list(self.dbc_only_ids),
            "manual_ids": list(self.manual_ids),
        }

def _stats(values: list[float]) -> dict[str, Optional[float]]:
    """Return descriptive population statistics, or nulls for no values."""
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None}
    return {
        "mean": mean(values),
        "std": pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def _signal_range_values(
    database: dbc.DbcDatabase,
    can_id: int,
    data: bytes,
    ranges: dict[str, dict[str, object]],
) -> None:
    """Decode one payload and merge its observed values into ``ranges``."""
    message = database.message_for_id(can_id)
    if message is None:
        return
    decoded = database.decode_signals(can_id, data)
    definitions = {signal.name: signal for signal in message.signals}
    for name, value in decoded.items():
        signal = definitions[name]
        item = ranges.setdefault(name, {
            "min": value,
            "max": value,
            "unit": signal.unit,
        })
        item["min"] = min(item["min"], value)
        item["max"] = max(item["max"], value)


def _window_counts(
    timestamps: list[int],
    window_us: int,
) -> list[int]:
    """Count frames in consecutive windows anchored at the first timestamp."""
    if not timestamps:
        return []
    origin = timestamps[0]
    count = math.floor((timestamps[-1] - origin) / window_us) + 1
    windows = [0] * count
    for timestamp in timestamps:
        windows[math.floor((timestamp - origin) / window_us)] += 1
    return windows


def _incomplete_intervals(
    records: list[trace.TraceRecord],
) -> list[dict[str, object]]:
    """Collect bounded incomplete intervals carried by bus-stat records."""
    intervals: list[dict[str, object]] = []
    for record in records:
        decoded = record.decoded
        if not isinstance(decoded, dict):
            continue
        interval = decoded.get("interval")
        if (isinstance(interval, dict)
                and interval.get("complete") is False
                and interval.get("start_ts64") is not None
                and interval.get("end_ts64") is not None):
            intervals.append(interval)
    return intervals


def _frame_in_incomplete_interval(
    item: trace.TraceRecord,
    incomplete: list[dict[str, object]],
) -> bool:
    """Return whether a timed frame lies in an incomplete channel interval."""
    if item.ts64 is None or item.frame is None:
        return False
    timestamp = int(item.ts64)
    channel = int(item.frame.channel)
    return any(
        int(interval["channel"]) == channel
        and int(interval["start_ts64"]) <= timestamp <= int(interval["end_ts64"])
        for interval in incomplete
    )


def _interval_crosses_incomplete(
    previous: trace.TraceRecord,
    current: trace.TraceRecord,
    incomplete: list[dict[str, object]],
) -> bool:
    """Return whether a candidate pair spans a skipped interval."""
    if previous.ts64 is None or current.ts64 is None:
        return False
    channel = int(current.frame.channel)
    start = int(previous.ts64)
    end = int(current.ts64)
    return any(
        int(interval["channel"]) == channel
        and start <= int(interval["end_ts64"])
        and end >= int(interval["start_ts64"])
        for interval in incomplete
    )


def compute_baseline(
    records: Iterable[trace.TraceRecord],
    database: dbc.DbcDatabase,
    *,
    window_us: int = 1_000_000,
    min_interval_observations: int = 100,
) -> dict[int, dict[str, object]]:
    """Compute baseline metrics for every CAN ID in ``records``.

    ``frame_count`` and ``observed_dlc`` include every decoded frame outside
    incomplete trace intervals, including frames without a reconstructed
    timestamp.  A frame inside an incomplete interval is excluded from all
    baseline metrics.  Time-dependent metrics use only ``timing_eligible``
    frames.  Intervals are formed only between consecutive usable frames of the
    same CAN ID and channel through ``trace.frame_interval_us``; a candidate
    pair is also rejected when it crosses an incomplete interval or a partial
    session boundary.

    An ID is assigned a period only after ``min_interval_observations`` valid
    intervals have been observed.  IDs below that threshold remain in the
    profile, retain their observed DLC values, and are explicitly marked
    ``no_period``.  For qualified IDs, the observed interval range is exposed as
    the period tolerance.  This keeps the rule deterministic and avoids making
    a sparse reference trace look more precise than its measurements justify.

    Window counts are anchored at the first timed frame of each CAN ID and
    include empty windows between the first and last observation.  Standard
    deviations are population standard deviations, since the profile describes
    the observed reference movement rather than estimating a larger population.
    """
    if window_us <= 0:
        raise ValueError("window_us must be greater than zero")
    if min_interval_observations <= 0:
        raise ValueError("min_interval_observations must be greater than zero")

    trace_records = list(records)
    incomplete = _incomplete_intervals(trace_records)
    frames_by_id: dict[int, list[trace.TraceRecord]] = defaultdict(list)
    for record in trace_records:
        if (record.frame is not None
                and not _frame_in_incomplete_interval(record, incomplete)):
            frames_by_id[int(record.frame.can_id)].append(record)

    profile: dict[int, dict[str, object]] = {}
    for can_id in sorted(frames_by_id):
        frames = frames_by_id[can_id]
        observed_dlc = sorted({int(item.frame.dlc) for item in frames})
        signal_ranges: dict[str, dict[str, object]] = {}
        for item in frames:
            _signal_range_values(database, can_id, item.frame.data,
                                 signal_ranges)

        timed = sorted(
            (item for item in frames if item.timing_eligible),
            key=lambda item: (item.ts64, item.seq),
        )
        intervals: list[float] = []
        timestamps: list[int] = []
        previous_by_channel: dict[int, trace.TraceRecord] = {}
        for item in timed:
            timestamps.append(int(item.ts64))
            channel = int(item.frame.channel)
            previous = previous_by_channel.get(channel)
            if previous is not None:
                interval = trace.frame_interval_us(previous, item)
                if (interval is not None and interval >= 0
                        and not _interval_crosses_incomplete(
                            previous, item, incomplete)):
                    intervals.append(float(interval))
            previous_by_channel[channel] = item

        counts = _window_counts(timestamps, window_us)
        interval_summary = _stats(intervals)
        count_summary = _stats([float(value) for value in counts])
        has_period = len(intervals) >= min_interval_observations
        profile[can_id] = {
            "can_id": can_id,
            "frame_count": len(frames),
            "inter_frame_interval_us": {
                **interval_summary,
                "count": len(intervals),
            },
            "period_status": "observed" if has_period else "no_period",
            "period_us": interval_summary["mean"] if has_period else None,
            "tolerance_us": (
                {
                    "min": interval_summary["min"],
                    "max": interval_summary["max"],
                }
                if has_period else None
            ),
            "min_interval_observations": min_interval_observations,
            "observed_dlc": observed_dlc,
            "window_us": window_us,
            "window_counts": {
                **count_summary,
                "count": len(counts),
            },
            "signal_ranges": {
                name: {
                    "min": values["min"],
                    "max": values["max"],
                    "unit": values["unit"],
                }
                for name, values in sorted(signal_ranges.items())
            },
        }
    return profile


# The task and surrounding terminology call the operation both "compute" and
# "build".  Keep one implementation while offering the natural alias for
# callers that use the latter spelling.
build_baseline = compute_baseline


__all__ = ["build_baseline", "compute_baseline"]


PROFILE_FORMAT = "can-baseline-profile"
PROFILE_SCHEMA_VERSION = 1
REFERENCE_ASSUMPTION = (
    "The reference movement is assumed to be anomaly-free; this is an "
    "assumption, not a measurement."
)


def dbc_sha256(path: str | Path) -> str:
    """Return the SHA-256 checksum of the exact DBC file used for a profile."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unit_for_value(path: tuple[str, ...], inherited: Optional[str] = None) -> str:
    """Choose a stable, explicit unit for a profile scalar."""
    key = path[-1] if path else ""
    if inherited:
        return inherited
    if key in {"can_id"}:
        return "CAN ID"
    if key in {"frame_count", "count", "window_counts"}:
        return "frames"
    if key == "observed_dlc":
        return "bytes"
    if key.endswith("_us") or key in {"mean", "std", "min", "max"} and any(
            "interval" in part for part in path):
        return "microseconds"
    if "window" in path and key in {"mean", "std", "min", "max"}:
        return "frames per window"
    return "dimensionless"


def _annotate_value(value: Any, path: tuple[str, ...] = (),
                    inherited_unit: Optional[str] = None) -> Any:
    """Annotate every scalar in a profile for editable, auditable storage."""
    if isinstance(value, dict):
        # Signal ranges already carry the physical unit from the DBC.
        unit = value.get("unit") if isinstance(value.get("unit"), str) else inherited_unit
        return {str(key): _annotate_value(item, path + (str(key),), unit)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_annotate_value(item, path, inherited_unit) for item in value]
    return {
        "value": value,
        "unit": _unit_for_value(path, inherited_unit),
        "provenance": "reference_derived",
    }


def _unannotate_value(value: Any) -> Any:
    """Read annotated values back into the legacy in-memory profile shape."""
    if isinstance(value, dict):
        if set(value) == {"value", "unit", "provenance"}:
            return value["value"]
        return {key: _unannotate_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unannotate_value(item) for item in value]
    return value


def _normalise_reference_metadata(
    *, session_id: Optional[str], duration_us: Optional[int],
    frame_count: Optional[int], skipped_intervals: Iterable[Mapping[str, Any]],
    dbc_checksum: Optional[str],
    reference_assumption: str,
    metadata: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the versioned reference metadata without inventing measurements."""
    result = dict(metadata or {})
    skipped = [dict(item) for item in skipped_intervals]
    result.update({
        "session_id": session_id,
        "duration_us": duration_us,
        "frame_count": frame_count,
        "skipped_interval_count": len(skipped),
        "skipped_intervals": skipped,
        "dbc_sha256": dbc_checksum,
        "reference_assumption": reference_assumption,
    })
    return result


def save_profile(
    profile: Mapping[int, Mapping[str, Any]],
    path: str | Path,
    *,
    session_id: Optional[str] = None,
    duration_us: Optional[int] = None,
    frame_count: Optional[int] = None,
    skipped_intervals: Iterable[Mapping[str, Any]] = (),
    dbc_path: Optional[str | Path] = None,
    dbc_checksum: Optional[str] = None,
    reference_assumption: str = REFERENCE_ASSUMPTION,
    metadata: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Write a deterministic, human-editable and versionable profile file.

    The numeric in-memory profile remains convenient for detector code.  The
    file format adds provenance and units to every scalar, so a manual edit is
    visible in review and can be distinguished from a value derived from the
    reference trace.  ``dbc_path`` is read only to calculate its checksum.
    """
    if dbc_path is not None and dbc_checksum is not None:
        raise ValueError("provide dbc_path or dbc_checksum, not both")
    if dbc_path is not None:
        dbc_checksum = dbc_sha256(dbc_path)
    if not reference_assumption.strip():
        raise ValueError("reference_assumption must not be empty")

    entries = []
    for can_id in sorted(profile, key=int):
        item = dict(profile[can_id])
        item.setdefault("can_id", int(can_id))
        entries.append({
            "can_id": int(can_id),
            "provenance": "reference_derived",
            "values": _annotate_value(item, ("entry", str(can_id))),
        })
    document = {
        "format": PROFILE_FORMAT,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "metadata": _normalise_reference_metadata(
            session_id=session_id, duration_us=duration_us,
            frame_count=frame_count, skipped_intervals=skipped_intervals,
            dbc_checksum=dbc_checksum,
            reference_assumption=reference_assumption, metadata=metadata),
        "entries": entries,
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return document


def load_profile_document(path: str | Path) -> dict[str, Any]:
    """Load and validate the complete editable profile document."""
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid baseline profile: {source}") from exc
    if document.get("format") != PROFILE_FORMAT:
        raise ValueError(f"unsupported baseline profile format: {document.get('format')!r}")
    if document.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"unsupported baseline profile schema: {document.get('schema_version')!r}")
    if not isinstance(document.get("metadata"), dict) or not isinstance(document.get("entries"), list):
        raise ValueError("baseline profile must contain metadata and entries")
    return document


def _contains_manual_provenance(value: Any) -> bool:
    """Detect a manually supplied value in an annotated profile entry."""
    if isinstance(value, dict):
        if value.get("provenance") == "manual" or value.get("origin") == "manual":
            return True
        return any(_contains_manual_provenance(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_manual_provenance(item) for item in value)
    return False


def profile_discrepancies(
    document: Mapping[str, Any], database: dbc.DbcDatabase,
) -> ProfileDiscrepancies:
    """Compare profile CAN IDs with the IDs in an already loaded DBC.

    The comparison is deliberately reported rather than treated as a checksum
    failure: a reference run may contain an ID no longer present in the DBC,
    and an updated DBC may contain IDs absent from that run.
    """
    entries = document["entries"]
    profile_ids = {int(entry["can_id"]) for entry in entries}
    dbc_ids = set(database.messages_by_id)
    manual_ids = {
        int(entry["can_id"])
        for entry in entries
        if entry.get("provenance") == "manual"
        or entry.get("origin") == "manual"
        or _contains_manual_provenance(entry.get("values"))
    }
    return ProfileDiscrepancies(
        tuple(sorted(profile_ids - dbc_ids)),
        tuple(sorted(dbc_ids - profile_ids)),
        tuple(sorted(manual_ids)),
    )


def load_profile(
    path: str | Path,
    database: Optional[dbc.DbcDatabase] = None,
    *,
    report: Optional[dict[str, Any]] = None,
) -> dict[int, dict[str, Any]]:
    """Load a profile, optionally validating it against a loaded DBC.

    When ``database`` is supplied, the checksum in profile metadata is
    compared with the exact DBC bytes used by :func:`can_detect.dbc.load_dbc`.
    A mismatch raises :class:`DbcChecksumMismatch` before any detector session
    can start.  Identifier discrepancies are non-fatal and are copied into the
    supplied session-report mapping under ``profile_dbc_discrepancies``.
    """
    document = load_profile_document(path)
    metadata = document["metadata"]
    if database is not None:
        expected = metadata.get("dbc_sha256")
        actual = database.sha256
        if expected != actual:
            raise DbcChecksumMismatch(expected, actual, database.path)
        discrepancies = profile_discrepancies(document, database)
        if report is not None:
            report["profile_dbc_discrepancies"] = discrepancies.as_dict()

    result: dict[int, dict[str, Any]] = {}
    for entry in document["entries"]:
        if not isinstance(entry, dict) or "can_id" not in entry or "values" not in entry:
            raise ProfileError("baseline profile entry must contain can_id and values")
        can_id = int(entry["can_id"])
        if can_id in result:
            raise ProfileError(f"duplicate CAN ID in baseline profile: {can_id}")
        result[can_id] = _unannotate_value(entry["values"])
    return result


# Explicit aliases make the persistence operation discoverable to callers that
# use read/write terminology rather than load/save terminology.
write_profile = save_profile
read_profile = load_profile_document


__all__ = [
    "DbcChecksumMismatch", "PROFILE_FORMAT", "PROFILE_SCHEMA_VERSION",
    "ProfileDiscrepancies", "ProfileError", "REFERENCE_ASSUMPTION",
    "build_baseline", "compute_baseline", "dbc_sha256", "load_profile",
    "load_profile_document", "profile_discrepancies", "read_profile",
    "save_profile", "write_profile",
]
