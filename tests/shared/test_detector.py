from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import detector, performance, trace  # noqa: E402
from tests import synthetic  # noqa: E402


class SharedDetectorTests(unittest.TestCase):
    PROFILE = {
        0x100: {
            "period_status": "observed",
            "period_us": 100_000.0,
            "tolerance_us": {"min": 90_000.0, "max": 110_000.0},
            "window_counts": {"min": 3, "max": 3},
            "inter_frame_interval_us": {"mean": 100_000.0, "std": 10_000.0},
        },
    }

    @staticmethod
    def _records() -> list[trace.TraceRecord]:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        builder.frame(0x100, b"\x00", 1_100_000)
        builder.frame(0x100, b"\x00", 1_200_000)
        builder.frame(0x100, b"\x00", 1_350_000)
        return list(trace.iter_trace_bytes(builder.bytes()))

    def _run(self, records: list[trace.TraceRecord]) -> list[dict[str, object]]:
        return detector.detect_anomalies(
            iter(records), {}, self.PROFILE,
            window_us=1_000_000,
            missing_frame_multiplier=2.0,
            sigma_multiplier=3.0,
        )

    def test_shared_pipeline_composes_protocol_and_timing_rules(self) -> None:
        events = self._run(self._records())
        self.assertEqual(
            [event["type"] for event in events],
            ["period_violation", "frequency_increase", "statistical_deviation"],
        )
        self.assertEqual(events[0]["ts64"], 1_350_000)
        self.assertEqual(events[1]["window_start_us"], 1_000_000)
        self.assertEqual(events[2]["measured"], 150_000.0)

    def test_same_trace_and_configuration_reproduce_all_event_fields_and_order(self) -> None:
        """A rerun is equal as a complete ordered event stream, not only by type."""
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        builder.frame(0x100, b"\x00", 1_100_000)
        builder.frame(0x100, b"\x00", 1_200_000)
        builder.frame(0x100, b"\x00", 1_350_000)
        raw_trace = builder.bytes()

        first = self._run(trace.iter_trace_bytes(raw_trace))
        second = self._run(trace.iter_trace_bytes(raw_trace))

        self.assertEqual(first, second)
        self.assertEqual(
            [(event["type"], event.get("ts64"), event.get("measured"),
              event.get("window_start_us")) for event in first],
            [("period_violation", 1_350_000, None, 1_000_000),
             ("frequency_increase", 2_000_000, None, 1_000_000),
             ("statistical_deviation", 2_000_000, 150_000.0, 1_000_000)],
        )

    def test_pipeline_does_not_depend_on_host_time(self) -> None:
        """Device timestamps, not iterator-consumption speed, define decisions."""
        records = self._records()
        self.assertEqual(self._run(iter(records)), self._run(iter(records)))

    def test_seed_is_recorded_in_the_session_report(self) -> None:
        report: dict[str, object] = {}
        instance = detector.AnomalyDetector(
            {}, self.PROFILE, random_seed=20260919, report=report)

        self.assertEqual(report["random_seed"], 20260919)
        self.assertEqual(instance.random.random(),
                         detector.AnomalyDetector(
                             {}, self.PROFILE, random_seed=20260919).random.random())

    def test_seed_must_be_an_unsigned_32_bit_integer(self) -> None:
        for seed in (-1, 2 ** 32, 1.5, True):
            with self.subTest(seed=seed):
                with self.assertRaises(ValueError):
                    detector.AnomalyDetector(
                        {}, self.PROFILE, random_seed=seed)  # type: ignore[arg-type]

    def test_performance_metrics_record_time_memory_and_queue_peak(self) -> None:
        ticks = iter([100, 1_100, 2_100, 5_100])
        memory = iter([10, 30])
        metrics = performance.PerformanceMetrics(
            clock_ns=lambda: next(ticks), memory_bytes=lambda: next(memory))
        metrics.observe_queue_backlog(2)
        metrics.start_record()
        metrics.finish_record()
        metrics.observe_queue_backlog(7)
        metrics.start_record()
        metrics.finish_record()

        self.assertEqual(metrics.snapshot(), {
            "records_processed": 2,
            "processing_time_per_record_ns": 2_000.0,
            "processing_time_per_record_us": 2.0,
            "processing_time_total_ns": 4_000,
            "processing_time_max_ns": 3_000,
            "peak_resident_memory_bytes": 30,
            "max_input_queue_backlog": 7,
        })

    def test_detector_persists_performance_in_session_report(self) -> None:
        report: dict[str, object] = {}
        instance = detector.AnomalyDetector({}, self.PROFILE, report=report)
        import itertools
        instance.performance.clock_ns = itertools.count(0, 10).__next__
        instance.performance.memory_bytes = lambda: 4096
        list(instance.process_all(iter(self._records()), queue_backlog=lambda: 4))
        metrics = instance.report_state()["performance"]

        self.assertEqual(metrics["records_processed"], 5)
        self.assertEqual(metrics["max_input_queue_backlog"], 4)
        self.assertEqual(metrics["peak_resident_memory_bytes"], 4096)
        self.assertEqual(metrics["processing_time_per_record_ns"], 10.0)

        builder = synthetic.TraceBuilder()
        builder.time_sync(2_000_000)
        builder.frame(0x100, b"\x00", 2_000_000)
        builder.frame(0x100, b"\x00", 2_250_000)
        events = detector.detect_anomalies(
            trace.iter_trace_bytes(builder.bytes()), {}, self.PROFILE,
            window_us=1_000_000, missing_frame_multiplier=3.0)
        period_events = [event for event in events
                         if event["type"] == "period_violation"]
        self.assertEqual(len(period_events), 1)
        self.assertEqual(period_events[0]["measured_interval_us"], 250_000.0)

