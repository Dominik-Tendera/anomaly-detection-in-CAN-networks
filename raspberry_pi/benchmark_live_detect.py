#!/usr/bin/env python3
"""Measure the live engine with real DBC decoding and event-file I/O, without UART.

Run on the Raspberry Pi itself to obtain relevant CPU/memory measurements.
This is accelerated replay, not proof of lossless physical UART capture.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import time

import cantools
import live_detect
from rpi_receiver import can_stream_protocol as proto


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dbc', type=Path, default=live_detect.DEFAULT_DBC)
    p.add_argument('--frames', type=int, default=85000)
    p.add_argument('--rate', type=live_detect.positive, default=8500)
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'benchmarks')
    args = p.parse_args(argv)
    if args.frames < 100:
        p.error('--frames must be >=100')
    directory = args.output.resolve() / time.strftime('%Y%m%d_%H%M%S')
    directory.mkdir(parents=True, exist_ok=False)
    database = cantools.database.load_file(str(args.dbc))
    messages = sorted((m for m in database.messages if not m.is_extended_frame and not m.is_fd and m.length <= 8),
                      key=lambda m: len(m.signals), reverse=True)[:10]
    if not messages:
        raise ValueError('No standard CAN messages in DBC')
    period_us = 1e6 * len(messages) / args.rate
    profile = {'format': 'can-live-baseline-v1', 'ready': True,
               'dbc_sha256': live_detect.checksum(args.dbc), 'channel': 1,
               'messages': {str(m.frame_id): {'period_us': period_us, 'std_us': 1.0,
                                            'interval_count': 1000} for m in messages},
               'note': 'Synthetic benchmark profile, not a measured reference session'}
    live_detect.save_json(directory / 'input.baseline.json', profile)
    seq = 0
    def wire(kind, payload):
        nonlocal seq
        body = struct.pack('<BH', kind, seq) + payload
        seq = (seq + 1) & 0xffff
        return proto.cobs_encode(body + bytes([proto.crc8(body)]))
    trace = directory / 'input.canbin'
    print(f'Generating {args.frames} frames, {len(messages)} IDs, nominal {args.rate:g} frames/s...', flush=True)
    with trace.open('xb') as output:
        next_sync = 0
        for index in range(args.frames):
            ts = round(index * 1e6 / args.rate)
            if ts >= next_sync:
                output.write(wire(proto.REC_TIME_SYNC, struct.pack('<Q', ts)))
                next_sync = ts + 100_000
            message = messages[index % len(messages)]
            # Zero payloads exercise signal decoding, including out-of-range
            # checks; use deterministic ID/payload faults near the end to also
            # exercise event writes, not only the normal path.
            can_id = message.frame_id
            data = bytes(message.length)
            if index % 1000 == 999:
                data = data[:-1] if data else b'\x00'
            idc = can_id | len(data) << 11
            output.write(wire(proto.REC_FRAME, struct.pack('<HI', idc, ts & 0xffffffff) + data))
    started = time.perf_counter()
    code = live_detect.main(['--dbc', str(args.dbc), '--baseline', str(directory / 'input.baseline.json'),
                             '--replay', str(trace), '--output', str(directory), '--session', 'analysis'])
    elapsed = time.perf_counter() - started
    report = json.loads((directory / 'analysis/report.json').read_text(encoding='utf-8'))
    measured_fps = args.frames / elapsed
    result = {'nominal_frame_rate': args.rate, 'frames': args.frames,
              'replay_elapsed_s': elapsed, 'end_to_end_replay_frames_per_second': measured_fps,
              'replay_headroom_ratio': measured_fps / args.rate,
              'analysis_complete': report.get('analysis_complete'),
              'performance': report.get('performance'),
              'hardware_validation': 'UART, SD-card capture writes and live scheduling still require a physical test.'}
    live_detect.save_json(directory / 'benchmark.json', result)
    print(json.dumps(result, indent=2))
    if measured_fps < args.rate * 1.3:
        print('Insufficient or small replay margin. Reduce traffic/rules and verify on hardware before claiming live support.')
    print(f'Benchmark: {directory / "benchmark.json"}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
