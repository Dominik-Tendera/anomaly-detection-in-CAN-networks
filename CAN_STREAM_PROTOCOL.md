# CAN stream protocol, version 1

Binary record stream sent by the STM32 logger on USART1 towards a Raspberry Pi.
It replaces the previous text telemetry, which transmitted one frame per 5 ms
from a table of last values per identifier and carried neither a timestamp nor
the DLC.

Reference implementations:

| Side | File | Note |
|---|---|---|
| Encoder and decoder in C | `can_logger/Core/Src/can_stream_codec.c` | no HAL dependency, compiled into the firmware and into the host tests |
| Transport in the firmware | `can_logger/Core/Src/can_stream.c` | byte ring plus DMA on USART1 |
| Decoder in Python | `raspberry_pi/rpi_receiver/can_stream_protocol.py` | mirrors the C file field by field |
| Reference receiver | `raspberry_pi/rpi_receiver/can_stream_rx.py` | stores the trace, measures the link |

The agreement between the C and the Python side is verified by
`tests/integration/test_vectors.py`, which decodes a stream produced by the C
encoder and compares every field.

## Link

| Parameter | Value |
|---|---|
| Interface | USART1, PA15 as TX, PB3 as RX |
| Format | 8N1, no flow control |
| Default speed | 2 000 000 baud |
| Configurable | 9600 to 4 000 000 baud, command `stream baud <n>` on the debug channel |

The default speed is exact on both sides. USART1 runs from APB2 at 100 MHz, so
USARTDIV equals 3.125 and is representable without error. The PL011 in a
Raspberry Pi 4B runs from a 48 MHz reference, where the divider is 1.5, also
exact.

On the Raspberry Pi side a PL011 is required. The mini UART, which the device
tree calls `uart1` and Linux exposes as `/dev/ttyS0`, derives its clock from the
core frequency and is not usable at this rate. The primary PL011 is `uart0`, and
on a Pi 4 it is occupied by Bluetooth by default, so it has to be released:

```
# /boot/firmware/config.txt on Bookworm, /boot/config.txt on Bullseye and older
enable_uart=1
dtoverlay=disable-bt
```

The serial console must also be removed from the kernel command line, otherwise
getty keeps the port open. The device is then `/dev/ttyAMA0` on GPIO 14 as TX and
GPIO 15 as RX.

Wiring, both sides run at 3.3 V so no level shifter is needed, and a common
ground is mandatory:

| STM32 | Raspberry Pi 4B |
|---|---|
| PA15, USART1_TX | GPIO 15, RXD, header pin 10 |
| PB3, USART1_RX | GPIO 14, TXD, header pin 8 (unused, the data channel is one way) |
| GND | GND, header pin 6 |

Link budget for a bus at 1 Mbps loaded to 40 percent
`[INFERENCJA — from the frame bit count in the Bosch CAN 2.0 specification]`:

| Traffic profile | Frames per second | Record size | Throughput | Utilisation at 2 Mbaud |
|---|---|---|---|---|
| only DLC 8 | about 3600 | 20 B | 72 kB/s | 36 % |
| only DLC 0 | about 8500 | 12 B | 102 kB/s | 51 % |

The second profile is the demanding one: short frames yield more records at the
same bus load. Measured values have to replace these numbers in the thesis.

## Framing

Every record is COBS encoded and terminated by a single `0x00` byte. A `0x00`
therefore never occurs inside an encoded record, so the decoder can always
resynchronise on the next delimiter without knowing the record layout. The
overhead is exactly one code byte plus the delimiter, so an encoded record is two
bytes longer than the decoded one.

The last byte of a decoded record is a CRC-8 over all preceding bytes:
polynomial `0x07`, initial value `0x00`, no reflection, no final xor. It detects
every single bit error in a record, which the host tests check exhaustively.

