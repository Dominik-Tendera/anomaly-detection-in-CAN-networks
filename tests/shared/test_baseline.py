"""Focused synthetic tests for per-CAN-ID baseline statistics."""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import baseline, dbc, trace  # noqa: E402
from tests import synthetic  # noqa: E402


class BaselineMetrics(unittest.TestCase):
    def setUp(self) -> None:
        signal = dbc.SignalDefinition(
            name="speed", start_bit=0, bit_length=8,
            minimum=0.0, maximum=255.0, unit="km/h",
        )
        self.database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Status", 2, (signal,)),
                0x200: dbc.MessageDefinition(0x200, "Heartbeat", 1, ()),
            },
        )

    def test_computes_intervals_windows_dlc_and_signal_ranges(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0a", 1_000_000)
        builder.frame(0x100, b"\x14\x00", 1_100_000)
        builder.frame(0x100, b"\x1e", 2_400_000)
        builder.frame(0x200, b"\x00", 2_400_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        profile = baseline.compute_baseline(
            records, self.database, window_us=1_000_000)

        status = profile[0x100]
        intervals = status["inter_frame_interval_us"]
        self.assertEqual(status["frame_count"], 3)
        self.assertEqual(status["observed_dlc"], [1, 2])
        self.assertEqual(intervals["count"], 2)
        self.assertEqual(intervals["mean"], 700_000.0)
        self.assertEqual(intervals["std"], 600_000.0)
        self.assertEqual(intervals["min"], 100_000.0)
        self.assertEqual(intervals["max"], 1_300_000.0)
        self.assertEqual(status["window_counts"]["mean"], 1.5)
        self.assertEqual(status["window_counts"]["std"], 0.5)
        self.assertEqual(status["window_counts"]["count"], 2)
        self.assertEqual(status["signal_ranges"]["speed"], {
            "min": 10.0, "max": 30.0, "unit": "km/h",
        })

        heartbeat = profile[0x200]
        self.assertEqual(heartbeat["frame_count"], 1)
        self.assertEqual(heartbeat["observed_dlc"], [1])
        self.assertEqual(heartbeat["inter_frame_interval_us"]["count"], 0)
        self.assertEqual(heartbeat["inter_frame_interval_us"]["std"], None)
        self.assertEqual(heartbeat["window_counts"]["mean"], 1.0)

    def test_skips_incomplete_interval_and_does_not_bridge_it(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(0)
        builder.bus_stats(channel=1, ts64=1_000_000)
        builder.frame(0x100, b"\x0a", 900_000)
        builder.frame(0x100, b"\x14", 1_500_000)
        builder.bus_stats(channel=1, ts64=2_000_000, ring_dropped=1)
        builder.frame(0x100, b"\x1e", 2_500_000)
        builder.frame(0x100, b"\x28", 2_800_000)
        builder.bus_stats(channel=1, ts64=3_000_000, ring_dropped=1)

        reader = trace.TraceReader()
        records = list(trace.iter_trace_bytes(builder.bytes(), reader=reader))
        self.assertEqual(len(reader.incomplete_intervals), 1)

        profile = baseline.compute_baseline(
            records, self.database, min_interval_observations=1)
        item = profile[0x100]

        # The frame in [1 s, 2 s] is excluded, and the two surviving frames on
        # either side of that interval are not treated as adjacent.
        self.assertEqual(item["frame_count"], 3)
        self.assertEqual(item["signal_ranges"]["speed"]["min"], 10.0)
        self.assertEqual(item["signal_ranges"]["speed"]["max"], 40.0)
        self.assertEqual(item["inter_frame_interval_us"]["count"], 1)
        self.assertEqual(item["inter_frame_interval_us"]["mean"], 300_000.0)
        self.assertEqual(item["inter_frame_interval_us"]["min"], 300_000.0)
        self.assertEqual(item["inter_frame_interval_us"]["max"], 300_000.0)

    def test_counts_untimed_frames_but_excludes_them_from_time_metrics(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.frame(0x100, b"\x05", 10)
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0f", 1_000_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        profile = baseline.build_baseline(records, self.database)

        item = profile[0x100]
        self.assertEqual(item["frame_count"], 2)
        self.assertEqual(item["observed_dlc"], [1])
        self.assertEqual(item["inter_frame_interval_us"]["count"], 0)
        self.assertEqual(item["signal_ranges"]["speed"]["min"], 5.0)
        self.assertEqual(item["signal_ranges"]["speed"]["max"], 15.0)

    def test_marks_sparse_ids_without_period_or_tolerance_and_keeps_dlc(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0a", 1_000_000)
        builder.frame(0x100, b"\x14\x00", 1_100_000)
        builder.frame(0x100, b"\x1e", 2_400_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        profile = baseline.compute_baseline(
            records, self.database, min_interval_observations=3)

        status = profile[0x100]
        self.assertEqual(status["inter_frame_interval_us"]["count"], 2)
        self.assertEqual(status["period_status"], "no_period")
        self.assertIsNone(status["period_us"])
        self.assertIsNone(status["tolerance_us"])
        self.assertEqual(status["observed_dlc"], [1, 2])
        self.assertEqual(status["min_interval_observations"], 3)

    def test_assigns_period_and_observed_tolerance_at_threshold(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        for timestamp in (1_000_000, 1_100_000, 2_400_000):
            builder.frame(0x100, b"\x0a", timestamp)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        status = baseline.compute_baseline(
            records, self.database, min_interval_observations=2)[0x100]

        self.assertEqual(status["period_status"], "observed")
        self.assertEqual(status["period_us"], 700_000.0)
        self.assertEqual(status["tolerance_us"], {
            "min": 100_000.0, "max": 1_300_000.0,
        })

    def test_matches_known_period_jitter_and_minimum_count(self) -> None:
        """A synthetic reference run should preserve its known timing profile."""
        period_us = 100_000
        jitter_us = (0, 10_000, -10_000, 5_000, -5_000)
        timestamps = [1_000_000]
        for jitter in jitter_us:
            timestamps.append(timestamps[-1] + period_us + jitter)

        builder = synthetic.TraceBuilder()
        builder.time_sync(timestamps[0])
        for timestamp in timestamps:
            builder.frame(0x100, b"\x2a", timestamp)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        qualified = baseline.compute_baseline(
            records, self.database, min_interval_observations=len(jitter_us))[
                0x100]
        intervals = qualified["inter_frame_interval_us"]

        self.assertEqual(intervals["count"], len(jitter_us))
        self.assertAlmostEqual(intervals["mean"], period_us, delta=1.0)
        expected_std = math.sqrt(sum(jitter * jitter for jitter in jitter_us)
                                 / len(jitter_us))
        self.assertAlmostEqual(intervals["std"], expected_std, delta=1.0)
        self.assertEqual(intervals["min"], period_us - 10_000.0)
        self.assertEqual(intervals["max"], period_us + 10_000.0)
        self.assertEqual(qualified["period_status"], "observed")
        self.assertAlmostEqual(qualified["period_us"], period_us, delta=1.0)
        self.assertEqual(qualified["tolerance_us"], {
            "min": period_us - 10_000.0,
            "max": period_us + 10_000.0,
        })

        below_minimum = baseline.compute_baseline(
            records, self.database,
            min_interval_observations=len(jitter_us) + 1)[0x100]
        self.assertEqual(below_minimum["period_status"], "no_period")
        self.assertIsNone(below_minimum["period_us"])
        self.assertIsNone(below_minimum["tolerance_us"])
        self.assertEqual(below_minimum["observed_dlc"], [1])
        self.assertEqual(
            below_minimum["inter_frame_interval_us"]["count"], len(jitter_us))

    def test_rejects_non_positive_window(self) -> None:
        with self.assertRaises(ValueError):
            baseline.compute_baseline([], self.database, window_us=0)

    def test_rejects_non_positive_minimum_interval_observations(self) -> None:
        with self.assertRaises(ValueError):
            baseline.compute_baseline(
                [], self.database, min_interval_observations=0)


class BaselineProfilePersistence(unittest.TestCase):
    def test_writes_versionable_text_with_units_provenance_and_reference_metadata(self) -> None:
        profile = {
            0x100: {
                "can_id": 0x100,
                "frame_count": 3,
                "inter_frame_interval_us": {
                    "mean": 100_000.0, "std": 0.0,
                    "min": 100_000.0, "max": 100_000.0, "count": 2,
                },
                "observed_dlc": [1, 2],
                "window_us": 1_000_000,
                "window_counts": {
                    "mean": 1.5, "std": 0.5, "min": 1.0,
                    "max": 2.0, "count": 2,
                },
                "signal_ranges": {
                    "speed": {"min": 10.0, "max": 30.0, "unit": "km/h"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "baseline.json"
            document = baseline.save_profile(
                profile, destination, session_id="reference-01",
                duration_us=60_000_000, frame_count=3,
                skipped_intervals=[{"start_us": 2_000_000, "end_us": 2_500_000}],
                dbc_checksum="a" * 64,
            )
            self.assertEqual(document["format"], baseline.PROFILE_FORMAT)
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["metadata"]["session_id"], "reference-01")
            self.assertEqual(document["metadata"]["duration_us"], 60_000_000)
            self.assertEqual(document["metadata"]["frame_count"], 3)
            self.assertEqual(document["metadata"]["skipped_interval_count"], 1)
            self.assertEqual(document["metadata"]["skipped_intervals"], [
                {"start_us": 2_000_000, "end_us": 2_500_000},
            ])
            self.assertEqual(document["metadata"]["dbc_sha256"], "a" * 64)
            self.assertIn("assumption, not a measurement", 
                          document["metadata"]["reference_assumption"])

            text = destination.read_text(encoding="utf-8")
            self.assertTrue(text.endswith("\n"))
            self.assertIn('"provenance": "reference_derived"', text)
            self.assertIn('"unit": "microseconds"', text)
            self.assertIn('"unit": "km/h"', text)
            self.assertEqual(baseline.load_profile(destination), profile)
            self.assertEqual(baseline.load_profile_document(destination), document)

    def test_dbc_checksum_is_sha256_of_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "network.dbc"
            source.write_bytes(b"VERSION \"test\"\n")
            self.assertEqual(
                baseline.dbc_sha256(source),
                "84e5aa86d1ee3ca78a096f9e708164cb1b0f44b1704a06c9791d66114fe7e293",
            )


class BaselineProfileLoading(unittest.TestCase):
    def test_load_reports_profile_dbc_and_manual_identifier_discrepancies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbc_path = root / "network.dbc"
            dbc_path.write_bytes(b'VERSION "test"\n')
            database = dbc.DbcDatabase(
                path=dbc_path,
                messages_by_id={
                    0x100: dbc.MessageDefinition(0x100, "Known", 1, ()),
                },
            )
            profile_path = root / "baseline.json"
            baseline.save_profile(
                {0x100: {"can_id": 0x100}, 0x200: {"can_id": 0x200}},
                profile_path, dbc_path=dbc_path)
            document = json.loads(profile_path.read_text(encoding="utf-8"))
            document["entries"][1]["provenance"] = "manual"
            profile_path.write_text(json.dumps(document), encoding="utf-8")

            report = {}
            loaded = baseline.load_profile(profile_path, database, report=report)

            self.assertEqual(set(loaded), {0x100, 0x200})
            self.assertEqual(report["profile_dbc_discrepancies"], {
                "profile_only_ids": [0x200],
                "dbc_only_ids": [],
                "manual_ids": [0x200],
            })

    def test_load_terminates_on_dbc_checksum_mismatch_and_reports_both_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbc_path = root / "network.dbc"
            dbc_path.write_bytes(b'VERSION "one"\n')
            database = dbc.DbcDatabase(path=dbc_path, messages_by_id={})
            profile_path = root / "baseline.json"
            baseline.save_profile({}, profile_path, dbc_checksum="0" * 64)

            with self.assertRaises(baseline.DbcChecksumMismatch) as caught:
                baseline.load_profile(profile_path, database)

            error = caught.exception
            self.assertEqual(error.expected, "0" * 64)
            self.assertEqual(error.actual, database.sha256)
            self.assertIn("profile='" + "0" * 64, str(error))
            self.assertIn(error.actual, str(error))


if __name__ == "__main__":
    unittest.main(verbosity=2)
