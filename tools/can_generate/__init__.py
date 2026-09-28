"""Generator of reference CAN traffic and of injected anomaly episodes.

The generator drives a USB CAN adapter over SLCAN and writes, for every run, the
ground truth that the evaluation compares the detector against: start time, end
time, type, CAN ID and parameters of each episode, plus the marker frames that
tie the generator clock to the device time in the trace.

Two invariants hold for every module added here:

  the transmit schedule follows a monotonic clock rather than a fixed sleep, so
  that configured periods do not drift over a session,

  a run is reproducible from its seed and its configuration alone, since the
  ground truth is worthless if the traffic it describes cannot be regenerated.

Modules are added by the later tasks of the rpi-detekcja-anomalii plan: slcan,
traffic, episodes and truth.
"""

from __future__ import annotations

from .time_reference import (
    DEFAULT_MARKER_CAN_ID,
    MARKER_END,
    MARKER_START,
    TimeMarker,
    TimeMarkerError,
    decode_marker,
    encode_marker,
)
from .episodes import AnomalyEpisode, AnomalyType
from .traffic import (
    ReferenceMessage,
    ReferenceTrafficGenerator,
    FileFrameSender,
    ScheduledFrame,
    TrafficConfigurationError,
    TrafficRun,
    HOST_LINK_CAPACITY_DLC8,
    HOST_LINK_CAPACITY_WARNING,
)

from .truth import GroundTruthError, GroundTruthRecorder

__all__ = [
    "AnomalyEpisode",
    "AnomalyType",
    "DEFAULT_MARKER_CAN_ID",
    "MARKER_END",
    "MARKER_START",
    "TimeMarker",
    "TimeMarkerError",
    "decode_marker",
    "encode_marker",
    "GroundTruthError",
    "GroundTruthRecorder",
    "ReferenceMessage",
    "ReferenceTrafficGenerator",
    "FileFrameSender",
    "ScheduledFrame",
    "TrafficConfigurationError",
    "TrafficRun",
    "HOST_LINK_CAPACITY_DLC8",
    "HOST_LINK_CAPACITY_WARNING",
]
