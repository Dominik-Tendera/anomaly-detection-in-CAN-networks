"""Timing rules comparing feature windows with the baseline profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Optional

from .features import FeatureWindow, IdentifierFeatures

PERIOD_VIOLATION_EVENT = "period_violation"
SHORT_BURST_EVENT = "short_burst"
STATISTICAL_DEVIATION_EVENT = "statistical_deviation"
_DISABLED_REPORT_KEY = "disabled_statistical_rules"


def _statistic(entry: Mapping[str, Any], feature: str) -> Optional[Mapping[str, Any]]:
    """Return the baseline statistics for a feature, if present."""
    value = entry.get(feature)
    if isinstance(value, Mapping):
        return value
    statistics = entry.get("statistics")
    if isinstance(statistics, Mapping):
        value = statistics.get(feature)
        if isinstance(value, Mapping):
            return value
    return None


def _number(statistics: Mapping[str, Any], name: str) -> Optional[float]:
    value = statistics.get(name)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _record_disabled_case(
    report_state: Optional[MutableMapping[str, Any]],
    *, channel: int, can_id: int, feature: str, standard_deviation: float,
) -> None:
    """Record a disabled zero-spread rule once in the caller's report state."""
    if report_state is None:
        return
    cases = report_state.setdefault(_DISABLED_REPORT_KEY, [])
    case = {
        "channel": int(channel),
        "can_id": int(can_id),
        "feature": feature,
        "reason": "zero_baseline_standard_deviation",
        "standard_deviation": float(standard_deviation),
    }
    if case not in cases:
        cases.append(case)


def _statistical_event(
    window: FeatureWindow, item: IdentifierFeatures, feature: str,
    measured: float, mean_value: float, standard_deviation: float,
    sigma_multiplier: float,
) -> dict[str, object]:
    spread = sigma_multiplier * standard_deviation
    return {
        "type": STATISTICAL_DEVIATION_EVENT,
        "method": "statistical",
        "ts64": int(window.end_us),
        "channel": int(item.channel),
        "can_id": int(item.can_id),
        "frame_id": int(item.can_id),
        "feature": feature,
        "window_start_us": int(window.start_us),
        "window_end_us": int(window.end_us),
        "measured": float(measured),
        "mean": float(mean_value),
        "standard_deviation": float(standard_deviation),
        "sigma_multiplier": float(sigma_multiplier),
        "expected_min": float(mean_value - spread),
        "expected_max": float(mean_value + spread),
    }


@dataclass
class StatisticalRulesDetector:
    """Detect feature values outside a configurable k-sigma interval.

    The profile contains the reference mean and standard deviation under the
    feature name. A zero reference spread disables that feature deliberately:
    treating the interval as a point would turn normal floating-point or
    measurement variation into an alarm, so the case is retained in the
    supplied report state instead.
    """

    profile: Mapping[Any, Any]
    sigma_multiplier: float = 3.0
    report_state: Optional[MutableMapping[str, Any]] = None
    events: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sigma_multiplier <= 0:
            raise ValueError("sigma_multiplier must be greater than zero")

    def process_window(self, window: FeatureWindow) -> list[dict[str, object]]:
        generated: list[dict[str, object]] = []
        for (channel, can_id), item in window.features.items():
            entry = _profile_entry(self.profile, int(channel), int(can_id))
            if entry is None:
                continue
            for feature, values in self._observations(item):
                statistics = _statistic(entry, feature)
                if statistics is None:
                    continue
                mean_value = _number(statistics, "mean")
                standard_deviation = _number(statistics, "std")
                if mean_value is None or standard_deviation is None:
                    continue
                if standard_deviation == 0.0:
                    _record_disabled_case(
                        self.report_state, channel=item.channel,
                        can_id=item.can_id, feature=feature,
                        standard_deviation=standard_deviation)
                    continue
                lower = mean_value - self.sigma_multiplier * standard_deviation
                upper = mean_value + self.sigma_multiplier * standard_deviation
                for value in values:
                    if value < lower or value > upper:
                        generated.append(_statistical_event(
                            window, item, feature, value, mean_value,
                            standard_deviation, self.sigma_multiplier))
        self.events.extend(generated)
        return generated

    @staticmethod
    def _observations(item: IdentifierFeatures) -> Iterator[tuple[str, tuple[float, ...]]]:
        if item.inter_frame_intervals_us:
            yield "inter_frame_interval_us", tuple(
                float(value) for value in item.inter_frame_intervals_us)
        count = item.statistics.get("frame_count")
        if count is not None and count.mean is not None:
            yield "window_counts", (float(item.frame_count),)

    def process_all(self, windows: Iterable[FeatureWindow]) -> Iterator[dict[str, object]]:
        for window in windows:
            yield from self.process_window(window)


