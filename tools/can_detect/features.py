"""Streaming time-window features for CAN frames.

The extractor consumes enriched :class:`can_detect.trace.TraceRecord` objects.
Only the device timestamp reconstructed by ``trace`` is used; wall-clock time
of the analysing machine is never consulted.  State is limited to the current
analysis window and one preceding timestamp per ``(channel, CAN ID)``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean, pvariance, pstdev
from typing import Any, Iterable, Iterator, Mapping, Optional

from . import trace


@dataclass(frozen=True)
class DescriptiveStatistics:
    """Population descriptive statistics for one feature in a window."""

    count: int
    mean: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    variance: Optional[float]
    standard_deviation: Optional[float]

    @classmethod
    def from_values(cls, values: Iterable[float]) -> "DescriptiveStatistics":
        numbers = [float(value) for value in values]
        if not numbers:
            return cls(0, None, None, None, None, None)
        return cls(
            count=len(numbers),
            mean=mean(numbers),
            minimum=min(numbers),
            maximum=max(numbers),
            variance=pvariance(numbers) if len(numbers) > 1 else 0.0,
            standard_deviation=pstdev(numbers) if len(numbers) > 1 else 0.0,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "variance": self.variance,
            "std": self.standard_deviation,
        }


@dataclass(frozen=True)
class IdentifierFeatures:
    """Features for one channel and CAN ID in one analysis window."""

    channel: int
    can_id: int
    frame_count: int
    inter_frame_intervals_us: tuple[float, ...]
    interval_end_timestamps_us: tuple[int, ...]
    jitter_us: Optional[float]
    statistics: Mapping[str, DescriptiveStatistics]

    @property
    def interval_statistics(self) -> DescriptiveStatistics:
        return self.statistics["inter_frame_interval_us"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "can_id": self.can_id,
            "frame_count": self.frame_count,
            "inter_frame_intervals_us": list(self.inter_frame_intervals_us),
            "interval_end_timestamps_us": list(self.interval_end_timestamps_us),
            "jitter_us": self.jitter_us,
            "statistics": {
                name: values.as_dict()
                for name, values in self.statistics.items()
            },
        }


@dataclass(frozen=True)
class FeatureWindow:
    """All per-ID features for a half-open device-time window."""

    start_us: int
    end_us: int
    features: Mapping[tuple[int, int], IdentifierFeatures]

    @property
    def window_start_us(self) -> int:
        return self.start_us

    @property
    def window_end_us(self) -> int:
        return self.end_us

    def for_id(self, can_id: int, channel: int = 1) -> Optional[IdentifierFeatures]:
        return self.features.get((int(channel), int(can_id)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start_us": self.start_us,
            "window_end_us": self.end_us,
            "features": {
                f"{channel}:{can_id}": value.as_dict()
                for (channel, can_id), value in sorted(self.features.items())
            },
        }


@dataclass
class _IdentifierState:
    frame_timestamps: list[int]
    intervals_us: list[float]
    interval_end_timestamps_us: list[int]
    previous_timestamp_us: Optional[int] = None


class FeatureExtractor:
    """Incrementally aggregate timed frames into fixed device-time windows.

    ``update`` returns every complete window crossed by the supplied record.
    ``finish`` emits the final non-empty window.  A new ``session_index`` starts
    a separate timeline and never creates an interval across the restart.
    """

    def __init__(self, window_us: int = 1_000_000) -> None:
        if window_us <= 0:
            raise ValueError("window_us must be greater than zero")
        self.window_us = int(window_us)
        self._window_start_us: Optional[int] = None
        self._window_session_index: Optional[int] = None
        self._states: dict[tuple[int, int], _IdentifierState] = {}
        self._last_session_index: Optional[int] = None

    @property
    def state(self) -> Mapping[tuple[int, int], _IdentifierState]:
        """Current bounded state, exposed for diagnostics and memory tests."""
        return self._states

    @property
    def active_id_count(self) -> int:
        return len(self._states)

    @property
    def buffered_sample_count(self) -> int:
        return sum(len(item.frame_timestamps) for item in self._states.values())

    def update(self, record: trace.TraceRecord) -> list[FeatureWindow]:
        """Consume one record and return windows completed by its device time."""
        if record.frame is None or not record.timing_eligible:
            return []
        timestamp = int(record.ts64)  # guarded by timing_eligible
        session_index = int(record.session_index)
        completed: list[FeatureWindow] = []

        if (self._window_session_index is not None
                and session_index != self._window_session_index):
            final = self._emit_current()
            if final is not None:
                completed.append(final)
            self._reset()

        if self._window_start_us is None:
            self._window_start_us = timestamp
            self._window_session_index = session_index
        elif timestamp < self._window_start_us:
            # A device-time regression is a boundary even when an input source
            # did not expose a session record. Do not manufacture a negative
            # interval or mix two timelines.
            final = self._emit_current()
            if final is not None:
                completed.append(final)
            self._reset()
            self._window_start_us = timestamp
            self._window_session_index = session_index

        while timestamp >= self._window_start_us + self.window_us:
            empty_or_current = self._emit_current()
            if empty_or_current is not None:
                completed.append(empty_or_current)
            self._advance_window()

        key = (int(record.frame.channel), int(record.frame.can_id))
        item = self._states.setdefault(key, _IdentifierState([], [], []))
        if item.previous_timestamp_us is not None:
            interval = timestamp - item.previous_timestamp_us
            if interval >= 0:
                item.intervals_us.append(float(interval))
                item.interval_end_timestamps_us.append(timestamp)
        item.frame_timestamps.append(timestamp)
        item.previous_timestamp_us = timestamp
        self._last_session_index = session_index
        return completed

    def finish(self) -> list[FeatureWindow]:
        """Emit the final non-empty window and clear bounded working state."""
        item = self._emit_current()
        self._reset()
        return [item] if item is not None else []

    def _advance_window(self) -> None:
        assert self._window_start_us is not None
        self._window_start_us += self.window_us
        self._states = {
            key: _IdentifierState([], [], [], state.previous_timestamp_us)
            for key, state in self._states.items()
        }

    def _reset(self) -> None:
        self._window_start_us = None
        self._window_session_index = None
        self._states = {}

    def _emit_current(self) -> Optional[FeatureWindow]:
        if self._window_start_us is None:
            return None
        features: dict[tuple[int, int], IdentifierFeatures] = {}
        for key, state in self._states.items():
            intervals = tuple(state.intervals_us)
            jitter = (pstdev(intervals) if len(intervals) > 1
                      else (0.0 if intervals else None))
            count_stats = DescriptiveStatistics.from_values(
                [float(len(state.frame_timestamps))])
            interval_stats = DescriptiveStatistics.from_values(intervals)
            jitter_stats = DescriptiveStatistics.from_values(
                [jitter] if jitter is not None else [])
            features[key] = IdentifierFeatures(
                channel=key[0], can_id=key[1],
                frame_count=len(state.frame_timestamps),
                inter_frame_intervals_us=intervals,
                interval_end_timestamps_us=tuple(state.interval_end_timestamps_us),
                jitter_us=jitter,
                statistics={
                    "frame_count": count_stats,
                    "inter_frame_interval_us": interval_stats,
                    "jitter_us": jitter_stats,
                },
            )
        # Empty windows are not useful to downstream consumers and would make
        # a long idle gap needlessly expensive. Absence is represented by the
        # next observed window and by the retained per-ID baseline state.
        if not features:
            return None
        return FeatureWindow(
            start_us=self._window_start_us,
            end_us=self._window_start_us + self.window_us,
            features=features,
        )


def iter_feature_windows(
    records: Iterable[trace.TraceRecord], *, window_us: int = 1_000_000,
) -> Iterator[FeatureWindow]:
    """Yield window features from a live or replayed trace iterator."""
    extractor = FeatureExtractor(window_us)
    for record in records:
        yield from extractor.update(record)
    yield from extractor.finish()


def compute_features(
    records: Iterable[trace.TraceRecord], *, window_us: int = 1_000_000,
) -> list[FeatureWindow]:
    """Materialize :func:`iter_feature_windows` for batch/replay callers."""
    return list(iter_feature_windows(records, window_us=window_us))


__all__ = [
    "DescriptiveStatistics", "FeatureExtractor", "FeatureWindow",
    "IdentifierFeatures", "compute_features", "iter_feature_windows",
]
