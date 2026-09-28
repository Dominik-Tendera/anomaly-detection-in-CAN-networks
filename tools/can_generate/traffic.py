"""Deterministic reference-traffic scheduling for the CAN generator.

The scheduler deliberately knows nothing about serial hardware.  It accepts the
same ``send(can_id, data)`` interface as :class:`can_generate.slcan.SLCANClient`,
which makes timing and payload construction testable without an adapter.

Each configured period is represented by an absolute deadline on a monotonic
clock.  After a late send, the next deadline remains the previous deadline plus
one period, rather than being calculated from the late send time.  This avoids
fixed-sleep drift while keeping the schedule deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import random
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from can_detect.dbc import DbcDatabase, MessageDefinition, SignalDefinition
from .episodes import AnomalyEpisode, AnomalyType
from .time_reference import (
    DEFAULT_MARKER_CAN_ID,
    MARKER_END,
    MARKER_START,
    encode_marker,
    validate_marker_id,
)
from .truth import GroundTruthRecorder


# The measured/inferred host-link ceiling for SLCAN DLC8 frames.  Keep this
# explicit and approximate: the generator reports a warning, not a hard limit.
HOST_LINK_CAPACITY_DLC8 = 520.0
HOST_LINK_CAPACITY_WARNING = (
    "requested DLC8 frame rate exceeds the approximate host-link capacity "
    "of 520 frames/s"
)


class FrameSender(Protocol):
    def send(self, can_id: int, data: bytes) -> None:
        """Send one standard CAN data frame."""


class FileFrameSender:
    """Write transmitted frames as deterministic JSON Lines for replay tests."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="\n")

    def send(self, can_id: int, data: bytes) -> None:
        self._file.write(json.dumps(
            {"can_id": can_id, "data": data.hex()},
            sort_keys=True, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "FileFrameSender":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class TrafficConfigurationError(ValueError):
    """A reference-traffic message is not valid for the selected DBC."""


@dataclass(frozen=True)
class ReferenceMessage:
    """One periodic message in a reference-traffic configuration.

    ``payload`` is optional when ``signal_values`` is supplied.  Missing bytes
    are initialized to zero, then the supplied physical signal values are
    packed according to the selected DBC message definition.
    """

    can_id: int
    period: float
    payload: bytes | bytearray | Sequence[int] | None = None
    signal_values: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledFrame:
    """A frame emitted by a run, with time relative to the run start."""

    elapsed: float
    can_id: int
    data: bytes


@dataclass(frozen=True)
class TrafficRun:
    """Hardware-independent result of one reference-traffic run.

    ``frames`` contains frames whose send call completed within the requested
    run interval.  A slow host link can therefore leave planned frames
    unsent; the counters below make that loss explicit instead of silently
    extending the run past its requested duration.
    """

    frames: tuple[ScheduledFrame, ...]
    duration: float
    session_id: str | None = None
    frames_requested: int = 0
    frames_sent: int = 0
    frames_not_sent: int = 0
    requested_rate: float = 0.0
    achieved_rate: float = 0.0
    warnings: tuple[str, ...] = ()

    @property
    def counts_by_id(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for frame in self.frames:
            counts[frame.can_id] = counts.get(frame.can_id, 0) + 1
        return counts


class ReferenceTrafficGenerator:
    """Build and emit periodic traffic selected from a :class:`DbcDatabase`."""

    def __init__(
        self,
        database: DbcDatabase,
        messages: Iterable[ReferenceMessage],
        *,
        seed: int | None = None,
    ) -> None:
        self.database = database
        if seed is not None and not isinstance(seed, int):
            raise TrafficConfigurationError("generator seed must be an integer or None")
        self.seed = seed
        # Keep an owned RNG so future randomized scenario choices cannot depend
        # on process-global random state.  Current configured payloads remain
        # unchanged, making the seed an explicit part of the reproducibility
        # contract without changing legacy traffic.
        self._rng = random.Random(seed)
        self.messages = tuple(self._prepare(message) for message in messages)
        if not self.messages:
            raise TrafficConfigurationError("at least one reference message is required")

    @property
    def configuration(self) -> dict[str, object]:
        """Return the canonical, serializable configuration of this generator."""
        return {
            "seed": self.seed,
            "messages": [
                {"can_id": message.can_id, "period": message.period,
                 "payload": message.payload.hex(),
                 "signal_values": dict(message.signal_values)}
                for message in self.messages
            ],
        }

    @classmethod
    def from_config(
        cls,
        database: DbcDatabase,
        config: Iterable[ReferenceMessage | Mapping[str, object]],
        *,
        seed: int | None = None,
    ) -> "ReferenceTrafficGenerator":
        """Create a generator from dataclasses or simple text-config mappings.

        Mapping entries use ``can_id``, ``period`` and optionally ``payload``
        and ``signal_values``.  Payloads may be bytes, an iterable of integer
        octets, or a hexadecimal string with or without spaces.
        """
        messages = []
        for entry in config:
            if isinstance(entry, ReferenceMessage):
                messages.append(entry)
                continue
            try:
                messages.append(
                    ReferenceMessage(
                        can_id=int(entry["can_id"]),
                        period=float(entry["period"]),
                        payload=entry.get("payload"),
                        signal_values=entry.get("signal_values", {}),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise TrafficConfigurationError(
                    "each reference message needs can_id and period"
                ) from exc
        return cls(database, messages, seed=seed)

    def run(
        self,
        duration: float,
        sender: FrameSender | Callable[[int, bytes], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        session_id: str | None = None,
        ground_truth: GroundTruthRecorder | None = None,
        anomalies: Iterable[AnomalyEpisode] = (),
    ) -> TrafficRun:
        """Emit reference traffic and optional independent anomaly episodes.

        The relative schedule is planned before transmission. This keeps anomaly
        placement deterministic and works identically with a real SLCAN sender
        and a hardware-free recording sender. Transmission still uses absolute
        deadlines, so adapter send time does not drift configured periods.
        """
        if duration < 0:
            raise ValueError("duration must be non-negative")
        if ground_truth is not None and session_id is None:
            session_id = ground_truth.session_id
        if duration == 0:
            return TrafficRun((), duration, session_id)
        planned = self._apply_anomalies(
            self._plan_frames(duration), duration, tuple(anomalies)
        )
        start = clock()
        end = start + duration
        frames: list[ScheduledFrame] = []
        for frame in planned:
            deadline = start + frame.elapsed
            while True:
                wait = deadline - clock()
                if wait <= 0:
                    break
                sleeper(wait)
            # Do not silently keep transmitting after the requested interval.
            # The current and all remaining planned frames are reported as
            # unsent when the host link has fallen behind the schedule.
            if clock() >= end:
                break
            self._send(sender, frame.can_id, frame.data)
            frames.append(frame)

        requested = len(planned)
        sent = len(frames)
        not_sent = requested - sent
        requested_rate = requested / duration
        achieved_rate = sent / duration
        dlc8_requested = sum(len(frame.data) == 8 for frame in planned)
        warnings: tuple[str, ...] = ()
        if dlc8_requested / duration > HOST_LINK_CAPACITY_DLC8:
            warnings = (HOST_LINK_CAPACITY_WARNING,)
        result = TrafficRun(
            tuple(frames), duration, session_id, requested, sent, not_sent,
            requested_rate, achieved_rate, warnings,
        )
        if ground_truth is not None:
            if ground_truth.session_id != session_id:
                raise TrafficConfigurationError(
                    "ground-truth session ID must match the traffic run session ID"
                )
            if ground_truth.seed is None:
                ground_truth.seed = self.seed
            elif ground_truth.seed != self.seed:
                raise TrafficConfigurationError(
                    "ground-truth seed must match the traffic generator seed"
                )
            ground_truth.finalize_achieved_intensity(result.frames)
        return result

    def run_session(
        self,
        duration: float,
        sender: FrameSender | Callable[[int, bytes], None],
        *,
        marker_can_id: int = DEFAULT_MARKER_CAN_ID,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        session_id: str | None = None,
        anomalies: Iterable[AnomalyEpisode] = (),
    ) -> TrafficRun:
        """Run traffic bracketed by generator-clock marker CAN frames.

        Marker timestamps use the same monotonic clock as the schedule and are
        encoded in microseconds. The marker ID is intentionally not required
        to occur in the DBC, because it belongs to the measurement harness.
        """
        if duration < 0:
            raise ValueError("duration must be non-negative")
        marker_can_id = validate_marker_id(marker_can_id)
        if marker_can_id in {message.can_id for message in self.messages}:
            raise TrafficConfigurationError(
                "marker CAN ID must be dedicated and not used by reference traffic")

        start = clock()
        start_data = encode_marker(MARKER_START, int(round(start * 1_000_000)))
        self._send(sender, marker_can_id, start_data)
        frames = [ScheduledFrame(0.0, marker_can_id, start_data)]
        run = self.run(duration, sender, clock=clock, sleeper=sleeper,
                       session_id=session_id, anomalies=anomalies)
        frames.extend(run.frames)
        end = clock()
        end_data = encode_marker(MARKER_END, int(round(end * 1_000_000)))
        self._send(sender, marker_can_id, end_data)
        frames.append(ScheduledFrame(max(0.0, end - start), marker_can_id, end_data))
        return TrafficRun(tuple(frames), duration, session_id)

    def _plan_frames(self, duration: float) -> list[ScheduledFrame]:
        planned: list[ScheduledFrame] = []
        for message in self.messages:
            elapsed = 0.0
            while elapsed < duration:
                planned.append(ScheduledFrame(elapsed, message.can_id,
                                              message.payload))
                elapsed += message.period
        planned.sort(key=lambda frame: frame.elapsed)
        return planned

    def _apply_anomalies(self, frames: list[ScheduledFrame], duration: float,
                         episodes: tuple[AnomalyEpisode, ...]) -> list[ScheduledFrame]:
        result = list(frames)
        for episode in episodes:
            if episode.end > duration:
                raise TrafficConfigurationError(
                    f"anomaly episode ends at {episode.end}, beyond run duration {duration}"
                )
            if episode.kind in (AnomalyType.FREQUENCY_INCREASE,
                                AnomalyType.FREQUENCY_DECREASE,
                                AnomalyType.DISAPPEARANCE):
                self._apply_period_episode(result, episode)
            elif episode.kind is AnomalyType.BURST:
                result.extend(self._burst_frames(episode))
            elif episode.kind is AnomalyType.UNKNOWN_DBC_ID:
                result.append(self._special_frame(episode, unknown=True))
            elif episode.kind in (AnomalyType.DLC_MISMATCH,
                                  AnomalyType.OUT_OF_RANGE_SIGNAL):
                result.append(self._special_frame(episode, unknown=False))
        result.sort(key=lambda frame: frame.elapsed)
        return result

    def _apply_period_episode(self, frames: list[ScheduledFrame],
                              episode: AnomalyEpisode) -> None:
        can_id = self._episode_can_id(episode)
        message = next((item for item in self.messages if item.can_id == can_id), None)
        if message is None:
            raise TrafficConfigurationError(
                f"CAN ID 0x{can_id:x} is not configured reference traffic"
            )
        frames[:] = [frame for frame in frames
                     if not (frame.can_id == can_id
                             and episode.start <= frame.elapsed < episode.end)]
        if episode.kind is AnomalyType.DISAPPEARANCE:
            return
        default = 2.0 if episode.kind is AnomalyType.FREQUENCY_INCREASE else 0.5
        factor = float(episode.parameter("frequency_factor", default))
        if episode.kind is AnomalyType.FREQUENCY_INCREASE and factor <= 1:
            raise TrafficConfigurationError("frequency increase factor must exceed one")
        if episode.kind is AnomalyType.FREQUENCY_DECREASE and not 0 < factor < 1:
            raise TrafficConfigurationError("frequency decrease factor must be between zero and one")
        elapsed = episode.start
        while elapsed < episode.end - 1e-12:
            frames.append(ScheduledFrame(elapsed, can_id, message.payload))
            elapsed += message.period / factor

    def _burst_frames(self, episode: AnomalyEpisode) -> list[ScheduledFrame]:
        can_id = self._episode_can_id(episode)
        message = next((item for item in self.messages if item.can_id == can_id), None)
        if message is None:
            raise TrafficConfigurationError(f"CAN ID 0x{can_id:x} is not configured reference traffic")
        count = int(episode.parameter("count", 3))
        interval = float(episode.parameter("interval", message.period / 10))
        if count <= 0 or interval <= 0 or episode.start + (count - 1) * interval >= episode.end:
            raise TrafficConfigurationError("burst count, interval, or duration is invalid")
        return [ScheduledFrame(episode.start + index * interval, can_id, message.payload)
                for index in range(count)]

    def _special_frame(self, episode: AnomalyEpisode, *, unknown: bool) -> ScheduledFrame:
        if unknown:
            can_id = int(episode.parameter("can_id", 0x7FF))
            if self.database.message_for_id(can_id) is not None:
                raise TrafficConfigurationError("unknown anomaly CAN ID exists in the DBC")
            return ScheduledFrame(episode.start, can_id,
                                  _normalize_anomaly_payload(episode.parameter("payload", b"")))
        can_id = self._episode_can_id(episode)
        message = self.database.message_for_id(can_id)
        if message is None:
            raise TrafficConfigurationError(f"CAN ID 0x{can_id:x} is absent from the selected DBC")
        if episode.kind is AnomalyType.DLC_MISMATCH:
            dlc = int(episode.parameter("dlc", message.data_length + 1))
            if not 0 <= dlc <= 8 or dlc == message.data_length:
                raise TrafficConfigurationError("anomaly DLC must differ from the DBC DLC and fit CAN")
            return ScheduledFrame(episode.start, can_id, bytes(dlc))
        signal_name = episode.parameter("signal")
        if not isinstance(signal_name, str):
            signal = next((item for item in message.signals if item.has_reference_range), None)
            if signal is None:
                raise TrafficConfigurationError("out-of-range anomaly needs a ranged DBC signal")
            signal_name = signal.name
        signal = next((item for item in message.signals if item.name == signal_name), None)
        if signal is None or not signal.has_reference_range:
            raise TrafficConfigurationError("out-of-range anomaly signal has no DBC range")
        value = float(episode.parameter("value", signal.maximum + 1))
        data = _encode_signal_values(message, bytes(message.data_length),
                                     {signal_name: value}, allow_out_of_range=True)
        return ScheduledFrame(episode.start, can_id, data)

    def _episode_can_id(self, episode: AnomalyEpisode) -> int:
        if episode.can_id is None:
            raise TrafficConfigurationError(f"{episode.kind.value} anomaly requires can_id")
        return int(episode.can_id)

    def _prepare(self, message: ReferenceMessage) -> ReferenceMessage:
        if not 0 <= message.can_id <= 0x7FF:
            raise TrafficConfigurationError(
                f"CAN ID {message.can_id!r} is not a standard 11-bit identifier"
            )
        if message.period <= 0:
            raise TrafficConfigurationError("message period must be positive")
        definition = self.database.message_for_id(message.can_id)
        if definition is None:
            raise TrafficConfigurationError(
                f"CAN ID 0x{message.can_id:x} is absent from the selected DBC"
            )
        payload = _normalize_payload(message.payload, definition.data_length)
        if message.signal_values:
            payload = _encode_signal_values(definition, payload,
                                            message.signal_values)
        return ReferenceMessage(message.can_id, float(message.period), payload,
                                dict(message.signal_values))

    @staticmethod
    def _send(sender: FrameSender | Callable[[int, bytes], None],
              can_id: int, payload: bytes) -> None:
        send = getattr(sender, "send", None)
        if send is not None:
            send(can_id, payload)
        else:
            sender(can_id, payload)


def _normalize_payload(payload: bytes | bytearray | Sequence[int] | None,
                        data_length: int) -> bytes:
    if payload is None:
        return bytes(data_length)
    if isinstance(payload, str):
        compact = payload.replace(" ", "")
        try:
            payload = bytes.fromhex(compact)
        except ValueError as exc:
            raise TrafficConfigurationError("payload is not hexadecimal") from exc
    try:
        normalized = bytes(payload)
    except (TypeError, ValueError) as exc:
        raise TrafficConfigurationError("payload must contain byte values") from exc
    if len(normalized) != data_length:
        raise TrafficConfigurationError(
            f"payload length {len(normalized)} does not match DBC DLC {data_length}"
        )
    return normalized


def _encode_signal_values(definition: MessageDefinition, payload: bytes,
                          values: Mapping[str, float],
                          *, allow_out_of_range: bool = False) -> bytes:
    signals = {signal.name: signal for signal in definition.signals}
    unknown = set(values) - set(signals)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise TrafficConfigurationError(
            f"signal(s) {names} are absent from CAN ID 0x{definition.can_id:x}"
        )
    result = bytearray(payload)
    for name, physical in values.items():
        signal = signals[name]
        value = float(physical)
        if (signal.has_reference_range and not allow_out_of_range
                and not signal.minimum <= value <= signal.maximum):
            raise TrafficConfigurationError(
                f"signal {name}={value} is outside DBC range "
                f"[{signal.minimum}, {signal.maximum}]"
            )
        if signal.scale == 0:
            raise TrafficConfigurationError(f"signal {name} has zero DBC scale")
        raw_float = (value - signal.offset) / signal.scale
        raw = round(raw_float)
        if abs(raw - raw_float) > 1e-9:
            raise TrafficConfigurationError(
                f"signal {name}={value} cannot be represented by its DBC scale"
            )
        if signal.is_signed:
            lower, upper = -(1 << (signal.bit_length - 1)), (1 << (signal.bit_length - 1)) - 1
        else:
            lower, upper = 0, (1 << signal.bit_length) - 1
        if not lower <= raw <= upper:
            raise TrafficConfigurationError(
                f"signal {name} raw value {raw} does not fit {signal.bit_length} bits"
            )
        encoded = raw if raw >= 0 else raw + (1 << signal.bit_length)
        positions = _bit_positions(signal)
        if not positions or max(positions) >= len(result) * 8:
            raise TrafficConfigurationError(
                f"signal {name} does not fit the DBC message payload"
            )
        for offset, position in enumerate(positions):
            bit = ((encoded >> (signal.bit_length - 1 - offset)) & 1
                   if signal.byte_order == "big_endian"
                   else (encoded >> offset) & 1)
            mask = 1 << (position % 8)
            if bit:
                result[position // 8] |= mask
            else:
                result[position // 8] &= ~mask
    return bytes(result)


def _bit_positions(signal: SignalDefinition) -> tuple[int, ...]:
    if signal.bit_length <= 0 or signal.start_bit < 0:
        return ()
    if signal.byte_order == "big_endian":
        positions = []
        position = signal.start_bit
        for _ in range(signal.bit_length):
            positions.append(position)
            position = position + 15 if position % 8 == 0 else position - 1
        return tuple(positions)
    return tuple(signal.start_bit + offset for offset in range(signal.bit_length))


__all__ = [
    "FileFrameSender",
    "ReferenceMessage",
    "ReferenceTrafficGenerator",
    "ScheduledFrame",
    "TrafficConfigurationError",
    "TrafficRun",
    "HOST_LINK_CAPACITY_DLC8",
    "HOST_LINK_CAPACITY_WARNING",
]


def _normalize_anomaly_payload(payload: object) -> bytes:
    """Normalize a special-frame payload without imposing a DBC DLC."""
    if isinstance(payload, str):
        try:
            payload = bytes.fromhex(payload.replace(" ", ""))
        except ValueError as exc:
            raise TrafficConfigurationError("anomaly payload is not hexadecimal") from exc
    try:
        data = bytes(payload)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TrafficConfigurationError("anomaly payload must contain byte values") from exc
    if len(data) > 8:
        raise TrafficConfigurationError("CAN anomaly payload cannot exceed eight bytes")
    return data
