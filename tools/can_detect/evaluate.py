"""Align generated ground truth with detector events.

The generator records episode times relative to the generator run.  Marker
frames bracket the run and carry the generator's monotonic timestamp.  This
module converts those times to the device timestamp used by detector events
and performs the requirement 8.4 match without consulting the host clock.
"""

from __future__ import annotations

from dataclasses import dataclass
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .time_reference import TimeScaleReference, compute_time_scale_reference


class EvaluationError(ValueError):
    """Ground truth or event data cannot be evaluated."""


# Keep this order stable: the CSV is copied directly into thesis tables.
EVALUATION_CSV_COLUMNS = (
    "session_id",
    "method",
    "anomaly_type",
    "true_positives",
    "false_positives",
    "false_negatives",
    "precision",
    "recall",
    "f1_score",
    "mean_detection_latency_us",
)


@dataclass(frozen=True)
class EvaluatedEpisode:
    """An episode with its run-relative and device-time boundaries."""

    index: int
    data: Mapping[str, Any]
    start_us: float
    end_us: float
    can_id: int | None


@dataclass(frozen=True)
class EventMatch:
    """One detector event associated with an episode, or no episode."""

    event_index: int
    event: Mapping[str, Any]
    episode_index: int | None
    event_time_us: float | None


@dataclass(frozen=True)
class EvaluationResult:
    """Raw associations used by later per-episode metric calculations.

    Events and episodes that fall in an incomplete trace interval are omitted
    from the associations.  The intervals remain in the result so reporting
    can explain exactly which parts of the trace were not scored.
    """

    matches: tuple[EventMatch, ...]
    unmatched_event_indices: tuple[int, ...]
    unmatched_episode_indices: tuple[int, ...]
    episodes: tuple[EvaluatedEpisode, ...]
    time_reference: TimeScaleReference | None = None
    incomplete_intervals: tuple[Mapping[str, Any], ...] = ()

    @property
    def matched_event_indices(self) -> tuple[int, ...]:
        return tuple(item.event_index for item in self.matches
                     if item.episode_index is not None)

    @property
    def incomplete_interval_durations_us(self) -> tuple[int, ...]:
        """Durations of the skipped intervals, in device microseconds."""
        return tuple(int(interval["duration_us"])
                     for interval in self.incomplete_intervals
                     if interval.get("duration_us") is not None)

    @property
    def detected_episode_indices(self) -> tuple[int, ...]:
        """Return each detected episode once, in source order."""
        return tuple(sorted({item.episode_index for item in self.matches
                             if item.episode_index is not None}))

    @property
    def detected_episode_count(self) -> int:
        """Number of ground-truth episodes detected at least once."""
        return len(self.detected_episode_indices)

    @property
    def redundant_event_indices(self) -> tuple[int, ...]:
        """Return matched events after the first event for their episode.

        The first matching event is the episode detection. Further matching
        events are redundant alarms and are kept separate from unmatched
        events, which represent false positives.
        """
        seen_episodes: set[int] = set()
        redundant: list[int] = []
        for item in self.matches:
            if item.episode_index is None:
                continue
            if item.episode_index in seen_episodes:
                redundant.append(item.event_index)
            else:
                seen_episodes.add(item.episode_index)
        return tuple(redundant)

    @property
    def redundant_alarm_count(self) -> int:
        """Number of matched alarms beyond one detection per episode."""
        return len(self.redundant_event_indices)

    def metrics(self) -> "EvaluationMetrics":
        """Calculate episode metrics for this evaluation result."""
        return calculate_metrics(self)


@dataclass(frozen=True)
class MetricSummary:
    """Episode metrics for one anomaly type and detector method.

    Repeated events for one episode count once as a true positive.  Additional
    matching events are reported as ``redundant_events`` rather than false
    positives, so the score measures episode detection rather than alarm rate.
    """

    anomaly_type: str
    method: str
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    latencies_us: tuple[float, ...] = ()
    mean_latency_us: float | None = None
    redundant_events: int = 0

    @property
    def tp(self) -> int:
        return self.true_positives

    @property
    def fp(self) -> int:
        return self.false_positives

    @property
    def fn(self) -> int:
        return self.false_negatives


