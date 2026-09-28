#!/usr/bin/env python3
"""Generate controlled CAN anomaly scenarios and machine-readable ground truth."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from repo_paths import configure_imports
configure_imports()

from anomaly_scenarios import (build_schedule, default_suite, parse_episodes,
                               resolve_auto_targets, transmit_schedule)
from can_generate.time_reference import DEFAULT_MARKER_CAN_ID
from generate_can_traffic import (DEFAULT_DBC, can_id, positive, positive_int,
                                  write_json)
from uccb_generator import (BITRATES, HOST_BAUD, UccbAdapter, VirtualAdapter,
                            make_plan)


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--dbc', type=Path, default=DEFAULT_DBC)
    p.add_argument('--port', help='Port adaptera, np. COM4')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--bitrate', type=int, choices=sorted(BITRATES), default=1000000)
    p.add_argument('--duration', type=positive, default=120.0)
    p.add_argument('--ids', nargs='+', type=can_id)
    p.add_argument('--seed', type=int)
    p.add_argument('--max-messages', type=positive_int, default=20)
    p.add_argument('--period-ms', type=positive, help='Wymus wspolny okres zamiast okresow DBC')
    p.add_argument('--default-period-ms', type=positive, default=100.0)
    p.add_argument('--phase-mode', choices=['random', 'staggered', 'zero'], default='random')
    p.add_argument('--max-fps', type=positive, default=100.0)
    p.add_argument('--host-utilisation', type=positive, default=.4)
    p.add_argument('--signals', choices=['sine', 'constant'], default='sine')
    p.add_argument('--wave-period', type=positive, default=10.0)
    p.add_argument('--scenario', type=Path,
                   help='JSON z lista episodes; bez pliku uruchamia pakiet wszystkich anomalii')
    p.add_argument('--marker-id', type=can_id, default=DEFAULT_MARKER_CAN_ID)
    p.add_argument('--timeout', type=positive, default=1.0)
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'captures')
    p.add_argument('--session')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.dry_run and not args.port:
        raise ValueError('Podaj --port COMx albo --dry-run')
    if args.host_utilisation > .8:
        raise ValueError('--host-utilisation musi byc <= 0.8')
    if args.seed is None:
        args.seed = secrets.randbits(64)
    import cantools
    database = cantools.database.load_file(str(args.dbc))
    if any(message.frame_id == args.marker_id for message in database.messages):
        raise ValueError('--marker-id koliduje z wiadomoscia DBC')
    plan, budget = make_plan(
        database, ids=args.ids, max_messages=args.max_messages,
        period_ms=args.period_ms, max_fps=args.max_fps,
        host_fraction=args.host_utilisation, bitrate=args.bitrate,
        mode=args.signals, wave_period=args.wave_period, seed=args.seed,
        phase_mode=args.phase_mode, default_period_ms=args.default_period_ms)
    if args.scenario:
        document = json.loads(args.scenario.read_text(encoding='utf-8'))
        episodes = parse_episodes(document['episodes'] if isinstance(document, dict) else document)
        episodes = resolve_auto_targets(episodes, plan, database, args.signals, args.wave_period)
        scenario_source = str(args.scenario.resolve())
    else:
        episodes = default_suite(plan, database, duration=args.duration,
                                 mode=args.signals, wave_period=args.wave_period)
        scenario_source = 'built_in_all_v1'
    frames, truth = build_schedule(plan, episodes, args.duration,
                                   marker_id=args.marker_id, mode=args.signals,
                                   wave_period=args.wave_period)

    session = args.session or datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', session):
        raise ValueError('Niepoprawna nazwa sesji')
    directory = args.output.resolve() / session
    directory.mkdir(parents=True, exist_ok=False)
    dbc_hash = hashlib.sha256(args.dbc.read_bytes()).hexdigest()
    manifest = {
        'session_id': session, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'dry_run' if args.dry_run else 'hardware',
        'dbc': str(args.dbc.resolve()), 'dbc_sha256': dbc_hash,
        'seed': args.seed, 'scenario_source': scenario_source,
        'arguments': {key: str(value) if isinstance(value, Path) else value
                      for key, value in vars(args).items()},
        'host_baud': HOST_BAUD, 'budget': budget,
        'messages': [{'can_id': item.message.frame_id, 'name': item.message.name,
                      'dlc': item.message.length, 'period_s': item.period,
                      'period_source': item.period_source, 'phase_s': item.phase}
                     for item in plan],
        'marker': {'can_id': args.marker_id, 'start_relative_us': 0,
                   'purpose': 'alignment of generator truth with Raspberry Pi device timestamps'},
    }
    write_json(directory / 'config.json', manifest)
    truth_document = {
        'format': 'can-anomaly-ground-truth-v1', 'session_id': session,
        'dbc_sha256': dbc_hash, 'seed': args.seed,
        'time_reference': 'generator_monotonic_seconds_since_start_marker',
        'marker_can_id': args.marker_id, 'episodes': truth,
    }
    write_json(directory / 'truth.json', truth_document)
    stats = {'commands_attempted': 0, 'commands_acknowledged': 0,
             'slots_skipped_late': 0, 'max_lateness_s': 0.0,
             'per_episode': {item['episode_id']: {'acknowledged_frames': 0,
                                                  'skipped_frames': 0}
                             for item in truth}}
    report = {'session_id': session, 'status': 'starting', 'mode': manifest['mode'],
              'statistics': stats, 'adapter': {}, 'error': None}
    report_path = directory / 'report.json'
    write_json(report_path, report)
    print(f'Ziarno: {args.seed}; DBC SHA-256: {dbc_hash}')
    print(f'Plan: {len(plan)} ID, {budget["nominal_fps"]:.1f} ramek/s; '
          f'{len(episodes)} epizodow anomalii')
    for item in truth:
        print(f'  {item["episode_id"]} {item["start"]:g}-{item["end"]:g} s '
              f'{item["type"]} ID=0x{item["can_id"]:03X}')

    adapter = VirtualAdapter() if args.dry_run else UccbAdapter(args.port, args.bitrate, args.timeout)
    begun = None
    result = 0
    try:
        with (directory / 'tx.jsonl').open('x', encoding='utf-8', buffering=1) as stream:
            if not args.dry_run:
                adapter.connect()
                report['adapter'] = adapter.info
            report['status'] = 'running'
            write_json(report_path, report)
            begun = time.monotonic()

            def log(frame):
                stream.write(json.dumps(frame, separators=(',', ':')) + '\n')

            def progress(elapsed):
                stats['elapsed_s'] = elapsed
                write_json(report_path, report)

            timing = {'clock': adapter.clock, 'sleeper': adapter.sleep} if args.dry_run else {
                'clock': time.monotonic, 'sleeper': time.sleep}
            transmit_schedule(frames, adapter, args.duration, log, stats,
                              progress=progress, **timing)
            report['status'] = 'completed_with_skips' if stats['slots_skipped_late'] else 'completed'
            result = 2 if stats['slots_skipped_late'] else 0
    except KeyboardInterrupt:
        report['status'], report['error'], result = 'interrupted', 'Ctrl+C', 130
    except Exception as exc:
        report['status'], report['error'], result = 'failed', f'{type(exc).__name__}: {exc}', 1
    finally:
        if not args.dry_run:
            try:
                adapter.close()
            except Exception as exc:
                report['close_error'] = str(exc)
                report['status'], result = 'failed', 1
        if begun is not None and 'elapsed_s' not in stats and not args.dry_run:
            stats['elapsed_s'] = time.monotonic() - begun
        for item in truth_document['episodes']:
            item.update(stats['per_episode'][item['episode_id']])
        write_json(directory / 'truth.json', truth_document)
        write_json(report_path, report)
    print(f'Status: {report["status"]}; potwierdzone: {stats["commands_acknowledged"]}; '
          f'pominiete: {stats["slots_skipped_late"]}')
    print(f'Wyniki: {directory}')
    if report['error']:
        print(report['error'], file=sys.stderr)
    return result


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'BLAD: {error}', file=sys.stderr)
        raise SystemExit(1)
