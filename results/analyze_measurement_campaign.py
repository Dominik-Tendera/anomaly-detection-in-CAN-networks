"""Read-only audit of the September CAN campaign; writes derived results only.

RX traces are aligned by independent CAN start/end markers and checked against
acknowledged TX payloads and order. Metrics refer to NEW event onsets unless
explicitly labelled otherwise. No original capture file is modified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from repo_paths import configure_imports
configure_imports()

from campaign_trace_audit import (locate_trace, decode_trace, align_markers,
                                  match_frames, replay_events, baseline_subsequence)

ROOT = Path(__file__).resolve().parents[1]
METHODS = ['dbc', 'timeout', 'period', 'frequency', 'three_sigma', 'burst', 'controller']
PRIMARY = {'frequency_increase': 'frequency_increase',
           'frequency_decrease': 'frequency_decrease', 'disappearance': 'missing_frame',
           'burst': 'burst', 'unknown_dbc_id': 'unknown_id',
           'dlc_mismatch': 'dlc_mismatch', 'out_of_range_signal': 'signal_out_of_range'}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()] if path.exists() else []


def digest(path, lf=False):
    data = path.read_bytes()
    return hashlib.sha256(data.replace(b'\r\n', b'\n') if lf else data).hexdigest()


def write_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                         for k, v in row.items()} for row in rows)


def available_seconds(start, end, exclusions):
    intervals = sorted((max(start,a), min(end,b)) for a,b in exclusions if b > start and a < end)
    covered, current = 0.0, start
    for a,b in intervals:
        if b > max(a,current): covered += b-max(a,current)
        current = max(current,b)
    return max(0,end-start-covered)


def dbc_transition_model(tx, database):
    """Predicted DBC event onsets from acknowledged TX, assuming complete RX.

    Mirrors state clearing (only valid DLC can clear a signal range condition).
    It intentionally does not simulate any unobserved RX loss or quality reset.
    """
    active = set()
    counts = Counter()
    last_good = {}
    at_start = {}
    for frame in tx:
        episode = frame.get('episode_id')
        mid = frame['can_id']
        if episode and episode not in at_start:
            at_start[episode] = {'dlc_active': ('dlc', mid) in active,
                                 'range_active': any(k[0] == 'range' and k[1] == mid for k in active),
                                 'last_good_dlc_s': last_good.get(('dlc', mid)),
                                 'last_good_range_s': last_good.get(('range', mid))}
        try:
            message = database.get_message_by_frame_id(mid)
        except KeyError:
            continue
        checks = [(('dlc', mid), frame['dlc'] != message.length)]
        if frame['dlc'] == message.length:
            signals = message.decode(bytes.fromhex(frame['data']), decode_choices=False)
            for signal in message.signals:
                if signal.name not in signals or signal.minimum is None or signal.maximum is None or signal.minimum == signal.maximum:
                    continue
                checks.append((('range', mid, signal.name), not signal.minimum - 1e-9 <= signals[signal.name] <= signal.maximum + 1e-9))
        for key, bad in checks:
            if bad:
                if key not in active:
                    counts[(episode, key[0])] += 1
                active.add(key)
            else:
                active.discard(key)
                last_good[(key[0], mid)] = frame['scheduled_s']
    return counts, at_start


def alignment(episodes, tx, events):
    unknown = [e for e in episodes if e['type'] == 'unknown_dbc_id']
    anchors = []
    for index, ep in enumerate(unknown):
        observed = [e for e in events if e['type'] == 'unknown_id' and e['can_id'] == ep['can_id']]
        injected = [f for f in tx if f.get('episode_id') == ep['episode_id']]
        if len(observed) <= index or not injected:
            raise ValueError('Missing unknown-ID anchor; do not silently guess a time origin')
        event = observed[index]
        command = injected[0]['command_started_s']
        anchors.append({'episode_id': ep['episode_id'], 'rx_event_s': event['ts64'] / 1e6,
                        'tx_command_s': command, 'offset_s': event['ts64'] / 1e6 - command})
    if not anchors:
        raise ValueError('No alignment anchors')
    offset = statistics.median(a['offset_s'] for a in anchors)
    spread = max(a['offset_s'] for a in anchors) - min(a['offset_s'] for a in anchors)
    if spread > .1:
        raise ValueError(f'Inconsistent unknown-ID anchors: {spread:g}s')
    return offset, anchors, spread


def analyze(root, output):
    import cantools
    database = cantools.database.load_file(str(root / 'generator/dbc/RTE_3.5_CAN1_CAR.dbc'))
    output.mkdir(parents=True, exist_ok=True)
    rxroot = root / 'raspberry_pi/captures'
    genroot = root / 'generator/captures'
    sessions, episode_rows, event_rows, quality_rows, baseline_rows = [], [], [], [], []
    trace_rows, loss_rows, timing_rows = [], [], []
    profile_rows, validation, source_manifest, baseline_sources = [], [], [], []
    for folder in genroot.iterdir():
        c = read_json(folder / 'config.json')
        if c and 'scenario_source' not in c and c.get('mode') == 'hardware':
            baseline_sources.append((folder, c))
    for folder in sorted(rxroot.glob('baseline*')):
        report, config, profile = [read_json(folder / n) for n in ('report.json', 'config.json', 'baseline.json')]
        analysis = report.get('analysis', {})
        events = read_lines(folder / 'events.jsonl')
        seed = int(folder.name.split('seed')[-1])
        if not config or not profile:
            trace_path = locate_trace(folder)
            tr = decode_trace(trace_path) if trace_path else {}
            baseline_rows.append({'session': folder.name, 'seed': seed, 'status': 'trace_only_no_manifests',
                                  'trace_available': trace_path is not None, 'frames_rx': len(tr.get('frames', [])),
                                  'ready': None, 'decoder': tr.get('decoder')})
            if trace_path:
                source_manifest.append({'path': trace_path.relative_to(root).as_posix(), 'bytes': trace_path.stat().st_size, 'sha256': digest(trace_path)})
            continue
        candidates = [(p, c) for p, c in baseline_sources if c.get('seed', c.get('arguments', {}).get('seed')) == seed
                      and {str(m['can_id']) for m in c.get('messages', [])} == set(profile.get('messages', {}))]
        # Prefer explicit baseline names, then the nearest timestamp. Never use another seed.
        candidates.sort(key=lambda pc: (not pc[0].name.startswith('baseline'),
                                       abs(__import__('datetime').datetime.fromisoformat(pc[1]['created_utc']).timestamp() -
                                           __import__('datetime').datetime.fromisoformat(config['created_utc']).timestamp())))
        pair = candidates[0] if candidates else None
        genfolder, genc = pair if pair else (None, {})
        genr = read_json(genfolder / 'report.json') if genfolder else {}
        profile_end = profile.get('learning_end_us')
        row = {'session': folder.name, 'seed': seed, 'generator_session': genfolder.name if genfolder else None,
               'ready': profile.get('ready'), 'ids': len(profile.get('messages', {})),
               'periodic_ids': len(analysis.get('periodic_ids', [])),
               'learning_s': ((profile_end - profile['learning_start_us']) / 1e6) if profile_end and profile.get('learning_start_us') else None,
               'post_learning_device_s': ((analysis['last_device_time_us'] - profile_end) / 1e6) if profile_end else None,
               'frames_rx': analysis.get('counters', {}).get('frames'),
               'commands_ack': genr.get('statistics', {}).get('commands_acknowledged'),
               'generator_status': genr.get('status'), 'crc_errors': analysis.get('decoder', {}).get('crc_errors'),
               'cobs_errors': analysis.get('decoder', {}).get('cobs_errors'),
               'quality_entries': len(read_lines(folder / 'quality.jsonl')),
               'events': len(events), 'events_by_method': dict(Counter(e['method'] for e in events)),
               'trace_available': (folder / 'trace.canbin').exists()}
        if row['commands_ack'] is not None:
            row['rx_minus_ack'] = row['frames_rx'] - row['commands_ack']
        trace_path = locate_trace(folder)
        if trace_path:
            tr = decode_trace(trace_path)
            row['trace_sha_matches_report'] = tr['sha256'] == report['trace_sha256']
            reproduced, q, profiles, snap = replay_events(trace_path, config, database)
            row['replay_events_equal'] = reproduced == events
            row['replay_profile_equal'] = bool(profiles) and profiles[-1] == profile
            row['trace_frame_count_matches'] = len(tr['frames']) == row['frames_rx']
            last_frame = max(f['ts_us'] for f in tr['frames'] if f['ts_us'] is not None)
            row['post_learning_with_traffic_s'] = max(0, (last_frame - profile_end) / 1e6)
            row['events_after_last_frame'] = sum(e['ts64'] > last_frame for e in events)
            row['events_after_learning_before_last_frame'] = sum(profile_end <= e['ts64'] <= last_frame for e in events)
            if genfolder:
                row.update(baseline_subsequence(tr, read_lines(genfolder / 'tx.jsonl')))
            for key in ('trace_sha_matches_report', 'replay_events_equal', 'replay_profile_equal', 'trace_frame_count_matches'):
                validation.append({'session': folder.name, 'check': key, 'passed': row[key]})
        baseline_rows.append(row)
        print('Baseline:', folder.name, flush=True)
        for mid, p in profile.get('messages', {}).items():
            gp = next((m['period_s'] for m in genc.get('messages', []) if m['can_id'] == int(mid)), None)
            profile_rows.append({'baseline': folder.name, 'seed': seed, 'can_id': int(mid), **p,
                                 'generator_period_s': gp,
                                 'period_error_pct': (p['period_us'] / (gp * 1e6) - 1) * 100 if gp and p.get('period_us') else None})
        for file in sorted(folder.iterdir()):
            if file.is_file(): source_manifest.append({'path': file.relative_to(root).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)})
        if genfolder:
            for file in sorted(genfolder.iterdir()):
                if file.is_file() and file.suffix in ('.json', '.jsonl'):
                    source_manifest.append({'path': file.relative_to(root).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)})

    folders = sorted([p for p in rxroot.iterdir() if p.is_dir() and
                      (p.name.startswith('anomaly_seed') and '_matrix_' in p.name and (p / 'config.json').exists()
                       or p.name == 'anomalies_smoke_03')])
    for folder in folders:
        genfolder = genroot / folder.name.replace('anomaly_', 'anomalies_')
        config, report, profile = [read_json(folder / n) for n in ('config.json', 'report.json', 'baseline.json')]
        gc, gr, truth = [read_json(genfolder / n) for n in ('config.json', 'report.json', 'truth.json')]
        if not all((config, report, gc, gr, truth)):
            raise ValueError(f'Missing manifest: {folder.name}')
        events, tx, qualities = read_lines(folder / 'events.jsonl'), read_lines(genfolder / 'tx.jsonl'), read_lines(folder / 'quality.jsonl')
        episodes = truth['episodes']
        old_offset, anchors, spread = alignment(episodes, tx, events)
        trace_path = locate_trace(folder)
        if trace_path is None:
            raise ValueError('Missing trace: ' + folder.name)
        trace = decode_trace(trace_path)
        mapping = align_markers(trace, tx)
        matches = match_frames(trace, tx, mapping)
        offset, scale = mapping['offset_s'], mapping['scale']
        rx_for_tx = {i: frame for i, frame, _ in matches['matched']}
        rx_by_episode = defaultdict(list)
        for i, frame, residual in matches['matched']:
            frame['relative_s'] = (frame['ts_us'] / 1e6 - offset) / scale
            if tx[i].get('episode_id'):
                rx_by_episode[tx[i]['episode_id']].append(frame)
        for i in matches['missing_indices']:
            loss_rows.append({'session': folder.name, 'tx_index': i, **tx[i]})
        reproduced, replay_quality, _, snap = replay_events(trace_path, config, database, baseline=profile)
        replay_equal = reproduced == events
        duration = gc['arguments']['duration']
        for e in events:
            e['relative_s'] = (e['ts64'] / 1e6 - offset) / scale if e['ts64'] is not None else None
        independent_residuals = []
        for ep in episodes:
            if ep['type'] not in ('dlc_mismatch', 'out_of_range_signal'):
                continue
            frames = [f for f in tx if f.get('episode_id') == ep['episode_id']]
            for event in events:
                if event['type'] != PRIMARY[ep['type']] or event['can_id'] != ep['can_id']:
                    continue
                measured = ep['parameters'].get('injected_dlc', ep['parameters'].get('physical_value'))
                if event['measured'] != measured or event['relative_s'] is None:
                    continue
                if ep['start'] - .1 <= event['relative_s'] <= ep['end'] + .1 and frames:
                    residual = min((event['relative_s'] - f['command_started_s'] for f in frames), key=abs)
                    independent_residuals.append(residual)
        active_quality, quality_windows = [], []
        # Bound UART gaps by the immediately surrounding valid records in the
        # raw trace, then conservatively mask 3.05 s of rule reinitialisation.
        bounded_quality = [{'reason': q['reason'], 'start_us': q['start_us'], 'end_us': q['next_us']} for q in trace['errors']]
        bounded_quality += [q for q in qualities if q['reason'] not in ('stream_decode_error', 'sequence_gap')]
        for q in bounded_quality:
            start = (q['start_us'] / 1e6 - offset) / scale if q.get('start_us') is not None else None
            end = (q['end_us'] / 1e6 - offset) / scale if q.get('end_us') is not None else None
            if q['reason'] == 'truncated_last_record': end = start
            if start is None and end is not None: start = -1e9
            if end is None: end = duration
            masked_end = end + 3.05
            quality_windows.append((start if start is not None else -1e9, masked_end))
            # An open-ended issue is unresolved through the end of the observation.
            intersects = (start is None or start <= duration) and masked_end >= 0
            active_quality.append(intersects)
            quality_rows.append({'session': folder.name, **q, 'relative_start_s': start,
                                 'relative_end_s': end, 'mask_end_s': masked_end, 'may_overlap_active_run': intersects})
        analysis = report['analysis']
        matched_baseline = rxroot / Path(config['arguments']['baseline'].replace('\\', '/')).parent.name / 'baseline.json'
        same_profile = read_json(matched_baseline) == profile
        hash_ok = config['baseline_sha256'] in (digest(matched_baseline), digest(matched_baseline, True)) if matched_baseline.exists() else False
        selected = {str(m['can_id']) for m in gc['messages']}
        code_match = {name: digest(root / 'archive/deployment/can-live-rpi/tools' / ('rpi_receiver/' if name != 'live_detect.py' else '') / name, True) == sha
                      for name, sha in config.get('code_sha256', {}).items()}
        model_counts, model_start = dbc_transition_model(tx, database)
        row = {'session': folder.name, 'generator_session': genfolder.name, 'seed': gc['seed'],
               'kind': 'matrix' if 'matrix' in folder.name else 'smoke',
               'duration_s': duration, 'nominal_fps': gc['budget']['nominal_fps'],
               'ids': len(selected), 'periodic_ids': len(analysis['periodic_ids']),
               'tx_ack': gr['statistics']['commands_acknowledged'], 'tx_log_rows': len(tx),
               'rx_frames': analysis['counters']['frames'],
               'rx_minus_tx': analysis['counters']['frames'] - gr['statistics']['commands_acknowledged'],
               'slots_skipped': gr['statistics']['slots_skipped_late'],
               'crc_errors': analysis['decoder']['crc_errors'], 'cobs_errors': analysis['decoder']['cobs_errors'],
               'quality_entries': len(qualities), 'quality_may_overlap_run': any(active_quality),
               'records_missing': analysis['counters'].get('records_missing_on_link', 0),
               'trace_available': True, 'trace_path': trace_path.relative_to(root).as_posix(),
               'trace_sha_matches_report': trace['sha256'] == report['trace_sha256'],
               'trace_frames_match_report': len(trace['frames']) == analysis['counters']['frames'],
               'replay_events_equal': replay_equal,
               'replay_event_count': len(reproduced), 'saved_event_count': len(events),
               'exact_full_frame_order': matches['exact_full_order'],
               'matched_rx_frames': len(matches['matched']), 'missing_rx_frames': len(matches['missing_indices']),
               'extra_rx_frames': len(matches['extra_frames']),
               'max_matching_residual_ms': matches['max_matching_residual_ms'],
               'markers': mapping,
               'dbc_hash_equal': config['dbc_sha256'] == gc['dbc_sha256'] == truth['dbc_sha256'] == profile['dbc_sha256'],
               'profile_ids_equal': selected == set(profile['messages']),
               'profile_content_equal': same_profile, 'baseline_hash_matches_raw_or_lf': hash_ok,
               'code_matches_local_lf': code_match, 'baseline_source': matched_baseline.relative_to(root).as_posix(),
               'ready': profile['ready'], 'analysis_complete': report['analysis_complete'],
               'analysis_overflow': report['analysis_queue_overflow'],
               'rx_error': report.get('error') or report.get('analysis_error'),
               'tx_status': gr['status'], 'rx_status': report['status'],
               'offset_marker_s': offset, 'marker_scale': scale, 'old_offset_inferred_s': old_offset,
               'anchor_spread_ms': spread * 1000,
               'alignment_anchors': anchors, 'elapsed_host_s': report['elapsed_s'],
               'validation_anchor_count': len(independent_residuals),
               'validation_anchor_max_abs_ms': max(map(abs, independent_residuals), default=0) * 1000,
               **report['performance']}
        for key, ok in [('tx_count', len(tx) == row['tx_ack']),
                        ('events_count', dict(Counter(e['type'] for e in events)) == analysis['events_by_type']),
                        ('profile_match', same_profile and hash_ok), ('ids_match', row['profile_ids_equal']),
                        ('dbc_match', row['dbc_hash_equal']), ('trace_hash', row['trace_sha_matches_report']),
                        ('trace_frames', row['trace_frames_match_report']), ('replay_events_equal', replay_equal)]:
            validation.append({'session': folder.name, 'check': key, 'passed': ok})
        for ep in episodes:
            ep_tx = [f for f in tx if f.get('episode_id') == ep['episode_id']]
            validation.append({'session': folder.name, 'check': ep['episode_id'] + '_ack',
                               'passed': len(ep_tx) == ep['acknowledged_frames'] == gr['statistics']['per_episode'][ep['episode_id']]['acknowledged_frames']})
            same_id = [e for e in events if e['can_id'] == ep['can_id'] and e['relative_s'] is not None]
            ep_rx = rx_by_episode[ep['episode_id']]
            observed_start = min((f['relative_s'] for f in ep_rx), default=ep['start'])
            observed_end = ep['end']
            if ep['type'] in ('frequency_increase', 'frequency_decrease', 'disappearance'):
                return_frames = [rx_for_tx[i]['relative_s'] for i, f in enumerate(tx)
                                 if f['can_id'] == ep['can_id'] and f.get('kind') == 'reference'
                                 and f['scheduled_s'] >= ep['end'] and i in rx_for_tx]
                if return_frames: observed_end = min(return_frames)
            hits = [e for e in same_id if observed_start - 1e-7 <= e['relative_s'] < observed_end - 1e-7]
            delayed = [e for e in same_id if observed_end - 1e-7 <= e['relative_s'] <= ep['end'] + 1.05]
            # Interval-based rules cannot detect a silence before the next frame.
            # The boundary tolerance must not credit a return-frame alarm as
            # an online detection of the preceding disappearance.
            if ep['type'] == 'disappearance':
                delayed += [e for e in hits if e['method'] in ('period', 'three_sigma', 'burst')]
                hits = [e for e in hits if e['method'] not in ('period', 'three_sigma', 'burst')]
            core = [e for e in same_id if ep['start'] + .15 <= e['relative_s'] <= ep['end'] - .15]
            pentry = profile['messages'].get(str(ep['can_id']), {})
            ep_row = {'session': folder.name, 'seed': gc['seed'], 'kind': row['kind'],
                      'episode': ep['episode_id'], 'type': ep['type'], 'variant': ep['parameters'].get('variant'),
                      'can_id': ep['can_id'], 'start_s': ep['start'], 'end_s': ep['end'],
                      'planned_frames': ep['planned_frames'], 'acknowledged_frames': ep['acknowledged_frames'],
                      'skipped_frames': ep['skipped_frames'], 'rx_frames_per_episode': len(ep_rx),
                      'observed_start_s': observed_start, 'observed_end_s': observed_end,
                      'quality_mask_overlap': any(a <= observed_end and b >= observed_start for a, b in quality_windows),
                      'primary_type': PRIMARY[ep['type']], 'primary_onset': any(e['type'] == PRIMARY[ep['type']] for e in hits),
                      'any_onset': bool(hits), 'new_onsets': len(hits), 'event_types': dict(Counter(e['type'] for e in hits)),
                      'delayed_methods': dict(Counter(e['method'] for e in delayed)),
                      'core_methods': dict(Counter(e['method'] for e in core)),
                      'quality_free_run': not any(active_quality),
                      'period_us': pentry.get('period_us'), 'std_us': pentry.get('std_us'),
                      'parameters': ep['parameters'], **model_start.get(ep['episode_id'], {})}
            # For cessation, assert no acknowledged frames of the target ID inside the scheduled gap.
            ep_row['silence_tx_verified'] = (not any(f['can_id'] == ep['can_id'] and ep['start'] <= f['scheduled_s'] < ep['end'] for f in tx)) if ep['type'] == 'disappearance' else None
            validation.append({'session': folder.name, 'check': ep['episode_id'] + '_plan_balance',
                               'passed': ep['planned_frames'] == ep['acknowledged_frames'] + ep['skipped_frames']})
            if ep['type'] == 'disappearance':
                validation.append({'session': folder.name, 'check': ep['episode_id'] + '_silence',
                                   'passed': ep_row['silence_tx_verified']})
            if ep['type'] in ('dlc_mismatch', 'out_of_range_signal'):
                key = 'dlc' if ep['type'] == 'dlc_mismatch' else 'range'
                ep_row['predicted_dbc_onsets_from_tx'] = model_counts[(ep['episode_id'], key)]
                ep_row['no_new_onset_expected_due_to_latch'] = model_counts[(ep['episode_id'], key)] == 0
            for method in METHODS:
                mh = [e for e in hits if e['method'] == method]
                ep_row['hit_' + method] = bool(mh)
                ep_row['core_' + method] = any(e['method'] == method for e in core)
                ep_row['latency_' + method + '_s'] = min(e['relative_s'] for e in mh) - observed_start if mh else None
            if ep['type'] == 'burst':
                times = sorted(f['ts_us'] for f in ep_rx)
                gaps = [(b-a)/1000 for a,b in zip(times,times[1:])]
                ep_row['injected_gap_min_ms'] = min(gaps, default=None)
                ep_row['injected_gap_max_ms'] = max(gaps, default=None)
                ep_row['injected_gap_median_ms'] = statistics.median(gaps) if gaps else None
            for tol in (.0, .02, .1):
                ep_row[f'any_onset_tol_{tol:g}'] = any(ep['start'] - tol <= e['relative_s'] <= ep['end'] + tol for e in same_id)
            episode_rows.append(ep_row)
        for e in events:
            t = e['relative_s']
            if t is None:
                category = 'no_timestamp'
            elif t < 0 or t > duration:
                category = 'outside_run'
            elif t < 3.05 or t > duration - .05:
                category = 'boundary_guard'
            elif any(ep['can_id'] == e['can_id'] and ep['start'] - .05 <= t <= ep['end'] + .05 for ep in episodes):
                category = 'target_episode'
            elif any(ep['can_id'] == e['can_id'] and ep['end'] + .05 < t <= ep['end'] + 1.05 for ep in episodes):
                category = 'target_recovery'
            elif any(ep['start'] - .05 <= t <= ep['end'] + 1.05 for ep in episodes):
                category = 'other_id_during_episode'
            else:
                category = 'background_candidate'
            event_rows.append({'session': folder.name, 'seed': gc['seed'], 'kind': row['kind'],
                               'category': category, 'quality_masked': t is None or any(a <= t <= b for a,b in quality_windows), **e})
        row['background_duration_s'] = (duration - .05 - 3.05) - sum(ep['end'] + 1.05 - (ep['start'] - .05) for ep in episodes)
        ep_windows = [(ep['start']-.05,ep['end']+1.05) for ep in episodes]
        row['background_clean_duration_s'] = available_seconds(3.05,duration-.05, ep_windows + quality_windows)
        # Episode intervals are disjoint in these supplied matrices.
        assert all(a['end'] + 1.05 < b['start'] - .05 for a, b in zip(episodes, episodes[1:]))
        for method in METHODS:
            row['background_' + method] = sum(e['category'] == 'background_candidate' and e['method'] == method for e in event_rows if e['session'] == folder.name)
            row['other_id_' + method] = sum(e['category'] == 'other_id_during_episode' and e['method'] == method for e in event_rows if e['session'] == folder.name)
            row['background_clean_' + method] = sum(e['category'] == 'background_candidate' and not e['quality_masked'] and e['method'] == method for e in event_rows if e['session'] == folder.name)
        sessions.append(row)
        print('Trace verified:', folder.name, 'matched', len(matches['matched']), 'replay', replay_equal, flush=True)
        for parent in (folder, genfolder):
            for file in sorted(parent.iterdir()):
                if file.is_file() and file.suffix in ('.json', '.jsonl', '.canbin'):
                    source_manifest.append({'path': file.relative_to(root).as_posix(), 'bytes': file.stat().st_size, 'sha256': digest(file)})
        if trace_path.parent != folder:
            source_manifest.append({'path': trace_path.relative_to(root).as_posix(), 'bytes': trace_path.stat().st_size, 'sha256': digest(trace_path)})
    results = {'methodology': {'alignment': 'independent_start_end_CAN_markers_affine', 'tolerance_s': .05,
                              'recovery_s': 1.05, 'startup_guard_s': 3.05,
                              'warning': 'Event times are device timestamps, not host notification latency. Alarm onsets are not frame recall.'},
               'sessions': sessions, 'episodes': episode_rows, 'baselines': baseline_rows,
               'validation': validation}
    (output / 'analysis.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    for name, rows in [('sessions', sessions), ('episodes', episode_rows), ('events_audit', event_rows),
                       ('quality_audit', quality_rows), ('baselines', baseline_rows),
                       ('profile_parameters', profile_rows), ('validation', validation), ('source_manifest', source_manifest)]:
        write_csv(output / (name + '.csv'), rows)
    write_csv(output / 'missing_rx_frames.csv', loss_rows)
    summary = []
    matrix = [e for e in episode_rows if e['kind'] == 'matrix']
    for eid in sorted({e['episode'] for e in matrix}):
        items = [e for e in matrix if e['episode'] == eid]
        summary.append({'episode': eid, 'type': items[0]['type'], 'variant': items[0]['variant'],
                        'runs': len(items), 'planned': sum(e['planned_frames'] for e in items),
                        'ack': sum(e['acknowledged_frames'] for e in items),
                        'rx': sum(e['rx_frames_per_episode'] for e in items),
                        'primary_onsets': sum(e['primary_onset'] for e in items),
                        'any_onsets': sum(e['any_onset'] for e in items),
                        'latch_no_new_onset_expected': sum(e.get('no_new_onset_expected_due_to_latch', False) for e in items),
                        'unmasked_runs': sum(not e['quality_mask_overlap'] for e in items),
                        'unmasked_any': sum(e['any_onset'] and not e['quality_mask_overlap'] for e in items),
                        **{m: sum(e['hit_' + m] for e in items) for m in METHODS},
                        **{'core_' + m: sum(e['core_' + m] for e in items) for m in METHODS}})
    write_csv(output / 'scenario_summary.csv', summary)
    print(json.dumps({'sessions': len(sessions), 'matrix_runs': sum(s['kind'] == 'matrix' for s in sessions),
                      'baselines': len(baseline_rows), 'failed_checks': [v for v in validation if not v['passed']],
                      'matrix_scenarios': summary}, ensure_ascii=False, indent=2))
    return results, event_rows, summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT / 'results/reports/campaign_2026-09-20')
    args = parser.parse_args()
    analyze(args.root, args.output)