def detect_statistical_deviations(
    windows: Iterable[FeatureWindow], profile: Mapping[Any, Any],
    *, sigma_multiplier: float = 3.0,
    report_state: Optional[MutableMapping[str, Any]] = None,
) -> list[dict[str, object]]:
    """Return k-sigma events and record disabled zero-spread cases."""
    return list(StatisticalRulesDetector(
        profile, sigma_multiplier, report_state).process_all(windows))


# Requirement-oriented aliases.
detect_sigma_deviations = detect_statistical_deviations
SigmaRulesDetector = StatisticalRulesDetector
FREQUENCY_INCREASE_EVENT = "frequency_increase"
FREQUENCY_DECREASE_EVENT = "frequency_decrease"


def _profile_entry(profile: Mapping[Any, Any], channel: int, can_id: int) -> Optional[Mapping[str, Any]]:
    """Return a profile entry, accepting integer or JSON string CAN-ID keys."""
    entry = profile.get((channel, can_id))
    if entry is None:
        entry = profile.get(can_id)
    if entry is None:
        entry = profile.get(str(can_id))
    return entry if isinstance(entry, Mapping) else None


def _tolerance(entry: Mapping[str, Any]) -> Optional[tuple[float, float]]:
    """Read an observed tolerance and ignore IDs without a period."""
    if entry.get("period_status") == "no_period":
        return None
    value = entry.get("tolerance_us")
    if not isinstance(value, Mapping):
        return None
    try:
        lower = float(value["min"])
        upper = float(value["max"])
    except (KeyError, TypeError, ValueError):
        return None
    if lower > upper:
        raise ValueError("profile tolerance_us.min must not exceed max")
    return lower, upper


def _frequency_thresholds(entry: Mapping[str, Any]) -> Optional[tuple[float, float]]:
    """Return lower and upper per-window count thresholds from a profile entry.

    ``window_counts`` is the canonical baseline representation.  The explicit
    ``frequency_thresholds`` form is also accepted so a hand-edited profile can
    separate decision thresholds from descriptive baseline statistics.
    """
    value = entry.get("frequency_thresholds", entry.get("window_counts"))
    if not isinstance(value, Mapping):
        return None
    try:
        lower = float(value["min"])
        upper = float(value["max"])
    except (KeyError, TypeError, ValueError):
        return None
    if lower > upper:
        raise ValueError("profile frequency thresholds min must not exceed max")
    return lower, upper


def _frequency_event(window: FeatureWindow, item: IdentifierFeatures,
                     event_type: str, lower: float, upper: float) -> dict[str, object]:
    """Build one count-based event for the complete analysis window."""
    return {
        "type": event_type,
        "method": "timing",
        "ts64": int(window.end_us),
        "channel": int(item.channel),
        "can_id": int(item.can_id),
        "frame_id": int(item.can_id),
        "window_start_us": int(window.start_us),
        "window_end_us": int(window.end_us),
        "measured_frame_count": int(item.frame_count),
        "expected_min_frame_count": lower,
        "expected_max_frame_count": upper,
    }


def detect_frequency_events(window: FeatureWindow,
                            profile: Mapping[Any, Any]) -> list[dict[str, object]]:
    """Detect per-ID count increases and decreases in one analysis window."""
    generated: list[dict[str, object]] = []
    for (channel, can_id), item in window.features.items():
        entry = _profile_entry(profile, int(channel), int(can_id))
        if entry is None:
            continue
        thresholds = _frequency_thresholds(entry)
        if thresholds is None:
            continue
        lower, upper = thresholds
        if item.frame_count > upper:
            generated.append(_frequency_event(
                window, item, FREQUENCY_INCREASE_EVENT, lower, upper))
        elif item.frame_count < lower:
            generated.append(_frequency_event(
                window, item, FREQUENCY_DECREASE_EVENT, lower, upper))
    return generated


@dataclass
class FrequencyRulesDetector:
    """Detect count deviations from per-ID baseline window thresholds."""

    profile: Mapping[Any, Any]
    events: list[dict[str, object]] = field(default_factory=list)

    def process_window(self, window: FeatureWindow) -> list[dict[str, object]]:
        generated = detect_frequency_events(window, self.profile)
        self.events.extend(generated)
        return generated

    def process_all(self, windows: Iterable[FeatureWindow]) -> Iterator[dict[str, object]]:
        for window in windows:
            yield from self.process_window(window)


def detect_frequency_deviations(
    windows: Iterable[FeatureWindow], profile: Mapping[Any, Any]
) -> list[dict[str, object]]:
    """Return frequency increase/decrease events in device-window order."""
    return list(FrequencyRulesDetector(profile).process_all(windows))


