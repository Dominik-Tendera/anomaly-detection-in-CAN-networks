"""Live workflow acceptance tests. No UART device is required."""
from pathlib import Path
import dataclasses
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cantools
from cantools.database.can import Database, Message, Signal
import live_detect
from rpi_receiver.live_rules import LiveRules, Settings
from tests.synthetic import TraceBuilder


def database():
    return Database(messages=[Message(0x100, 'Speed', 1, [Signal('speed', 0, 8, minimum=0, maximum=100)])])


def profile(period=100_000, std=5_000):
    return {'format': 'can-live-baseline-v1', 'ready': True, 'dbc_sha256': 'test', 'channel': 1,
            'messages': {'256': {'period_us': period, 'std_us': std, 'interval_count': 200}}}


class RulesTests(unittest.TestCase):
    def engine(self, baseline=True, **settings):
        self.events, self.quality, self.profiles = [], [], []
        return LiveRules(database(), 'test', Settings(**settings), self.events.append,
                         self.quality.append, baseline=profile() if baseline else None,
                         baseline_sink=self.profiles.append)

    def test_dbc_rules_start_without_learning_or_time_sync(self):
        engine = self.engine(False)
        b = TraceBuilder().frame(0x321, b'\x00', 1).frame(0x321, b'\x00', 2)
        b.frame(0x100, b'', 3).frame(0x100, b'\xff', 4)
        engine.feed(b.bytes())
        self.assertEqual([e['type'] for e in self.events],
                         ['unknown_id', 'dlc_mismatch', 'signal_out_of_range'])
        self.assertTrue(all(e['ts64'] is None for e in self.events))
        self.assertEqual(engine.counters['frames_without_time'], 4)

    def test_learning_uses_only_seen_ids_and_freezes(self):
        engine = self.engine(False, learn_seconds=.3, min_intervals=2)
        b = TraceBuilder().time_sync(1_000_000)
        for t in (1_000_000, 1_100_000, 1_200_000, 1_300_000, 1_500_000):
            b.frame(0x100, b'\x32', t)
        engine.feed(b.bytes())
        self.assertTrue(engine.ready)
        self.assertEqual(engine.profile[0x100]['period_us'], 100_000)
        self.assertEqual(list(engine.profile), [0x100])
        self.assertEqual(len(self.profiles), 1)
        self.assertIn('period_violation', [e['type'] for e in self.events])
        self.assertEqual(engine.snapshot()['sigma_disabled_zero_variance'], [0x100])

    def test_learning_survives_isolated_link_gap(self):
        engine = self.engine(False, learn_seconds=.3, min_intervals=2)
        b = TraceBuilder().time_sync(1_000_000).frame(0x100, b'\x32', 1_000_000)
        b.frame(0x100, b'\x32', 1_100_000).skip_seq(1).frame(0x100, b'\x32', 1_200_000)
        b.frame(0x100, b'\x32', 1_300_000)
        engine.feed(b.bytes())
        self.assertTrue(engine.ready)
        self.assertEqual(engine.learning_start, 1_000_000)
        self.assertEqual(self.quality[0]['reason'], 'sequence_gap')

    def test_missing_detected_from_diagnostics_with_no_frames(self):
        engine = self.engine()
        b = TraceBuilder().time_sync(1_000_000).frame(0x100, b'\x32', 1_000_000)
        b.bus_stats(ts64=1_400_000).bus_stats(ts64=1_500_000)
        engine.feed(b.bytes())
        missing = [e for e in self.events if e['type'] == 'missing_frame']
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['ts64'], 1_400_000)
        b2 = TraceBuilder(b.next_seq).frame(0x100, b'\x32', 1_600_000).bus_stats(ts64=2_000_000)
        engine.feed(b2.bytes())
        self.assertEqual(sum(e['type'] == 'missing_frame' for e in self.events), 2)

    def test_period_sigma_burst_and_frequency(self):
        engine = self.engine()
        b = TraceBuilder().time_sync(1_000_000)
        for t in range(1_000_000, 2_000_000, 10_000):
            b.frame(0x100, b'\x32', t)
        b.bus_stats(ts64=2_000_000).bus_stats(ts64=3_000_000)
        engine.feed(b.bytes())
        types = {e['type'] for e in self.events}
        self.assertTrue({'period_violation', 'statistical_deviation', 'burst',
                         'frequency_increase', 'frequency_decrease', 'missing_frame'} <= types)

    def test_partial_window_is_not_extrapolated_at_shutdown(self):
        engine = self.engine()
        b = TraceBuilder().time_sync(1_000_000).frame(0x100, b'\x32', 1_000_000)
        engine.feed(b.bytes())
        engine.finish()
        self.assertFalse(any(e['method'] == 'frequency' for e in self.events))

    def test_unknown_and_mismatched_dlc_rearm_after_recovery(self):
        engine = self.engine(False)
        b = TraceBuilder().time_sync(1_000_000)
        for t, data in [(1_000_000, b''), (1_100_000, b''), (1_200_000, b'\x32'), (1_300_000, b'')]:
            b.frame(0x100, data, t)
        engine.feed(b.bytes())
        self.assertEqual(sum(e['type'] == 'dlc_mismatch' for e in self.events), 2)

    def test_sequence_gap_prevents_false_interval_alarm(self):
        engine = self.engine()
        b = TraceBuilder().time_sync(1_000_000).frame(0x100, b'\x32', 1_000_000)
        b.skip_seq(2).frame(0x100, b'\x32', 1_500_000)
        engine.feed(b.bytes())
        self.assertEqual(engine.counters['records_missing_on_link'], 2)
        self.assertEqual(self.quality[0]['reason'], 'sequence_gap')
        self.assertFalse(any(e['method'] in ('period', 'three_sigma') for e in self.events))

    def test_counter_deltas_not_historical_totals_and_wrap(self):
        engine = self.engine()
        b = TraceBuilder().bus_stats(ts64=1_000_000, ring_dropped=0xffffffff, crc_errors=10)
        b.bus_stats(ts64=1_100_000, ring_dropped=0, crc_errors=11)
        engine.feed(b.bytes())
        self.assertEqual(len(self.quality), 1)
        self.assertEqual(self.quality[0]['deltas']['ring_dropped'], 1)
        self.assertEqual(self.events[0]['counter'], 'crc_errors')
        self.assertIsNone(self.events[0]['can_id'])

    def test_restart_clears_old_sync_and_does_not_create_link_gap(self):
        engine = self.engine()
        b = TraceBuilder().session().time_sync(0xfffffff0).frame(0x100, b'\x32', 0x100000020)
        b2 = TraceBuilder().session().frame(0x100, b'\x32', 100).time_sync(200)
        engine.feed(b.bytes() + b2.bytes())
        self.assertEqual(engine.counters['frames_without_time'], 1)
        self.assertEqual(engine.counters['records_missing_on_link'], 0)
        self.assertEqual(engine.session_index, 1)

    def test_device_clock_and_sequence_wrap(self):
        engine = self.engine(False)
        b = TraceBuilder(0xffff).time_sync(0xfffffff0).frame(0x321, b'\x00', 0x100000020)
        engine.feed(b.bytes())
        self.assertEqual(self.events[0]['ts64'], 0x100000020)
        self.assertEqual(engine.counters['records_missing_on_link'], 0)

    def test_frame_stamped_just_before_first_sync_keeps_its_time(self):
        engine = self.engine()
        b = TraceBuilder().time_sync(1_000_000).frame(0x100, b'\xff', 999_950)
        engine.feed(b.bytes())
        self.assertEqual(engine.last_frames[0x100], 999_950)
        self.assertEqual(self.events[0]['ts64'], 999_950)
        self.assertEqual(self.quality, [])

    def test_bad_crc_truncated_tail_and_unknown_record_are_retained_as_quality(self):
        engine = self.engine(False)
        b = TraceBuilder().time_sync(1_000_000)
        data = b.bytes()
        engine.feed(data + b'\x02\xff\x00' + b'\x02\xaa')
        engine.finish()
        self.assertIn('stream_decode_error', [q['reason'] for q in self.quality])
        self.assertIn('truncated_last_record', [q['reason'] for q in self.quality])

    def test_chunk_boundaries_do_not_change_events(self):
        b = TraceBuilder().time_sync(1_000_000)
        for t, data in [(1_000_000, b'\x32'), (1_100_000, b'\xff'), (1_500_000, b'')]:
            b.frame(0x100, data, t)
        blob = b.bytes()
        a = self.engine()
        a.feed(blob)
        a.finish()
        expected = list(self.events)
        other = self.engine()
        for byte in blob:
            other.feed(bytes([byte]))
        other.finish()
        self.assertEqual(self.events, expected)

    def test_profile_checksum_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, 'checksum'):
            LiveRules(database(), 'different', Settings(), lambda e: None, lambda q: None,
                      baseline=profile())