Multi-byte fields are little endian. Every record starts with the same three
byte header, so a receiver can follow the sequence numbering without
understanding the record body:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | record type |
| 1 | 2 | sequence number, 16-bit, wraps |

An unknown record type is skipped and counted, never treated as an error. That
is what makes the reserved type `0x20` for CANH and CANL aggregates addable
later without breaking an existing receiver.

## Sequence numbering

One counter numbers **every** record, not only frames. A number is consumed only
when the record really entered the transmit buffer.

Consequences, and this is the point of the whole design:

- a gap in the sequence means records lost **on the serial link**;
- a frame that the logger had to drop never consumes a number, it is reported in
  the `ring_dropped` counter and, rate limited, as a ring drop event;
- a record that did not fit into the transmit buffer is reported in
  `tx_records_dropped` and as a transmit overflow event.

The three causes of loss stay distinguishable, which the losslessness argument in
the thesis depends on.

## Time

The device time is a 64-bit microsecond counter, `RTCGetTickUs()`, built from the
TIM2 overflow count and the hardware counter. A frame record carries only the low
32 bits, because eight bytes of timestamp per record would consume a large part
of the link budget. The full value is restored from the newest time
synchronisation record:

```
delta   = (int32_t)(frame_ts32 - (uint32_t)sync_ts64)   # signed on purpose
full_ts = sync_ts64 + delta
```

Treating the difference as signed also reconstructs frames stamped shortly before
the synchronisation record. The 32-bit field wraps after about 4295 s, the
synchronisation record is sent every second and additionally before the first
frame after start-up and after a mode change, so the reconstruction is never
ambiguous.

The text log on the USB drive uses a different scale, microseconds since
midnight modulo 24 h, computed by `gettime()`. The receiver reproduces it from
the RTC fields of the session record, which is what makes the two files
comparable.

## Records

### `0x01` frame, 10 + DLC bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x01` |
| 1 | 2 | sequence number |
| 3 | 2 | `id` in bits 10..0, `dlc` in bits 14..11, channel in bit 15 (0 means CAN1) |
| 5 | 4 | device time, low 32 bits, microseconds |
| 9 | DLC | payload, exactly DLC bytes |
| 9+DLC | 1 | CRC-8 |

Only standard data frames are carried: 11-bit identifier, DLC 0 to 8. There are
no IDE and RTR flags, because a frame outside that scope is not transmitted as a
frame record at all. It is counted and reported as an event, since its presence
on a bus configured for CAN 2.0A is itself worth detecting.

### `0x02` time synchronisation, 12 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x02` |
| 1 | 2 | sequence number |
| 3 | 8 | full device time, microseconds |
| 11 | 1 | CRC-8 |

### `0x03` session start, 43 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x03` |
| 1 | 2 | sequence number |
| 3 | 1 | protocol version |
| 4 | 1 | flags: bit 0 measurement mode, bit 1 USB logging active, bit 2 stream enabled |
| 5 | 2 | longest record the device emits |
| 7 | 8 | device time at the RTC read |
| 15 | 4 | CAN1 bit rate, computed from BTR |
| 19 | 4 | CAN2 bit rate |
| 23 | 4 | CAN1 raw BTR |
| 27 | 4 | CAN2 raw BTR |
| 31 | 4 | data channel baud rate |
| 35 | 1 | RTC valid |
| 36 | 6 | year (2000 based), month, day, hour, minute, second |
| 42 | 1 | CRC-8 |

Sent at start-up, when the stream is enabled, after a baud rate change and after
every device reset. Receiving it during a session means the device restarted, so
the sequence numbering starts over.

### `0x04` bus event, 25 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x04` |
| 1 | 2 | sequence number |
| 3 | 1 | channel |
| 4 | 1 | reason, see the table below |
| 5 | 1 | controller state: 0 active, 1 warning, 2 passive, 3 bus-off |
| 6 | 2 | frame identifier, `0xFFFF` when not known |
| 8 | 4 | HAL error mask, or the extended identifier for a rejected extended frame |
| 12 | 4 | events of this class suppressed by rate limiting since the previous one |
| 16 | 8 | full device time |
| 24 | 1 | CRC-8 |

