#!/usr/bin/env python3
"""Receive STM32 UART records, detect CAN anomalies live and persist results."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import queue
import re
import signal
import sys
import threading
import time

import cantools
from rpi_receiver.live_rules import LiveRules, Settings, validate_profile

DEFAULT_DBC = Path(__file__).resolve().parent.parent / 'CAN_PARSER/RTE_3.5_CAN1_CAR.dbc'


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def rss_bytes():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == 'darwin' else value * 1024)
    except ImportError:
        return None  # Do not label Python allocations as resident memory.


class AnalysisWorker:
    """A bounded chunk queue. A failed/overloaded consumer never blocks UART."""
    def __init__(self, engine, capacity=128):
        self.engine = engine
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self.run, name='can-analysis', daemon=True)
        self.error = None
        self.overflow = False
        self.peak = 0
        self.processing_ns = 0
        self.chunks = 0
        self.bytes_processed = 0
        self.last_snapshot = engine.snapshot()
        self.started = time.monotonic()
        self.thread.start()

    def submit(self, chunk, replay=False):
        if self.failed.is_set() or self.overflow:
            return False
        while True:
            try:
                self.queue.put(chunk, timeout=.05 if replay else 0)
                self.peak = max(self.peak, self.queue.qsize())
                return True
            except queue.Full:
                if not replay:
                    self.overflow = True
                    return False
                if self.failed.is_set():
                    return False

    def run(self):
        try:
            last_snapshot = time.monotonic()
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    chunk = self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    start = time.perf_counter_ns()
                    self.engine.feed(chunk)
                    self.processing_ns += time.perf_counter_ns() - start
                    self.chunks += 1
                    self.bytes_processed += len(chunk)
                    if time.monotonic() - last_snapshot >= 1:
                        self.last_snapshot = self.engine.snapshot()
                        last_snapshot = time.monotonic()
                finally:
                    self.queue.task_done()
            self.engine.finish()
            self.last_snapshot = self.engine.snapshot()
        except BaseException as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            self.failed.set()
            self.last_snapshot = self.engine.snapshot()

    def finish(self):
        self.stop.set()
        # Non-daemon ownership is held by the caller until all accepted input
        # and output have finished. No close while this worker is still writing.
        while self.thread.is_alive():
            self.thread.join(timeout=.25)

    def snapshot(self):
        analysis = self.last_snapshot
        records = analysis.get('counters', {}).get('records', 0)
        seconds = self.processing_ns / 1e9
        return {'analysis': analysis, 'analysis_error': self.error,
                'analysis_queue_overflow': self.overflow,
                'analysis_complete': not self.failed.is_set() and not self.overflow,
                'performance': {'analysis_processing_s': seconds,
                                'analysis_records_per_processing_second': records / seconds if seconds else None,
                                'analysis_us_per_record': seconds * 1e6 / records if records else None,
                                'analysis_bytes_processed': self.bytes_processed,
                                'queue_chunks': self.queue.qsize(), 'queue_peak_chunks': self.peak,
                                'queue_capacity_chunks': self.queue.maxsize,
                                'peak_resident_memory_bytes': rss_bytes()}}


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return result


def nonnegative(value):
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError('must be finite and >= 0')
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--port', default='/dev/ttyAMA0')
    p.add_argument('--baud', type=int, default=2_000_000)
    p.add_argument('--seconds', type=nonnegative, default=0, help='0 = until Ctrl+C')
    p.add_argument('--dbc', type=Path, default=DEFAULT_DBC)
    p.add_argument('--channel', type=int, choices=(1, 2), default=1)
    p.add_argument('--baseline', type=Path, help='Completed baseline.json from an earlier reference capture')
    p.add_argument('--learn-seconds', type=positive, default=60.0)
    p.add_argument('--min-intervals', type=int, default=100)
    p.add_argument('--window-seconds', type=positive, default=1.0)
    p.add_argument('--missing-multiplier', type=positive, default=3.0)
    p.add_argument('--period-tolerance', type=positive, default=.30)
    p.add_argument('--frequency-tolerance', type=positive, default=.50)
    p.add_argument('--sigma', type=positive, default=3.0)
    p.add_argument('--burst-factor', type=positive, default=.25)
    p.add_argument('--burst-frames', type=int, default=4)
    p.add_argument('--ignore-id', action='append', type=lambda s: int(s, 0), default=None)
    p.add_argument('--queue-chunks', type=int, default=128, help='Each chunk <=4096 bytes')
    p.add_argument('--output', type=Path, default=Path('captures_live'))
    p.add_argument('--session', help='New subdirectory name, never overwritten')
    p.add_argument('--replay', type=Path, help='Analyze a raw .canbin using exactly the same rule engine')
    return p


def run(args, source=None):
    """source is an optional byte-chunk iterator for hardware-free acceptance tests."""
    settings = Settings(channel=args.channel, learn_seconds=args.learn_seconds,
                        min_intervals=args.min_intervals, window_seconds=args.window_seconds,
                        missing_multiplier=args.missing_multiplier, period_tolerance=args.period_tolerance,
                        frequency_tolerance=args.frequency_tolerance, sigma_multiplier=args.sigma,
                        burst_factor=args.burst_factor, burst_frames=args.burst_frames,
                        ignore_ids=tuple(args.ignore_id if args.ignore_id is not None else [0x7FE]))
    settings.validate()
    if args.queue_chunks < 1 or args.queue_chunks > 16384 or args.baud <= 0:
        raise ValueError('queue-chunks must be 1..16384 and baud must be positive')
    dbc_hash = checksum(args.dbc)
    database = cantools.database.load_file(str(args.dbc))
    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        validate_profile(baseline, dbc_hash, settings.channel)
    if args.replay:
        with args.replay.open('rb') as check:
            if check.read(8).startswith(b'CANTRACE'):
                raise ValueError('Expected raw UART .canbin, not a header-wrapped trace')
    session = args.session or datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', session):
        raise ValueError('Invalid session name')
    directory = args.output.resolve() / session
    directory.mkdir(parents=True, exist_ok=False)
    mode = 'replay' if args.replay else ('test_stream' if source is not None else 'live')
    manifest = {'session_id': session, 'mode': mode, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                'settings': asdict(settings), 'dbc_sha256': dbc_hash,
                'baseline_sha256': checksum(args.baseline) if args.baseline else None,
                'cantools_version': cantools.__version__, 'python': platform.python_version(),
                'platform': platform.platform(), 'machine': platform.machine(),
                'code_sha256': {p.name: checksum(p) for p in [Path(__file__),
                    Path(__file__).parent / 'rpi_receiver/live_rules.py',
                    Path(__file__).parent / 'rpi_receiver/can_stream_protocol.py']}}
    comparison = {'comparable': None, 'mismatches': [], 'reason': 'no source manifest'}
    source_report = None
    if args.replay:
        source_manifest = args.replay.parent / 'config.json'
        if source_manifest.is_file():
            original = json.loads(source_manifest.read_text(encoding='utf-8'))
            for key in ('settings', 'dbc_sha256', 'baseline_sha256', 'code_sha256'):
                # Normalize tuples to their JSON list representation.
                current = json.loads(json.dumps(manifest[key]))
                if current != original.get(key):
                    comparison['mismatches'].append(key)
            comparison.update(comparable=not comparison['mismatches'], reason='source manifest checked')
        if (args.replay.parent / 'report.json').is_file():
            source_report = json.loads((args.replay.parent / 'report.json').read_text(encoding='utf-8'))
    save_json(directory / 'config.json', manifest)
    if baseline is not None:
        save_json(directory / 'baseline.json', baseline)
    stop = threading.Event()
    previous_signals = {}
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGINT', 'SIGTERM'):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous_signals[signum] = signal.signal(signum, lambda *unused: stop.set())
    started = time.monotonic()
    last_input = started
    bytes_received = 0
    raw_hash = hashlib.sha256()
    report = {'session_id': session, 'mode': mode, 'status': 'starting', 'error': None,
              'replay_comparison': comparison if args.replay else None,
              'files': {'trace': str(args.replay.resolve()) if args.replay else str(directory / 'trace.canbin'),
                        'events': str(directory / 'events.jsonl'), 'quality': str(directory / 'quality.jsonl')}}
    report['trace_recording'] = {
        'enabled': not bool(args.replay),
        'path': None if args.replay else str(directory / 'trace.canbin'),
        'bytes_written': 0,
    }
    worker = port = raw = events = quality = None
    exit_code = 0
    warned = False

    def write_report():
        report.update({'bytes_received': bytes_received, 'elapsed_s': time.monotonic() - started,
                       'input_idle_s': time.monotonic() - last_input})
        if worker:
            report.update(worker.snapshot())
            processing = report['performance']['analysis_processing_s']
            report['performance']['processing_fraction_of_capture_time'] = processing / max(report['elapsed_s'], 1e-9)
        save_json(directory / 'report.json', report)

    try:
        events = (directory / 'events.jsonl').open('x', encoding='utf-8', buffering=1)
        quality = (directory / 'quality.jsonl').open('x', encoding='utf-8', buffering=1)
        def event_sink(event):
            events.write(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
        def quality_sink(interval):
            quality.write(json.dumps(interval, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
        engine = LiveRules(database, dbc_hash, settings, event_sink, quality_sink,
                           baseline=baseline, baseline_sink=lambda value: save_json(directory / 'baseline.json', value))
        worker = AnalysisWorker(engine, args.queue_chunks)
        if args.replay:
            port = args.replay.open('rb')
            chunks = iter(lambda: port.read(4096), b'')
        elif source is not None:
            raw = (directory / 'trace.canbin').open('xb')
            chunks = iter(source)
        else:
            import serial
            raw = (directory / 'trace.canbin').open('xb')
            port = serial.Serial(args.port, args.baud, timeout=.05)
            # Unlike read(65536), this returns promptly when even one byte arrives.
            def uart_chunks():
                while not stop.is_set():
                    yield port.read(min(4096, max(1, port.in_waiting)))
            chunks = uart_chunks()
        report['status'] = 'running'
        write_report()
        print(f'{mode}: CAN{args.channel}; output: {directory}', flush=True)
        print('DBC rules active now. Timing: ' + ('loaded baseline.' if baseline else
              f'learning {args.learn_seconds:g} s of normal traffic, then frozen baseline.'), flush=True)
        last_report = time.monotonic()
        prior_baseline_state = 'ready' if baseline else 'learning'
        for chunk in chunks:
            if stop.is_set():
                break
            if chunk:
                # Tests may supply larger chunks; keep the memory limit valid.
                for offset in range(0, len(chunk), 4096):
                    part = chunk[offset:offset + 4096]
                    if raw is not None:
                        written = raw.write(part)
                        raw.flush()  # Persist the input before analysis handoff.
                        if written != len(part):
                            raise OSError('Short raw trace write')
                        report['trace_recording']['bytes_written'] += written
                    raw_hash.update(part)
                    bytes_received += len(part)
                    last_input = time.monotonic()
                    accepted = worker.submit(part, replay=bool(args.replay))
                    if not accepted and not warned:
                        print('Analysis stopped or overloaded. Raw capture continues; replay is required.', flush=True)
                        warned = True
            now = time.monotonic()
            if now - last_report >= 1:
                write_report()
                current = worker.last_snapshot['baseline_state']
                if current != prior_baseline_state:
                    print('Baseline ready; timing rules active. Saved baseline.json.', flush=True)
                    prior_baseline_state = current
                last_report = now
            if args.seconds and now - started >= args.seconds:
                break
        report['status'] = 'interrupted' if stop.is_set() else 'completed'
        exit_code = 130 if stop.is_set() else 0
    except KeyboardInterrupt:
        report['status'], exit_code = 'interrupted', 130
    except Exception as exc:
        report['status'], exit_code = 'failed', 1
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            for stream in (port, raw):
                if stream is not None:
                    try:
                        if stream is raw:
                            stream.flush()
                            os.fsync(stream.fileno())
                        stream.close()
                    except Exception as exc:
                        report['status'], exit_code = 'failed', 1
                        report['close_error'] = f'{type(exc).__name__}: {exc}'
            if worker is not None:
                worker.finish()
                if (worker.failed.is_set() or worker.overflow) and exit_code == 0:
                    report['status'], exit_code = 'analysis_incomplete', 2
            for stream in (events, quality):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception as exc:
                        report['status'], exit_code = 'failed', 1
                        report['close_error'] = f'{type(exc).__name__}: {exc}'
            report['trace_sha256'] = raw_hash.hexdigest()
            if source_report and source_report.get('trace_sha256') != report['trace_sha256']:
                comparison['mismatches'].append('trace_sha256')
                comparison['comparable'] = False
            if source_report and source_report.get('analysis_complete') is False:
                comparison['comparable'] = False
                comparison['mismatches'].append('source_analysis_incomplete')
            write_report()
        finally:
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)
    print(f"Status: {report['status']}; received {bytes_received} bytes; report: {directory / 'report.json'}")
    if report['error']:
        print(report['error'], file=sys.stderr)
    return exit_code


def main(argv=None):
    return run(parser().parse_args(argv))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
