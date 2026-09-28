"""Synthetic tests for inter-frame period violations."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import features, rules_timing, trace  # noqa: E402
from tests import synthetic  # noqa: E402


class PeriodRules(unittest.TestCase):
    def _windows(self, timestamps: tuple[int, ...]) -> list[features.FeatureWindow]:
        builder = synthetic.TraceBuilder()
        builder.time_sync(timestamps[0])
        for timestamp in timestamps:
            builder.frame(0x100, b"\x00", timestamp)
        return features.compute_features(
            trace.iter_trace_bytes(builder.bytes()), window_us=1_000_000)

    def test_reports_interval_outside_profile_tolerance_with_device_timestamp(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000, 1_450_000))
        events = rules_timing.detect_period_violations(windows, {
            0x100: {
                "period_status": "observed",
                "period_us": 100_000.0,
                "tolerance_us": {"min": 90_000.0, "max": 110_000.0},
            },
        })

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "period_violation")
        self.assertEqual(events[0]["method"], "timing")
        self.assertEqual(events[0]["can_id"], 0x100)
        self.assertEqual(events[0]["ts64"], 1_450_000)
        self.assertEqual(events[0]["measured_interval_us"], 250_000.0)
        self.assertEqual(events[0]["expected_min_us"], 90_000.0)
        self.assertEqual(events[0]["expected_max_us"], 110_000.0)

    def test_does_not_report_intervals_inside_tolerance_or_ids_without_period(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000))
        self.assertEqual(
            rules_timing.detect_period_violations(windows, {
                0x100: {
                    "period_status": "observed",
                    "tolerance_us": {"min": 100_000.0, "max": 100_000.0},
                },
            }), [])
        self.assertEqual(
            rules_timing.detect_period_violations(windows, {
                0x100: {"period_status": "no_period", "tolerance_us": None},
            }), [])

    def test_reports_frequency_increase_and_decrease_against_window_thresholds(self) -> None:
        increase_windows = self._windows((1_000_000, 1_100_000, 1_200_000, 1_300_000))
        profile = {0x100: {"window_counts": {"min": 3, "max": 3}}}
        increase = rules_timing.detect_frequency_deviations(
            increase_windows, profile)

        self.assertEqual(len(increase), 1)
        self.assertEqual(increase[0]["type"], "frequency_increase")
        self.assertEqual(increase[0]["method"], "timing")
        self.assertEqual(increase[0]["can_id"], 0x100)
        self.assertEqual(increase[0]["ts64"], 2_000_000)
        self.assertEqual(increase[0]["measured_frame_count"], 4)
        self.assertEqual(increase[0]["expected_min_frame_count"], 3.0)
        self.assertEqual(increase[0]["expected_max_frame_count"], 3.0)

        decrease_windows = self._windows((1_000_000, 1_100_000))
        decrease = rules_timing.detect_frequency_deviations(
            decrease_windows, profile)
        self.assertEqual(len(decrease), 1)
        self.assertEqual(decrease[0]["type"], "frequency_decrease")
        self.assertEqual(decrease[0]["measured_frame_count"], 2)

    def test_does_not_report_frequency_at_or_between_thresholds(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000))
        profile = {0x100: {"window_counts": {"min": 2, "max": 4}}}

        self.assertEqual(
            rules_timing.detect_frequency_deviations(windows, profile), [])

    def test_accepts_explicit_frequency_thresholds(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000, 1_300_000))
        events = rules_timing.detect_frequency_changes(windows, {
            0x100: {"frequency_thresholds": {"min": 1, "max": 2}},
        })
        self.assertEqual([event["type"] for event in events], [
            "frequency_increase",
        ])

    def test_accepts_json_profile_keys_and_rejects_reversed_tolerance(self) -> None:
        windows = self._windows((1_000_000, 1_200_000))
        with self.assertRaises(ValueError):
            rules_timing.detect_period_violations(windows, {
                "256": {"tolerance_us": {"min": 300_000, "max": 100_000}},
            })

    def test_configurable_sigma_rule_reports_outlier_with_profile_statistics(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000, 1_340_000))
        events = rules_timing.detect_statistical_deviations(windows, {
            0x100: {"inter_frame_interval_us": {
                "mean": 100_000.0, "std": 10_000.0,
            }},
        }, sigma_multiplier=3.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "statistical_deviation")
        self.assertEqual(events[0]["feature"], "inter_frame_interval_us")
        self.assertEqual(events[0]["measured"], 140_000.0)
        self.assertEqual(events[0]["expected_min"], 70_000.0)
        self.assertEqual(events[0]["expected_max"], 130_000.0)
        self.assertEqual(events[0]["sigma_multiplier"], 3.0)

    def test_sigma_multiplier_changes_decision_boundary(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_200_000, 1_340_000))
        profile = {0x100: {"inter_frame_interval_us": {
            "mean": 100_000.0, "std": 20_000.0,
        }}}
        self.assertEqual(rules_timing.detect_statistical_deviations(
            windows, profile, sigma_multiplier=3.0), [])
        self.assertEqual(len(rules_timing.detect_statistical_deviations(
            windows, profile, sigma_multiplier=1.5)), 1)

    def test_zero_baseline_standard_deviation_disables_rule_and_is_reported(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_250_000))
        report = {}
        events = rules_timing.detect_statistical_deviations(windows, {
            0x100: {"inter_frame_interval_us": {
                "mean": 100_000.0, "std": 0.0,
            }},
        }, report_state=report)
        self.assertEqual(events, [])
        self.assertEqual(report["disabled_statistical_rules"], [{
            "channel": 1,
            "can_id": 0x100,
            "feature": "inter_frame_interval_us",
            "reason": "zero_baseline_standard_deviation",
            "standard_deviation": 0.0,
        }])

    def test_zero_baseline_case_is_recorded_once_across_windows(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 2_000_000, 2_100_000))
        report = {}
        rules_timing.detect_statistical_deviations(windows, {
            0x100: {"inter_frame_interval_us": {
                "mean": 100_000.0, "std": 0.0,
            }},
        }, report_state=report)
        self.assertEqual(len(report["disabled_statistical_rules"]), 1)

    def test_reports_one_event_for_a_qualifying_short_burst(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_140_000, 1_180_000,
                                 1_220_000, 1_320_000))
        events = rules_timing.detect_short_bursts(
            windows, {0x100: {"period_status": "observed", "period_us": 100_000}},
            interval_factor=0.5, min_frames=3)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "short_burst")
        self.assertEqual(events[0]["interval_count"], 3)
        self.assertEqual(events[0]["burst_start_ts64"], 1_140_000)
        self.assertEqual(events[0]["burst_end_ts64"], 1_220_000)
        self.assertEqual(events[0]["intervals_us"], [40_000.0] * 3)
        self.assertEqual(events[0]["short_interval_threshold_us"], 50_000.0)

    def test_requires_count_and_strictly_short_intervals(self) -> None:
        windows = self._windows((1_000_000, 1_100_000, 1_140_000, 1_180_000,
                                 1_230_000))
        profile = {0x100: {"period_status": "observed", "period_us": 100_000}}
        self.assertEqual(rules_timing.detect_short_bursts(
            windows, profile, interval_factor=0.5, min_frames=3), [])
        self.assertEqual(rules_timing.detect_short_bursts(
            windows, profile, interval_factor=0.5, min_frames=2)[0]["interval_count"], 2)

        threshold_windows = self._windows((1_000_000, 1_100_000, 1_150_000,
                                           1_200_000))
        self.assertEqual(rules_timing.detect_short_bursts(
            threshold_windows, profile, interval_factor=0.5, min_frames=2), [])

    def test_each_timing_rule_has_a_single_injected_deviation_and_clean_trace(self) -> None:
        """Exercise one positive and one negative synthetic trace per rule.

        The positive traces contain exactly one changed feature for the selected
        CAN ID.  The negative traces use the same baseline and stay within its
        configured limits, so unrelated traffic cannot make the assertion pass.
        """
        period_profile = {
            0x100: {
                "period_status": "observed",
                "period_us": 100_000.0,
                "tolerance_us": {"min": 90_000.0, "max": 110_000.0},
            },
        }
        clean_period = self._windows((1_000_000, 1_100_000, 1_200_000,
                                      1_300_000))
        injected_period = self._windows((1_000_000, 1_100_000, 1_200_000,
                                         1_350_000))
        self.assertEqual(
            rules_timing.detect_period_violations(clean_period, period_profile),
            [],
        )
        period_events = rules_timing.detect_period_violations(
            injected_period, period_profile)
        self.assertEqual([event["type"] for event in period_events],
                         ["period_violation"])
        self.assertEqual(period_events[0]["measured_interval_us"], 150_000.0)

        frequency_profile = {0x100: {"window_counts": {"min": 3, "max": 3}}}
        clean_frequency = self._windows((1_000_000, 1_100_000, 1_200_000))
        injected_increase = self._windows((1_000_000, 1_100_000, 1_200_000,
                                           1_300_000))
        injected_decrease = self._windows((1_000_000, 1_100_000))
        self.assertEqual(
            rules_timing.detect_frequency_deviations(
                clean_frequency, frequency_profile),
            [],
        )
        self.assertEqual(
            [event["type"] for event in rules_timing.detect_frequency_deviations(
                injected_increase, frequency_profile)],
            ["frequency_increase"],
        )
        self.assertEqual(
            [event["type"] for event in rules_timing.detect_frequency_deviations(
                injected_decrease, frequency_profile)],
            ["frequency_decrease"],
        )

        burst_profile = {
            0x100: {"period_status": "observed", "period_us": 100_000.0},
        }
        clean_burst = self._windows((1_000_000, 1_100_000, 1_200_000,
                                     1_300_000, 1_400_000))
        injected_burst = self._windows((1_000_000, 1_100_000, 1_140_000,
                                        1_180_000, 1_220_000, 1_320_000))
        self.assertEqual(
            rules_timing.detect_short_bursts(
                clean_burst, burst_profile, interval_factor=0.5, min_frames=3),
            [],
        )
        burst_events = rules_timing.detect_short_bursts(
            injected_burst, burst_profile, interval_factor=0.5, min_frames=3)
        self.assertEqual([event["type"] for event in burst_events],
                         ["short_burst"])
        self.assertEqual(burst_events[0]["interval_count"], 3)

        sigma_profile = {0x100: {"inter_frame_interval_us": {
            "mean": 100_000.0, "std": 10_000.0,
        }}}
        clean_sigma = self._windows((1_000_000, 1_100_000, 1_200_000,
                                     1_300_000))
        injected_sigma = self._windows((1_000_000, 1_100_000, 1_200_000,
                                        1_340_000))
        self.assertEqual(
            rules_timing.detect_statistical_deviations(
                clean_sigma, sigma_profile, sigma_multiplier=3.0),
            [],
        )
        sigma_events = rules_timing.detect_statistical_deviations(
            injected_sigma, sigma_profile, sigma_multiplier=3.0)
        self.assertEqual([event["type"] for event in sigma_events],
                         ["statistical_deviation"])
        self.assertEqual(sigma_events[0]["measured"], 140_000.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