| Reason | Meaning |
|---|---|
| 0 | read from the controller failed |
| 1 | extended identifier frame rejected |
| 2 | remote frame rejected |
| 3 | DLC above 8 rejected |
| 4 | valid frame dropped in the RAM ring buffer |
| 5 | receive queue full |
| 6 | receive queue overrun |
| 7 | controller error, class in the error mask |
| 8 | controller state change |
| 9 | record dropped, transmit buffer full |
| 10 | USB logging started or stopped |

Events of the same class on the same bus are spaced at least 10 ms apart. The
suppressed count keeps the picture complete, and the exact totals are always in
the bus statistics.

A limitation inherited from the hardware: for an ACK, CRC, form, stuff or bit
error the identifier of the damaged frame is not trustworthy, and a receive queue
overrun reports how many events occurred, not which frames were lost. Only a ring
buffer drop happens after the header was read, so only there is the identifier
known.

### `0x05` bus statistics, 111 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x05` |
| 1 | 2 | sequence number |
| 3 | 1 | channel |
| 4 | 8 | full device time |
| 12 | 4 × 23 | counters, in order: `rx_frames`, `valid_frames`, `buffered_frames`, `payload_bytes`, `rx_read_errors`, `extended_frames`, `remote_frames`, `invalid_dlc`, `fifo_full`, `fifo_overrun`, `ring_dropped`, `warning_entries`, `passive_entries`, `bus_off_entries`, `stuff_errors`, `form_errors`, `ack_errors`, `bit_recessive_errors`, `bit_dominant_errors`, `crc_errors`, `other_errors`, `last_error_code`, `last_ring_drop_id` |
| 104 | 6 | `fifo_max_fill`, `current_rec`, `current_tec`, `max_rec`, `max_tec`, state |
| 110 | 1 | CRC-8 |

Emitted every 100 ms per bus. The counters are the ones the diagnostics module
already keeps for the file on the USB drive, read through the non-consuming
`CAN_DiagnosticsGetSnapshot()`, so the existing USB path keeps working unchanged.
They are cumulative since start-up and 32 bits wide, so a receiver has to compute
increments modulo 2^32 and read a decrease as a wrap.

### `0x06` logger statistics, 49 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x06` |
| 1 | 2 | sequence number |
| 3 | 8 | full device time |
| 11 | 4 | ring buffer drops in total |
| 15 | 4 | records transmitted |
| 19 | 4 | bytes transmitted |
| 23 | 4 | records dropped, transmit buffer full |
| 27 | 4 | bytes dropped |
| 31 | 4 | events dropped, event queue full |
| 35 | 4 | longest observed main loop iteration, microseconds |
| 39 | 2 | peak ring buffer occupancy |
| 41 | 2 | usable ring buffer capacity |
| 43 | 2 | peak transmit buffer occupancy |
| 45 | 2 | usable transmit buffer capacity |
| 47 | 1 | flags, as in the session record |
| 48 | 1 | CRC-8 |

The longest loop iteration matters because the independent watchdog resets the
device if a single pass takes too long. It is a measured value reported for every
session, not an assumption.

### `0x07` command acknowledgement, 18 bytes

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `0x07` |
| 1 | 2 | sequence number |
| 3 | 1 | status, 0 accepted, 1 rejected |
| 4 | 1 | command: 1 stream on or off, 2 measurement mode, 3 baud rate, 4 RTC set |
| 5 | 4 | value |
| 9 | 8 | full device time |
| 17 | 1 | CRC-8 |

Setting the RTC reboots the device, so the acknowledgement for command 4 is the
last record of the session.

### `0x20` reserved

Reserved for a record carrying CANH, CANL, `V_diff` and `V_CM` aggregates, for
the correlation of the physical layer with frame content. Not implemented. A
receiver must skip unknown types, so adding it will not break anything.

