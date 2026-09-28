"""Builds synthetic record traces for the tests of can_detect.

A test of the reader needs traces the bench cannot produce on demand: a 32-bit
timestamp caught mid wrap, a frame stamped just before the synchronisation
record, a session record from a logger whose RTC was never set. They are
assembled here, record by record.

The framing is not reimplemented. Every record leaves this module through
`proto.crc8` and `proto.cobs_encode`, the same two functions the decoder under
test relies on and the same ones `can_logger/Core/Src/can_stream_codec.c` implements on the
firmware side. Only the field layouts are written out, straight from the tables
in `CAN_STREAM_PROTOCOL.md`, and the decoder itself checks them: a builder that
laid a field out wrongly would produce a record the decoder rejects or decodes
into different values, which the tests compare against what was asked for.

The sequence number is handled by the builder and advances once per record,
frames and everything else alike, exactly as the firmware numbers records on the
link.

Usage:
    builder = TraceBuilder()
    builder.session(device_us=1_000, rtc_valid=False)
    builder.time_sync(1_000_000)
    builder.frame(can_id=0x123, data=b"\\x01\\x02", ts64=1_000_500)
    trace = builder.bytes()
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[1] / "tools")
RECEIVER_DIR = TOOLS_DIR.parent / "raspberry_pi/rpi_receiver"
for entry in (str(TOOLS_DIR), str(RECEIVER_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import can_stream_protocol as proto  # noqa: E402

# RTC reading of a logger whose clock has never been set, the state of the bench.
RTC_UNSET = (0, 1, 1, 0, 0, 0)  # year is 2000 based, so 0 means 2000


def wrap(payload: bytes) -> bytes:
    """Append the CRC-8 and COBS encode, as the firmware does before sending."""
    return proto.cobs_encode(payload + bytes([proto.crc8(payload)]))


class TraceBuilder:
    """Collects records into a byte string shaped like a stored trace."""

    def __init__(self, first_seq: int = 0) -> None:
        self._parts: list[bytes] = []
        self._seq = first_seq & 0xFFFF

    # -- plumbing ---------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    def raw(self, payload: bytes) -> "TraceBuilder":
        """Add an already assembled record body, without the CRC."""
        self._parts.append(wrap(payload))
        return self

    def bytes(self) -> bytes:
        return b"".join(self._parts)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.bytes())
        return path

    @property
    def next_seq(self) -> int:
        """Sequence number the next record will carry."""
        return self._seq

    def skip_seq(self, count: int) -> "TraceBuilder":
        """Advance the numbering without emitting records: a gap on the link."""
        self._seq = (self._seq + count) & 0xFFFF
        return self

    # -- records ----------------------------------------------------------

    def session(self, device_us: int = 0, rtc_valid: bool = False,
                rtc: tuple[int, int, int, int, int, int] = RTC_UNSET,
                flags: int = proto.FLAG_MEASUREMENT_MODE
                | proto.FLAG_STREAM_ENABLED,
                can1_bitrate: int = 1_000_000, can2_bitrate: int = 1_000_000,
                uart_baudrate: int = 2_000_000,
                proto_version: int = proto.PROTO_VERSION) -> "TraceBuilder":
        """Record 0x03. `rtc` is year offset from 2000, month, day, h, m, s."""
        year, month, day, hour, minute, second = rtc
        payload = struct.pack(
            "<BHBBHQIIIIIBBBBBBB",
            proto.REC_SESSION, self._next_seq(), proto_version, flags,
            proto.MAX_RECORD, device_us, can1_bitrate, can2_bitrate,
            0, 0, uart_baudrate, 1 if rtc_valid else 0,
            year, month, day, hour, minute, second)
        assert len(payload) == proto.LEN_SESSION - 1
        return self.raw(payload)

    def time_sync(self, ts64: int) -> "TraceBuilder":
        """Record 0x02, the full 64-bit device time."""
        payload = struct.pack("<BHQ", proto.REC_TIME_SYNC, self._next_seq(),
                              ts64 & 0xFFFFFFFFFFFFFFFF)
        assert len(payload) == proto.LEN_TIME_SYNC - 1
        return self.raw(payload)

    def bus_stats(self, channel: int = 1, ts64: int = 0,
                  **counters: int) -> "TraceBuilder":
        """Record 0x05 with selected cumulative diagnostic counters."""
        unknown = set(counters) - set(proto.BUS_STATS_COUNTERS)
        if unknown:
            raise ValueError(f"unknown bus statistic(s): {sorted(unknown)}")
        values = {name: 0 for name in proto.BUS_STATS_COUNTERS}
        values.update(counters)
        payload = struct.pack(
            "<BHBQ",
            proto.REC_BUS_STATS, self._next_seq(), channel,
            ts64 & 0xFFFFFFFFFFFFFFFF)
        payload += struct.pack(
            "<23I", *(values[name] for name in proto.BUS_STATS_COUNTERS))
        payload += struct.pack("<6B", 0, 0, 0, 0, 0, 0)
        assert len(payload) == proto.LEN_BUS_STATS - 1
        return self.raw(payload)

    def event(self, reason: int, channel: int = 1, ts64: int = 0,
              frame_id: int = proto.FRAME_ID_UNKNOWN, error_code: int = 0,
              suppressed: int = 0, state: int = 0) -> "TraceBuilder":
        """Record 0x04, a controller event with its diagnostic context."""
        payload = struct.pack(
            "<BHBBBHIIQ", proto.REC_EVENT, self._next_seq(), channel,
            reason, state, frame_id, error_code, suppressed,
            ts64 & 0xFFFFFFFFFFFFFFFF)
        assert len(payload) == proto.LEN_EVENT - 1
        return self.raw(payload)

    def frame(self, can_id: int, data: bytes, ts64: int,
              channel: int = 1) -> "TraceBuilder":
        """Record 0x01. Only the low 32 bits of `ts64` reach the wire."""
        dlc = len(data)
        if not 0 <= dlc <= 8:
            raise ValueError(f"DLC out of range: {dlc}")
        if not 0 <= can_id <= 0x7FF:
            raise ValueError(f"identifier is not an 11-bit one: {can_id:#x}")
        if channel not in (1, 2):
            raise ValueError(f"channel is 1 or 2, not {channel}")
        idc = (can_id & 0x7FF) | (dlc << 11) | ((channel - 1) << 15)
        payload = struct.pack("<BHHI", proto.REC_FRAME, self._next_seq(), idc,
                              ts64 & 0xFFFFFFFF) + bytes(data)
        assert len(payload) == proto.LEN_FRAME_BASE - 1 + dlc
        return self.raw(payload)
