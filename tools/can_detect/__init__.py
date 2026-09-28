"""Anomaly detection over the binary CAN record stream of the STM32 logger.

The package consumes an iterator of decoded records, identical in the live mode
and in the replay mode, and produces anomaly events, a session report and the
effectiveness tables used in the thesis.

The wire format is decoded by rpi_receiver/can_stream_protocol.py, which is kept
byte for byte in agreement with can_logger/Core/Src/can_stream_codec.c by the cross
language vectors in tests/can_logger/run_tests.sh. Nothing in this package may carry
its own copy of that format: a second decoder would drift from the firmware
without any test noticing.

Two invariants hold for every module added here:

  time comes from the records, never from the clock of the analysing machine,
  otherwise the live mode and the replay mode would disagree,

  state is kept proportional to the number of CAN identifiers and to the length
  of the analysis window, never to the length of the session.

Modules are added by the later tasks of the rpi-detekcja-anomalii plan: config,
trace, dbc, baseline, rules_protocol, rules_timing, rules_protocol_layer,
features, detector, evaluate, report, busload and ml.
"""

from .time_reference import (
    TimeReferenceError,
    TimeScaleReference,
    compute_time_scale_reference,
    find_time_markers,
    record_time_reference,
)

__all__ = [
    "TimeReferenceError",
    "TimeScaleReference",
    "compute_time_scale_reference",
    "find_time_markers",
    "record_time_reference",
]
