"""Build finite, reproducible CAN schedules with anomaly ground truth."""
from __future__ import annotations

from dataclasses import dataclass
import math

from can_generate.episodes import AnomalyEpisode, AnomalyType
from can_generate.time_reference import MARKER_END, MARKER_START, encode_marker
from uccb_generator import make_payload, raw_bounds


@dataclass
class ScheduledFrame:
    scheduled_s: float
    can_id: int
    data: bytes
    kind: str = 'reference'
    episode_id: str | None = None
    late_tolerance_s: float = .05


def out_of_range_payload(message, elapsed, mode='sine', wave_period=10.0,
                         *, direction='nearest', raw_steps=1, signal_name=None):
    """Create one encodable value that violates a physical DBC range."""
    normal = make_payload(message, elapsed, mode, wave_period)
    raw_values = message.decode(normal, scaling=False, decode_choices=False)
    if direction not in ('nearest', 'above', 'below') or int(raw_steps) < 1:
        raise ValueError('direction: nearest/above/below; raw_steps >= 1')
    for signal in message.signals:
        if signal_name is not None and signal.name != signal_name:
            continue
        if signal.name not in raw_values or signal.is_multiplexer or signal.is_float:
            continue
        if signal.minimum is None or signal.maximum is None or signal.minimum == signal.maximum:
            continue
        valid_low, valid_high = raw_bounds(signal)
        domain_low = -(1 << (signal.length - 1)) if signal.is_signed else 0
        domain_high = (1 << (signal.length - (1 if signal.is_signed else 0))) - 1
        steps = int(raw_steps)
        if direction in ('nearest', 'above') and valid_high < domain_high:
            invalid_raw = min(domain_high, valid_high + steps)
        elif direction in ('nearest', 'below') and valid_low > domain_low:
            invalid_raw = max(domain_low, valid_low - steps)
        else:
            continue
        changed = dict(raw_values)
        changed[signal.name] = invalid_raw
        payload = message.encode(changed, scaling=False, strict=False)
        physical = float(invalid_raw * signal.scale + signal.offset)
        return payload, {'signal': signal.name, 'raw_value': invalid_raw,
                         'physical_value': physical,
                         'minimum': signal.minimum, 'maximum': signal.maximum}
    raise ValueError(f'0x{message.frame_id:03X} {message.name}: brak kodowalnej wartosci poza zakresem DBC')


def _pick_timing_target(plan):
    return min(plan, key=lambda item: (abs(item.period - .1), item.message.frame_id))


def _pick_second_target(plan, first):
    candidates = [item for item in plan if item.message.frame_id != first.message.frame_id]
    return min(candidates or plan, key=lambda item: (abs(item.period - .1), item.message.frame_id))


def _pick_range_target(plan, mode, wave_period, **options):
    for item in sorted(plan, key=lambda value: value.message.frame_id):
        try:
            out_of_range_payload(item.message, 0, mode, wave_period, **options)
            return item
        except (ValueError, KeyError, OverflowError):
            pass
    raise ValueError('Wybrany zestaw ID nie zawiera sygnalu z ograniczonym zakresem DBC')


def default_suite(plan, database, *, duration=120.0, mode='sine', wave_period=10.0):
    """Return a suite covering all rule-based anomaly families at two intensities."""
    if duration < 110:
        raise ValueError('Pakiet all wymaga --duration co najmniej 110 s')
    target = _pick_timing_target(plan)
    second = _pick_second_target(plan, target)
    dlc_target = next((item for item in plan if item.message.length > 0), target)
    range_target = _pick_range_target(plan, mode, wave_period)
    used = {message.frame_id for message in database.messages}
    unknown_id = next((value for value in range(0x7FD, -1, -1) if value not in used), None)
    if unknown_id is None:
        raise ValueError('Brak wolnego standardowego ID dla scenariusza unknown_dbc_id')
    fast_interval = max(.002, min(.010, second.period * .10))
    definitions = [
        (AnomalyType.FREQUENCY_INCREASE, 15, 8, target.message.frame_id,
         {'frequency_factor': 1.5, 'variant': 'mild'}),
        (AnomalyType.FREQUENCY_INCREASE, 27, 8, target.message.frame_id,
         {'frequency_factor': 4.0, 'variant': 'strong'}),
        (AnomalyType.FREQUENCY_DECREASE, 39, 8, target.message.frame_id,
         {'frequency_factor': .5, 'variant': 'half_rate'}),
        (AnomalyType.DISAPPEARANCE, 51, 8, target.message.frame_id,
         {'variant': 'temporary_silence'}),
        (AnomalyType.BURST, 63, 2, second.message.frame_id,
         {'count': 8, 'interval_s': fast_interval, 'variant': 'short'}),
        (AnomalyType.BURST, 71, 2, second.message.frame_id,
         {'count': 30, 'interval_s': .010, 'variant': 'dense'}),
        (AnomalyType.UNKNOWN_DBC_ID, 79, 4, unknown_id,
         {'count': 8, 'interval_s': .05, 'data_hex': 'DEADBEEF', 'variant': 'repeated'}),
        (AnomalyType.DLC_MISMATCH, 87, 4, dlc_target.message.frame_id,
         {'count': 8, 'interval_s': .05, 'variant': 'shorter'}),
        (AnomalyType.OUT_OF_RANGE_SIGNAL, 95, 4, range_target.message.frame_id,
         {'count': 8, 'interval_s': .20, 'variant': 'above_or_below_limit'}),
    ]
    return [AnomalyEpisode(kind, start, length, can_id, parameters)
            for kind, start, length, can_id, parameters in definitions]


