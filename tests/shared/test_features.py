"""Synthetic tests for streaming CAN time-window features."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import features, trace  # noqa: E402
from tests import synthetic  # noqa: E402


class FeatureExtraction(unittest.TestCase):
    def test_extracts_counts_intervals_jitter_and_descriptive_statistics(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        builder.frame(0x100, b"\x00", 1_100_000)
        builder.frame(0x100, b"\x00", 1_210_000)
        builder.frame(0x200, b"\x00", 1_250_000)

        windows = features.compute_features(
            trace.iter_trace_bytes(builder.bytes()), window_us=1_000_000)
        self.assertEqual(len(windows), 1)
        status = windows[0].for_id(0x100)
        assert status is not None
        self.assertEqual(status.frame_count, 3)
        self.assertEqual(status.inter_frame_intervals_us, (100_000.0, 110_000.0))
        self.assertAlmostEqual(status.jitter_us, 5_000.0)
        self.assertEqual(status.interval_statistics.as_dict(), {
            "count": 2,
            "mean": 105_000.0,
            "min": 100_000.0,
            "max": 110_000.0,
            "variance": 25_000_000.0,
            "std": 5_000.0,
        })
        self.assertEqual(status.statistics["frame_count"].mean, 3.0)
        self.assertEqual(windows[0].for_id(0x200).frame_count, 1)

    def test_uses_only_device_time_and_excludes_frames_without_sync(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.frame(0x100, b"\x01", 10)
        builder.time_sync(5_000_000)
        builder.frame(0x100, b"\x02", 5_100_000)

        windows = features.compute_features(
            trace.iter_trace_bytes(builder.bytes()), window_us=1_000_000)
        self.assertEqual(len(windows), 1)
        item = windows[0].for_id(0x100)
        assert item is not None
        self.assertEqual(item.frame_count, 1)
        self.assertEqual(item.inter_frame_intervals_us, ())
        self.assertIsNone(item.jitter_us)

    def test_working_state_does_not_accumulate_the_session(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(0)
        for index in range(100):
            builder.frame(0x100, b"\x00", index * 1_000_000)

        extractor = features.FeatureExtractor(window_us=1_000_000)
        produced = []
        for record in trace.iter_trace_bytes(builder.bytes()):
            produced.extend(extractor.update(record))
            self.assertLessEqual(extractor.buffered_sample_count, 1)
            self.assertLessEqual(extractor.active_id_count, 1)
        produced.extend(extractor.finish())

        self.assertEqual(len(produced), 100)
        self.assertEqual(extractor.buffered_sample_count, 0)
        self.assertEqual(extractor.active_id_count, 0)
        self.assertEqual(produced[-1].for_id(0x100).frame_count, 1)

    def test_does_not_bridge_restart_or_device_time_regression(self) -> None:
        first = synthetic.TraceBuilder(first_seq=0)
        first.session(device_us=0)
        first.time_sync(2_000_000)
        first.frame(0x100, b"\x00", 2_000_000)
        first.frame(0x100, b"\x00", 2_100_000)
        second = synthetic.TraceBuilder(first_seq=0)
        second.session(device_us=0)
        second.time_sync(100)
        second.frame(0x100, b"\x00", 100)

        records = trace.iter_trace_bytes(first.bytes() + second.bytes())
        windows = features.compute_features(records, window_us=1_000_000)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].for_id(0x100).inter_frame_intervals_us,
                         (100_000.0,))
        self.assertEqual(windows[1].for_id(0x100).inter_frame_intervals_us, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
