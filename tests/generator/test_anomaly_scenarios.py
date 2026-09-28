"""Focused checks for finite anomaly schedules and truth counts."""
from pathlib import Path
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))

import cantools
from anomaly_scenarios import build_schedule, default_suite, transmit_schedule
from can_detect.evaluate import convert_episodes
from can_detect.time_reference import TimeScaleReference
from uccb_generator import VirtualAdapter, make_plan

DBC = TOOLS.parent / 'generator/dbc/RTE_3.5_CAN1_CAR.dbc'


class AnomalyScenarioChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = cantools.database.load_file(str(DBC))

    def test_default_suite_covers_every_supported_family_and_records_truth(self):
        plan, _ = make_plan(self.db, period_ms=100, seed=42)
        episodes = default_suite(plan, self.db, duration=120)
        self.assertEqual({episode.kind.value for episode in episodes}, {
            'frequency_increase', 'frequency_decrease', 'disappearance',
            'burst', 'unknown_dbc_id', 'dlc_mismatch', 'out_of_range_signal'})
        frames, truth = build_schedule(plan, episodes, 120)
        self.assertEqual(len(truth), 9)
        self.assertEqual(frames[0].kind, 'marker_start')
        self.assertEqual(sum(frame.kind == 'marker_end' for frame in frames), 1)
        self.assertTrue(all(item['planned_frames'] > 0
                            for item in truth if item['type'] != 'disappearance'))
        document = {'session_id': 'test', 'episodes': truth}
        reference = TimeScaleReference(0x7FE, 1_000_000,
                                       ({'kind': 'start', 'generator_time_us': 0},))
        converted = convert_episodes(document, reference)
        self.assertEqual(converted[0].start_us, 16_000_000)

    def test_virtual_transmission_updates_achieved_episode_counts(self):
        plan, _ = make_plan(self.db, period_ms=100, seed=42)
        episodes = default_suite(plan, self.db, duration=120)
        frames, truth = build_schedule(plan, episodes, 120)
        adapter = VirtualAdapter()
        stats = {'commands_attempted': 0, 'commands_acknowledged': 0,
                 'slots_skipped_late': 0, 'max_lateness_s': 0,
                 'per_episode': {item['episode_id']: {'acknowledged_frames': 0,
                                                      'skipped_frames': 0}
                                 for item in truth}}
        transmit_schedule(frames, adapter, 120, lambda frame: None, stats,
                          clock=adapter.clock, sleeper=adapter.sleep)
        self.assertGreater(stats['commands_acknowledged'], 0)
        self.assertEqual(stats['commands_attempted'], stats['commands_acknowledged'])
        self.assertTrue(any(value['acknowledged_frames'] > 0
                            for value in stats['per_episode'].values()))


if __name__ == '__main__':
    unittest.main()