class WorkflowTests(unittest.TestCase):
    def args(self, directory, session, *extra):
        return live_detect.parser().parse_args(['--output', str(directory), '--session', session, *extra])

    def test_real_capture_path_writes_events_before_input_ends_then_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'live'
            b = TraceBuilder().time_sync(1_000_000).frame(0x7fd, b'\x00', 1_000_000)
            blob = b.bytes()
            def incoming():
                yield blob
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if (folder / 'events.jsonl').read_text():
                        break
                    time.sleep(.005)
                self.assertTrue((folder / 'events.jsonl').read_text())
                self.assertEqual((folder / 'trace.canbin').read_bytes(), blob)
            with patch('builtins.print'):
                self.assertEqual(live_detect.run(self.args(tmp, 'live'), incoming()), 0)
                self.assertEqual(live_detect.run(self.args(tmp, 'replay', '--replay', str(folder / 'trace.canbin'))), 0)
            self.assertEqual((folder / 'events.jsonl').read_bytes(), (Path(tmp) / 'replay/events.jsonl').read_bytes())
            replay_report = json.loads((Path(tmp) / 'replay/report.json').read_text())
            self.assertTrue(replay_report['replay_comparison']['comparable'])
            with patch('builtins.print'), self.assertRaises(FileExistsError):
                live_detect.run(self.args(tmp, 'live'), iter([blob]))

    def test_learned_profile_can_be_used_for_next_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = TraceBuilder().time_sync(1_000_000)
            for index in range(602):
                b.frame(0x201, bytes(8), 1_000_000 + index * 100_000)
            with patch('builtins.print'):
                self.assertEqual(live_detect.run(self.args(tmp, 'reference'), [b.bytes()]), 0)
            baseline = Path(tmp) / 'reference/baseline.json'
            learned = json.loads(baseline.read_text())
            self.assertTrue(learned['ready'])
            self.assertEqual(learned['messages']['513']['period_us'], 100_000)
            test = TraceBuilder().time_sync(1_000_000).frame(0x201, bytes(8), 1_000_000)
            test.frame(0x201, bytes(8), 1_050_000).bus_stats(ts64=1_500_000)
            with patch('builtins.print'):
                self.assertEqual(live_detect.run(self.args(tmp, 'test', '--baseline', str(baseline)), [test.bytes()]), 0)
                self.assertEqual(live_detect.run(self.args(tmp, 'repeat', '--baseline', str(baseline),
                    '--replay', str(Path(tmp) / 'test/trace.canbin'))), 0)
            events = (Path(tmp) / 'test/events.jsonl').read_text()
            self.assertIn('period_violation', events)
            self.assertIn('missing_frame', events)
            self.assertEqual(events, (Path(tmp) / 'repeat/events.jsonl').read_text())

    def test_failed_raw_write_is_not_delivered_to_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = TraceBuilder().time_sync(100).bytes()
            real_open = Path.open
            class FailedFile:
                def write(self, data):
                    raise OSError('disk full')
                def close(self):
                    pass
            def patched_open(path, *args, **kwargs):
                return FailedFile() if path.name == 'trace.canbin' else real_open(path, *args, **kwargs)
            with patch.object(Path, 'open', patched_open), patch('builtins.print'):
                self.assertEqual(live_detect.run(self.args(tmp, 'diskfailure'), [blob]), 1)
            report = json.loads((Path(tmp) / 'diskfailure/report.json').read_text())
            self.assertIn('disk full', report['error'])
            self.assertEqual(report['bytes_received'], 0)
            self.assertEqual(report['analysis']['counters'].get('records', 0), 0)

    def test_actual_serial_entry_reads_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = TraceBuilder().time_sync(1_000_000).frame(0x7fd, b'\x01', 1_000_000).bytes()
            class Serial:
                in_waiting = len(blob)
                closed = False
                read_count = 0
                def read(self, size):
                    self.read_count += 1
                    if self.read_count == 1:
                        return blob
                    raise OSError('UART disconnected')
                def close(self):
                    self.closed = True
            serial = Serial()
            with patch('serial.Serial', return_value=serial), patch('builtins.print'):
                code = live_detect.run(self.args(tmp, 'serial'))
            report = json.loads((Path(tmp) / 'serial/report.json').read_text())
            self.assertEqual(code, 1)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['analysis']['counters']['frames'], 1)
            self.assertTrue(serial.closed)
            self.assertEqual((Path(tmp) / 'serial/trace.canbin').read_bytes(), blob)

    def test_queue_overflow_keeps_complete_raw_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = threading.Event()
            entered = threading.Event()
            original_feed = LiveRules.feed
            blob = TraceBuilder().time_sync(1_000_000).bytes()
            def slow(engine, chunk):
                entered.set()
                if not gate.wait(timeout=2):
                    raise RuntimeError('test timeout')
                original_feed(engine, chunk)
            def incoming():
                yield blob
                self.assertTrue(entered.wait(timeout=2))
                yield blob
                yield blob  # queue capacity=1, analysis blocked, overload certain
                gate.set()
            with patch.object(LiveRules, 'feed', slow), patch('builtins.print'):
                code = live_detect.run(self.args(tmp, 'overload', '--queue-chunks', '1'), incoming())
            report = json.loads((Path(tmp) / 'overload/report.json').read_text())
            self.assertEqual(code, 2)
            self.assertTrue(report['analysis_queue_overflow'])
            self.assertFalse(report['analysis_complete'])
            self.assertEqual((Path(tmp) / 'overload/trace.canbin').read_bytes(), blob * 3)

    def test_detector_failure_preserves_raw_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = TraceBuilder().time_sync(100).bytes()
            with patch.object(LiveRules, 'feed', side_effect=RuntimeError('detector failure')), patch('builtins.print'):
                code = live_detect.run(self.args(tmp, 'failure'), iter([blob, blob]))
            self.assertEqual(code, 2)
            report = json.loads((Path(tmp) / 'failure/report.json').read_text())
            self.assertIn('detector failure', report['analysis_error'])
            self.assertEqual((Path(tmp) / 'failure/trace.canbin').read_bytes(), blob * 2)

    def test_keyboard_interrupt_closes_and_writes_partial_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            blob = TraceBuilder().time_sync(100).bytes()
            def incoming():
                yield blob
                raise KeyboardInterrupt()
            with patch('builtins.print'):
                code = live_detect.run(self.args(tmp, 'interrupt'), incoming())
            self.assertEqual(code, 130)
            report = json.loads((Path(tmp) / 'interrupt/report.json').read_text())
            self.assertEqual(report['status'], 'interrupted')
            self.assertEqual(report['bytes_received'], len(blob))


if __name__ == '__main__':
    unittest.main()
