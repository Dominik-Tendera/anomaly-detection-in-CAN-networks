from __future__ import annotations

from types import SimpleNamespace
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.evaluate import (  # noqa: E402
    EVALUATION_CSV_COLUMNS,
    EvaluationError,
    EvaluationMetrics,
    MetricSummary,
    calculate_metrics,
    evaluate,
    export_evaluation_csv,
    load_ground_truth,
)
from can_generate.time_reference import (  # noqa: E402
    DEFAULT_MARKER_CAN_ID,
    MARKER_END,
    MARKER_START,
    encode_marker,
)


class EvaluationTests(unittest.TestCase):
    @staticmethod
    def record(kind: int, generator_us: int, device_us: int):
        return SimpleNamespace(
            frame=SimpleNamespace(
                can_id=DEFAULT_MARKER_CAN_ID,
                data=encode_marker(kind, generator_us),
            ),
            ts64=device_us,
        )

    def truth(self):
        return {
            "format": "can-anomaly-ground-truth-v1",
            "session_id": "session-1",
            "episodes": [
                {"start": 1.0, "end": 2.0, "type": "burst", "can_id": 0x100},
                {"start": 3.0, "end": 3.5, "type": "unknown_dbc_id", "can_id": None},
            ],
        }

    def records(self):
        # Generator run starts at 10 s; device clock is generator clock + 250 ms.
        return [
            self.record(MARKER_START, 10_000_000, 10_250_000),
            self.record(MARKER_END, 14_000_000, 14_250_000),
        ]

    def test_manual_case_ideal_detection_has_perfect_metrics(self):
        truth = {
            "session_id": "manual-ideal",
            "episodes": [{"start": 1.0, "end": 2.0,
                           "type": "burst", "can_id": 0x100}],
        }
        result = evaluate(
            [{"type": "burst", "method": "rules", "can_id": 0x100,
              "ts64": 11_750_000}],
            truth, self.records(), tolerance_s=0.0,
        )

        summary = calculate_metrics(result).by_type_and_method()[("burst", "rules")]
        self.assertEqual((summary.tp, summary.fp, summary.fn), (1, 0, 0))
        self.assertEqual((summary.precision, summary.recall, summary.f1),
                         (1.0, 1.0, 1.0))
        self.assertEqual(summary.latencies_us, (500_000.0,))

    def test_manual_case_no_detection_is_a_false_negative(self):
        truth = {
            "session_id": "manual-none",
            "episodes": [{"start": 1.0, "end": 2.0,
                           "type": "burst", "can_id": 0x100}],
        }
        result = evaluate(
            [{"type": "burst", "method": "rules", "can_id": 0x100,
              "ts64": 12_500_000}],
            truth, self.records(), tolerance_s=0.0,
        )

        summary = calculate_metrics(result).by_type_and_method()[("burst", "rules")]
        self.assertEqual((summary.tp, summary.fp, summary.fn), (0, 1, 1))
        self.assertEqual((summary.precision, summary.recall, summary.f1),
                         (0.0, 0.0, 0.0))

    def test_manual_case_alarm_outside_tolerance_is_not_a_detection(self):
        truth = {
            "session_id": "manual-tolerance",
            "episodes": [{"start": 1.0, "end": 2.0,
                           "type": "burst", "can_id": 0x100}],
        }
        result = evaluate(
            [{"type": "burst", "method": "rules", "can_id": 0x100,
              "ts64": 12_300_001}],
            truth, self.records(), tolerance_s=0.05,
        )

        self.assertEqual(result.matches[0].episode_index, None)
        summary = calculate_metrics(result).by_type_and_method()[("burst", "rules")]
        self.assertEqual((summary.tp, summary.fp, summary.fn), (0, 1, 1))

    def test_manual_case_multiple_alarms_for_episode_count_once(self):
        truth = {
            "session_id": "manual-multiple",
            "episodes": [{"start": 1.0, "end": 2.0,
                           "type": "burst", "can_id": 0x100}],
        }
        result = evaluate(
            [
                {"type": "burst", "method": "rules", "can_id": 0x100,
                 "ts64": 11_300_000},
                {"type": "burst", "method": "rules", "can_id": 0x100,
                 "ts64": 12_000_000},
            ],
            truth, self.records(), tolerance_s=0.0,
        )

        summary = calculate_metrics(result).by_type_and_method()[("burst", "rules")]
        self.assertEqual((summary.tp, summary.fp, summary.fn), (1, 0, 0))
        self.assertEqual(summary.redundant_events, 1)
        self.assertEqual(result.redundant_alarm_count, 1)

    def test_manual_case_alarm_in_incomplete_interval_is_excluded(self):
        truth = {
            "session_id": "manual-incomplete",
            "episodes": [{"start": 1.0, "end": 2.0,
                           "type": "burst", "can_id": 0x100}],
        }
        records = self.records()
        records.insert(1, SimpleNamespace(
            decoded={"interval": {
                "channel": 1,
                "start_ts64": 11_500_000,
                "end_ts64": 12_500_000,
                "duration_us": 1_000_000,
                "complete": False,
                "reasons": ["device_loss"],
            }}
        ))
        result = evaluate(
            [{"type": "burst", "method": "rules", "can_id": 0x100,
              "ts64": 11_750_000}],
            truth, records, tolerance_s=0.0,
        )

        self.assertEqual(result.matches, ())
        self.assertEqual(result.episodes, ())
        self.assertEqual(result.incomplete_interval_durations_us, (1_000_000,))
        self.assertEqual(calculate_metrics(result).summaries, ())

    def test_marker_alignment_converts_relative_truth_to_device_time(self):
        result = evaluate(
            [{"type": "alarm", "can_id": 0x100, "ts64": 11_750_000}],
            self.truth(), self.records(), tolerance_s=0.0,
        )

        self.assertEqual(result.matches[0].episode_index, 0)
        self.assertEqual(result.episodes[0].start_us, 11_250_000)
        self.assertEqual(result.episodes[0].end_us, 12_250_000)
        self.assertEqual(result.time_reference.offset_us, 250_000)

    def test_can_id_and_tolerance_are_both_required(self):
        result = evaluate(
            [
                {"can_id": 0x101, "ts64": 11_750_000},  # wrong ID
                {"can_id": 0x100, "ts64": 12_300_001},  # outside 50 ms
            ],
            self.truth(), self.records(), tolerance_s=0.05,
        )

        self.assertEqual(result.unmatched_event_indices, (0, 1))
        self.assertEqual(result.unmatched_episode_indices, (0, 1))

    def test_episode_without_can_id_matches_event_by_time_only(self):
        result = evaluate(
            [{"type": "alarm", "can_id": 0x555, "timestamp_us": 13_500_000}],
            self.truth(), self.records(), tolerance_s=0.0,
        )
        self.assertEqual(result.matches[0].episode_index, 1)

    def test_multiple_events_can_be_associated_with_one_episode(self):
        result = evaluate(
            [
                {"can_id": 0x100, "ts64": 11_300_000},
                {"can_id": 0x100, "ts64": 12_000_000},
            ],
            self.truth(), self.records(), tolerance_s=0.0,
        )
        self.assertEqual([match.episode_index for match in result.matches], [0, 0])
        self.assertEqual(result.unmatched_episode_indices, (1,))
        self.assertEqual(result.detected_episode_indices, (0,))
        self.assertEqual(result.detected_episode_count, 1)
        self.assertEqual(result.redundant_event_indices, (1,))
        self.assertEqual(result.redundant_alarm_count, 1)

    def test_one_alarm_per_episode_is_not_redundant(self):
        result = evaluate(
            [
                {"can_id": 0x100, "ts64": 11_300_000},
                {"can_id": 0x555, "ts64": 13_500_000},
            ],
            self.truth(), self.records(), tolerance_s=0.0,
        )
        self.assertEqual(result.detected_episode_indices, (0, 1))
        self.assertEqual(result.detected_episode_count, 2)
        self.assertEqual(result.redundant_event_indices, ())
        self.assertEqual(result.redundant_alarm_count, 0)

    def test_unmatched_alarm_is_not_counted_as_redundant(self):
        result = evaluate(
            [
                {"can_id": 0x100, "ts64": 11_300_000},
                {"can_id": 0x101, "ts64": 11_400_000},
            ],
            self.truth(), self.records(), tolerance_s=0.0,
        )
        self.assertEqual(result.detected_episode_count, 1)
        self.assertEqual(result.unmatched_event_indices, (1,))
        self.assertEqual(result.redundant_alarm_count, 0)

    def test_incomplete_interval_is_excluded_and_duration_is_reported(self):
        records = self.records()
        records.insert(1, SimpleNamespace(
            decoded={
                "interval": {
                    "channel": 1,
                    "start_ts64": 11_500_000,
                    "end_ts64": 12_500_000,
                    "duration_us": 1_000_000,
                    "complete": False,
                    "reasons": ["device_loss"],
                }
            }
        ))
        result = evaluate(
            [
                {"can_id": 0x100, "ts64": 11_750_000},
                {"can_id": 0x555, "ts64": 13_500_000},
            ],
            self.truth(), records, tolerance_s=0.0,
        )

        # The first episode overlaps the skipped interval, and its alarm is
        # excluded rather than counted as either a detection or a false alarm.
        self.assertEqual([episode.index for episode in result.episodes], [1])
        self.assertEqual(result.matches[0].event_index, 1)
        self.assertEqual(result.matches[0].episode_index, 1)
        self.assertEqual(result.unmatched_event_indices, ())
        self.assertEqual(result.unmatched_episode_indices, ())
        self.assertEqual(result.incomplete_interval_durations_us, (1_000_000,))
        self.assertEqual(result.incomplete_intervals[0]["reasons"], ["device_loss"])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truth.json"
            path.write_text('{"session_id":"s", "episodes":[]}', encoding="utf-8")
            self.assertEqual(load_ground_truth(path)["session_id"], "s")

    def test_missing_marker_is_an_evaluation_error(self):
        with self.assertRaises(Exception):
            evaluate([], self.truth(), self.records()[:1], tolerance_s=0.0)

    def test_invalid_tolerance_is_rejected(self):
        with self.assertRaises(EvaluationError):
            evaluate([], self.truth(), self.records(), tolerance_s=-1.0)

    def test_metrics_are_per_type_and_method_with_first_match_latency(self):
        result = evaluate(
            [
                # Deliberately reverse these two events: latency uses the
                # earliest event timestamp, not input-list order.
                {"type": "alarm", "method": "timing", "can_id": 0x100,
                 "ts64": 12_000_000},
                {"type": "alarm", "method": "timing", "can_id": 0x100,
                 "ts64": 11_300_000},
                {"type": "alarm", "method": "protocol", "can_id": 0x555,
                 "ts64": 13_500_000},
                {"type": "spurious", "method": "timing", "can_id": 0x101,
                 "ts64": 14_000_000},
            ],
            self.truth(), self.records(), tolerance_s=0.0,
        )

        metrics = calculate_metrics(result).by_type_and_method()
        burst = metrics[("burst", "timing")]
        self.assertEqual((burst.tp, burst.fp, burst.fn), (1, 0, 0))
        self.assertEqual((burst.precision, burst.recall, burst.f1), (1.0, 1.0, 1.0))
        self.assertEqual(burst.latencies_us, (50_000.0,))
        self.assertEqual(burst.mean_latency_us, 50_000.0)
        self.assertEqual(burst.redundant_events, 1)

        unknown = metrics[("unknown_dbc_id", "protocol")]
        self.assertEqual((unknown.tp, unknown.fp, unknown.fn), (1, 0, 0))
        self.assertEqual(unknown.latencies_us, (250_000.0,))

        # A false alarm is counted separately from redundant matched events.
        spurious = metrics[("spurious", "timing")]
        self.assertEqual((spurious.tp, spurious.fp, spurious.fn), (0, 1, 0))
        self.assertEqual((spurious.precision, spurious.recall, spurious.f1),
                         (0.0, 0.0, 0.0))
        self.assertEqual(calculate_metrics(result).redundant_event_indices, (0,))

    def test_metrics_count_an_undetected_episode_as_false_negative(self):
        result = evaluate(
            [{"type": "alarm", "method": "timing", "can_id": 0x100,
              "ts64": 11_500_000}],
            self.truth(), self.records(), tolerance_s=0.0,
        )
        summary = calculate_metrics(result).by_type_and_method()[(
            "unknown_dbc_id", "timing")]
        self.assertEqual((summary.tp, summary.fp, summary.fn), (0, 0, 1))
        self.assertEqual(summary.precision, 0.0)
        self.assertEqual(summary.recall, 0.0)
        self.assertEqual(summary.f1, 0.0)
        self.assertIsNone(summary.mean_latency_us)

    def test_csv_export_has_stable_header_order_and_values(self):
        row = {
            "session_id": "session-1",
            "method": "rules",
            "anomaly_type": "burst",
            "true_positives": 2,
            "false_positives": 1,
            "false_negatives": 0,
            "precision": 2 / 3,
            "recall": 1.0,
            "f1_score": 0.8,
            "mean_detection_latency_us": 125000,
            "ignored_internal_field": "not exported",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.csv"
            returned = export_evaluation_csv([row], path)
            self.assertEqual(returned, path)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "session_id,method,anomaly_type,true_positives,false_positives,false_negatives,precision,recall,f1_score,mean_detection_latency_us\n"
                "session-1,rules,burst,2,1,0,0.6666666666666666,1.0,0.8,125000\n",
            )

    def test_csv_export_accepts_native_metric_summaries(self):
        metrics = EvaluationMetrics((MetricSummary(
            "burst", "rules", 1, 2, 3, 0.5, 0.25, 1 / 3,
            (1000.0,), 1000.0, 1,
        ),))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.csv"
            export_evaluation_csv(metrics, path, session_id="session-2")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], ",".join(EVALUATION_CSV_COLUMNS))
            self.assertEqual(
                lines[1],
                "session-2,rules,burst,1,2,3,0.5,0.25,0.3333333333333333,1000.0",
            )

    def test_csv_export_rejects_rows_with_missing_columns(self):
        with self.assertRaisesRegex(EvaluationError, "false_negatives"):
            export_evaluation_csv(
                [{"session_id": "s"}],
                Path(tempfile.gettempdir()) / "evaluation-missing.csv",
            )

    def test_csv_column_contract_is_explicit(self):
        self.assertEqual(
            EVALUATION_CSV_COLUMNS,
            (
                "session_id", "method", "anomaly_type", "true_positives",
                "false_positives", "false_negatives", "precision", "recall",
                "f1_score", "mean_detection_latency_us",
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
