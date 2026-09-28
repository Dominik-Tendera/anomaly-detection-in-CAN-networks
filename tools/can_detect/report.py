"""Write anomaly events as deterministic JSON Lines.

Rule implementations deliberately emit small dictionaries tailored to their
measurements.  This module is the boundary at which those dictionaries become
the stable, human-readable event format used by a session.  It adds common
field aliases without changing the rule-specific values, keeps window alarms
identified by their complete window rather than an arbitrary frame sequence,
and orders output by the reconstructed device timestamp.

Absolute time is optional because the logger RTC may be unset.  A caller can
provide a resolver based on :meth:`TraceReader.absolute_time`; an event may
also carry an explicit ``absolute_time`` value, which is preserved.
"""

from __future__ import annotations

from datetime import date, datetime, time
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional


AbsoluteTimeResolver = Callable[[Optional[int]], object]


def _device_time(event: Mapping[str, Any]) -> Optional[int]:
    """Return the event's device time using the supported input aliases."""
    value = event.get("device_time_us", event.get("ts64", event.get("timestamp_us")))
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("event device time must be an integer in microseconds") from exc


def _json_time(value: object) -> object:
    """Convert datetime-like absolute times to the JSON report representation."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def _first(event: Mapping[str, Any], *names: str) -> object:
    for name in names:
        if name in event and event[name] is not None:
            return event[name]
    return None


def _measurement(event: Mapping[str, Any]) -> tuple[object, Optional[str]]:
    """Find a rule-specific measured value and infer its unit when possible."""
    value = _first(
        event, "measured_value", "measured", "value", "measured_interval_us",
        "measured_frame_count", "increment", "score",
    )
    unit = _first(event, "measured_unit", "unit")
    if unit is None:
        if "measured_interval_us" in event:
            unit = "us"
        elif "measured_frame_count" in event or "increment" in event:
            unit = "frames"
    return value, None if unit is None else str(unit)


def _expected(event: Mapping[str, Any]) -> tuple[object, object, Optional[str]]:
    """Find an expected value or lower/upper bounds and their unit."""
    expected = _first(event, "expected", "expected_value", "threshold")
    minimum = _first(event, "expected_min", "minimum", "expected_min_us")
    maximum = _first(event, "expected_max", "maximum", "expected_max_us")
    unit = _first(event, "expected_unit")
    if unit is None:
        if "expected_min_us" in event or "expected_max_us" in event:
            unit = "us"
        elif "expected_min_frame_count" in event or "expected_max_frame_count" in event:
            unit = "frames"
    if expected is None and minimum is None and maximum is None:
        return None, None, None if unit is None else str(unit)
    if expected is None and (minimum is not None or maximum is not None):
        expected = {"min": minimum, "max": maximum}
    return expected, (minimum, maximum), None if unit is None else str(unit)


def normalize_event(
    event: Mapping[str, Any],
    *,
    session_id: Optional[str] = None,
    absolute_time: Optional[AbsoluteTimeResolver] = None,
) -> dict[str, Any]:
    """Return one JSON-serialisable event in the reporting schema.

    Existing rule-specific keys are retained.  The canonical fields added by
    this function are useful to consumers that should not need to know which
    detector produced the event.  For window rules ``frame_seq`` is removed,
    even if a caller supplied one, because the window boundaries are the
    event's location and identity.
    """
    if not isinstance(event, Mapping):
        raise TypeError("anomaly event must be a mapping")

    result: dict[str, Any] = dict(event)
    ts64 = _device_time(event)
    result["device_time_us"] = ts64
    # Keep ts64 for compatibility with the detector and evaluator APIs.
    result.setdefault("ts64", ts64)
    result.setdefault("absolute_time", None)
    if result["absolute_time"] is None and absolute_time is not None:
        result["absolute_time"] = absolute_time(ts64)
    result["absolute_time"] = _json_time(result["absolute_time"])

    if session_id is not None:
        result.setdefault("session_id", session_id)
    result.setdefault("channel", None)
    result.setdefault("can_id", result.get("frame_id"))
    result.setdefault("rule_type", result.get("type"))
    result.setdefault("method", None)

    measured, measured_unit = _measurement(event)
    result.setdefault("measured_value", measured)
    result.setdefault("measured_unit", measured_unit)
    expected, bounds, expected_unit = _expected(event)
    result.setdefault("expected", expected)
    result.setdefault("expected_bounds", (
        {"min": bounds[0], "max": bounds[1]}
        if bounds is not None and bounds != (None, None) else None
    ))
    result.setdefault("expected_unit", expected_unit)

    is_window_event = "window_start_us" in event or "window_end_us" in event
    if is_window_event:
        if "window_start_us" not in result or "window_end_us" not in result:
            raise ValueError("window anomaly event must contain both window boundaries")
        result.pop("frame_seq", None)
    else:
        result.setdefault("frame_seq", event.get("frame_seq"))
    return result


def sort_events(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Stable-sort events by device time, placing unavailable times last."""
    materialized = list(events)
    return sorted(
        materialized,
        key=lambda event: (
            _device_time(event) is None,
            _device_time(event) if _device_time(event) is not None else 0,
        ),
    )


