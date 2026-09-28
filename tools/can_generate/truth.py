"""Ground-truth recording for generated CAN anomaly episodes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable, Mapping


class GroundTruthError(ValueError):
    """A ground-truth episode is incomplete or internally inconsistent."""


@dataclass
class AnomalyEpisode:
    start: float
    end: float
    type: str
    can_id: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    nominal_intensity: float | None = None
    achieved_intensity: float | None = None

    def __post_init__(self) -> None:
        self.start = float(self.start)
        self.end = float(self.end)
        if self.start < 0 or self.end < self.start:
            raise GroundTruthError("episode end must not precede its non-negative start")
        if not self.type:
            raise GroundTruthError("episode type must not be empty")
        if self.can_id is not None and not 0 <= int(self.can_id) <= 0x7FF:
            raise GroundTruthError("episode CAN ID must be a standard 11-bit identifier")
        if self.nominal_intensity is not None and self.nominal_intensity < 0:
            raise GroundTruthError("nominal intensity must be non-negative")
        if self.achieved_intensity is not None and self.achieved_intensity < 0:
            raise GroundTruthError("achieved intensity must be non-negative")

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GroundTruthRecorder:
    """Collect and persist anomaly episodes for one generator session."""

    def __init__(
        self,
        session_id: str,
        *,
        time_reference: str = "generator_monotonic_seconds_since_run_start",
        time_alignment: str = "relative generator time; align with trace marker frames",
        seed: int | None = None,
    ) -> None:
        if not session_id:
            raise GroundTruthError("session_id must not be empty")
        self.session_id = session_id
        if seed is not None and not isinstance(seed, int):
            raise GroundTruthError("seed must be an integer or None")
        self.seed = seed
        self.time_reference = time_reference
        self.time_alignment = time_alignment
        self._episodes: list[AnomalyEpisode] = []
        self._open: dict[int, dict[str, Any]] = {}
        self._next_token = 0
        self._time_scale_reference: dict[str, Any] | None = None

    @property
    def episodes(self) -> tuple[AnomalyEpisode, ...]:
        return tuple(self._episodes)

    def record_time_scale_reference(self, reference: Mapping[str, Any] | Any) -> None:
        """Store the marker-derived generator/device alignment metadata."""
        if hasattr(reference, "as_dict"):
            reference = reference.as_dict()
        if not isinstance(reference, Mapping):
            raise GroundTruthError("time-scale reference must be a mapping")
        self._time_scale_reference = dict(reference)

    def record_episode(self, *, start: float, end: float, anomaly_type: str,
                       can_id: int | None = None,
                       parameters: Mapping[str, Any] | None = None,
                       nominal_intensity: float | None = None,
                       achieved_intensity: float | None = None) -> AnomalyEpisode:
        episode = AnomalyEpisode(
            start=start, end=end, type=anomaly_type, can_id=can_id,
            parameters=dict(parameters or {}), session_id=self.session_id,
            nominal_intensity=nominal_intensity,
            achieved_intensity=achieved_intensity)
        self._episodes.append(episode)
        return episode

    def start_episode(self, *, start: float, anomaly_type: str,
                      can_id: int | None = None,
                      parameters: Mapping[str, Any] | None = None,
                      nominal_intensity: float | None = None) -> int:
        token = self._next_token
        self._next_token += 1
        self._open[token] = {
            "start": start, "anomaly_type": anomaly_type, "can_id": can_id,
            "parameters": dict(parameters or {}),
            "nominal_intensity": nominal_intensity,
        }
        return token

    def end_episode(self, token: int, *, end: float,
                    achieved_intensity: float | None = None) -> AnomalyEpisode:
        try:
            values = self._open.pop(token)
        except KeyError as exc:
            raise GroundTruthError(f"unknown or already closed episode token {token}") from exc
        return self.record_episode(end=end, achieved_intensity=achieved_intensity, **values)

    def finalize_achieved_intensity(self, frames: Iterable[Any], *,
                                    intensity_can_id: bool = True) -> None:
        frame_list = tuple(frames)
        for episode in self._episodes:
            if episode.achieved_intensity is not None or episode.duration == 0:
                continue
            count = sum(
                1 for frame in frame_list
                if episode.start <= float(frame.elapsed) <= episode.end
                and (not intensity_can_id or episode.can_id is None
                     or frame.can_id == episode.can_id))
            episode.achieved_intensity = count / episode.duration

    def as_dict(self) -> dict[str, Any]:
        if self._open:
            raise GroundTruthError("cannot serialize ground truth with open episodes")
        return {
            "format": "can-anomaly-ground-truth-v1",
            "session_id": self.session_id,
            "seed": self.seed,
            "time_reference": self.time_reference,
            "time_alignment": self.time_alignment,
            "time_scale_reference": self._time_scale_reference,
            "episodes": [episode.to_dict() for episode in self._episodes],
        }

    def write(self, path: str | Path, *, frames: Iterable[Any] | None = None) -> Path:
        if frames is not None:
            self.finalize_achieved_intensity(frames)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.as_dict(), ensure_ascii=False,
                                      indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        return output

    @classmethod
    def read(cls, path: str | Path) -> "GroundTruthRecorder":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        recorder = cls(payload["session_id"],
                        time_reference=payload["time_reference"],
                        time_alignment=payload["time_alignment"],
                        seed=payload.get("seed"))
        recorder._time_scale_reference = payload.get("time_scale_reference")
        for item in payload.get("episodes", []):
            item = dict(item)
            item["anomaly_type"] = item.pop("type")
            recorder.record_episode(**{key: item[key] for key in (
                "start", "end", "anomaly_type", "can_id", "parameters",
                "nominal_intensity", "achieved_intensity")})
        return recorder


__all__ = ["AnomalyEpisode", "GroundTruthError", "GroundTruthRecorder"]
