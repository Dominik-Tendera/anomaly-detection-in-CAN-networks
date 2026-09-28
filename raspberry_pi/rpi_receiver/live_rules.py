"""Bounded-state CAN rules using the existing STM32 stream decoder.

All decisions use device time. Host clocks belong only to performance metrics.
The two sinks append events and data-quality intervals immediately to disk.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import math
from cantools.database.errors import DecodeError

from rpi_receiver import can_stream_protocol as proto

METHODS = ('dbc', 'timeout', 'period', 'frequency', 'three_sigma', 'burst', 'controller')
LOSS_COUNTERS = ('ring_dropped', 'fifo_overrun', 'rx_read_errors')
ERROR_COUNTERS = ('stuff_errors', 'form_errors', 'ack_errors', 'bit_recessive_errors',
                  'bit_dominant_errors', 'crc_errors', 'bus_off_entries',
                  'passive_entries', 'warning_entries')


@dataclass(frozen=True)
class Settings:
    channel: int = 1
    learn_seconds: float = 60.0
    min_intervals: int = 100
    window_seconds: float = 1.0
    missing_multiplier: float = 3.0
    period_tolerance: float = 0.30
    frequency_tolerance: float = 0.50
    sigma_multiplier: float = 3.0
    burst_factor: float = 0.25
    burst_frames: int = 4
    ignore_ids: tuple = (0x7FE,)  # Previous generator's session marker.

    def validate(self):
        if self.channel not in (1, 2) or self.min_intervals < 2 or self.burst_frames < 2:
            raise ValueError('channel=1/2, min_intervals>=2, burst_frames>=2 required')
        for name in ('learn_seconds', 'window_seconds', 'missing_multiplier', 'sigma_multiplier'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        for name in ('period_tolerance', 'frequency_tolerance', 'burst_factor'):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f'{name} must be between 0 and 1')
        if self.missing_multiplier <= 1 or any(not 0 <= i <= 0x7ff for i in self.ignore_ids):
            raise ValueError('missing_multiplier>1 and standard ignore_ids required')


@dataclass
class Moments:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, value):
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    @property
    def std(self):
        return math.sqrt(max(0.0, self.m2 / self.n)) if self.n else 0.0


def validate_profile(profile, checksum, channel):
    if profile.get('format') != 'can-live-baseline-v1' or not profile.get('ready'):
        raise ValueError('Baseline must be a completed can-live-baseline-v1 profile')
    if profile.get('dbc_sha256') != checksum or profile.get('channel') != channel:
        raise ValueError('Baseline DBC checksum or CAN channel does not match this run')
    entries = profile.get('messages')
    if not isinstance(entries, dict) or not entries or len(entries) > 2048:
        raise ValueError('Baseline has no messages or invalid message map')
    for key, entry in entries.items():
        if not 0 <= int(key) <= 0x7ff:
            raise ValueError('Invalid baseline CAN ID')
        period = entry.get('period_us')
        if period is not None:
            if not isinstance(period, (int, float)) or not math.isfinite(period) or period <= 0:
                raise ValueError('Invalid baseline period_us')
            std = entry.get('std_us')
            if not isinstance(std, (int, float)) or not math.isfinite(std) or std < 0:
                raise ValueError('Invalid baseline std_us')


class LiveRules:
    def __init__(self, database, checksum, settings, event_sink, quality_sink,
                 baseline=None, baseline_sink=None):
        settings.validate()
        self.settings, self.checksum = settings, checksum
        self.messages = {m.frame_id: m for m in database.messages
                         if not m.is_extended_frame and not m.is_fd}
        self.ranges = {i: [(s.name, s.minimum, s.maximum, s.unit) for s in m.signals
                          if s.minimum is not None and s.maximum is not None and s.minimum != s.maximum]
                       for i, m in self.messages.items()}
        self.event_sink, self.quality_sink = event_sink, quality_sink
        self.baseline_sink = baseline_sink or (lambda value: None)
        self.decoder = proto.StreamDecoder()
        self.expected_seq = None
        self.sync = None
        self.time = None
        self.rtc = None
        self.session_index = 0
        self.seen_any = False
        self.last_bus = {}
        self.last_logger = None
        self.session_record = None
        self.counters = Counter()
        self.events_by_type = Counter()
        self.events_by_method = Counter({name: 0 for name in METHODS})
        self.active = set()
        self.last_frames = {}
        self.burst_counts = Counter()
        self.window_counts = Counter()
        self.window_start = None
        self.closed_window_end = None
        self.detect_start = None
        self.next_scan = 0
        self.learning_start = None
        self.learn_moments = {}
        self.learn_counts = Counter()
        self.profile = {}
        self.ready = baseline is not None
        self.loaded_baseline = baseline is not None
        self.baseline_document = baseline
        self.last_decoder_errors = 0
        if baseline is not None:
            validate_profile(baseline, checksum, settings.channel)
            self.profile = {int(k): v for k, v in baseline['messages'].items()}

    def _absolute(self, ts):
        if self.rtc is None or ts is None:
            return None
        try:
            return (self.rtc + timedelta(microseconds=ts)).isoformat()
        except OverflowError:
            return None

    def emit(self, kind, method, ts, can_id=None, measured=None, expected=None,
             unit=None, seq=None, **extra):
        event = {'type': kind, 'method': method, 'ts64': ts,
                 'absolute_time': self._absolute(ts), 'session_index': self.session_index,
                 'channel': self.settings.channel, 'can_id': can_id, 'seq': seq,
                 'measured': measured, 'expected': expected, 'unit': unit,
                 'quality': 'check_quality_intervals', **extra}
        self.event_sink(event)
        self.events_by_type[kind] += 1
        self.events_by_method[method] += 1

    def condition(self, key, bad, kind, method, ts, can_id, measured, expected,
                  unit=None, seq=None, **extra):
        if not bad:
            self.active.discard(key)
        elif key not in self.active:
            self.active.add(key)
            self.emit(kind, method, ts, can_id, measured, expected, unit, seq, **extra)

    def reset_timing(self, reset_learning=True):
        self.last_frames.clear()
        self.burst_counts.clear()
        self.window_counts.clear()
        self.window_start = self.detect_start = None
        self.closed_window_end = None
        self.next_scan = 0
        self.active.clear()
        if reset_learning and not self.ready:
            self.learning_start = None
            self.learn_moments.clear()
            self.learn_counts.clear()

    def quality(self, reason, start=None, end=None, preserve_learning=False, **extra):
        self.counters['quality_intervals'] += 1
        self.quality_sink({'reason': reason, 'session_index': self.session_index,
                           'start_us': start, 'end_us': end, **extra})
        # A damaged UART record or a sequence gap invalidates only the
        # interval around the gap.  It must not discard minutes of otherwise
        # valid baseline learning.  Hard session/time discontinuities keep
        # the historical behaviour and restart learning.
        self.reset_timing(reset_learning=not preserve_learning)

    def _transport_errors(self):
        c = self.decoder.counters
        errors = c.crc_errors + c.cobs_errors + c.short_records + c.sync_losses
        if errors != self.last_decoder_errors:
            self.quality('stream_decode_error', self.time, None,
                         count=errors - self.last_decoder_errors,
                         preserve_learning=True)
            self.last_decoder_errors = errors

    def feed(self, chunk):
        for raw in self.decoder.feed(chunk):
            self._transport_errors()
            self.process(raw)

    def process(self, raw):
        self.counters['records'] += 1
        if raw.type == proto.REC_SESSION:
            body = proto.decode_session(raw)
            if body is None:
                self.quality('invalid_session_record', self.time, None)
                return
            if body['proto_version'] != proto.PROTO_VERSION:
                raise ValueError(f"Unsupported stream version: {body['proto_version']}")
            if self.seen_any:
                self.quality('device_session_boundary', self.time, body['ts64'])
                self.session_index += 1
                self.counters['session_boundaries'] += 1
            self.expected_seq = None
            self.sync = None  # Require a fresh time-sync, including after reboot.
            self.time = None
            self.last_bus.clear()
            self.last_logger = None
            self.session_record = body
            self.rtc = None
            if body['rtc_valid'] and not body['rtc'].startswith('2000-'):
                try:
                    self.rtc = datetime.fromisoformat(body['rtc']) - timedelta(microseconds=body['ts64'])
                except (ValueError, OverflowError):
                    pass
        if self.expected_seq is not None and raw.seq != self.expected_seq:
            missing = (raw.seq - self.expected_seq) & 0xffff
            self.counters['records_missing_on_link'] += missing
            self.quality('sequence_gap', self.time, None, missing=missing,
                         preserve_learning=True)
        self.expected_seq = (raw.seq + 1) & 0xffff
        self.seen_any = True

        ts = None
        if raw.type == proto.REC_FRAME:
            frame = proto.decode_frame(raw)
            if frame is None:
                self.quality('invalid_frame_record', self.time, None,
                             preserve_learning=True)
                return
            self.counters['frames_all_channels'] += 1
            if frame.channel != self.settings.channel:
                return
            self.counters['frames'] += 1
            if self.sync is not None:
                ts = proto.restore_time(self.sync, frame.ts32)
            else:
                self.counters['frames_without_time'] += 1
            self.frame(frame, ts)
            return
        if raw.type == proto.REC_TIME_SYNC:
            ts = proto.decode_time_sync(raw)
            if ts is not None:
                self.sync = ts
        elif raw.type == proto.REC_BUS_STATS:
            body = proto.decode_bus_stats(raw)
            if body is not None:
                ts = body['ts64']
                if body['channel'] == self.settings.channel:
                    self.bus_statistics(body, raw.seq)
        elif raw.type == proto.REC_LOGGER_STATS:
            body = proto.decode_logger_stats(raw)
            if body is not None:
                ts = body['ts64']
                previous = self.last_logger
                if previous:
                    changes = {n: (body[n] - previous[n]) & 0xffffffff
                               for n in ('tx_records_dropped', 'event_queue_dropped', 'ring_drop_total')}
                    changes = {n: d for n, d in changes.items() if d}
                    if changes:
                        self.counters['device_loss_reports'] += 1
                        self.quality('logger_loss', previous['ts64'], ts, deltas=changes)
                self.last_logger = body
        elif raw.type == proto.REC_EVENT:
            body = proto.decode_event(raw)
            if body is not None:
                ts = body['ts64']
                if body['channel'] == self.settings.channel:
                    if body['reason_name'] == 'extended_id_remapped':
                        self.counters['extended_id_remapped'] += 1
                    elif body['reason_name'] in ('rx_read_error', 'ring_drop', 'fifo_overrun', 'tx_buffer_overflow'):
                        self.counters['device_loss_reports'] += 1
                        self.quality(body['reason_name'], self.time, ts,
                                     can_id=body['frame_id'] if body['reason_name'] == 'ring_drop' else None)
                    elif body['reason_name'] in ('controller_error', 'state_change',
                                               'remote_frame_rejected', 'extended_frame_rejected', 'invalid_dlc'):
                        self.emit('controller_event', 'controller', ts,
                                  measured=body['reason_name'], seq=raw.seq, details=body)
        elif raw.type == proto.REC_SESSION:
            ts = self.session_record['ts64']
        elif raw.type == proto.REC_ACK:
            body = proto.decode_ack(raw)
            if body is not None:
                ts = body['ts64']
        if ts is not None:
            self.tick(ts)

    def bus_statistics(self, body, seq):
        previous = self.last_bus.get(body['channel'])
        self.last_bus[body['channel']] = body
        if body['extended_frames']:
            self.counters['extended_frames_observed'] = body['extended_frames']
        if previous is None:
            return  # Counters before attachment are not losses in this capture.
        deltas = {n: (body[n] - previous[n]) & 0xffffffff for n in LOSS_COUNTERS + ERROR_COUNTERS}
        losses = {n: deltas[n] for n in LOSS_COUNTERS if deltas[n]}
        if losses:
            self.counters['device_loss_reports'] += 1
            self.quality('can_receive_loss', previous['ts64'], body['ts64'], deltas=losses)
        for name in ERROR_COUNTERS:
            if deltas[name]:
                self.emit('controller_counter_increase', 'controller', body['ts64'],
                          measured=deltas[name], expected=0, unit='count', seq=seq,
                          counter=name, interval_start_us=previous['ts64'])

    def frame(self, frame, ts):
        i = frame.can_id
        if i in self.settings.ignore_ids:
            return
        message = self.messages.get(i)
        previous = self.last_frames.get(i)
        if ts is not None and previous is not None and ts - previous > 1_000_000:
            # A fresh unknown-ID episode after a second of silence.
            self.active.discard(('unknown', i))
        self.condition(('unknown', i), message is None and i not in self.profile,
                       'unknown_id', 'dbc', ts, i, i, 'ID in DBC or baseline', seq=frame.seq)
        valid = message is not None and frame.dlc == message.length
        if message is not None:
            self.condition(('dlc', i), not valid, 'dlc_mismatch', 'dbc', ts, i,
                           frame.dlc, message.length, 'bytes', frame.seq)
        if valid and self.ranges[i]:
            try:
                signals = message.decode(frame.data, decode_choices=False)
                for name, low, high, unit in self.ranges[i]:
                    if name in signals:
                        value = float(signals[name])
                        bad = not math.isfinite(value) or value < low - 1e-9 or value > high + 1e-9
                        self.condition(('range', i, name), bad, 'signal_out_of_range', 'dbc', ts, i,
                                       value if math.isfinite(value) else str(value),
                                       {'min': low, 'max': high}, unit, frame.seq, signal=name)
            except (ValueError, KeyError, DecodeError):
                self.counters['signal_decode_errors'] += 1
        if ts is None:
            return
        # Time-stamped frames may precede the newest TIME_SYNC record. Only
        # reject true within-ID reversal or a frame from an already closed window.
        if ((previous is not None and ts < previous) or
                (self.closed_window_end is not None and ts < self.closed_window_end)):
            self.counters['late_frames'] += 1
            self.quality('out_of_order_frame', ts, self.time)
            return
        was_ready = self.ready
        self.tick(ts, incoming_id=i)
        previous = self.last_frames.get(i)
        if not self.ready:
            if valid:
                if self.learning_start is None:
                    self.learning_start = ts
                self.learn_counts[i] += 1
                if previous is not None and ts > previous:
                    self.learn_moments.setdefault(i, Moments()).add(ts - previous)
        elif was_ready:
            entry = self.profile.get(i, {})
            period = entry.get('period_us')
            self.active.discard(('missing', i))
            self.window_counts[i] += 1
            if previous is not None and period:
                dt = ts - previous
                tolerance = self.settings.period_tolerance * period
                self.condition(('period', i), abs(dt - period) > tolerance,
                               'period_violation', 'period', ts, i, dt,
                               {'min': period - tolerance, 'max': period + tolerance}, 'us', frame.seq)
                std = entry['std_us']
                if std > 0:
                    threshold = self.settings.sigma_multiplier * std
                    self.condition(('sigma', i), abs(dt - period) > threshold,
                                   'statistical_deviation', 'three_sigma', ts, i, dt,
                                   {'mean': period, 'std': std, 'sigma': self.settings.sigma_multiplier}, 'us', frame.seq)
                self.burst_counts[i] = self.burst_counts[i] + 1 if dt < period * self.settings.burst_factor else 0
                self.condition(('burst', i), self.burst_counts[i] >= self.settings.burst_frames - 1,
                               'burst', 'burst', ts, i, self.burst_counts[i] + 1,
                               {'min_frames': self.settings.burst_frames,
                                'max_interval_us': period * self.settings.burst_factor}, 'frames', frame.seq)
        if self.ready or valid:
            self.last_frames[i] = ts

    def tick(self, ts, incoming_id=None):
        if self.time is not None and ts - self.time > 10_000_000:
            self.quality('device_time_gap', self.time, ts)
        self.time = ts if self.time is None else max(ts, self.time)
        now = self.time
        if not self.ready:
            if self.learning_start is not None and now - self.learning_start >= self.settings.learn_seconds * 1e6:
                self.freeze()
            else:
                return
        if self.detect_start is None:
            self.detect_start = now
            self.window_start = now
        window = round(self.settings.window_seconds * 1e6)
        while now >= self.window_start + window:
            end = self.window_start + window
            for i, entry in self.profile.items():
                period = entry.get('period_us')
                if not period:
                    continue
                expected = window / period
                # One frame of quantisation tolerance avoids edge-count alarms.
                margin = max(1.0, expected * self.settings.frequency_tolerance)
                count = self.window_counts[i]
                for kind, bad in [('frequency_increase', count > expected + margin),
                                  ('frequency_decrease', count < max(0, expected - margin))]:
                    self.condition((kind, i), bad, kind, 'frequency', end, i, count,
                                   {'min': max(0, expected - margin), 'max': expected + margin},
                                   'frames/window', window_start_us=self.window_start, window_end_us=end)
            self.window_counts.clear()
            self.window_start = end
            self.closed_window_end = end
        if now >= self.next_scan:
            self.next_scan = now + 10_000
            for i, entry in self.profile.items():
                if i == incoming_id:
                    continue
                period = entry.get('period_us')
                if period:
                    last = self.last_frames.get(i, self.detect_start)
                    elapsed = now - last
                    limit = period * self.settings.missing_multiplier
                    self.condition(('missing', i), elapsed > limit, 'missing_frame', 'timeout', now, i,
                                   elapsed, limit, 'us', last_seen_us=self.last_frames.get(i))

    def freeze(self):
        self.profile = {}
        for i, count in sorted(self.learn_counts.items()):
            moments = self.learn_moments.get(i, Moments())
            self.profile[i] = {'frame_count': count, 'interval_count': moments.n,
                               'period_us': moments.mean if moments.n >= self.settings.min_intervals else None,
                               'std_us': moments.std}
        self.ready = True
        self.baseline_document = self.profile_document()
        self.baseline_sink(self.baseline_document)

    def profile_document(self):
        return {'format': 'can-live-baseline-v1', 'ready': self.ready and bool(self.profile),
                'dbc_sha256': self.checksum, 'channel': self.settings.channel,
                'learning_start_us': self.learning_start, 'learning_end_us': self.time,
                'min_intervals': self.settings.min_intervals,
                'reference_is_normal': 'assumption supplied by operator, not verified by detector',
                'messages': {str(i): v for i, v in self.profile.items()}}

    def finish(self):
        self._transport_errors()
        if self.decoder._buffer or self.decoder._overflow:
            self.quality('truncated_last_record', self.time, None)
        # Never invent the future end of a partial frequency window.
        if not self.ready:
            self.baseline_sink(self.profile_document())

    def snapshot(self):
        return {'decoder': self.decoder.counters.as_dict(), 'counters': dict(self.counters),
                'session_record': self.session_record, 'last_device_time_us': self.time,
                'baseline_state': 'ready' if self.ready else 'learning',
                'periodic_ids': [i for i, e in self.profile.items() if e.get('period_us')],
                'ids_without_period': [i for i, e in self.profile.items() if not e.get('period_us')],
                'sigma_disabled_zero_variance': [i for i, e in self.profile.items()
                                                if e.get('period_us') and not e['std_us']],
                'events_by_type': dict(self.events_by_type), 'events_by_method': dict(self.events_by_method),
                'settings': asdict(self.settings),
                'absolute_time_available': self.rtc is not None,
                'quality_note': 'Intervals are provisional until diagnostic counters arrive; exclude overlaps during evaluation.',
                'has_quality_issues': bool(self.counters['quality_intervals'])}