def write_events(
    path: Path | str,
    events: Iterable[Mapping[str, Any]],
    *,
    session_id: Optional[str] = None,
    absolute_time: Optional[AbsoluteTimeResolver] = None,
    append: bool = False,
) -> Path:
    """Write all anomaly events to ``path`` as one compact JSON object per line.

    The complete input is sorted before writing, so output is deterministic
    even when rule stages yield events in different orders.  ``append`` is
    intended for explicitly managed continuation sessions; normal session
    output should use the default replacement mode and a session-specific path.
    """
    normalized = [
        normalize_event(event, session_id=session_id, absolute_time=absolute_time)
        for event in sort_events(events)
    ]
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with destination.open(mode, encoding="utf-8", newline="\n") as stream:
        for event in normalized:
            stream.write(json.dumps(
                event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ))
            stream.write("\n")
    return destination


class EventReporter:
    """Small stateful adapter for callers that receive events incrementally."""

    def __init__(self, path: Path | str, *, session_id: Optional[str] = None,
                 absolute_time: Optional[AbsoluteTimeResolver] = None) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.absolute_time = absolute_time
        self._events: list[Mapping[str, Any]] = []

    def add(self, event: Mapping[str, Any]) -> None:
        self._events.append(event)

    def write(self) -> Path:
        return write_events(
            self.path, self._events, session_id=self.session_id,
            absolute_time=self.absolute_time,
        )


__all__ = ["EventReporter", "normalize_event", "sort_events", "write_events"]


def write_session_snapshot(
    path: Path | str,
    report: Mapping[str, Any],
) -> Path:
    """Persist the current session snapshot as a JSON report.

    The report is written through a sibling temporary file and replaced only
    after serialization succeeds.  This keeps an already written report intact
    if a later interruption occurs while the snapshot is being rendered, while
    the supplied snapshot retains all results collected before termination.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "EventReporter", "normalize_event", "sort_events", "write_events",
    "write_session_snapshot",
]


# ---------------------------------------------------------------------------
# Session summary reporting


def _json_value(value: Any) -> Any:
    """Convert report-layer objects to values accepted by ``json.dumps``."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _json_value(value.as_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _json_value({
            name: getattr(value, name) for name in value.__dataclass_fields__
        })
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def _source_mapping(source: object) -> Mapping[str, Any]:
    if source is None:
        return {}
    if isinstance(source, Mapping):
        return source
    if hasattr(source, "report_state") and callable(source.report_state):
        value = source.report_state()
        return value if isinstance(value, Mapping) else {}
    if hasattr(source, "report") and callable(source.report):
        value = source.report()
        return value if isinstance(value, Mapping) else {}
    return {}


