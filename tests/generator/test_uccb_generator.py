"""Generator acceptance checks without a CAN adapter."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import json

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import cantools
import generate_can_traffic as cli
from uccb_generator import (UccbAdapter, AdapterError, CommandRejected,
                            has_out_of_range_signal, make_payload, make_plan,
                            new_stats, transmit, VirtualAdapter)

DBC = TOOLS.parent / 'generator/dbc/RTE_3.5_CAN1_CAR.dbc'


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.pending = bytearray()
        self.written = []
        self.closed = False
        self.reject_tx = False
        self.silent = False

    def write(self, data):
        self.written.append(data)
        if not self.silent:
            replies = {b'v\r': b'v123\r', b'V\r': b'V10\r', b'N\r': b'Nabcd\r'}
            if data.startswith(b't'):
                self.pending.extend(b'\x07' if self.reject_tx else b't123100\rz\r')
            else:
                self.pending.extend(replies.get(data, b'\r'))
        return len(data)

    def read(self, count):
        data = bytes(self.pending[:count])
        del self.pending[:count]
        return data

    def reset_input_buffer(self):
        self.pending.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.closed = True


class GeneratorChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = cantools.database.load_file(str(DBC))

    def test_default_budget_and_round_trip_real_dbc(self):
        plan, budget = make_plan(self.db)
        self.assertGreater(len(plan), 0)
        self.assertLessEqual(len(plan), 20)
        self.assertLessEqual(budget['nominal_fps'], 100)
        self.assertLessEqual(budget['host_utilisation'], .4)
        # Check every station message, not just the default subset.
        for message in self.db.messages:
            for elapsed in (0, 1.3, 5, 9.9):
                data = make_payload(message, elapsed)
                self.assertEqual(len(data), message.length)
                decoded = message.decode(data, decode_choices=False)
                for signal in message.signals:
                    if signal.minimum is not None and signal.maximum is not None and signal.minimum != signal.maximum:
                        self.assertGreaterEqual(decoded[signal.name], signal.minimum - 1e-6)
                        self.assertLessEqual(decoded[signal.name], signal.maximum + 1e-6)

    def test_signal_variation_and_constant_mode(self):
        message = self.db.get_message_by_frame_id(0x201)
        self.assertNotEqual(make_payload(message, 0), make_payload(message, 2))
        self.assertEqual(make_payload(message, 0, 'constant'), make_payload(message, 2, 'constant'))

    def test_selection_seed_repeats_experiment_and_changes_subset(self):
        choose = lambda seed: [p.message.frame_id for p in make_plan(self.db, seed=seed)[0]]
        self.assertEqual(choose(42), choose(42))
        self.assertNotEqual(choose(42), choose(43))

    def test_random_subset_reserves_out_of_range_capable_message(self):
        plan, _ = make_plan(self.db, seed=1, max_messages=1)
        self.assertTrue(has_out_of_range_signal(plan[0].message))

    def test_explicit_ids_are_not_silently_removed(self):
        with self.assertRaisesRegex(ValueError, 'budzet'):
            make_plan(self.db, ids=[0x200, 0x201], max_fps=10)
        with self.assertRaisesRegex(ValueError, 'nieobecne'):
            make_plan(self.db, ids=[0x7fe])

    def test_virtual_schedule_has_exact_count_and_random_reproducible_phases(self):
        plan, _ = make_plan(self.db, seed=123, period_ms=100)
        same, _ = make_plan(self.db, seed=123, period_ms=100)
        self.assertEqual([p.phase for p in plan], [p.phase for p in same])
        normalized = [round(p.phase / p.period, 6) for p in plan]
        self.assertGreater(len(set(normalized)), 1)
        sender = VirtualAdapter()
        stats, frames = new_stats(plan), []
        transmit(plan, sender, 1, frames.append, stats, clock=sender.clock, sleeper=sender.sleep)
        self.assertEqual(stats['commands_acknowledged'], 100)
        self.assertEqual(stats['slots_skipped_late'], 0)
        self.assertEqual(len(frames), 100)

    def test_project_dbc_period_attribute_is_used(self):
        message = self.db.get_message_by_frame_id(0x009)
        plan, _ = make_plan(self.db, ids=[0x009], max_fps=2, seed=1)
        self.assertEqual(plan[0].period, 1.0)
        self.assertEqual(plan[0].period_source, 'dbc:Period')

    def test_slow_adapter_skips_instead_of_extending_schedule(self):
        class Slow(VirtualAdapter):
            def send(self, can_id, data):
                self.now += .15
        plan, _ = make_plan(self.db, ids=[0x201])
        sender = Slow()
        stats, frames = new_stats(plan), []
        transmit(plan, sender, 1, frames.append, stats, clock=sender.clock, sleeper=sender.sleep)
        self.assertGreater(stats['slots_skipped_late'], 0)
        self.assertEqual(stats['slots_skipped_late'] + stats['commands_acknowledged'], 10)
        self.assertTrue(all(f['command_started_s'] < 1 for f in frames))

    def test_initialization_and_tx_ack_with_interleaved_rx(self):
        adapter = UccbAdapter('FAKE', 1000000, serial_factory=FakeSerial)
        with patch('uccb_generator.time.sleep'):
            adapter.connect()
        port = adapter.port
        adapter.send(0x201, b'\x12\x34')
        self.assertIn(b'S8\r', port.written)
        self.assertIn(b'O\r', port.written)
        self.assertIn(b't20121234\r', port.written)
        self.assertEqual(adapter.acknowledged_frames, 1)
        self.assertEqual(adapter.received_frames, 1)
        adapter.close()
        self.assertTrue(port.closed)
        self.assertEqual(port.written[-1], b'C\r')

    def test_bell_and_timeout_are_failures_not_successful_frames(self):
        adapter = UccbAdapter('FAKE', 1000000, timeout=.005, serial_factory=FakeSerial)
        with patch('uccb_generator.time.sleep'):
            adapter.connect()
        adapter.port.reject_tx = True
        with self.assertRaises(CommandRejected):
            adapter.send(1, b'')
        self.assertEqual(adapter.acknowledged_frames, 0)
        adapter.port.silent = True
        with self.assertRaisesRegex(AdapterError, 'Brak odpowiedzi'):
            adapter.send(1, b'')
        adapter.port.silent = False
        adapter.close()

    def test_cli_dry_run_artifacts_and_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['--dry-run', '--duration', '1', '--period-ms', '100',
                    '--phase-mode', 'staggered', '--output', tmp, '--session', 'test']
            with patch('builtins.print'):
                self.assertEqual(cli.main(args), 0)
            folder = Path(tmp) / 'test'
            report = json.loads((folder / 'report.json').read_text())
            self.assertEqual(report['mode'], 'dry_run')
            self.assertEqual(report['statistics']['commands_acknowledged'], 100)
            original = (folder / 'tx.jsonl').read_bytes()
            with patch('builtins.print'), self.assertRaises(FileExistsError):
                cli.main(args)
            self.assertEqual((folder / 'tx.jsonl').read_bytes(), original)

    def test_cli_failure_and_ctrl_c_save_partial_report_and_close(self):
        for error, status, code in [(RuntimeError('disconnected'), 'failed', 1),
                                    (KeyboardInterrupt(), 'interrupted', 130)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                class FailingAdapter:
                    info = {}
                    received_frames = 0
                    closed = False
                    count = 0
                    def connect(self):
                        pass
                    def send(self, can_id, data):
                        self.count += 1
                        if self.count == 2:
                            raise error
                    def close(self):
                        self.closed = True
                adapter = FailingAdapter()
                with patch.object(cli, 'UccbAdapter', return_value=adapter), patch('builtins.print'):
                    self.assertEqual(cli.main(['--port', 'FAKE', '--duration', '1',
                                               '--output', tmp, '--session', 'partial']), code)
                report = json.loads((Path(tmp) / 'partial/report.json').read_text())
                self.assertEqual(report['status'], status)
                self.assertEqual(report['statistics']['commands_acknowledged'], 1)
                self.assertTrue(adapter.closed)


if __name__ == '__main__':
    unittest.main()
