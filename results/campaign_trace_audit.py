"""Trace decoding, marker alignment and payload/order checks for campaign audit."""
from collections import Counter
from dataclasses import fields
import hashlib
import json
from pathlib import Path
from rpi_receiver import can_stream_protocol as proto
from rpi_receiver.live_rules import LiveRules, Settings
from can_generate.time_reference import decode_marker


def locate_trace(folder):
    direct = folder / 'trace.canbin'
    alternate = folder.parent / folder.name.replace('anomaly_seed', 'anomalies_seed') / 'trace.canbin'
    return direct if direct.exists() else alternate if alternate.exists() else None


def decode_trace(path, channel=1):
    decoder = proto.StreamDecoder()
    frames, markers, record_times, errors = [], [], [], []
    sync = None
    last_ts = None
    previous_errors = 0
    previous_seq = None
    data = path.read_bytes()
    for record in decoder.feed(data):
        c = decoder.counters
        count = c.crc_errors + c.cobs_errors + c.short_records + c.sync_losses
        ts = None
        if record.type == proto.REC_SESSION:
            session = proto.decode_session(record)
            sync = None
            if session: ts = session['ts64']
        if record.type == proto.REC_TIME_SYNC:
            sync = proto.decode_time_sync(record)
            ts = sync
        elif record.type == proto.REC_FRAME:
            frame = proto.decode_frame(record)
            if frame:
                ts = proto.restore_time(sync, frame.ts32) if sync is not None else None
                if frame.channel == channel:
                    item = {'can_id': frame.can_id, 'dlc': frame.dlc, 'data': frame.data.hex(),
                            'ts_us': ts, 'ts32': frame.ts32, 'seq': frame.seq, 'order': len(frames)}
                    frames.append(item)
                    marker = decode_marker(frame.data) if frame.can_id == 0x7fe else None
                    if marker:
                        markers.append({**item, 'kind': marker.kind, 'generator_s': marker.generator_time_us / 1e6})
        elif record.type in (proto.REC_BUS_STATS, proto.REC_LOGGER_STATS, proto.REC_EVENT, proto.REC_ACK):
            fn = {proto.REC_BUS_STATS: proto.decode_bus_stats, proto.REC_LOGGER_STATS: proto.decode_logger_stats,
                  proto.REC_EVENT: proto.decode_event, proto.REC_ACK: proto.decode_ack}[record.type]
            body = fn(record)
            if body: ts = body['ts64']
        if count != previous_errors:
            errors.append({'reason': 'decoder', 'start_us': last_ts, 'next_us': ts, 'count': count - previous_errors})
        if previous_seq is not None and record.seq != (previous_seq + 1) % 65536:
            errors.append({'reason': 'sequence_gap', 'start_us': last_ts, 'next_us': ts,
                           'missing': (record.seq - previous_seq - 1) % 65536})
        previous_seq, previous_errors = record.seq, count
        if ts is not None:
            record_times.append(ts)
            last_ts = max(ts, last_ts) if last_ts is not None else ts
    return {'frames': frames, 'markers': markers, 'errors': errors,
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'decoder': decoder.counters.as_dict(), 'min_ts_us': min(record_times, default=None),
            'max_ts_us': max(record_times, default=None),
            'trailing_bytes': len(decoder._buffer)}


def align_markers(trace, tx):
    start = [m for m in trace['markers'] if m['kind'] == 1]
    end = [m for m in trace['markers'] if m['kind'] == 2]
    if len(start) != 1 or len(end) != 1 or start[0]['ts_us'] is None or end[0]['ts_us'] is None:
        raise ValueError('Expected one time-stamped start and end marker')
    a, b = start[0], end[0]
    g0, g1 = a['generator_s'], b['generator_s']
    scale = (b['ts_us'] - a['ts_us']) / 1e6 / (g1 - g0)
    offset = a['ts_us'] / 1e6 - scale * g0
    # A second mapping uses actual host command times to check matching residuals.
    txstart = next(f for f in tx if f.get('kind') == 'marker_start')
    txend = next(f for f in tx if f.get('kind') == 'marker_end')
    command_scale = (b['ts_us'] - a['ts_us']) / 1e6 / (txend['command_started_s'] - txstart['command_started_s'])
    command_offset = a['ts_us'] / 1e6 - command_scale * txstart['command_started_s']
    return {'offset_s': offset, 'scale': scale, 'apparent_clock_difference_ppm': (scale - 1) * 1e6,
            'start_us': a['ts_us'], 'end_us': b['ts_us'], 'command_scale': command_scale,
            'command_offset_s': command_offset, 'start_order': a['order'], 'end_order': b['order']}


