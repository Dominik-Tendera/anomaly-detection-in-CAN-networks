#!/usr/bin/env python3
"""Load a DBC file and expose the metadata used by anomaly rules.

The detector must use the network definition rather than a second, hand-written
list of identifiers.  ``cantools`` remains responsible for parsing the DBC;
this module converts its mutable model into small immutable records that are
safe to pass to the baseline and protocol-rule code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Optional, Sequence

import cantools
from cantools.database.can import Database


class DbcError(ValueError):
    """The DBC file cannot be loaded or does not contain usable metadata."""


@dataclass(frozen=True)
class SignalDefinition:
    """Metadata for one signal in a DBC message."""

    name: str
    start_bit: int
    bit_length: int
    minimum: Optional[float]
    maximum: Optional[float]
    unit: Optional[str]
    byte_order: str = "little_endian"
    is_signed: bool = False
    scale: float = 1.0
    offset: float = 0.0

    @property
    def has_reference_range(self) -> bool:
        """Whether the DBC supplies a non-zero-width physical range."""
        return (self.minimum is not None and self.maximum is not None
                and self.minimum != self.maximum)


@dataclass(frozen=True)
class MessageDefinition:
    """Metadata for one CAN message, indexed by its CAN frame identifier."""

    can_id: int
    name: str
    data_length: int
    signals: tuple[SignalDefinition, ...]


@dataclass(frozen=True)
class DbcDatabase:
    """Immutable view of a loaded DBC database.

    ``messages_by_id`` is keyed by the numeric CAN frame identifier, not by an
    ECU or sender identifier.  CAN itself does not encode sender addressing.
    """

    path: Path
    messages_by_id: dict[int, MessageDefinition]
    _decode_stats: dict[str, int] = field(default_factory=lambda: {"skipped_signals": 0},
                                          compare=False, repr=False)

    @property
    def messages(self) -> dict[int, MessageDefinition]:
        """Alias retained for callers that use the shorter collection name."""
        return self.messages_by_id

    def message_for_id(self, can_id: int) -> Optional[MessageDefinition]:
        """Return the definition for ``can_id``, or ``None`` if it is unknown."""
        return self.messages_by_id.get(int(can_id))

    def __len__(self) -> int:
        return len(self.messages_by_id)

    @property
    def skipped_signal_count(self) -> int:
        """Number of signals skipped because their bits were absent from a frame."""
        return self._decode_stats["skipped_signals"]

    @property
    def skipped_signals(self) -> int:
        """Backward-friendly alias for :attr:`skipped_signal_count`."""
        return self.skipped_signal_count

    def decode_signals(self, can_id: int, data: bytes | bytearray | Sequence[int]) -> dict[str, float]:
        """Decode available signals from one CAN data field.

        A short DLC is not itself an anomaly here.  Each signal whose required
        bit is outside ``data`` is omitted and counted; signals fully present
        in the field are decoded independently, including their DBC scaling.
        """
        message = self.message_for_id(can_id)
        if message is None:
            return {}
        return self.decode_message(message, data)

    def decode_message(self, message: MessageDefinition,
                       data: bytes | bytearray | Sequence[int]) -> dict[str, float]:
        """Decode one known message without raising for a short DLC."""
        payload = bytes(data)
        decoded: dict[str, float] = {}
        for signal in message.signals:
            positions = _signal_bit_positions(signal)
            if not positions or max(positions) >= len(payload) * 8:
                self._decode_stats["skipped_signals"] += 1
                continue
            raw = _extract_signal(payload, signal, positions)
            if signal.is_signed and raw & (1 << (signal.bit_length - 1)):
                raw -= 1 << signal.bit_length
            decoded[signal.name] = raw * signal.scale + signal.offset
        return decoded

    @property
    def signal_count(self) -> int:
        return sum(len(message.signals) for message in self.messages_by_id.values())

    @property
    def sha256(self) -> str:
        """Return the checksum of the exact DBC bytes used to load the model."""
        try:
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 16), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise DbcError(f"DBC file cannot be read for checksum: {self.path} ({exc})") from exc
        return digest.hexdigest()

    @property
    def checksum(self) -> str:
        """Backward-friendly alias for the DBC SHA-256 checksum."""
        return self.sha256


def load_dbc(path: str | Path) -> DbcDatabase:
    """Parse ``path`` with cantools and return message and signal metadata.

    File and parser failures are normalised to :class:`DbcError` so callers do
    not have to depend on cantools' exception hierarchy.  Signal limits remain
    ``None`` when the DBC does not define them; a range check must therefore be
    conditional on :attr:`SignalDefinition.has_reference_range`.
    """
    source = Path(path)
    try:
        database: Database = cantools.database.load_file(str(source))
    except (OSError, cantools.database.Error) as exc:
        raise DbcError(f"DBC file cannot be loaded: {source} ({exc})") from exc

    definitions: dict[int, MessageDefinition] = {}
    for message in database.messages:
        signals = tuple(
            SignalDefinition(
                name=signal.name,
                start_bit=int(signal.start),
                bit_length=int(signal.length),
                minimum=_number_or_none(signal.minimum),
                maximum=_number_or_none(signal.maximum),
                unit=signal.unit,
                byte_order=signal.byte_order,
                is_signed=bool(signal.is_signed),
                scale=float(signal.scale),
                offset=float(signal.offset),
            )
            for signal in message.signals
        )
        can_id = int(message.frame_id)
        definitions[can_id] = MessageDefinition(
            can_id=can_id,
            name=message.name,
            data_length=int(message.length),
            signals=signals,
        )

    return DbcDatabase(path=source.resolve(), messages_by_id=definitions)


def _signal_bit_positions(signal: SignalDefinition) -> tuple[int, ...]:
    """Return physical bit positions occupied by a DBC signal."""
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


def _extract_signal(payload: bytes, signal: SignalDefinition,
                    positions: tuple[int, ...]) -> int:
    """Extract an unsigned raw value from positions already known to exist."""
    if signal.byte_order == "big_endian":
        value = 0
        for position in positions:
            value = (value << 1) | ((payload[position // 8] >> (position % 8)) & 1)
        return value
    value = 0
    for offset, position in enumerate(positions):
        value |= ((payload[position // 8] >> (position % 8)) & 1) << offset
    return value


def _number_or_none(value: object) -> Optional[float]:
    """Keep absent limits absent while normalising cantools numeric values."""
    return None if value is None else float(value)


__all__ = [
    "DbcDatabase",
    "DbcError",
    "MessageDefinition",
    "SignalDefinition",
    "load_dbc",
]
