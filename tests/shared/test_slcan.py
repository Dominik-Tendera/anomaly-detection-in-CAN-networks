"""Hardware-free tests for the minimal SLCAN client."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TOOLS_DIR = (Path(__file__).resolve().parents[2] / "tools")
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from can_generate.slcan import SLCANClient  # noqa: E402


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def close(self):
        self.closed = True


class SlcanClientTests(unittest.TestCase):
    def setUp(self):
        self.devices = []

        def factory(**kwargs):
            device = FakeSerial(**kwargs)
            self.devices.append(device)
            return device

        self.client = SLCANClient("/dev/fake-can", serial_factory=factory)

    def test_connect_uses_115200_and_one_mbps_uses_standard_code(self):
        self.client.connect()
        self.client.open(1_000_000)

        device = self.devices[0]
        self.assertEqual(device.kwargs["baudrate"], 115200)
        self.assertEqual(device.writes, [b"S8\r", b"O\r"])

    def test_standard_frame_is_encoded_with_t_prefix(self):
        self.client.connect()
        self.client.open()
        self.client.send(0x123, bytes.fromhex("0a ff 20"))
        self.client.close()

        self.assertEqual(
            self.devices[0].writes,
            [b"S8\r", b"O\r", b"t12330aff20\r", b"C\r"],
        )

    def test_custom_bitrate_uses_register_timing_command(self):
        self.client.connect()
        self.client.open(125_001)

        command = self.devices[0].writes[0].decode("ascii")
        self.assertTrue(command.startswith("s"))
        self.assertEqual(len(command), 8)  # s + 6 hexadecimal timing digits + CR
        self.assertEqual(self.devices[0].writes[1], b"O\r")

    def test_standard_frame_limits_are_checked(self):
        self.client.connect()
        with self.assertRaises(ValueError):
            self.client.send(0x800, b"")
        with self.assertRaises(ValueError):
            self.client.send(0x100, b"123456789")

    def test_disconnect_closes_open_channel_and_serial_port(self):
        self.client.connect()
        self.client.open()
        self.client.disconnect()

        self.assertEqual(self.devices[0].writes[-1], b"C\r")
        self.assertTrue(self.devices[0].closed)
        self.assertFalse(self.client.connected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
