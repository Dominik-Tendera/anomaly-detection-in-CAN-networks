"""Parameterized, hardware-independent CAN anomaly episode definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class AnomalyType(str, Enum):
    """Anomaly scenarios supported by the reference traffic generator."""

    FREQUENCY_INCREASE = "frequency_increase"
    FREQUENCY_DECREASE = "frequency_decrease"
    DISAPPEARANCE = "disappearance"
    BURST = "burst"
    UNKNOWN_DBC_ID = "unknown_dbc_id"
    DLC_MISMATCH = "dlc_mismatch"
    OUT_OF_RANGE_SIGNAL = "out_of_range_signal"


@dataclass(frozen=True)
class AnomalyEpisode:
    """One independent anomaly interval, expressed in run-relative seconds.

    ``parameters`` is intentionally serializable and scenario-specific. Common
    parameters are documented by :func:`parameter`: frequency scenarios accept
    ``frequency_factor`` (greater than one increases frequency, below one
    decreases it), burst accepts ``count`` and ``interval``, DLC mismatch accepts
    ``dlc``, and out-of-range signal accepts ``signal`` and ``value``.
    """

    anomaly_type: AnomalyType | str
    start: float
    duration: float
    can_id: int | None = None
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            kind = AnomalyType(self.anomaly_type)
        except ValueError as exc:
            raise ValueError(f"unsupported anomaly type: {self.anomaly_type!r}") from exc
        object.__setattr__(self, "anomaly_type", kind)
        if self.start < 0:
            raise ValueError("anomaly start must be non-negative")
        if self.duration <= 0:
            raise ValueError("anomaly duration must be positive")
        if self.can_id is not None and not 0 <= int(self.can_id) <= 0x7FF:
            raise ValueError("anomaly CAN ID must be a standard 11-bit identifier")
        object.__setattr__(self, "parameters", dict(self.parameters))

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def kind(self) -> AnomalyType:
        """Alias useful when consuming an episode from configuration."""
        return self.anomaly_type

    def parameter(self, name: str, default: object = None) -> object:
        return self.parameters.get(name, default)


__all__ = ["AnomalyEpisode", "AnomalyType"]