def _collect_incomplete_intervals(*sources: object) -> list[dict[str, Any]]:
    """Collect and de-duplicate bounded incomplete intervals from all layers."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            candidate = value.get("incomplete_intervals")
            if isinstance(candidate, (list, tuple)):
                for interval in candidate:
                    if not isinstance(interval, Mapping):
                        continue
                    item = dict(interval)
                    start = item.get("start_ts64")
                    end = item.get("end_ts64")
                    if item.get("duration_us") is None and start is not None and end is not None:
                        item["duration_us"] = max(0, int(end) - int(start))
                    key = json.dumps(_json_value(item), sort_keys=True, separators=(",", ":"))
                    if key not in seen:
                        seen.add(key)
                        result.append(item)
            for item in value.values():
                if isinstance(item, Mapping):
                    visit(item)

    for source in sources:
        visit(_source_mapping(source))
        native_intervals = getattr(source, "incomplete_intervals", None)
        if isinstance(native_intervals, (list, tuple)):
            visit({"incomplete_intervals": native_intervals})
    return result


def _event_counts(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_method: dict[str, int] = {}
    total = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type", event.get("rule_type", "unknown")))
        method = str(event.get("method", "unknown"))
        by_type[event_type] = by_type.get(event_type, 0) + 1
        by_method[method] = by_method.get(method, 0) + 1
        total += 1
    return {
        "total": total,
        "by_type": dict(sorted(by_type.items())),
        "by_method": dict(sorted(by_method.items())),
    }


def _evaluation_payload(evaluation: object) -> dict[str, Any] | None:
    """Serialize native evaluation objects and already-exported mappings."""
    if evaluation is None:
        return None
    value = evaluation
    if hasattr(value, "metrics") and callable(value.metrics):
        result = value
        metrics = value.metrics()
        payload: dict[str, Any] = {
            "incomplete_intervals": list(getattr(result, "incomplete_intervals", ())),
            "matched_events": len(getattr(result, "matched_event_indices", ())),
            "detected_episodes": getattr(result, "detected_episode_count", 0),
            "redundant_events": getattr(result, "redundant_alarm_count", 0),
            "unmatched_events": len(getattr(result, "unmatched_event_indices", ())),
            "unmatched_episodes": len(getattr(result, "unmatched_episode_indices", ())),
        }
        value = metrics
    if hasattr(value, "summaries"):
        summaries = []
        for summary in value.summaries:
            summaries.append({
                "anomaly_type": summary.anomaly_type,
                "method": summary.method,
                "true_positives": summary.true_positives,
                "false_positives": summary.false_positives,
                "false_negatives": summary.false_negatives,
                "precision": summary.precision,
                "recall": summary.recall,
                "f1_score": summary.f1,
                "mean_detection_latency_us": summary.mean_latency_us,
                "redundant_events": summary.redundant_events,
            })
        if 'payload' not in locals():
            payload = {}
        payload["summaries"] = summaries
        payload["redundant_event_count"] = len(getattr(value, "redundant_event_indices", ()))
        return _json_value(payload)
    if isinstance(evaluation, Mapping):
        return _json_value(evaluation)
    raise TypeError("evaluation must be an EvaluationResult, EvaluationMetrics, mapping, or None")


def build_session_report(
    session_id: str,
    *,
    receiver: object = None,
    detector: object = None,
    events: Iterable[Mapping[str, Any]] = (),
    evaluation: object = None,
    configuration: object = None,
    status: Optional[str] = None,
    termination_reason: Optional[str] = None,
    performance: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compose the stable session report from receiver, detector and evaluator layers.

    Each layer remains usable on its own.  The composer accepts either their
    native objects or report mappings, which makes live and replay orchestration
    use the same schema and permits a partial report when a session stops early.
    """
    receiver_report = dict(_source_mapping(receiver))
    detector_report = dict(_source_mapping(detector))
    event_list = list(events)
    if not event_list:
        candidate = detector_report.get("events", ())
        if isinstance(candidate, Iterable) and not isinstance(candidate, (str, bytes, Mapping)):
            event_list = list(candidate)

    intervals = _collect_incomplete_intervals(receiver_report, detector_report, evaluation)
    durations = [int(item["duration_us"]) for item in intervals
                 if item.get("duration_us") is not None]
    config_payload = configuration
    if hasattr(configuration, "manifest") and callable(configuration.manifest):
        config_payload = configuration.manifest()
    elif configuration is not None and not isinstance(configuration, Mapping):
        config_payload = _json_value(configuration)

    receive_counters = {
        "bytes_received": receiver_report.get("bytes_received"),
        "decoder": receiver_report.get("decoder", {}),
        "sequence": receiver_report.get("sequence", {}),
        "frames": receiver_report.get("frames", 0),
        "frames_per_channel": receiver_report.get("frames_per_channel", {}),
        "unknown_record_types": receiver_report.get("unknown_record_types", {}),
    }
    # Preserve device/logger counters and all decoder counters without making
    # consumers know whether the capture was live or replayed.
    for name in ("device_counters", "logger_stats", "acks", "session_restarts"):
        if name in receiver_report:
            receive_counters[name] = receiver_report[name]

    detector_performance = detector_report.get("performance")
    if performance is None:
        performance_payload = {
            "receiver": receiver_report.get("performance"),
            "detector": detector_performance,
        }
    else:
        performance_payload = _json_value(performance)

    final_status = status or receiver_report.get("status") or "completed"
    final_reason = (termination_reason if termination_reason is not None
                    else receiver_report.get("termination_reason"))
    report = {
        "session_id": session_id,
        "receive_counters": _json_value(receive_counters),
        "incomplete_intervals": _json_value(intervals),
        "incomplete_interval_count": len(intervals),
        "incomplete_interval_duration_us": sum(durations),
        "events": _event_counts(event_list),
        "evaluation": _evaluation_payload(evaluation),
        "performance": _json_value(performance_payload),
        "configuration": _json_value(config_payload),
        "status": final_status,
        "termination": {
            "status": final_status,
            "reason": final_reason,
        },
    }
    # Retain diagnostic details from the producer reports for troubleshooting,
    # without duplicating their primary counters in the top-level schema.
    if receiver_report.get("trace_write_error") is not None:
        report["trace_write_error"] = _json_value(receiver_report["trace_write_error"])
    if detector_report:
        report["detector"] = _json_value(detector_report)
    return report


def write_session_report(
    path: Path | str,
    session_id: str,
    **kwargs: Any,
) -> Path:
    """Write one deterministic, indented JSON session summary."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = build_session_report(session_id, **kwargs)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


# Names used by orchestration code in earlier development stages.
compose_session_report = build_session_report
write_report = write_session_report

__all__ = [
    "EventReporter", "normalize_event", "sort_events", "write_events",
    "build_session_report", "compose_session_report", "write_session_report",
    "write_report", "write_session_snapshot",
]