def parse_episodes(items):
    episodes = []
    for item in items:
        value = dict(item)
        kind = value.pop('type')
        parameters = value.pop('parameters', {})
        can_id = value.pop('can_id', None)
        if isinstance(can_id, str):
            can_id = int(can_id, 0)
        episodes.append(AnomalyEpisode(kind, can_id=can_id, parameters=parameters, **value))
    return episodes


def resolve_auto_targets(episodes, plan, database, mode='sine', wave_period=10.0):
    timing = _pick_timing_target(plan)
    second = _pick_second_target(plan, timing)
    dlc_shorter = next((item for item in plan if item.message.length > 0), timing)
    dlc_longer = next((item for item in plan if item.message.length < 8), timing)
    used = {message.frame_id for message in database.messages}
    unknown = next(value for value in range(0x7FD, -1, -1) if value not in used)
    resolved = []
    for episode in episodes:
        if episode.can_id is not None:
            resolved.append(episode)
            continue
        if episode.kind in (AnomalyType.FREQUENCY_INCREASE,
                            AnomalyType.FREQUENCY_DECREASE,
                            AnomalyType.DISAPPEARANCE):
            can_id = timing.message.frame_id
        elif episode.kind == AnomalyType.BURST:
            can_id = second.message.frame_id
        elif episode.kind == AnomalyType.DLC_MISMATCH:
            variant = str(episode.parameters.get('variant', 'shorter'))
            can_id = (dlc_longer if variant == 'longer' else dlc_shorter).message.frame_id
        elif episode.kind == AnomalyType.OUT_OF_RANGE_SIGNAL:
            options = {key: episode.parameters[key] for key in
                       ('direction', 'raw_steps', 'signal_name') if key in episode.parameters}
            can_id = _pick_range_target(plan, mode, wave_period, **options).message.frame_id
        else:
            can_id = unknown
        resolved.append(AnomalyEpisode(episode.kind, episode.start, episode.duration,
                                       can_id, episode.parameters))
    return resolved