def signature(frame):
    return frame['can_id'], frame['dlc'], frame['data'].lower()


def match_frames(trace, tx, alignment):
    """Align frame order with bounded look-ahead, never reuse an RX frame.

    Exact global sequence equality is the primary test. When a deletion occurs,
    nearest marker-aligned time disambiguates repeated constant payloads.
    """
    rx = trace['frames'][alignment['start_order']:alignment['end_order'] + 1]
    matched, missing, extra, ambiguous = [], [], [], []
    i = j = 0
    while i < len(tx) and j < len(rx):
        predicted = (rx[j]['ts_us'] / 1e6 - alignment['command_offset_s']) / alignment['command_scale']
        residual = predicted - tx[i]['command_started_s']
        if signature(tx[i]) == signature(rx[j]) and abs(residual) <= .05:
            matched.append((i, rx[j], residual))
            i += 1
            j += 1
            continue
        options = []
        for di in range(min(12, len(tx) - i)):
            for dj in range(min(12, len(rx) - j)):
                if di == dj == 0 or signature(tx[i + di]) != signature(rx[j + dj]):
                    continue
                t = (rx[j + dj]['ts_us'] / 1e6 - alignment['command_offset_s']) / alignment['command_scale']
                delta = abs(t - tx[i + di]['command_started_s'])
                if delta <= .05:
                    options.append((di + dj, delta, di, dj))
        if not options:
            raise ValueError(f'Unresolved frame alignment at TX {i}, RX {j}; residual={residual}')
        _, _, di, dj = min(options)
        missing.extend(range(i, i + di))
        extra.extend(rx[j:j + dj])
        i += di
        j += dj
    missing.extend(range(i, len(tx)))
    extra.extend(rx[j:])
    assert len(matched) + len(missing) == len(tx)
    assert len(matched) + len(extra) == len(rx)
    return {'matched': matched, 'missing_indices': missing, 'extra_frames': extra,
            'active_rx': len(rx), 'exact_full_order': not missing and not extra,
            'max_matching_residual_ms': max((abs(x[2]) for x in matched), default=0) * 1000}


def replay_events(path, config, database, baseline=None):
    names = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in config['settings'].items() if k in names})
    events, qualities, profiles = [], [], []
    engine = LiveRules(database, config['dbc_sha256'], settings, events.append, qualities.append,
                       baseline=baseline, baseline_sink=profiles.append)
    with path.open('rb') as stream:
        while chunk := stream.read(4096): engine.feed(chunk)
    engine.finish()
    return events, qualities, profiles, engine.snapshot()


def baseline_subsequence(trace, tx):
    """Check a baseline without markers against an unambiguous payload sequence."""
    rx = trace['frames']
    a, b = [signature(f) for f in tx], [signature(f) for f in rx]
    n = min(20, len(b))
    # The complete RX sequence must fit inside TX. This resolves the 10 s
    # waveform repetitions that make a short prefix non-unique on its own.
    candidates = [i for i in range(max(0, len(a) - len(b) + 1)) if a[i:i+n] == b[:n]]
    if len(candidates) != 1:
        return {'alignment': 'unresolved', 'candidate_count': len(candidates)}
    start = candidates[0]
    if a[start:start + len(b)] == b:
        return {'alignment': 'exact_contiguous_subsequence', 'first_tx_index': start,
                'missing_before_capture': start, 'missing_after_capture': len(a) - start - len(b),
                'missing_inside_capture': 0, 'matched_frames': len(b),
                'first_matched_command_s': tx[start]['command_started_s'],
                'last_matched_command_s': tx[start + len(b) - 1]['command_started_s']}
    i, j, missing, extra = start, 0, [], []
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        options = [(di+dj, di, dj) for di in range(min(12, len(a)-i))
                   for dj in range(min(12, len(b)-j)) if (di or dj) and a[i+di:i+di+5] == b[j+dj:j+dj+5]]
        if not options:
            return {'alignment': 'interior_mismatch_unresolved', 'first_tx_index': start, 'tx_index': i, 'rx_index': j}
        _, di, dj = min(options)
        missing.extend(range(i, i+di))
        extra.extend(range(j,j+dj))
        i += di
        j += dj
    return {'alignment': 'sequence_with_interior_gaps', 'first_tx_index': start,
            'missing_before_capture': start, 'missing_after_capture': len(a)-i,
            'missing_inside_capture': len(missing), 'extra_rx': len(extra),
            'matched_frames': j-len(extra), 'missing_tx': [tx[k] for k in missing]}
