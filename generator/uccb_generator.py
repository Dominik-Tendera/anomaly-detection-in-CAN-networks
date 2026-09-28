"""DBC reference traffic and acknowledged uCCB/SLCAN transport.

Initialization follows uCCBViewer-p2.6/python/uccbviewer/usbtin.py.
An adapter acknowledgement is NOT a measured CAN-bus transmission timestamp.
This module is independent of the unfinished can_detect orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
import random
import time


BITRATES = {10000: '0', 20000: '1', 50000: '2', 100000: '3',
            125000: '4', 250000: '5', 500000: '6', 800000: '7',
            1000000: '8', 83300: '9'}
HOST_BAUD = 115200


class AdapterError(RuntimeError):
    pass


class CommandRejected(AdapterError):
    pass


class UccbAdapter:
    """One outstanding command; consume RX frames while awaiting its reply."""

    def __init__(self, port, bitrate, timeout=1.0, serial_factory=None):
        self.port_name, self.bitrate, self.timeout = port, bitrate, timeout
        self.serial_factory = serial_factory
        self.port = None
        self.opened = False
        self.received_frames = 0
        self.acknowledged_frames = 0
        self.info = {}

    def command(self, text):
        data = (text + '\r').encode('ascii')
        if self.port.write(data) != len(data):
            raise AdapterError('Niepelny zapis komendy do adaptera')
        deadline = time.monotonic() + self.timeout
        line = bytearray()
        while time.monotonic() < deadline:
            byte = self.port.read(1)
            if not byte:
                continue
            if byte == b'\x07':
                raise CommandRejected(f'Adapter odrzucil komende {text!r} (BELL)')
            if byte == b'\r':
                reply = line.decode('ascii', errors='replace')
                line.clear()
                if reply and reply[0] in 'tTrR':
                    self.received_frames += 1
                    continue
                return reply
            line.extend(byte)
            if len(line) > 128:
                raise AdapterError('Niepoprawna odpowiedz SLCAN (za dluga linia)')
        raise AdapterError(f'Brak odpowiedzi adaptera na {text!r}')

    def _ok(self, command):
        reply = self.command(command)
        if reply != '':
            raise AdapterError(f'Nieoczekiwana odpowiedz na {command!r}: {reply!r}')

    def connect(self):
        if self.serial_factory is None:
            import serial
            self.serial_factory = serial.Serial
        self.port = self.serial_factory(
            port=self.port_name, baudrate=HOST_BAUD, bytesize=8, parity='N',
            stopbits=1, timeout=min(0.05, self.timeout), write_timeout=self.timeout)
        try:
            self.port.write(b'\rC\r')
            time.sleep(0.1)
            self.port.reset_input_buffer()
            self.port.reset_output_buffer()
            try:
                self._ok('C')
            except CommandRejected:
                pass  # Viewer also accepts "already closed" here.
            for cmd, name in [('v', 'firmware'), ('V', 'hardware'), ('N', 'serial')]:
                try:
                    reply = self.command(cmd)
                    if not reply.startswith(cmd):
                        raise AdapterError(f'Niepoprawna odpowiedz na {cmd}: {reply!r}')
                    self.info[name] = reply[1:]
                except CommandRejected:
                    if cmd != 'N':
                        raise
            try:
                self._ok('W2D00')
            except CommandRejected:
                pass  # Optional, also optional in the Viewer.
            self._ok('S' + BITRATES[self.bitrate])
            self._ok('O')
            self.opened = True
        except BaseException:
            self.port.close()
            self.port = None
            raise

    def send(self, can_id, data):
        if not self.opened:
            raise AdapterError('Kanal CAN nie jest otwarty')
        if not 0 <= can_id <= 0x7ff or len(data) > 8:
            raise ValueError('Wymagane standardowe CAN 11-bit, DLC <= 8')
        reply = self.command(f't{can_id:03X}{len(data):X}{data.hex().upper()}')
        if reply not in ('', 'z', 'Z'):
            raise AdapterError(f'Nieoczekiwane potwierdzenie TX: {reply!r}')
        self.acknowledged_frames += 1

    def close(self):
        if self.port is not None:
            try:
                if self.opened:
                    self._ok('C')
            finally:
                self.port.close()
                self.port = None
                self.opened = False


def raw_bounds(signal):
    if signal.is_float:
        raise ValueError(f'{signal.name}: sygnal zmiennoprzecinkowy nieobslugiwany')
    if not signal.scale or not math.isfinite(float(signal.scale)):
        raise ValueError(f'{signal.name}: niepoprawna skala')
    lower = -(1 << (signal.length - 1)) if signal.is_signed else 0
    upper = (1 << (signal.length - (1 if signal.is_signed else 0))) - 1
    # [0|0] is commonly used for unspecified ranges in this station DBC.
    if signal.minimum is not None and signal.maximum is not None and signal.minimum != signal.maximum:
        a = (float(signal.minimum) - signal.offset) / signal.scale
        b = (float(signal.maximum) - signal.offset) / signal.scale
        lower = max(lower, math.ceil(min(a, b) - 1e-9))
        upper = min(upper, math.floor(max(a, b) + 1e-9))
    if lower > upper:
        raise ValueError(f'{signal.name}: zakres DBC nie zawiera kodowalnej wartosci')
    return lower, upper


def make_payload(message, elapsed, mode='sine', wave_period=10.0):
    """Quantize in raw units, then let cantools handle packing and endianness."""
    values = {}
    for index, signal in enumerate(message.signals):
        lower, upper = raw_bounds(signal)
        choices = sorted(k for k in (signal.choices or {}) if lower <= k <= upper)
        if signal.is_multiplexer:
            branches = sorted({v for s in message.signals
                               if s.multiplexer_signal == signal.name
                               for v in (s.multiplexer_ids or []) if lower <= v <= upper})
            values[signal.name] = (branches or choices or [lower])[0]
        elif choices:
            values[signal.name] = choices[0]  # Keep enumerated states stable.
        elif signal.minimum is None or signal.maximum is None or signal.minimum == signal.maximum:
            values[signal.name] = min(upper, max(lower, 0))
        else:
            phase = (message.frame_id % 17) / 17 + index / max(1, len(message.signals))
            fraction = 0.5 if mode == 'constant' else 0.5 + 0.4 * math.sin(
                2 * math.pi * (elapsed / wave_period + phase))
            values[signal.name] = min(upper, max(lower, round(lower + fraction * (upper - lower))))
    # Inactive multiplexed signals may be present in values. Range validity was
    # enforced above; strict=False also permits the conventional [0|0] fields.
    return message.encode(values, scaling=False, strict=False)


def has_out_of_range_signal(message):
    """Return whether an encodable value can violate a finite DBC range."""
    try:
        normal = make_payload(message, 0.0)
        raw_values = message.decode(normal, scaling=False, decode_choices=False)
    except (ValueError, OverflowError, KeyError, TypeError):
        return False
    for signal in message.signals:
        if signal.name not in raw_values or signal.is_multiplexer or signal.is_float:
            continue
        if signal.minimum is None or signal.maximum is None or signal.minimum == signal.maximum:
            continue
        try:
            valid_low, valid_high = raw_bounds(signal)
        except (ValueError, OverflowError, TypeError):
            continue
        domain_low = -(1 << (signal.length - 1)) if signal.is_signed else 0
        domain_high = (1 << (signal.length - (1 if signal.is_signed else 0))) - 1
        if valid_low > domain_low or valid_high < domain_high:
            return True
    return False


@dataclass
class TrafficMessage:
    message: object
    period: float
    phase: float = 0.0
    period_source: str = 'default'


def message_period_ms(message, override_ms=None, default_ms=100.0):
    """Return the period and its source.

    cantools recognizes the standard ``GenMsgCycleTime`` attribute as
    ``cycle_time``.  The DBC used in this project stores the same information
    in a Vector-style custom BO_ attribute named ``Period`` instead, so it has
    to be read explicitly.
    """
    if override_ms is not None:
        return float(override_ms), 'command_line'
    if message.cycle_time is not None:
        return float(message.cycle_time), 'dbc:GenMsgCycleTime'
    dbc_specifics = getattr(message, 'dbc', None)
    attributes = getattr(dbc_specifics, 'attributes', {}) or {}
    attribute = attributes.get('Period')
    if attribute is not None:
        value = getattr(attribute, 'value', attribute)
        return float(value), 'dbc:Period'
    return float(default_ms), 'default'


def make_plan(database, ids=None, max_messages=20, period_ms=None,
              max_fps=100.0, host_fraction=0.4, bitrate=1000000,
              mode='sine', wave_period=10.0, seed=None,
              phase_mode='random', default_period_ms=100.0):
    selected, skipped = [], []
    fps = host_bps = bus_bps = 0.0
    available = {m.frame_id for m in database.messages}
    if ids and set(ids) - available:
        raise ValueError('ID nieobecne w DBC: ' + ', '.join(hex(i) for i in sorted(set(ids) - available)))
    if phase_mode not in ('random', 'staggered', 'zero'):
        raise ValueError('phase_mode musi byc jednym z: random, staggered, zero')
    rng = random.Random(seed)
    candidates = sorted(database.messages, key=lambda m: m.frame_id)
    if not ids:
        rng.shuffle(candidates)
        # Always reserve one range-capable frame for the built-in and matrix
        # out-of-range scenarios.  The slowest eligible frame is preferred so
        # this reservation remains feasible under the USB/CAN budgets.  Both
        # the reference generator and the anomaly generator call make_plan,
        # therefore the selected set remains reproducible for a given seed.
        bounded = [m for m in candidates if has_out_of_range_signal(m)]
        if bounded:
            required = max(
                bounded,
                key=lambda m: message_period_ms(m, period_ms, default_period_ms)[0],
            )
            candidates.remove(required)
            candidates.insert(0, required)
    for message in candidates:
        if ids and message.frame_id not in ids:
            continue
        reason = None
        if message.is_extended_frame or message.is_fd or message.length > 8:
            reason = 'tylko standardowe CAN 11-bit, DLC <= 8'
        period_value_ms, period_source = message_period_ms(
            message, period_ms, default_period_ms)
        period = period_value_ms / 1000
        if not math.isfinite(period) or period <= 0:
            reason = 'niepoprawny okres'
        if reason is None:
            try:
                make_payload(message, 0, mode, wave_period)
            except (ValueError, OverflowError, KeyError) as exc:
                reason = f'kodowanie: {exc}'
        if reason is None:
            rate = 1 / period
            host_cost = (6 + 2 * message.length) * 10 * rate
            # Conservative stuffed CAN frame estimate including intermission.
            bus_cost = (math.ceil((34 + 8 * message.length) * 1.25) + 13) * rate
            if (fps + rate > max_fps + 1e-9 or
                    host_bps + host_cost > HOST_BAUD * host_fraction + 1e-9 or
                    bus_bps + bus_cost > bitrate * 0.3 + 1e-9):
                reason = 'budzet przepustowosci (fps / USB-serial / CAN)'
            elif not ids and len(selected) >= max_messages:
                reason = 'limit liczby wiadomosci'
        if reason:
            if ids:
                raise ValueError(f'0x{message.frame_id:03X} {message.name}: {reason}; zwieksz okres lub ogranicz ID')
            skipped.append({'can_id': message.frame_id, 'name': message.name, 'reason': reason})
            continue
        selected.append(TrafficMessage(message, period, period_source=period_source))
        fps += rate
        host_bps += host_cost
        bus_bps += bus_cost
    if not selected:
        raise ValueError('Brak wiadomosci mieszczacych sie w wybranych limitach')
    for index, item in enumerate(selected):
        if phase_mode == 'random':
            item.phase = rng.random() * item.period
        elif phase_mode == 'staggered':
            item.phase = item.period * index / len(selected)
        else:
            item.phase = 0.0
    return selected, {'nominal_fps': fps, 'host_bits_per_second': host_bps,
                      'host_utilisation': host_bps / HOST_BAUD,
                      'estimated_can_utilisation_upper': bus_bps / bitrate,
                      'skipped': skipped}


def new_stats(plan):
    return {'commands_attempted': 0, 'commands_acknowledged': 0,
            'slots_skipped_late': 0, 'max_lateness_s': 0.0,
            'per_id': {str(p.message.frame_id): {'acknowledged': 0, 'skipped': 0} for p in plan}}


def transmit(plan, sender, duration, log, stats, mode='sine', wave_period=10.0,
             clock=time.monotonic, sleeper=time.sleep, progress=None):
    """Bounded-state scheduler; skip expired slots, never catch up in bursts."""
    start = clock()
    heap = [(p.phase, i, 0) for i, p in enumerate(plan)]
    heapq.heapify(heap)
    last_progress = start
    while heap:
        scheduled, index, slot = heapq.heappop(heap)
        item = plan[index]
        if scheduled >= duration:
            continue
        delay = start + scheduled - clock()
        if delay > 0:
            sleeper(delay)
        now = clock() - start
        # A slot more than half a period late would turn normal traffic into
        # a catch-up burst. Discard expired slots up to the next future one.
        if now >= duration or now - scheduled > item.period / 2:
            last_due = min(now, math.nextafter(duration, -math.inf))
            count = max(1, math.floor((last_due - scheduled) / item.period) + 1)
            stats['slots_skipped_late'] += count
            stats['per_id'][str(item.message.frame_id)]['skipped'] += count
            slot += count
        else:
            data = make_payload(item.message, scheduled, mode, wave_period)
            command_time = clock() - start
            stats['commands_attempted'] += 1
            sender.send(item.message.frame_id, data)
            ack_time = clock() - start
            lateness = command_time - scheduled
            stats['commands_acknowledged'] += 1
            stats['per_id'][str(item.message.frame_id)]['acknowledged'] += 1
            stats['max_lateness_s'] = max(stats['max_lateness_s'], lateness)
            log({'scheduled_s': scheduled, 'command_started_s': command_time,
                 'adapter_ack_s': ack_time, 'lateness_s': lateness,
                 'can_id': item.message.frame_id, 'dlc': len(data), 'data': data.hex()})
            slot += 1
        next_time = item.phase + slot * item.period
        heapq.heappush(heap, (next_time, index, slot))
        if clock() - last_progress >= 1:
            if progress:
                progress(clock() - start)
            last_progress = clock()
    remaining = start + duration - clock()
    if remaining > 0:
        sleeper(remaining)
    stats['elapsed_s'] = clock() - start
    stats['acknowledged_fps'] = stats['commands_acknowledged'] / stats['elapsed_s'] if stats['elapsed_s'] else 0


class VirtualAdapter:
    """Dry-run clock and sender; never opens a serial port."""
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def send(self, can_id, data):
        self.now += (6 + 2 * len(data)) * 10 / HOST_BAUD