## Commands

Accepted as text lines on the debug channel, USART2 at 115200 baud. Each is
acknowledged on the debug channel in text and on the data channel as an
acknowledgement record.

| Command | Effect |
|---|---|
| `stream on`, `stream off` | enables or disables the record stream |
| `stream baud <n>` | changes the data channel speed, drains the buffer first, then sends a fresh session record at the new speed |
| `stream stats` | prints the counters as text on the debug channel |
| `measure on`, `measure off` | measurement mode: suspends writing to the USB drive so the throughput does not depend on the file system |
| `can selftest [1\|2]` | nine frames in silent loopback, see below |
| `set date DD MM YYYY`, `set time HH MM` | sets the RTC, reboots afterwards |

Command identifiers in the acknowledgement record: 1 stream on or off,
2 measurement mode, 3 baud rate, 4 RTC set, 5 self test.

## Self test in silent loopback

`can selftest` reconfigures one controller into silent loopback, transmits nine
frames with identifiers `0x600` to `0x608` and DLC sweeping 0 to 8, then restores
normal mode. The bit timing is not touched. In silent loopback the controller does
not drive the bus lines, so the test is safe with the bus connected and it does
not depend on any external node, on the bit rate present on the wire or on the
termination.

The frames return through the ordinary path: receive interrupt, ring buffer,
record stream and the file on the USB drive. Reading them on the Raspberry Pi
therefore proves the whole receive chain and separates a software fault from a
wiring or bit rate problem:

- nine frames decoded, `rx=9`, `valid=9`, `buffered=9`: the receive chain works
  end to end, so any absence of real traffic is external to the firmware;
- zero frames decoded: the fault is inside the device and the record stream is
  not the place to look first.

The test is disabled by default, see `CAN_SELFTEST_ON_BOOT` in `App.c`, and is
available on demand through the `can selftest` command.

## Reading a silent receiver

Counters, in this order, identify what is wrong without any guessing:

| Symptom | Meaning |
|---|---|
| `rx=0`, `rec=0`, no error counters | nothing reaches the controller: wiring, or the other node is not transmitting, possibly stuck in bus-off |
| `rx=0`, `rec` grows, `stuff` or `form` grow | bits arrive but do not decode: bit rate mismatch, CANH and CANL swapped, or missing termination |
| `rx>0`, `valid=0`, `ext>0` | frames arrive correctly and are rejected as extended identifier frames. The transmitter is configured for 29-bit identifiers while the record format covers 11-bit standard data frames only. The identifier is reported in the `error_code` field of the rejection event |
| `rx>0`, `valid=0`, `rtr>0` or `dlc_err>0` | remote frames, or a DLC above 8 |
| `valid>0`, `queued=0` | the ring buffer rejected the frames, see `ring_drop` |
| `queued>0`, no frames on the receiver | the fault is in the transmit path towards the Raspberry Pi, see `tx_records_dropped` |

The third row was the outcome of the first bring-up on the bench, and it took a
loopback self test to separate it from a suspected fault in the firmware.

## Deviations from the requirements, for approval

1. **Requirement 2.5** asks for a sequence number per frame accepted into the
   ring buffer. Implemented differently: the number counts records placed on the
   link. The reason is stated above under sequence numbering, it is what lets a
   receiver separate a loss on the link from a drop inside the device. Frame level
   losses remain fully accounted for through counters and events.
2. **Requirement 2.6** asks for the sequence number in the text log on the USB
   drive as well. Not implemented: it would add a fifth column to the format the
   existing `CAN_PARSER` reads. Left for a decision, either extend the parser or
   keep the number in the binary stream only.
3. **Requirement 7.7** asks for commands on the data channel too. Implemented on
   the debug channel only. The data channel stays unidirectional, which keeps the
   binary stream free of any parsing on the receive side.
