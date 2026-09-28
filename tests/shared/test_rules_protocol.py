"""Synthetic tests for DBC/profile protocol rules."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import dbc, rules_protocol, trace  # noqa: E402
from tests import synthetic  # noqa: E402


class DlcRules(unittest.TestCase):
    def setUp(self) -> None:
        self.database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(0x100, "Known", 2, ()),
            },
        )

    def test_mismatched_dlc_reports_measured_and_expected_lengths(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x01", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules_protocol.detect_dlc_mismatches(records, self.database)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], rules_protocol.DLC_MISMATCH_EVENT)
        self.assertEqual(events[0]["can_id"], 0x100)
        self.assertEqual(events[0]["measured_dlc"], 1)
        self.assertEqual(events[0]["expected_dlc"], 2)

    def test_matching_dlc_does_not_report_anomaly(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x01\x02", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        self.assertEqual(
            rules_protocol.detect_dlc_mismatches(records, self.database), [])


class UnknownIdentifierRules(unittest.TestCase):
    def setUp(self) -> None:
        self.database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(0x100, "Known", 1, ()),
            },
        )

    def test_repeated_unknown_frames_are_reported_once_per_episode(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x7FE, b"\x01", 1_000_000)
        builder.frame(0x7FE, b"\x02", 1_001_000)
        builder.frame(0x7FE, b"\x03", 1_002_000)
        builder.frame(0x100, b"\x00", 1_003_000)
        builder.frame(0x7FE, b"\x04", 1_004_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        events = rules_protocol.detect_unknown_identifiers(
            records, self.database, profile={})

        self.assertEqual(len(events), 2)
        self.assertEqual([event["type"] for event in events], [
            "unknown_identifier", "unknown_identifier",
        ])
        self.assertEqual([event["can_id"] for event in events], [0x7FE, 0x7FE])
        self.assertEqual([event["frame_seq"] for event in events], [1, 5])

    def test_profile_only_identifier_is_known(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x200, b"\x00", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules_protocol.detect_unknown_ids(
            records, self.database, profile={0x200: {"period_us": 100_000}})

        self.assertEqual(events, [])

    def test_different_channels_have_independent_unknown_episodes(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x7FE, b"\x01", 1_000_000, channel=1)
        builder.frame(0x7FE, b"\x02", 1_001_000, channel=2)
        builder.frame(0x7FE, b"\x03", 1_002_000, channel=1)
        builder.frame(0x7FE, b"\x04", 1_003_000, channel=2)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules_protocol.ProtocolDetector(
            self.database, {}).process_all(records)

        self.assertEqual(
            [(event["channel"], event["can_id"]) for event in events],
            [(1, 0x7FE), (2, 0x7FE)],
        )

    def test_session_restart_starts_a_new_episode(self) -> None:
        first = synthetic.TraceBuilder()
        first.session(device_us=0)
        first.time_sync(1_000_000)
        first.frame(0x7FE, b"\x01", 1_000_000)
        first.frame(0x7FE, b"\x02", 1_001_000)

        second = synthetic.TraceBuilder()
        second.session(device_us=0)
        second.time_sync(2_000_000)
        second.frame(0x7FE, b"\x03", 2_000_000)

        records = list(trace.iter_trace_bytes(first.bytes() + second.bytes()))
        events = rules_protocol.detect_unknown_identifiers(records, self.database)

        self.assertEqual(len(events), 2)
        self.assertEqual([event["frame_seq"] for event in events], [2, 2])

    def test_known_frame_ends_unknown_episode_even_when_profile_only(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x7FE, b"\x01", 1_000_000)
        builder.frame(0x200, b"\x00", 1_001_000)
        builder.frame(0x7FE, b"\x02", 1_002_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules_protocol.detect_unknown_identifiers(
            records, self.database, profile={0x200: {}})

        self.assertEqual(len(events), 2)
    def test_dbc_and_baseline_range_sources_are_reported_separately(self) -> None:
        database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Known", 1,
                    (dbc.SignalDefinition("speed", 0, 8, 0.0, 10.0, "km/h"),),
                ),
            },
        )
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0b", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules_protocol.detect_signal_ranges(
            records, database,
            profile={0x100: {"signal_ranges": {
                "speed": {"min": 0.0, "max": 5.0, "unit": "km/h"},
            }}},
        )

        self.assertEqual([event["range_source"] for event in events],
                         ["dbc", "baseline_profile"])
        self.assertEqual([event["signal"] for event in events], ["speed", "speed"])
        self.assertEqual(events[0]["minimum"], 0.0)
        self.assertEqual(events[0]["maximum"], 10.0)
        self.assertEqual(events[1]["maximum"], 5.0)
        self.assertEqual(events[0]["measured_value"], 11.0)

    def test_signal_with_bits_beyond_short_dlc_is_skipped(self) -> None:
        database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Known", 2,
                    (dbc.SignalDefinition("second_byte", 8, 8, 0.0, 1.0, None),),
                ),
            },
        )
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x02", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        self.assertEqual(rules_protocol.detect_signal_ranges(records, database), [])
        self.assertEqual(database.skipped_signal_count, 1)

    def test_signal_inside_ranges_does_not_alarm(self) -> None:
        database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Known", 1,
                    (dbc.SignalDefinition("speed", 0, 8, 0.0, 10.0, "km/h"),),
                ),
            },
        )
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x04", 1_000_000)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        self.assertEqual(rules_protocol.detect_signal_ranges(
            records, database,
            profile={0x100: {"signal_ranges": {
                "speed": {"min": 0.0, "max": 5.0},
            }}},
        ), [])


class SingleProtocolViolationRules(unittest.TestCase):
    """Each synthetic trace isolates one protocol-rule violation."""

    def setUp(self) -> None:
        self.database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Status", 1,
                    (dbc.SignalDefinition(
                        "speed", 0, 8, 0.0, 10.0, "km/h"),),
                ),
            },
        )
        self.period_profile = {
            0x100: {"period_status": "observed", "period_us": 50_000},
        }

    @staticmethod
    def _records(builder: synthetic.TraceBuilder) -> list[trace.TraceRecord]:
        return list(trace.iter_trace_bytes(builder.bytes()))

    def test_unknown_id_trace_has_exactly_one_violation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x7FE, b"\x01", 1_000_000)
        events = rules_protocol.detect_unknown_identifiers(
            self._records(builder), self.database, profile={})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], rules_protocol.UNKNOWN_IDENTIFIER_EVENT)
        self.assertEqual(events[0]["can_id"], 0x7FE)

    def test_dlc_trace_has_exactly_one_violation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x01\x02", 1_000_000)
        events = rules_protocol.detect_dlc_mismatches(
            self._records(builder), self.database)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], rules_protocol.DLC_MISMATCH_EVENT)
        self.assertEqual(events[0]["measured_dlc"], 2)
        self.assertEqual(events[0]["expected_dlc"], 1)

    def test_missing_expected_frame_trace_has_exactly_one_violation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x01", 1_000_000)
        builder.time_sync(1_100_001)
        events = rules_protocol.detect_missing_frames(
            self._records(builder), self.period_profile, multiplier=2.0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"],
                         rules_protocol.MISSING_EXPECTED_FRAME_EVENT)
        self.assertEqual(events[0]["can_id"], 0x100)
        self.assertEqual(events[0]["last_observation_ts64"], 1_000_000)

    def test_dbc_range_trace_has_exactly_one_violation(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0b", 1_000_000)
        events = rules_protocol.detect_signal_ranges(
            self._records(builder), self.database,
            profile={0x100: {"signal_ranges": {
                "speed": {"min": 0.0, "max": 20.0, "unit": "km/h"},
            }}},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], rules_protocol.SIGNAL_RANGE_EVENT)
        self.assertEqual(events[0]["range_source"], rules_protocol.DBC_RANGE_SOURCE)
        self.assertEqual(events[0]["signal"], "speed")

    def test_profile_range_trace_has_exactly_one_violation(self) -> None:
        database = dbc.DbcDatabase(
            path=Path("synthetic.dbc"),
            messages_by_id={
                0x100: dbc.MessageDefinition(
                    0x100, "Status", 1,
                    (dbc.SignalDefinition(
                        "speed", 0, 8, 0.0, 255.0, "km/h"),),
                ),
            },
        )
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x0b", 1_000_000)
        events = rules_protocol.detect_signal_ranges(
            self._records(builder), database,
            profile={0x100: {"signal_ranges": {
                "speed": {"min": 0.0, "max": 10.0, "unit": "km/h"},
            }}},
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], rules_protocol.SIGNAL_RANGE_EVENT)
        self.assertEqual(events[0]["range_source"],
                         rules_protocol.BASELINE_RANGE_SOURCE)
        self.assertEqual(events[0]["signal"], "speed")

    def test_clean_profile_trace_has_no_protocol_alarms(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x05", 1_000_000)
        builder.time_sync(1_050_000)
        records = self._records(builder)
        profile = {
            0x100: {
                "period_status": "observed", "period_us": 50_000,
                "signal_ranges": {
                    "speed": {"min": 0.0, "max": 10.0, "unit": "km/h"},
                },
            },
        }
        self.assertEqual(
            rules_protocol.detect_unknown_identifiers(records, self.database,
                                                      profile), [])
        self.assertEqual(
            rules_protocol.detect_dlc_mismatches(records, self.database,
                                                 profile), [])
        self.assertEqual(
            rules_protocol.detect_missing_frames(records, profile, 2.0), [])
        self.assertEqual(
            rules_protocol.detect_signal_ranges(records, self.database, profile), [])


class MissingFrameRules(unittest.TestCase):
    PROFILE = {
        0x100: {
            "period_status": "observed",
            "period_us": 50_000,
        },
        0x200: {
            "period_status": "no_period",
            "period_us": None,
        },
    }

    def test_gap_is_measured_from_device_records_and_reports_once_per_episode(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x100, b"\x00", 1_000_000)
        # Exactly period * multiplier is still permitted by the strict
        # "longer than" requirement.
        builder.time_sync(1_100_000)
        builder.time_sync(1_100_001)
        builder.frame(0x100, b"\x01", 1_160_000)
        builder.time_sync(1_170_000)
        builder.time_sync(1_260_001)
        builder.time_sync(1_270_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        events = rules_protocol.detect_missing_frames(
            records, self.PROFILE, multiplier=2.0)

        self.assertEqual(len(events), 2)
        self.assertEqual([event["ts64"] for event in events], [1_100_001, 1_260_001])
        self.assertEqual(events[0]["last_observation_ts64"], 1_000_000)
        self.assertEqual(events[0]["threshold_us"], 100_000.0)
        self.assertEqual(events[0]["threshold_multiplier"], 2.0)
        self.assertEqual(events[0]["can_id"], 0x100)

    def test_profile_without_period_is_ignored(self) -> None:
        builder = synthetic.TraceBuilder()
        builder.time_sync(1_000_000)
        builder.frame(0x200, b"\x00", 1_000_000)
        builder.time_sync(2_000_000)

        records = list(trace.iter_trace_bytes(builder.bytes()))
        self.assertEqual(
            rules_protocol.detect_missing_frames(records, self.PROFILE, 2.0), [])

    def test_session_restart_does_not_create_cross_session_gap(self) -> None:
        first = synthetic.TraceBuilder()
        first.session(device_us=0)
        first.time_sync(1_000_000)
        first.frame(0x100, b"\x00", 1_000_000)

        second = synthetic.TraceBuilder()
        second.session(device_us=0)
        second.time_sync(1_000)
        second.time_sync(200_000)

        records = list(trace.iter_trace_bytes(first.bytes() + second.bytes()))
        self.assertEqual(
            rules_protocol.detect_missing_frames(records, self.PROFILE, 2.0), [])

    def test_missing_frame_multiplier_must_be_positive_and_finite(self) -> None:
        with self.assertRaises(ValueError):
            rules_protocol.MissingFrameDetector(self.PROFILE, 0)
        with self.assertRaises(ValueError):
            rules_protocol.MissingFrameDetector(self.PROFILE, float("inf"))



if __name__ == "__main__":
    unittest.main(verbosity=2)
