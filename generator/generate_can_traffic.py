#!/usr/bin/env python3
"""Generate normal DBC-based traffic through the uCCB USB-CAN adapter."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from repo_paths import configure_imports
configure_imports()

from uccb_generator import (BITRATES, HOST_BAUD, UccbAdapter, VirtualAdapter,
                            make_payload, make_plan, message_period_ms, new_stats,
                            transmit)

DEFAULT_DBC = Path(__file__).resolve().parent / 'dbc/RTE_3.5_CAN1_CAR.dbc'


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Wymagana skonczona liczba wieksza od zera')
    return number


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Wymagana liczba calkowita >= 1')
    return number


def can_id(value):
    number = int(value, 16 if value.lower().startswith('0x') else 10)
    if not 0 <= number <= 0x7ff:
        raise argparse.ArgumentTypeError('ID musi byc w zakresie 0..0x7FF')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument('--dbc', type=Path, default=DEFAULT_DBC)
    p.add_argument('--port', help='Port adaptera, np. COM4; Viewer musi zwolnic ten port')
    p.add_argument('--list-ports', action='store_true')
    p.add_argument('--list-messages', action='store_true', help='Wypisz ID, nazwy i DLC z DBC')
    p.add_argument('--dry-run', action='store_true', help='Wirtualna sesja bez portu i bez czekania')
    p.add_argument('--bitrate', type=int, choices=sorted(BITRATES), default=1000000)
    p.add_argument('--duration', type=positive, default=60.0, help='Czas ruchu w sekundach')
    p.add_argument('--ids', nargs='+', type=can_id, help='Jawny zestaw ID, np. 0x200 0x201; bez tego dobor automatyczny')
    p.add_argument('--seed', type=int, help='Ziarno wyboru ID; bez tego nowy losowy zestaw w kazdej sesji')
    p.add_argument('--max-messages', type=positive_int, default=20, help='Limit automatycznie wybranych wiadomosci')
    p.add_argument('--period-ms', type=positive, help='Wspolny okres; bez tego okres DBC albo 100 ms')
    p.add_argument('--default-period-ms', type=positive, default=100.0,
                   help='Okres awaryjny tylko dla wiadomosci bez okresu w DBC')
    p.add_argument('--phase-mode', choices=['random', 'staggered', 'zero'], default='random',
                   help='Rozklad pierwszych ramek; random usuwa sztuczne rowne odstepy')
    p.add_argument('--max-fps', type=positive, default=100.0, help='Budzet lacznej liczby ramek/s')
    p.add_argument('--host-utilisation', type=positive, default=0.4, help='Budzet lacza hosta (0..0.8)')
    p.add_argument('--signals', choices=['sine', 'constant'], default='sine')
    p.add_argument('--wave-period', type=positive, default=10.0, help='Okres zmian sygnalow w sekundach')
    p.add_argument('--timeout', type=positive, default=1.0, help='Limit odpowiedzi adaptera w sekundach')
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'captures')
    p.add_argument('--session', help='Nazwa nowego podkatalogu wynikow; domyslnie data i czas')
    return p


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.host_utilisation > 0.8:
        p.error('--host-utilisation musi byc <= 0.8')
    if args.list_ports:
        from serial.tools.list_ports import comports
        ports = list(comports())
        for port in ports:
            print(f'{port.device}: {port.description}')
        if not ports:
            print('Nie znaleziono portow szeregowych')
        return 0
    import cantools
    database = cantools.database.load_file(str(args.dbc))
    if args.list_messages:
        for msg in sorted(database.messages, key=lambda m: m.frame_id):
            kind = 'extended/FD' if msg.is_extended_frame or msg.is_fd else 'standard'
            period, source = message_period_ms(msg, default_ms=args.default_period_ms)
            print(f'0x{msg.frame_id:03X}  DLC={msg.length}  {msg.name}  {kind}  '
                  f'okres={period:g} ms ({source})')
        return 0
    if not args.dry_run and not args.port:
        p.error('Podaj --port COMx albo --dry-run')
    if args.seed is None:
        args.seed = secrets.randbits(64)
    plan, budget = make_plan(database, ids=args.ids, max_messages=args.max_messages,
                             period_ms=args.period_ms, max_fps=args.max_fps,
                             host_fraction=args.host_utilisation, bitrate=args.bitrate,
                             mode=args.signals, wave_period=args.wave_period, seed=args.seed,
                             phase_mode=args.phase_mode,
                             default_period_ms=args.default_period_ms)
    print(f'Ziarno wyboru ID: {args.seed} (zachowaj je dla sesji odniesienia i testowej)')
    print(f'Wybrano {len(plan)} / {len(database.messages)} wiadomosci; '
          f'{budget["nominal_fps"]:.1f} ramek/s; '
          f'lacze hosta {budget["host_utilisation"]:.1%}')
    for item in plan:
        print(f'  0x{item.message.frame_id:03X} {item.message.name}: '
              f'DLC={item.message.length}, okres={item.period * 1000:g} ms')
    session = args.session or datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', session):
        raise ValueError('Niepoprawna nazwa sesji')
    directory = args.output.resolve() / session
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        'session_id': session, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'mode': 'dry_run' if args.dry_run else 'hardware',
        'dbc': str(args.dbc.resolve()), 'dbc_sha256': hashlib.sha256(args.dbc.read_bytes()).hexdigest(),
        'cantools_version': cantools.__version__,
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'engine_sha256': hashlib.sha256(Path(__file__).with_name('uccb_generator.py').read_bytes()).hexdigest(),
        'arguments': {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        'host_baud': HOST_BAUD, 'budget': budget,
        'messages': [{'can_id': x.message.frame_id, 'name': x.message.name,
                      'dlc': x.message.length, 'period_s': x.period, 'phase_s': x.phase,
                      'period_source': x.period_source,
                      'initial_data': make_payload(x.message, x.phase, args.signals, args.wave_period).hex()}
                     for x in plan],
        'timestamp_note': 'Czasy hosta. Potwierdzenie SLCAN nie jest pomiarem czasu ramki na CAN.',
        'traffic_note': 'Syntetyczny ruch referencyjny; brak modelu zachowania ECU, licznikow i checksum aplikacyjnych.',
    }
    write_json(directory / 'config.json', manifest)
    stats = new_stats(plan)
    report = {'session_id': session, 'mode': manifest['mode'], 'status': 'starting',
              'statistics': stats, 'adapter': {}, 'error': None,
              'timestamp_note': manifest['timestamp_note']}
    report_path = directory / 'report.json'
    write_json(report_path, report)
    adapter = VirtualAdapter() if args.dry_run else UccbAdapter(args.port, args.bitrate, args.timeout)
    begun = None
    result = 0
    try:
        with (directory / 'tx.jsonl').open('x', encoding='utf-8', buffering=1) as stream:
            if not args.dry_run:
                print(f'Otwieram {args.port}, CAN {args.bitrate} bit/s. Ctrl+C konczy sesje.')
                adapter.connect()
                report['adapter'] = adapter.info
            report['status'] = 'running'
            write_json(report_path, report)
            begun = time.monotonic()

            def log(frame):
                frame['mode'] = manifest['mode']
                stream.write(json.dumps(frame, separators=(',', ':')) + '\n')

            def progress(elapsed):
                stats['elapsed_s'] = elapsed
                write_json(report_path, report)

            timing = {'clock': adapter.clock, 'sleeper': adapter.sleep} if args.dry_run else {}
            transmit(plan, adapter, args.duration, log, stats, args.signals,
                     args.wave_period, progress=progress, **timing)
            report['status'] = 'completed_with_skips' if stats['slots_skipped_late'] else 'completed'
            if stats['slots_skipped_late']:
                result = 2
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        report['error'] = 'Ctrl+C'
        result = 130
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        result = 1
    finally:
        if not args.dry_run:
            try:
                adapter.close()
            except Exception as exc:
                report['close_error'] = str(exc)
                if result == 0:
                    report['status'], result = 'failed', 1
            report['received_frames_while_waiting'] = adapter.received_frames
        if begun is not None and report['status'] in ('failed', 'interrupted') and not args.dry_run:
            stats['elapsed_s'] = time.monotonic() - begun
        elapsed = stats.get('elapsed_s', 0)
        stats['acknowledged_fps'] = stats['commands_acknowledged'] / elapsed if elapsed else 0
        write_json(report_path, report)
    counter_label = 'symulowane komendy' if args.dry_run else 'potwierdzone komendy'
    print(f'Status: {report["status"]}; {counter_label}: {stats["commands_acknowledged"]}; '
          f'pominiete terminy: {stats["slots_skipped_late"]}')
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
