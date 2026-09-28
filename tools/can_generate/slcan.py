"""Minimal SLCAN client used by the CAN traffic generator.

The adapter is controlled with the small Lawicel command set.  The serial link
runs at 115200 baud; the CAN bitrate is selected with an ``S`` preset (or the
custom ``s`` register command), then the channel is opened with ``O``.  This
module deliberately supports only standard data frames because the generator
is required to produce 11-bit CAN identifiers.
"""

from __future__ import annotations

from typing import Callable, Optional

try:
    import serial
except ImportError:  # Keep hardware-free tests usable before dependencies are installed.
    class _MissingSerial:
        EIGHTBITS = 8
        PARITY_NONE = "N"
        STOPBITS_ONE = 1

        class SerialException(Exception):
            pass

        @staticmethod
        def Serial(**_kwargs):
            raise _MissingSerial.SerialException(
                "pyserial is required for a real SLCAN connection"
            )

    serial = _MissingSerial()


_STANDARD_BITRATES = {
    10_000: "0",
    20_000: "1",
    50_000: "2",
    100_000: "3",
    125_000: "4",
    250_000: "5",
    500_000: "6",
    800_000: "7",
    1_000_000: "8",
    83_300: "9",
}


class SLCANError(RuntimeError):
    """Raised when the SLCAN client cannot communicate with the adapter."""


class SLCANClient:
    """Small synchronous client for the commands needed by the generator."""

    def __init__(
        self,
        port: Optional[str] = None,
        *,
        timeout: float = 1.0,
        serial_factory: Optional[Callable[..., object]] = None,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self._serial_factory = serial_factory or serial.Serial
        self._serial = None
        self._opened = False

    @property
    def connected(self) -> bool:
        return self._serial is not None

    def connect(self, port: Optional[str] = None) -> None:
        """Open the fixed 115200 baud host connection."""
        port_name = port or self.port
        if not port_name:
            raise ValueError("serial port is required")
        if self.connected:
            return
        try:
            self._serial = self._serial_factory(
                port=port_name,
                baudrate=115200,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
            )
        except (serial.SerialException, ValueError) as exc:
            raise SLCANError(f"cannot open serial port {port_name}: {exc}") from exc

    def open(self, bitrate: int = 1_000_000) -> None:
        """Select *bitrate* and open the adapter's active CAN channel."""
        self._require_connection()
        if bitrate <= 0:
            raise ValueError("bitrate must be positive")
        if bitrate in _STANDARD_BITRATES:
            self._write("S" + _STANDARD_BITRATES[bitrate])
        else:
            self._write("s" + self._calculate_btr(bitrate))
        self._write("O")
        self._opened = True

    def open_can_channel(self, bitrate: int = 1_000_000) -> None:
        """Compatibility spelling used by the existing USBtin driver."""
        self.open(bitrate)

    def send(self, can_id: int, data: bytes = b"") -> None:
        """Transmit one standard CAN data frame using SLCAN's ``t`` command."""
        self._require_connection()
        if not 0 <= can_id <= 0x7FF:
            raise ValueError("standard CAN identifier must be in the range 0..0x7ff")
        payload = bytes(data)
        if len(payload) > 8:
            raise ValueError("CAN data field cannot exceed 8 bytes")
        frame = f"t{can_id:03x}{len(payload):x}{payload.hex()}"
        self._write(frame)

    def close(self) -> None:
        """Close the CAN channel if it is open."""
        if self.connected and self._opened:
            self._write("C")
            self._opened = False

    def close_can_channel(self) -> None:
        """Compatibility spelling used by the existing USBtin driver."""
        self.close()

    def disconnect(self) -> None:
        """Close the CAN channel and release the serial port."""
        try:
            self.close()
        finally:
            if self._serial is not None:
                try:
                    self._serial.close()
                except serial.SerialException as exc:
                    raise SLCANError(f"cannot close serial port: {exc}") from exc
                finally:
                    self._serial = None

    @staticmethod
    def _calculate_btr(bitrate: int) -> str:
        """Calculate the custom ``s`` timing payload for a 24 MHz adapter."""
        fosc = 24_000_000
        desired = fosc // bitrate
        selected_tq = 0
        selected_diff = 0
        selected_brp = 0
        for tq in range(11, 24):
            brp_factor = (desired * 10) // tq
            remainder = brp_factor % 20
            if remainder >= 10:
                brp_factor += 20
            brp_factor -= remainder
            brp_factor //= 10
            brp_factor = max(2, min(128, brp_factor))
            difference = abs(desired - tq * brp_factor)
            if selected_tq == 0 or difference <= selected_diff:
                selected_tq = tq
                selected_diff = difference
                selected_brp = brp_factor // 2 - 1

        cnf_values = [
            0x9203, 0x9303, 0x9B03, 0x9B04, 0x9C04, 0xA404, 0xA405,
            0xAC05, 0xAC06, 0xAD06, 0xB506, 0xB507, 0xBD07,
        ]
        return f"{selected_brp | 0xC0:02x}{cnf_values[selected_tq - 11]:04x}"

    def _require_connection(self) -> None:
        if not self.connected:
            raise SLCANError("not connected")

    def _write(self, command: str) -> None:
        try:
            self._serial.write((command + "\r").encode("ascii"))
        except serial.SerialException as exc:
            raise SLCANError(str(exc)) from exc


# Concise alias for callers that use the adapter name rather than the protocol
# name.  Both names refer to the same implementation.
SlcanClient = SLCANClient

__all__ = ["SLCANClient", "SlcanClient", "SLCANError", "_STANDARD_BITRATES"]