@dataclass(frozen=True)
class EvaluationMetrics:
    """All per-type and per-method summaries for an evaluation."""

    summaries: tuple[MetricSummary, ...]
    redundant_event_indices: tuple[int, ...] = ()

    def by_type_and_method(self) -> dict[tuple[str, str], MetricSummary]:
        return {(item.anomaly_type, item.method): item
                for item in self.summaries}


def _event_method(event: Mapping[str, Any]) -> str:
    value = event.get("method", "unknown")
    return str(value) if value not in (None, "") else "unknown"


def _event_type(event: Mapping[str, Any]) -> str:
    value = event.get("type", "unknown")
    return str(value) if value not in (None, "") else "unknown"


def _episode_type(episode: EvaluatedEpisode) -> str:
    value = episode.data.get("type", "unknown")
    return str(value) if value not in (None, "") else "unknown"


def _safe_divide(numerator: int, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0


def calculate_metrics(result: EvaluationResult) -> EvaluationMetrics:
    """Calculate precision, recall, F1 and first-detection latency.

    Metrics are calculated independently for every pair of truth anomaly type
    and event ``method``.  A method is compared with all episodes of the type,
    including episodes it did not match, which makes false negatives explicit.
    Unmatched events are false positives; redundant events matched to an
    already detected episode are counted separately and do not lower precision.
    """
    episodes_by_type: dict[str, set[int]] = {}
    for episode in result.episodes:
        episodes_by_type.setdefault(_episode_type(episode), set()).add(episode.index)

    methods = {_event_method(item.event) for item in result.matches}
    methods.update(_event_method(item.event) for item in result.matches
                   if item.episode_index is None)
    # No event means no detector method was exercised, so there is no meaningful
    # method row to emit.  This avoids inventing a method name in empty runs.
    if not methods:
        return EvaluationMetrics((), result.redundant_event_indices)

    # Keep only the first matched event for each (method, episode) pair.
    first_matches: dict[tuple[str, int], EventMatch] = {}
    redundant: list[int] = []
    for item in result.matches:
        if item.episode_index is None:
            continue
        key = (_event_method(item.event), item.episode_index)
        previous = first_matches.get(key)
        if previous is None:
            first_matches[key] = item
            continue
        previous_time = previous.event_time_us
        current_time = item.event_time_us
        # The earliest timestamp is the first detection, independent of input
        # container ordering.  Missing timestamps cannot be used for latency.
        if (previous_time is None or
                (current_time is not None and current_time < previous_time)):
            redundant.append(previous.event_index)
            first_matches[key] = item
        else:
            redundant.append(item.event_index)

    unmatched_by_group: dict[tuple[str, str], int] = {}
    for item in result.matches:
        if item.episode_index is None:
            key = (_event_type(item.event), _event_method(item.event))
            unmatched_by_group[key] = unmatched_by_group.get(key, 0) + 1

    types = set(episodes_by_type)
    types.update(anomaly_type for anomaly_type, _ in unmatched_by_group)
    summaries: list[MetricSummary] = []
    for anomaly_type in sorted(types):
        episode_indices = episodes_by_type.get(anomaly_type, set())
        for method in sorted(methods):
            detected = {episode_index for (candidate_method, episode_index)
                        in first_matches
                        if candidate_method == method
                        and episode_index in episode_indices}
            tp = len(detected)
            fn = len(episode_indices) - tp
            fp = unmatched_by_group.get((anomaly_type, method), 0)
            latencies = tuple(
                first_matches[(method, episode_index)].event_time_us -
                result.episodes[episode_index].start_us
                for episode_index in sorted(detected)
                if first_matches[(method, episode_index)].event_time_us is not None
            )
            mean_latency = (sum(latencies) / len(latencies)
                            if latencies else None)
            precision = _safe_divide(tp, tp + fp)
            recall = _safe_divide(tp, tp + fn)
            f1 = _safe_divide(2 * precision * recall, precision + recall)
            redundant_count = sum(
                1 for item in result.matches
                if item.episode_index in episode_indices
                and _event_method(item.event) == method
                and item.event_index in redundant
            )
            summaries.append(MetricSummary(
                anomaly_type, method, tp, fp, fn, precision, recall, f1,
                latencies, mean_latency, redundant_count))
    return EvaluationMetrics(tuple(summaries), tuple(sorted(set(redundant))))


# Names used by callers and reports in different stages of the project.
compute_metrics = calculate_metrics


def load_ground_truth(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a generator ground-truth JSON file."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read ground truth {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError("ground truth root must be a JSON object")
    if not payload.get("session_id"):
        raise EvaluationError("ground truth session_id is missing")
    episodes = payload.get("episodes")
    if not isinstance(episodes, list):
        raise EvaluationError("ground truth episodes must be a list")
    for index, episode in enumerate(episodes):
        _validate_episode(episode, index)
    return payload


def _ground_truth_mapping(value: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(value, (str, Path)):
        return load_ground_truth(value)
    if not isinstance(value, Mapping):
        raise EvaluationError("ground truth must be a mapping or JSON path")
    episodes = value.get("episodes")
    if not isinstance(episodes, list):
        raise EvaluationError("ground truth episodes must be a list")
    for index, episode in enumerate(episodes):
        _validate_episode(episode, index)
    return value


def _validate_episode(episode: Any, index: int) -> None:
    if not isinstance(episode, Mapping):
        raise EvaluationError(f"ground truth episode {index} must be an object")
    for name in ("start", "end", "type"):
        if name not in episode:
            raise EvaluationError(f"ground truth episode {index} lacks {name}")
    try:
        start = float(episode["start"])
        end = float(episode["end"])
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"ground truth episode {index} has invalid time") from exc
    if start < 0 or end < start:
        raise EvaluationError(f"ground truth episode {index} has invalid interval")
    if episode.get("can_id") is not None:
        try:
            can_id = int(episode["can_id"])
        except (TypeError, ValueError) as exc:
            raise EvaluationError(f"ground truth episode {index} has invalid CAN ID") from exc
        if not 0 <= can_id <= 0x7FF:
            raise EvaluationError(f"ground truth episode {index} CAN ID is not 11-bit")


def _event_time_us(event: Mapping[str, Any]) -> float | None:
    """Read the device timestamp used by detector event dictionaries."""
    for key in ("ts64", "device_time_us", "timestamp_us", "time_us"):
        if event.get(key) is not None:
            try:
                return float(event[key])
            except (TypeError, ValueError) as exc:
                raise EvaluationError(f"event has invalid {key}: {event!r}") from exc
    # Window events describe their boundary rather than a single timestamp.
    for key in ("window_start_us", "window_end_us"):
        if event.get(key) is not None:
            try:
                return float(event[key])
            except (TypeError, ValueError) as exc:
                raise EvaluationError(f"event has invalid {key}: {event!r}") from exc
    return None


def _event_can_id(event: Mapping[str, Any]) -> int | None:
    value = event.get("can_id", event.get("id"))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationError(f"event has invalid CAN ID: {event!r}") from exc


def convert_episodes(
    ground_truth: Mapping[str, Any] | str | Path,
    reference: TimeScaleReference,
    *,
    run_start_generator_us: float | None = None,
) -> tuple[EvaluatedEpisode, ...]:
    """Convert run-relative truth times to device microseconds.

    The start marker is the run origin.  It is obtained from the marker-derived
    reference when not supplied explicitly, which keeps the function useful
    with references reconstructed from either a trace or a persisted truth
    alignment record.
    """
    payload = _ground_truth_mapping(ground_truth)
    if run_start_generator_us is None:
        starts = [item for item in reference.markers if item.get("kind") == "start"]
        if not starts:
            raise EvaluationError("time reference has no start marker")
        run_start_generator_us = float(starts[0]["generator_time_us"])
    result = []
    for index, item in enumerate(payload["episodes"]):
        start = float(item["start"])
        end = float(item["end"])
        start_generator_us = run_start_generator_us + start * 1_000_000.0
        end_generator_us = run_start_generator_us + end * 1_000_000.0
        result.append(EvaluatedEpisode(
            index=index, data=item,
            start_us=reference.generator_to_device_us(start_generator_us),
            end_us=reference.generator_to_device_us(end_generator_us),
            can_id=None if item.get("can_id") is None else int(item["can_id"]),
        ))
    return tuple(result)


def _time_in_incomplete_interval(
    timestamp_us: float | None,
    intervals: Sequence[Mapping[str, Any]],
) -> bool:
    if timestamp_us is None:
        return False
    return any(
        interval.get("start_ts64") is not None
        and interval.get("end_ts64") is not None
        and float(interval["start_ts64"]) <= timestamp_us <= float(interval["end_ts64"])
        for interval in intervals
    )


def _episode_overlaps_incomplete_interval(
    episode: EvaluatedEpisode,
    intervals: Sequence[Mapping[str, Any]],
) -> bool:
    return any(
        interval.get("start_ts64") is not None
        and interval.get("end_ts64") is not None
        and episode.start_us <= float(interval["end_ts64"])
        and episode.end_us >= float(interval["start_ts64"])
        for interval in intervals
    )


def _incomplete_intervals_from_records(
    records: Sequence[object],
) -> tuple[Mapping[str, Any], ...]:
    """Collect unique bounded incomplete intervals attached by TraceReader."""
    result: list[Mapping[str, Any]] = []
    seen: set[tuple[object, object, object]] = set()
    for record in records:
        decoded = getattr(record, "decoded", None)
        interval = decoded.get("interval") if isinstance(decoded, Mapping) else None
        if not isinstance(interval, Mapping) or interval.get("complete") is not False:
            continue
        if interval.get("start_ts64") is None or interval.get("end_ts64") is None:
            continue
        key = (interval.get("channel"), interval.get("start_ts64"),
               interval.get("end_ts64"))
        if key not in seen:
            seen.add(key)
            result.append(dict(interval))
    return tuple(result)


def match_events(
    events: Iterable[Mapping[str, Any]],
    episodes: Sequence[EvaluatedEpisode],
    *,
    tolerance_s: float,
    incomplete_intervals: Sequence[Mapping[str, Any]] = (),
) -> EvaluationResult:
    """Associate events with compatible, complete episodes.

    Matching and scoring deliberately ignore events inside an incomplete trace
    interval and episodes overlapping one.  Such data cannot establish either
    a detection or a missed detection and must not affect the metrics.
    """
    tolerance_us = float(tolerance_s) * 1_000_000.0
    if tolerance_us < 0:
        raise EvaluationError("matching tolerance must be non-negative")
    event_list = list(events)
    eligible_episodes = tuple(
        episode for episode in episodes
        if not _episode_overlaps_incomplete_interval(episode, incomplete_intervals)
    )
    associations: list[EventMatch] = []
    unmatched_events: list[int] = []
    matched_episode_indices: set[int] = set()
    for event_index, event in enumerate(event_list):
        event_time = _event_time_us(event)
        if _time_in_incomplete_interval(event_time, incomplete_intervals):
            continue
        event_id = _event_can_id(event)
        candidates = []
        if event_time is not None:
            for episode in eligible_episodes:
                if episode.can_id is not None and event_id != episode.can_id:
                    continue
                if episode.start_us - tolerance_us <= event_time <= episode.end_us + tolerance_us:
                    candidates.append(episode)
        if candidates:
            # Deterministic tie-break for overlapping episodes: nearest start,
            # then source order.
            selected = min(candidates, key=lambda item: (abs(event_time - item.start_us), item.index))
            matched_episode_indices.add(selected.index)
            associations.append(EventMatch(event_index, event, selected.index, event_time))
        else:
            unmatched_events.append(event_index)
            associations.append(EventMatch(event_index, event, None, event_time))
    unmatched_episodes = tuple(item.index for item in eligible_episodes
                               if item.index not in matched_episode_indices)
    return EvaluationResult(tuple(associations), tuple(unmatched_events),
                             unmatched_episodes, eligible_episodes,
                             incomplete_intervals=tuple(incomplete_intervals))


def evaluate(
    events: Iterable[Mapping[str, Any]],
    ground_truth: Mapping[str, Any] | str | Path,
    records: Iterable[object],
    *,
    tolerance_s: float,
    marker_can_id: int | None = None,
) -> EvaluationResult:
    """Align markers, skip incomplete trace intervals, and match events."""
    record_list = list(records)
    kwargs = {} if marker_can_id is None else {"marker_can_id": marker_can_id}
    reference = compute_time_scale_reference(record_list, **kwargs)
    converted = convert_episodes(ground_truth, reference)
    incomplete = _incomplete_intervals_from_records(record_list)
    result = match_events(events, converted, tolerance_s=tolerance_s,
                          incomplete_intervals=incomplete)
    return EvaluationResult(result.matches, result.unmatched_event_indices,
                            result.unmatched_episode_indices, result.episodes,
                            reference, incomplete)


def export_evaluation_csv(
    results: EvaluationMetrics | Iterable[Mapping[str, Any] | MetricSummary],
    destination: str | Path,
    *,
    session_id: str | None = None,
) -> Path:
    """Write per-method and per-anomaly evaluation rows to a stable CSV.

    ``results`` may be the native :class:`EvaluationMetrics` object or
    row-oriented mappings.  Every exported row has the fields in
    :data:`EVALUATION_CSV_COLUMNS`; extra fields are ignored.  ``session_id``
    is used to fill that column when native metric summaries are exported.
    """
    if isinstance(results, EvaluationMetrics):
        rows: list[Mapping[str, Any]] = [
            {
                "session_id": session_id or "",
                "method": summary.method,
                "anomaly_type": summary.anomaly_type,
                "true_positives": summary.true_positives,
                "false_positives": summary.false_positives,
                "false_negatives": summary.false_negatives,
                "precision": summary.precision,
                "recall": summary.recall,
                "f1_score": summary.f1,
                "mean_detection_latency_us": summary.mean_latency_us,
            }
            for summary in results.summaries
        ]
    else:
        rows = []
        for item in results:
            if isinstance(item, MetricSummary):
                rows.append({
                    "session_id": session_id or "",
                    "method": item.method,
                    "anomaly_type": item.anomaly_type,
                    "true_positives": item.true_positives,
                    "false_positives": item.false_positives,
                    "false_negatives": item.false_negatives,
                    "precision": item.precision,
                    "recall": item.recall,
                    "f1_score": item.f1,
                    "mean_detection_latency_us": item.mean_latency_us,
                })
            else:
                row = dict(item)
                if session_id is not None:
                    row.setdefault("session_id", session_id)
                rows.append(row)

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EvaluationError(f"evaluation row {index} must be a mapping")
        missing = [name for name in EVALUATION_CSV_COLUMNS if name not in row]
        if missing:
            raise EvaluationError(
                f"evaluation row {index} lacks CSV columns: {', '.join(missing)}"
            )

    output = Path(destination)
    try:
        with output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=EVALUATION_CSV_COLUMNS,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise EvaluationError(f"cannot write evaluation CSV {output}: {exc}") from exc
    return output


# Descriptive aliases for callers that use report or metric terminology.
export_results_csv = export_evaluation_csv
export_metrics_csv = export_evaluation_csv


# Descriptive alias for callers that prefer the requirement terminology.
evaluate_events = evaluate

__all__ = [
    "EvaluationError", "EVALUATION_CSV_COLUMNS", "EvaluatedEpisode", "EventMatch", "EvaluationResult",
    "MetricSummary", "EvaluationMetrics", "calculate_metrics", "compute_metrics",
    "load_ground_truth", "convert_episodes", "match_events", "evaluate",
    "evaluate_events", "export_evaluation_csv", "export_results_csv",
    "export_metrics_csv",
]
