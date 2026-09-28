"""Tests for anomaly ground-truth recording and traffic integration."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect.dbc import DbcDatabase, MessageDefinition  # noqa: E402
from can_generate.traffic import (  # noqa: E402
    FileFrameSender,
    ReferenceMessage,
    ReferenceTrafficGenerator,
)
from can_generate.episodes import AnomalyEpisode, AnomalyType  # noqa: E402
from can_generate.truth import GroundTruthError, GroundTruthRecorder  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Sender:
    def send(self, can_id: int, data: bytes) -> None:
        pass


class GroundTruthTests(unittest.TestCase):
    def test_completed_episode_contains_session_parameters_and_intensities(self) -> None:
        recorder = GroundTruthRecorder("session-42")
        episode = recorder.record_episode(
            start=1,
            end=3,
            anomaly_type="frequency_increase",
            can_id=0x120,
            parameters={"period": 0.01},
            nominal_intensity=100,
            achieved_intensity=96.5,
        )

        document = recorder.as_dict()
        self.assertEqual(document["session_id"], "session-42")
        self.assertEqual(document["episodes"][0]["type"], "frequency_increase")
        self.assertEqual(episode.parameters["period"], 0.01)
        self.assertEqual(document["episodes"][0]["nominal_intensity"], 100)
        self.assertEqual(document["episodes"][0]["achieved_intensity"], 96.5)
        self.assertIn("generator_monotonic", document["time_reference"])
        self.assertIn("marker", document["time_alignment"])

    def test_start_and_end_episode_bind_the_same_session(self) -> None:
        recorder = GroundTruthRecorder("session-1")
        token = recorder.start_episode(
            start=2, anomaly_type="dropout", can_id=0x120,
            parameters={"duration": 0.5}, nominal_intensity=20,
        )
        episode = recorder.end_episode(token, end=4, achieved_intensity=0)
        self.assertEqual(episode.session_id, "session-1")
        self.assertEqual(episode.start, 2.0)
        self.assertEqual(episode.end, 4.0)
        self.assertEqual(recorder.episodes, (episode,))

    def test_write_and_read_round_trip_json(self) -> None:
        recorder = GroundTruthRecorder("session-json")
        recorder.record_episode(
            start=0, end=1, anomaly_type="unknown_id", can_id=0x7FF,
            parameters={"seed": 7}, nominal_intensity=1, achieved_intensity=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = recorder.write(Path(directory) / "truth.json")
            loaded = GroundTruthRecorder.read(path)
        self.assertEqual(loaded.as_dict(), recorder.as_dict())
        self.assertEqual(loaded.session_id, "session-json")

    def test_open_episode_cannot_be_serialized(self) -> None:
        recorder = GroundTruthRecorder("session-open")
        recorder.start_episode(start=0, anomaly_type="burst")
        with self.assertRaises(GroundTruthError):
            recorder.as_dict()

    def test_traffic_run_computes_achieved_intensity_and_binds_session(self) -> None:
        database = DbcDatabase(
            path=Path("station.dbc"),
            messages_by_id={0x120: MessageDefinition(0x120, "Status", 1, ())},
        )
        recorder = GroundTruthRecorder("session-traffic")
        recorder.record_episode(
            start=0, end=0.21, anomaly_type="frequency_increase", can_id=0x120,
            nominal_intensity=10,
        )
        generator = ReferenceTrafficGenerator(
            database, [ReferenceMessage(0x120, 0.1, payload=b"\x01")]
        )
        clock = FakeClock()
        run = generator.run(
            0.25, Sender(), clock=clock.monotonic, sleeper=clock.sleep,
            ground_truth=recorder,
        )
        self.assertEqual(run.session_id, "session-traffic")
        self.assertEqual(len(run.frames), 3)
        self.assertEqual(recorder.episodes[0].achieved_intensity, 3 / 0.21)
    def test_seeded_hardware_free_runs_match_frame_and_truth_files(self) -> None:
        database = DbcDatabase(
            path=Path("station.dbc"),
            messages_by_id={0x120: MessageDefinition(0x120, "Status", 1, ())},
        )
        generator = ReferenceTrafficGenerator(
            database, [ReferenceMessage(0x120, 0.1, payload=b"\x01")], seed=12345
        )
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            outputs = []
            for directory in (first, second):
                truth = GroundTruthRecorder("seeded-session")
                truth.record_episode(
                    start=0.0, end=0.21, anomaly_type="frequency_increase",
                    can_id=0x120, parameters={"frequency_factor": 2},
                    nominal_intensity=10,
                )
                clock = FakeClock()
                frame_path = Path(directory) / "frames.jsonl"
                truth_path = Path(directory) / "truth.json"
                with FileFrameSender(frame_path) as sender:
                    generator.run(
                        0.25, sender, clock=clock.monotonic, sleeper=clock.sleep,
                        session_id="seeded-session", ground_truth=truth,
                        anomalies=[AnomalyEpisode(
                            AnomalyType.FREQUENCY_INCREASE, 0.0, 0.2, 0x120,
                            {"frequency_factor": 2})],
                    )
                truth.write(truth_path)
                outputs.append((frame_path.read_bytes(), truth_path.read_bytes()))
            self.assertEqual(outputs[0], outputs[1])
            self.assertIn(b'"seed": 12345', outputs[0][1])
            self.assertEqual(generator.configuration["seed"], 12345)


if __name__ == "__main__":
    unittest.main(verbosity=2)
