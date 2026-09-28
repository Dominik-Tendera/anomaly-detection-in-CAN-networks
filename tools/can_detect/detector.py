"""Single-pass anomaly-detection pipeline for live and replay records.

The detector deliberately accepts an iterator of enriched :class:`TraceRecord`
objects.  A live receiver and a trace reader therefore use the same code path;
the iterator is the only source of input.  In particular, no host wall clock
is consulted.  All timing decisions are delegated to components that consume
the reconstructed device timestamp on each record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Mapping, Optional

if TYPE_CHECKING:
    from .config import SessionConfig

from . import dbc, features, performance, rules_protocol, rules_protocol_layer, rules_timing, trace


@dataclass
class AnomalyDetector:
    """Incremental detector shared by live and replay modes.

    ``process_all`` consumes records exactly once and yields events in
    deterministic device-record order.  The detector keeps only the bounded
    state held by the feature extractor and rule components; it does not
    retain the input trace.

    ``database`` may be a loaded :class:`DbcDatabase` or a mapping used by
    protocol tests and hand-built profiles.  ``profile`` is the unwrapped
    mapping returned by :func:`baseline.load_profile`.
    """

    database: dbc.DbcDatabase | Mapping[int, object]
    profile: Mapping[int, object]
    window_us: int = 1_000_000
    missing_frame_multiplier: float = 2.0
    sigma_multiplier: float = 3.0
    burst_interval_factor: Optional[float] = None
    burst_min_frames: Optional[int] = None
    report: dict[str, Any] = field(default_factory=dict)
    random_seed: Optional[int] = None
    performance: Optional[performance.PerformanceMetrics] = None

    def __post_init__(self) -> None:
        if self.random_seed is not None:
            if isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int):
                raise ValueError("random_seed must be an integer")
            if not 0 <= self.random_seed <= 2 ** 32 - 1:
                raise ValueError("random_seed must be between 0 and 2**32 - 1")
            # Keep the generator local to this detector.  It never uses global
            # process state, so future stochastic methods remain reproducible.
            self.random = random.Random(self.random_seed)
            self.report["random_seed"] = self.random_seed
        else:
            self.random = None
        if int(self.window_us) <= 0:
            raise ValueError("window_us must be greater than zero")
        self.window_us = int(self.window_us)
        self.protocol = rules_protocol.ProtocolRulesDetector(
            self.database, self.profile)
        self.protocol_layer = rules_protocol_layer.ProtocolLayerDetector()
        self.missing = rules_protocol.MissingFrameDetector(
            self.profile, self.missing_frame_multiplier)
        self.features = features.FeatureExtractor(self.window_us)
        self.timing = rules_timing.TimingRulesDetector(self.profile)
        self.frequency = rules_timing.FrequencyRulesDetector(self.profile)
        self.statistical = rules_timing.StatisticalRulesDetector(
            self.profile, self.sigma_multiplier, self.report)
        if self.burst_interval_factor is not None:
            if not 0 < float(self.burst_interval_factor) < 1:
                raise ValueError("burst_interval_factor must be greater than zero and less than one")
            if self.burst_min_frames is None or int(self.burst_min_frames) < 1:
                raise ValueError("burst_min_frames must be greater than zero when burst detection is enabled")
        elif self.burst_min_frames is not None:
            raise ValueError("burst_interval_factor is required when burst_min_frames is set")
        self.events: list[dict[str, object]] = []
        if self.performance is None:
            self.performance = performance.PerformanceMetrics()

    def _record_performance(self) -> None:
        self.report["performance"] = self.performance.snapshot()

    def _process_windows(self, windows: Iterable[features.FeatureWindow]
                         ) -> Iterator[dict[str, object]]:
        for window in windows:
            # Keep stage order stable.  Each stage receives the same immutable
            # feature window and none consults a machine clock.
            yield from self.timing.process_window(window)
            yield from self.frequency.process_window(window)
            yield from self.statistical.process_window(window)
            if self.burst_interval_factor is not None:
                yield from rules_timing.detect_short_bursts(
                    (window,), self.profile,
                    interval_factor=float(self.burst_interval_factor),
                    min_frames=int(self.burst_min_frames),
                )

    def process(self, record: trace.TraceRecord) -> list[dict[str, object]]:
        """Consume one record and return events generated by that record."""
        self.performance.start_record()
        try:
            generated: list[dict[str, object]] = []
            generated.extend(self.protocol.process(record))
            generated.extend(self.protocol_layer.process(record))
            generated.extend(self.missing.process(record))
            generated.extend(self._process_windows(self.features.update(record)))
            self.events.extend(generated)
            return generated
        finally:
            self.performance.finish_record()
            self._record_performance()

    def process_all(self, records: Iterable[trace.TraceRecord],
                    queue_backlog: Optional[Callable[[], int]] = None
                    ) -> Iterator[dict[str, object]]:
        """Consume a live or replay iterator and yield all anomaly events.

        ``queue_backlog`` is supplied by a live receiver when records are
        buffered between decoding and detection.  Replay callers omit it.
        """
        for record in records:
            if queue_backlog is not None:
                self.performance.observe_queue_backlog(queue_backlog())
            yield from self.process(record)
        # A final partial window is meaningful and must be processed identically
        # in live and replay modes when the input stream ends.
        yield from self._finish()
        self._record_performance()

    def _finish(self) -> Iterator[dict[str, object]]:
        generated = list(self._process_windows(self.features.finish()))
        self.events.extend(generated)
        yield from generated

    def detect(self, records: Iterable[trace.TraceRecord]) -> list[dict[str, object]]:
        """Materialize :meth:`process_all` for callers wanting a result list."""
        return list(self.process_all(records))

    def report_state(self) -> dict[str, Any]:
        """Return rule and performance diagnostics accumulated during the run."""
        self._record_performance()
        return self.report


def detect_anomalies(
    records: Iterable[trace.TraceRecord],
    database: dbc.DbcDatabase | Mapping[int, object],
    profile: Mapping[int, object],
    *,
    window_us: int = 1_000_000,
    missing_frame_multiplier: float = 2.0,
    sigma_multiplier: float = 3.0,
    burst_interval_factor: Optional[float] = None,
    burst_min_frames: Optional[int] = None,
    random_seed: Optional[int] = None,
    config: Optional["SessionConfig"] = None,
    report: Optional[dict[str, Any]] = None,
) -> list[dict[str, object]]:
    """Run the shared detector over any record iterator.

    ``config`` is the validated session configuration.  Supplying it wires the
    reproducibility-critical values into the detector, including its RNG seed;
    explicit arguments remain useful for small callers and tests.  An explicit
    value that conflicts with the configuration is rejected rather than
    silently producing a report for different settings.
    """
    if config is not None:
        configured = {
            "window_us": int(round(config.window_s * 1_000_000)),
            "missing_frame_multiplier": config.missing_frame_multiplier,
            "sigma_multiplier": config.sigma_multiplier,
            "burst_interval_factor": config.burst_interval_factor,
            "burst_min_frames": config.burst_min_frames,
        }
        explicit = {
            "window_us": window_us,
            "missing_frame_multiplier": missing_frame_multiplier,
            "sigma_multiplier": sigma_multiplier,
            "burst_interval_factor": burst_interval_factor,
            "burst_min_frames": burst_min_frames,
        }
        # These function defaults predate config integration, so only reject
        # values that callers actually changed from the defaults.
        defaults = {
            "window_us": 1_000_000,
            "missing_frame_multiplier": 2.0,
            "sigma_multiplier": 3.0,
            "burst_interval_factor": None,
            "burst_min_frames": None,
        }
        for name, value in explicit.items():
            if value != defaults[name] and value != configured[name]:
                raise ValueError(f"{name} conflicts with session configuration")
        window_us = configured["window_us"]
        missing_frame_multiplier = configured["missing_frame_multiplier"]
        sigma_multiplier = configured["sigma_multiplier"]
        burst_interval_factor = configured["burst_interval_factor"]
        burst_min_frames = configured["burst_min_frames"]
        if random_seed is not None and random_seed != config.random_seed:
            raise ValueError("random_seed conflicts with session configuration")
        random_seed = config.random_seed

    detector = AnomalyDetector(
        database, profile, window_us=window_us,
        missing_frame_multiplier=missing_frame_multiplier,
        sigma_multiplier=sigma_multiplier,
        burst_interval_factor=burst_interval_factor,
        burst_min_frames=burst_min_frames,
        random_seed=random_seed,
        report={} if report is None else report,
    )
    return detector.detect(records)



@dataclass(frozen=True)
class ReplayResult:
    """Deterministic replay output and its comparability verdict."""

    events: list[dict[str, object]]
    comparable: bool
    checksum_mismatches: tuple[str, ...]
    report: dict[str, Any]


def replay_trace(
    path: Path | str,
    database: dbc.DbcDatabase | Mapping[int, object],
    profile: Mapping[int, object],
    *,
    config=None,
    expected_checksums: Optional[Mapping[str, str]] = None,
    window_us: int = 1_000_000,
    missing_frame_multiplier: float = 2.0,
    sigma_multiplier: float = 3.0,
    burst_interval_factor: Optional[float] = None,
    burst_min_frames: Optional[int] = None,
) -> ReplayResult:
    """Run the detector on a saved trace and validate its input identity.

    Validation is performed before consuming records, but a mismatch does not
    discard useful diagnostic events.  Instead the result is explicitly marked
    non-comparable and lists every differing checksum.  ``config`` is normally
    a :class:`SessionConfig`; ``expected_checksums`` is provided for callers
    that already materialised a manifest.
    """
    path = Path(path)
    header = trace.read_trace_header(path)
    if config is not None:
        mismatches = trace.validate_trace_header(header, config)
    elif expected_checksums is not None:
        if header is None:
            mismatches = ["trace_header_missing"]
        else:
            mismatches = [
                f"{name}: trace={header.checksums.get(name)!r}, current={value!r}"
                for name, value in expected_checksums.items()
                if header.checksums.get(name) != value
            ]
            mismatches.extend(
                f"{name}: trace={value!r}, current=<absent>"
                for name, value in sorted(header.checksums.items())
                if name not in expected_checksums
            )
    else:
        mismatches = [] if header is not None else ["trace_header_missing"]

    reader = trace.TraceReader()
    report: dict[str, Any] = {
        "mode": "replay",
        "trace_path": str(path),
        "comparable": not mismatches,
        "checksum_mismatches": mismatches,
    }
    events = AnomalyDetector(
        database, profile, window_us=window_us,
        missing_frame_multiplier=missing_frame_multiplier,
        sigma_multiplier=sigma_multiplier,
        burst_interval_factor=burst_interval_factor,
        burst_min_frames=burst_min_frames,
        report=report,
    ).detect(trace.iter_trace_file(path, reader=reader))
    report["trace"] = trace.summary(reader)
    return ReplayResult(events, not mismatches, tuple(mismatches), report)


# Requirement-oriented and concise aliases for integration callers.
replay = replay_trace
detect = detect_anomalies
Detector = AnomalyDetector

__all__ = [
    "AnomalyDetector", "Detector", "ReplayResult", "detect", "detect_anomalies",
    "replay", "replay_trace",
]
