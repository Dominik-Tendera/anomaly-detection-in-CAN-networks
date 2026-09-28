from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.report import normalize_event, write_events  # noqa: E402


class EventReportTests(unittest.TestCase):
    def test_writes_complete_frame_event_and_absolute_time_as_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_events(
                Path(directory) / "session.events.jsonl",
                [{
                    "type": "period_violation",
                    "method": "timing",
                    "ts64": 1_250_000,
                    "channel": 1,
                    "can_id": 0x100,
                    "frame_seq": 42,
                    "measured_interval_us": 250_000.0,
                    "expected_min_us": 90_000.0,
                    "expected_max_us": 110_000.0,
                }],
                session_id="20260919_120000",
                absolute_time=lambda ts: datetime(
                    2026, 9, 19, 12, 0, 0, 250000, tzinfo=timezone.utc),
            )
            event = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(event["session_id"], "20260919_120000")
        self.assertEqual(event["device_time_us"], 1_250_000)
        self.assertEqual(event["ts64"], 1_250_000)
        self.assertEqual(event["absolute_time"], "2026-09-19T12:00:00.250000+00:00")
        self.assertEqual(event["channel"], 1)
        self.assertEqual(event["can_id"], 0x100)
        self.assertEqual(event["frame_seq"], 42)
        self.assertEqual(event["rule_type"], "period_violation")
        self.assertEqual(event["measured_value"], 250_000.0)
        self.assertEqual(event["measured_unit"], "us")
        self.assertEqual(event["expected_bounds"], {"min": 90_000.0, "max": 110_000.0})
        self.assertEqual(event["expected_unit"], "us")

    def test_sorts_by_device_time_and_uses_window_boundaries_instead_of_sequence(self) -> None:
        events = [
            {"type": "frequency_increase", "method": "timing", "ts64": 2_000_000,
             "channel": 1, "can_id": 0x100, "frame_seq": 99,
             "window_start_us": 1_000_000, "window_end_us": 2_000_000,
             "measured_frame_count": 12, "expected_max_frame_count": 10},
            {"type": "unknown_identifier", "method": "protocol", "ts64": 500_000,
             "channel": 1, "can_id": 0x555, "frame_seq": 7},
            {"type": "period_violation", "method": "timing", "ts64": 1_500_000,
             "channel": 1, "can_id": 0x100, "frame_seq": 8,
             "measured_interval_us": 300_000, "expected_min_us": 90_000,
             "expected_max_us": 110_000},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = write_events(Path(directory) / "events.jsonl", events)
            written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([event["device_time_us"] for event in written],
                         [500_000, 1_500_000, 2_000_000])
        window_event = written[2]
        self.assertEqual(window_event["window_start_us"], 1_000_000)
        self.assertEqual(window_event["window_end_us"], 2_000_000)
        self.assertNotIn("frame_seq", window_event)
        self.assertEqual(window_event["measured_value"], 12)
        self.assertEqual(window_event["measured_unit"], "frames")

    def test_preserves_unavailable_absolute_time_and_rejects_incomplete_window(self) -> None:
        event = normalize_event({
            "type": "device_frame_loss", "method": "protocol_layer", "ts64": None,
            "channel": 1, "frame_id": None,
        })
        self.assertIsNone(event["device_time_us"])
        self.assertIsNone(event["absolute_time"])
        self.assertIsNone(event["can_id"])
        with self.assertRaisesRegex(ValueError, "both window boundaries"):
            normalize_event({"type": "frequency_increase", "window_start_us": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SessionReportTests(unittest.TestCase):
    def test_snapshot_preserves_fields_without_building_a_summary(self) -> None:
        from can_detect.report import write_session_snapshot

        snapshot = {'session_id': 'partial', 'status': 'interrupted',
                    'termination_reason': 'user_interrupt', 'frames': 7}
        with tempfile.TemporaryDirectory() as directory:
            path = write_session_snapshot(Path(directory) / 'report.json', snapshot)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), snapshot)
            with self.assertRaises(TypeError):
                write_session_snapshot(path, {'invalid': object()})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), snapshot)
            self.assertFalse(path.with_name(path.name + '.tmp').exists())

    def test_composes_layer_reports_and_evaluation_metrics(self) -> None:
        from can_detect.report import build_session_report
        from can_detect.evaluate import EvaluationMetrics, MetricSummary

        evaluation = EvaluationMetrics((MetricSummary(
            "frequency_increase", "timing", 1, 0, 0, 1.0, 1.0, 1.0,
            (250.0,), 250.0, 2),), (4,))
        report = build_session_report(
            "session-1",
            receiver={
                "bytes_received": 100,
                "decoder": {"records_ok": 3},
                "sequence": {"records_missing_on_link": 1},
                "status": "completed",
                "performance": {"records_processed": 3},
            },
            detector={"performance": {"records_processed": 2}},
            events=[
                {"type": "frequency_increase", "method": "timing"},
                {"type": "frequency_increase", "method": "timing"},
                {"type": "unknown_identifier", "method": "protocol"},
            ],
            evaluation=evaluation,
            configuration={"values": {"window_s": 1.0}},
        )

        self.assertEqual(report["session_id"], "session-1")
        self.assertEqual(report["receive_counters"]["decoder"]["records_ok"], 3)
        self.assertEqual(report["events"], {
            "total": 3,
            "by_type": {"frequency_increase": 2, "unknown_identifier": 1},
            "by_method": {"protocol": 1, "timing": 2},
        })
        self.assertEqual(report["evaluation"]["summaries"][0]["f1_score"], 1.0)
        self.assertEqual(report["performance"]["detector"]["records_processed"], 2)
        self.assertEqual(report["status"], "completed")

    def test_deduplicates_intervals_and_preserves_interruption_reason(self) -> None:
        from can_detect.report import write_session_report

        with tempfile.TemporaryDirectory() as directory:
            path = write_session_report(
                Path(directory) / "session.report.json", "session-2",
                receiver={
                    "status": "interrupted",
                    "termination_reason": "user_interrupt",
                    "incomplete_intervals": [{
                        "channel": 1, "start_ts64": 10, "end_ts64": 30,
                    }],
                },
                detector={"trace": {"completeness": {
                    "incomplete_intervals": [{
                        "channel": 1, "start_ts64": 10, "end_ts64": 30,
                    }],
                }}},
            )
            report = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(report["incomplete_interval_count"], 1)
        self.assertEqual(report["incomplete_interval_duration_us"], 20)
        self.assertEqual(report["termination"], {
            "status": "interrupted", "reason": "user_interrupt",
        })
        self.assertEqual(report["status"], "interrupted")