def build_schedule(plan, episodes, duration, *, marker_id=0x7FE,
                   mode='sine', wave_period=10.0):
    """Create reference slots, apply timing modifiers, then add injected frames."""
    by_id = {item.message.frame_id: item for item in plan}
    modifiers = {}
    for index, episode in enumerate(episodes, 1):
        if episode.end > duration:
            raise ValueError(f'E{index:02d} konczy sie po czasie sesji')
        if episode.kind != AnomalyType.UNKNOWN_DBC_ID and episode.can_id not in by_id:
            raise ValueError(f'E{index:02d}: ID 0x{episode.can_id:03X} nie nalezy do planu ruchu')
        if episode.kind in (AnomalyType.FREQUENCY_INCREASE,
                            AnomalyType.FREQUENCY_DECREASE,
                            AnomalyType.DISAPPEARANCE):
            modifiers.setdefault(episode.can_id, []).append((index, episode))
    for can_id, values in modifiers.items():
        ordered = sorted(values, key=lambda pair: pair[1].start)
        if any(left[1].end > right[1].start for left, right in zip(ordered, ordered[1:])):
            raise ValueError(f'Nakladajace sie modyfikacje czasu dla 0x{can_id:03X}')

    frames = [ScheduledFrame(0, marker_id, encode_marker(MARKER_START, 0),
                             kind='marker_start', late_tolerance_s=.1)]
    truth = []
    for item in plan:
        slot = 0
        while True:
            when = item.phase + slot * item.period
            if when >= duration:
                break
            active = next(((number, episode) for number, episode in modifiers.get(item.message.frame_id, [])
                           if episode.start <= when < episode.end), None)
            if active is None:
                frames.append(ScheduledFrame(
                    when, item.message.frame_id,
                    make_payload(item.message, when, mode, wave_period),
                    late_tolerance_s=max(.001, item.period / 2)))
            slot += 1

    for number, episode in enumerate(episodes, 1):
        episode_id = f'E{number:02d}'
        item = by_id.get(episode.can_id)
        parameters = dict(episode.parameters)
        injected = []
        if episode.kind in (AnomalyType.FREQUENCY_INCREASE, AnomalyType.FREQUENCY_DECREASE):
            factor = float(parameters.get('frequency_factor', 2.0))
            if (episode.kind == AnomalyType.FREQUENCY_INCREASE and factor <= 1) or (
                    episode.kind == AnomalyType.FREQUENCY_DECREASE and not 0 < factor < 1):
                raise ValueError(f'{episode_id}: niepoprawny frequency_factor {factor}')
            interval = item.period / factor
            count = math.ceil(episode.duration / interval)
            injected = [(episode.start + i * interval,
                         make_payload(item.message, episode.start + i * interval, mode, wave_period))
                        for i in range(count) if episode.start + i * interval < episode.end]
            parameters['reference_period_s'] = item.period
            parameters['anomalous_period_s'] = interval
        elif episode.kind == AnomalyType.DISAPPEARANCE:
            injected = []
        else:
            count = int(parameters.get('count', 1))
            interval = float(parameters.get('interval_s', .05))
            if count < 1 or interval <= 0:
                raise ValueError(f'{episode_id}: count i interval_s musza byc dodatnie')
            times = [episode.start + i * interval for i in range(count)
                     if episode.start + i * interval < episode.end]
            if episode.kind == AnomalyType.UNKNOWN_DBC_ID:
                payload = bytes.fromhex(str(parameters.get('data_hex', 'DEADBEEF')))
                injected = [(when, payload) for when in times]
            elif episode.kind == AnomalyType.DLC_MISMATCH:
                normal = make_payload(item.message, episode.start, mode, wave_period)
                if 'dlc' in parameters:
                    dlc = int(parameters['dlc'])
                elif parameters.get('variant') == 'longer':
                    dlc = item.message.length + 1
                else:
                    dlc = max(0, item.message.length - 1) if item.message.length else 1
                if not 0 <= dlc <= 8 or dlc == item.message.length:
                    raise ValueError(f'{episode_id}: DLC musi byc 0..8 i rozne od DBC')
                payload = (normal + bytes(8))[:dlc]
                parameters['dbc_dlc'] = item.message.length
                parameters['injected_dlc'] = dlc
                injected = [(when, payload) for when in times]
            elif episode.kind == AnomalyType.OUT_OF_RANGE_SIGNAL:
                options = {key: parameters[key] for key in
                           ('direction', 'raw_steps', 'signal_name') if key in parameters}
                payload, details = out_of_range_payload(
                    item.message, episode.start, mode, wave_period, **options)
                parameters.update(details)
                injected = [(when, payload) for when in times]
            elif episode.kind == AnomalyType.BURST:
                injected = [(when, make_payload(item.message, when, mode, wave_period)) for when in times]
            else:
                raise ValueError(f'{episode_id}: nieobslugiwany typ {episode.kind.value}')
        interval_hint = float(parameters.get('anomalous_period_s', parameters.get('interval_s', item.period if item else .1)))
        for when, payload in injected:
            frames.append(ScheduledFrame(when, episode.can_id, payload,
                                         kind=episode.kind.value, episode_id=episode_id,
                                         late_tolerance_s=max(.001, interval_hint)))
        truth.append({'episode_id': episode_id, 'type': episode.kind.value,
                      'start': episode.start, 'end': episode.end,
                      'can_id': episode.can_id, 'parameters': parameters,
                      'planned_frames': len(injected), 'acknowledged_frames': 0,
                      'skipped_frames': 0})
    # Put the end marker after every traffic slot. It is allowed to finish just
    # beyond ``duration`` so the marker itself cannot make a final normal slot
    # look late or missing.
    end_time = duration
    frames.append(ScheduledFrame(end_time, marker_id,
                                 encode_marker(MARKER_END, round(end_time * 1_000_000)),
                                 kind='marker_end', late_tolerance_s=.1))
    frames.sort(key=lambda frame: (frame.scheduled_s, frame.kind != 'marker_start',
                                   frame.can_id, frame.episode_id or ''))
    return frames, truth


def transmit_schedule(frames, sender, duration, log, stats, *, clock, sleeper,
                      progress=None):
    start = clock()
    last_progress = start
    truth_stats = stats['per_episode']
    for frame in frames:
        delay = start + frame.scheduled_s - clock()
        if delay > 0:
            sleeper(delay)
        command_time = clock() - start
        if ((command_time >= duration and frame.kind != 'marker_end') or
                command_time - frame.scheduled_s > frame.late_tolerance_s):
            stats['slots_skipped_late'] += 1
            if frame.episode_id:
                truth_stats[frame.episode_id]['skipped_frames'] += 1
            continue
        stats['commands_attempted'] += 1
        sender.send(frame.can_id, frame.data)
        ack_time = clock() - start
        stats['commands_acknowledged'] += 1
        stats['max_lateness_s'] = max(stats['max_lateness_s'], command_time - frame.scheduled_s)
        if frame.episode_id:
            truth_stats[frame.episode_id]['acknowledged_frames'] += 1
        log({'scheduled_s': frame.scheduled_s, 'command_started_s': command_time,
             'adapter_ack_s': ack_time, 'lateness_s': command_time - frame.scheduled_s,
             'can_id': frame.can_id, 'dlc': len(frame.data), 'data': frame.data.hex(),
             'kind': frame.kind, 'episode_id': frame.episode_id})
        if clock() - last_progress >= 1:
            if progress:
                progress(clock() - start)
            last_progress = clock()
    remaining = start + duration - clock()
    if remaining > 0:
        sleeper(remaining)
    stats['elapsed_s'] = clock() - start
    stats['acknowledged_fps'] = stats['commands_acknowledged'] / stats['elapsed_s'] if stats['elapsed_s'] else 0