# Names matching the requirement terminology and convenient caller aliases.
detect_frequency_changes = detect_frequency_deviations
def _event(window: FeatureWindow, item: IdentifierFeatures, index: int,
           lower: float, upper: float) -> dict[str, object]:
    measured = float(item.inter_frame_intervals_us[index])
    timestamps = item.interval_end_timestamps_us
    timestamp = timestamps[index] if index < len(timestamps) else window.end_us
    return {
        "type": PERIOD_VIOLATION_EVENT,
        "method": "timing",
        "ts64": int(timestamp),
        "channel": int(item.channel),
        "can_id": int(item.can_id),
        "frame_id": int(item.can_id),
        "window_start_us": int(window.start_us),
        "window_end_us": int(window.end_us),
        "measured_interval_us": measured,
        "expected_min_us": lower,
        "expected_max_us": upper,
    }


@dataclass
class TimingRulesDetector:
    """Detect inter-frame periods outside the baseline tolerance."""

    profile: Mapping[Any, Any]
    events: list[dict[str, object]] = field(default_factory=list)

    def process_window(self, window: FeatureWindow) -> list[dict[str, object]]:
        generated: list[dict[str, object]] = []
        for (channel, can_id), item in window.features.items():
            entry = _profile_entry(self.profile, int(channel), int(can_id))
            if entry is None:
                continue
            tolerance = _tolerance(entry)
            if tolerance is None:
                continue
            lower, upper = tolerance
            for index, measured in enumerate(item.inter_frame_intervals_us):
                if measured < lower or measured > upper:
                    generated.append(_event(window, item, index, lower, upper))
        self.events.extend(generated)
        return generated

    def process_all(self, windows: Iterable[FeatureWindow]) -> Iterator[dict[str, object]]:
        for window in windows:
            yield from self.process_window(window)


def detect_period_violations(
    windows: Iterable[FeatureWindow], profile: Mapping[Any, Any]
) -> list[dict[str, object]]:
    """Return period-violation events for feature windows in device-time order."""
    return list(TimingRulesDetector(profile).process_all(windows))


# Natural aliases for callers using the requirement's wording.
detect_period_violations_from_windows = detect_period_violations
PeriodRulesDetector = TimingRulesDetector

def detect_short_bursts(
    windows: Iterable[FeatureWindow], profile: Mapping[Any, Any], *,
    interval_factor: float, min_frames: int,
) -> list[dict[str, object]]:
    """Detect one event for each run of sufficiently short intervals.

    The configured frame threshold is applied to consecutive intervals, as
    required by the timing rule.  An interval is short only when it is strictly
    below ``expected_period_us * interval_factor``.
    """
    factor = float(interval_factor)
    required = int(min_frames)
    if not 0 < factor < 1:
        raise ValueError("interval_factor must be greater than zero and less than one")
    if required < 1:
        raise ValueError("min_frames must be greater than zero")

    generated: list[dict[str, object]] = []
    for window in windows:
        for (channel, can_id), item in window.features.items():
            entry = _profile_entry(profile, int(channel), int(can_id))
            if entry is None or entry.get("period_status") == "no_period":
                continue
            try:
                expected = float(entry["period_us"])
            except (KeyError, TypeError, ValueError):
                continue
            if expected <= 0:
                continue
            threshold = expected * factor
            index = 0
            while index < len(item.inter_frame_intervals_us):
                if item.inter_frame_intervals_us[index] >= threshold:
                    index += 1
                    continue
                start = index
                while (index + 1 < len(item.inter_frame_intervals_us)
                       and item.inter_frame_intervals_us[index + 1] < threshold):
                    index += 1
                end = index
                if end - start + 1 >= required:
                    timestamps = item.interval_end_timestamps_us
                    intervals = item.inter_frame_intervals_us[start:end + 1]
                    generated.append({
                        "type": SHORT_BURST_EVENT,
                        "method": "timing",
                        "ts64": int(timestamps[end]),
                        "channel": int(item.channel),
                        "can_id": int(item.can_id),
                        "frame_id": int(item.can_id),
                        "window_start_us": int(window.start_us),
                        "window_end_us": int(window.end_us),
                        "burst_start_ts64": int(timestamps[start]),
                        "burst_end_ts64": int(timestamps[end]),
                        "interval_count": len(intervals),
                        "intervals_us": [float(value) for value in intervals],
                        "expected_period_us": expected,
                        "interval_factor": factor,
                        "short_interval_threshold_us": threshold,
                    })
                index += 1
    return generated


# Requirement-oriented aliases.
detect_short_burst = detect_short_bursts

__all__ = [
    "FREQUENCY_DECREASE_EVENT", "FREQUENCY_INCREASE_EVENT",
    "PERIOD_VIOLATION_EVENT", "SHORT_BURST_EVENT", "STATISTICAL_DEVIATION_EVENT",
    "FrequencyRulesDetector", "PeriodRulesDetector", "TimingRulesDetector",
    "StatisticalRulesDetector", "SigmaRulesDetector", "detect_frequency_changes",
    "detect_frequency_deviations", "detect_period_violations",
    "detect_period_violations_from_windows", "detect_short_burst",
    "detect_short_bursts", "detect_statistical_deviations",
    "detect_sigma_deviations",
]
