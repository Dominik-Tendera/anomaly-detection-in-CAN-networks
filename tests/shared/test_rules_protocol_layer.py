"""Tests for alarms derived from controller diagnostic statistics."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_detect import rules_protocol_layer as rules  # noqa: E402
from can_detect import trace  # noqa: E402
from tests import synthetic  # noqa: E402


SECOND = 1_000_000


class ProtocolLayerRules(unittest.TestCase):
    def _records(self, **second_counters: int):
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        builder.bus_stats(channel=1, ts64=2 * SECOND, **second_counters)
        return list(trace.iter_trace_bytes(builder.bytes()))

    def test_protocol_counter_increments_create_one_alarm_per_class(self):
        records = self._records(stuff_errors=2, ack_errors=1, crc_errors=3)

        events = rules.detect_protocol_layer(records)

        self.assertEqual(
            [(event["type"], event["error_class"], event["increment"])
             for event in events],
            [("protocol_error", "stuff_errors", 2),
             ("protocol_error", "ack_errors", 1),
             ("protocol_error", "crc_errors", 3)],
        )

    def test_controller_transition_reports_state_and_rec_tec(self):
        records = self._records()
        stats = [item for item in records if item.type == trace.proto.REC_BUS_STATS]
        stats[0].decoded["state_name"] = "active"
        stats[1].decoded.update({
            "state_name": "warning",
            "current_rec": 17,
            "current_tec": 4,
        })

        events = rules.detect_protocol_layer(records)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "controller_state_change")
        self.assertEqual(events[0]["state"], "warning")
        self.assertEqual(events[0]["previous_state"], "active")
        self.assertEqual(events[0]["rec"], 17)
        self.assertEqual(events[0]["tec"], 4)

    def test_increasing_statistics_counters_assign_alarms_and_intervals(self):
        """Cumulative statistics produce deltas and classify each interval."""
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        builder.bus_stats(
            channel=1,
            ts64=2 * SECOND,
            stuff_errors=2,
            form_errors=3,
            ack_errors=4,
            bit_recessive_errors=5,
            bit_dominant_errors=6,
            crc_errors=7,
        )
        builder.bus_stats(
            channel=1,
            ts64=3 * SECOND,
            stuff_errors=2,
            form_errors=3,
            ack_errors=4,
            bit_recessive_errors=5,
            bit_dominant_errors=6,
            crc_errors=7,
            fifo_overrun=2,
            ring_dropped=3,
            last_ring_drop_id=0x456,
        )
        builder.bus_stats(
            channel=1,
            ts64=4 * SECOND,
            stuff_errors=3,
            form_errors=3,
            ack_errors=4,
            bit_recessive_errors=5,
            bit_dominant_errors=6,
            crc_errors=7,
            rx_read_errors=1,
            fifo_overrun=2,
            ring_dropped=3,
            last_ring_drop_id=0x456,
        )
        records = list(trace.iter_trace_bytes(builder.bytes()))
        stats = [item for item in records
                 if item.type == trace.proto.REC_BUS_STATS]
        stats[0].decoded["state_name"] = "active"
        stats[1].decoded.update({
            "state_name": "warning", "current_rec": 11, "current_tec": 2,
        })
        stats[2].decoded.update({
            "state_name": "passive", "current_rec": 22, "current_tec": 8,
        })
        stats[3].decoded.update({
            "state_name": "bus_off", "current_rec": 0, "current_tec": 255,
        })

        events = rules.detect_protocol_layer(records)

        protocol_events = [event for event in events
                           if event["type"] == "protocol_error"]
        self.assertEqual(
            [(event["error_class"], event["increment"])
             for event in protocol_events],
            [(name, increment) for name, increment in zip(
                rules.PROTOCOL_ERROR_COUNTERS, range(2, 8))]
            + [("stuff_errors", 1)],
        )
        state_events = [event for event in events
                        if event["type"] == "controller_state_change"]
        self.assertEqual(
            [(event["previous_state"], event["state"], event["rec"],
              event["tec"]) for event in state_events],
            [("active", "warning", 11, 2),
             ("warning", "passive", 22, 8),
             ("passive", "bus_off", 0, 255)],
        )
        loss_events = [event for event in events
                       if event["type"] == "device_frame_loss"]
        self.assertEqual(
            [event["loss_increments"] for event in loss_events],
            [{"fifo_overrun": 2, "ring_dropped": 3},
             {"rx_read_errors": 1}],
        )
        self.assertEqual(loss_events[0]["frame_id"], 0x456)
        self.assertEqual(loss_events[0]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_RAM_BUFFER)
        self.assertIsNone(loss_events[1]["frame_id"])
        self.assertEqual(loss_events[1]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_UNTRUSTED)
        for event in protocol_events:
            self.assertIsNone(event["frame_id"])
            self.assertEqual(event["frame_id_source"],
                             rules.FRAME_ID_SOURCE_UNTRUSTED)

        intervals = [item.decoded["interval"] for item in stats[1:]]
        self.assertTrue(intervals[0]["complete"])
        self.assertEqual(intervals[0]["reasons"], [])
        self.assertFalse(intervals[1]["complete"])
        self.assertEqual(intervals[1]["reasons"], ["device_loss"])
        self.assertFalse(intervals[2]["complete"])
        self.assertEqual(intervals[2]["reasons"], ["device_loss"])
        self.assertEqual(intervals[2]["loss_increments"], {"rx_read_errors": 1})

    def test_device_loss_alarm_marks_interval_incomplete(self):
        records = self._records(fifo_overrun=2, ring_dropped=3,
                                last_ring_drop_id=0x456)
        stats = [item for item in records if item.type == trace.proto.REC_BUS_STATS]
        interval = stats[-1].decoded["interval"]
        self.assertTrue(interval["complete"] is False)

        events = rules.detect_protocol_layer(records)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "device_frame_loss")
        self.assertEqual(events[0]["loss_increments"], {
            "fifo_overrun": 2,
            "ring_dropped": 3,
        })
        self.assertIs(events[0]["interval"], interval)
        self.assertEqual(events[0]["frame_id"], 0x456)
        self.assertEqual(events[0]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_RAM_BUFFER)
        self.assertIn("device_loss", interval["reasons"])

    def test_rejected_frame_events_create_profile_mismatch_alarms(self):
        builder = synthetic.TraceBuilder()
        builder.event(1, ts64=SECOND, error_code=0x12345678)
        builder.event(2, channel=2, ts64=2 * SECOND, frame_id=0x321,
                      suppressed=2)
        builder.event(3, ts64=3 * SECOND, error_code=9)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        events = rules.detect_protocol_layer(records)

        self.assertEqual(
            [(event["type"], event["rejection_reason"], event["channel"])
             for event in events],
            [("frame_profile_mismatch", "extended_frame_rejected", 1),
             ("frame_profile_mismatch", "remote_frame_rejected", 2),
             ("frame_profile_mismatch", "invalid_dlc", 1)],
        )
        self.assertEqual(events[0]["error_code"], 0x12345678)
        self.assertIsNone(events[0]["frame_id"])
        self.assertEqual(events[0]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_UNTRUSTED)
        self.assertIsNone(events[1]["frame_id"])
        self.assertEqual(events[1]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_UNTRUSTED)
        self.assertEqual(events[1]["suppressed"], 2)
        self.assertEqual(events[1]["rejected_events"], 3)

    def test_non_rejection_events_do_not_create_frame_profile_alarm(self):
        builder = synthetic.TraceBuilder()
        builder.event(0, ts64=SECOND)
        builder.event(4, ts64=2 * SECOND, frame_id=0x123)
        builder.event(11, ts64=3 * SECOND)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        self.assertEqual(rules.detect_protocol_layer(records), [])

    def test_protocol_errors_never_expose_a_frame_identifier(self):
        records = self._records(stuff_errors=1, crc_errors=2,
                                last_ring_drop_id=0x321)

        events = rules.detect_protocol_layer(records)

        self.assertEqual(len(events), 2)
        for event in events:
            self.assertIsNone(event["frame_id"])
            self.assertEqual(event["frame_id_source"],
                             rules.FRAME_ID_SOURCE_UNTRUSTED)

    def test_receive_fifo_overflow_exposes_no_frame_identifier(self):
        records = self._records(fifo_overrun=1, last_ring_drop_id=0x321)

        events = rules.detect_protocol_layer(records)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "device_frame_loss")
        self.assertIsNone(events[0]["frame_id"])
        self.assertEqual(events[0]["frame_id_source"],
                         rules.FRAME_ID_SOURCE_UNTRUSTED)

    def test_first_statistics_sample_is_only_a_baseline(self):
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        records = list(trace.iter_trace_bytes(builder.bytes()))

        self.assertEqual(rules.detect_protocol_layer(records), [])

    def test_state_is_tracked_separately_per_channel(self):
        builder = synthetic.TraceBuilder()
        builder.bus_stats(channel=1, ts64=SECOND)
        builder.bus_stats(channel=2, ts64=SECOND)
        builder.bus_stats(channel=1, ts64=2 * SECOND)
        builder.bus_stats(channel=2, ts64=2 * SECOND)
        records = list(trace.iter_trace_bytes(builder.bytes()))
        stats = [item for item in records if item.type == trace.proto.REC_BUS_STATS]
        stats[0].decoded["state_name"] = "active"
        stats[1].decoded["state_name"] = "active"
        stats[2].decoded["state_name"] = "passive"
        stats[3].decoded["state_name"] = "warning"

        events = rules.detect_protocol_layer(records)

        self.assertEqual([event["channel"] for event in events], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