class ReplayModeTests(unittest.TestCase):
    def _trace_bytes(self) -> bytes:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        builder.frame(0x100, b"\x00", 1_100_000)
        return builder.bytes()

    def test_stream_iterator_and_saved_file_replay_have_identical_events(self) -> None:
        """The live byte iterator and file replay must share every event field."""
        from tempfile import TemporaryDirectory

        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        builder.frame(0x100, b"\x00", 1_100_000)
        builder.frame(0x100, b"\x00", 1_200_000)
        builder.frame(0x100, b"\x00", 1_350_000)
        raw_trace = builder.bytes()
        profile = SharedDetectorTests.PROFILE
        kwargs = {
            "window_us": 1_000_000,
            "missing_frame_multiplier": 2.0,
            "sigma_multiplier": 3.0,
        }
        live_events = detector.detect_anomalies(
            trace.iter_trace_bytes(raw_trace), {}, profile, **kwargs)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.canbin"
            trace.write_trace_file(
                path, raw_trace,
                trace.TraceHeader({"config_sha256": "cfg"}))
            replay_result = detector.replay_trace(
                path, {}, profile,
                expected_checksums={"config_sha256": "cfg"}, **kwargs)

        self.assertTrue(replay_result.comparable)
        self.assertEqual(replay_result.events, live_events)

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.canbin"
            header = trace.TraceHeader({"config_sha256": "cfg"})
            trace.write_trace_file(path, self._trace_bytes(), header)
            result = detector.replay_trace(
                path, {}, {}, expected_checksums={"config_sha256": "cfg"})
            self.assertTrue(result.comparable)
            self.assertEqual(result.checksum_mismatches, ())
            self.assertEqual(result.report["mode"], "replay")

    def test_replaying_same_saved_trace_twice_is_identical(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.canbin"
            trace.write_trace_file(
                path, self._trace_bytes(),
                trace.TraceHeader({"config_sha256": "cfg"}))
            first = detector.replay_trace(
                path, {}, {}, expected_checksums={"config_sha256": "cfg"})
            second = detector.replay_trace(
                path, {}, {}, expected_checksums={"config_sha256": "cfg"})
            self.assertEqual(first.events, second.events)
            self.assertEqual(first.report["trace"], second.report["trace"])

    def test_checksum_mismatch_marks_result_non_comparable_but_replays(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.canbin"
            trace.write_trace_file(
                path, self._trace_bytes(),
                trace.TraceHeader({"config_sha256": "recorded"}))
            result = detector.replay_trace(
                path, {}, {}, expected_checksums={"config_sha256": "current"})
            self.assertFalse(result.comparable)
            self.assertIn("config_sha256", result.checksum_mismatches[0])
            self.assertEqual(result.report["comparable"], False)

    def test_legacy_trace_without_header_is_non_comparable(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.canbin"
            path.write_bytes(self._trace_bytes())
            result = detector.replay_trace(path, {}, {})
            self.assertFalse(result.comparable)
            self.assertEqual(result.checksum_mismatches, ("trace_header_missing",))


if __name__ == "__main__":
    unittest.main()
