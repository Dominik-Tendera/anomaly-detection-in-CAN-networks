# CAN diagnostics log format

Normal CAN frame records keep the original four columns:

```text
<time_us> <standard_id_hex> <data_hex> <bus>
```

`#SESSION frame_format=2` means that the payload length is encoded by the
length of `data_hex`: two hexadecimal characters per payload byte. For
example, `030201` represents three bytes on the wire: `01 02 03`. DLC zero is
written as the single token `0`. Eight-byte frames retain the old 16-character
data field, so the legacy parser continues to decode all frame records. Old
logs without `frame_format=2` are treated as DLC 8 because they always padded
the field to eight bytes.

Diagnostic records start with `#`. Legacy parser versions skip them because
they are not valid frame records. The current parser reads the `key=value`
fields without needing a DBC file and can write them to its diagnostics TXT
report and Vector ASC export.

For TXT output, the parser now keeps two complementary diagnostics files:

- `*_CAN_diagnostics.txt` is the lossless, line-oriented technical output with
  decoded masks and the complete original logger record.
- `*_CAN_report.txt` is a Polish human-readable report with an overall result,
  final per-bus counters, logger buffer usage and plain-language descriptions
  of every `#CANEVENT`. It retains the raw `#CANEVENT` below each description,
  but does not repeat normal periodic snapshots in the event list.

All `t` values use the same microseconds-since-midnight clock as normal CAN
records. Counters marked `scope=boot` are cumulative since logger startup and
may wrap after `2^32 - 1`.

## Records

- `#SESSION` is written when a log file is opened. It contains format version,
  configured bitrates, raw BTR values, CAN modes and automatic bus-off recovery
  configuration.
- `#CANEVENT` is rate-limited to at most one aggregate record per CAN bus per
  second. `flags` bit 0 means a controller error, bit 1 a receive FIFO overrun,
  bit 2 a controller state change and bit 3 a valid frame dropped at the RAM
  ring buffer. It also records the current state, receive/transmit error
  counters (`rec`/`tec`), the latest HAL error mask and the last ring-drop ID.
- `#CANSTAT` is written at normal file rotation for each bus. It distinguishes
  frames received by hardware, valid standard data frames, frames accepted into
  the RAM ring buffer, payload-byte count, HAL read errors, unsupported
  extended/remote/invalid-DLC frames, FIFO pressure, FIFO loss and RAM
  ring-buffer loss. It also records the last known ring-drop ID plus current and
  peak REC/TEC. Counter deltas and timestamps allow an offline tool to estimate
  traffic rate and bus load without changing normal frame records.
- `#CANERR` is written at file rotation for each bus. It contains state-entry
  counts and protocol error classes: stuff, form, ACK, recessive bit, dominant
  bit and CRC.
- `#LOGGERSTAT` is written at file rotation. It contains the peak RAM ring
  occupancy, usable capacity and total ring-buffer drops for the current
  buffer lifetime.

The bxCAN peripheral can identify the error class and controller state, but a
corrupted frame is rejected before its identifier can be trusted. Therefore an
ACK/CRC/form/stuff/bit error cannot reliably contain the damaged frame ID.
Likewise, an RX FIFO overrun reports how many overrun events occurred, not the
IDs of frames that hardware discarded. A RAM ring-buffer drop occurs after a
valid frame header was read, so its most recent ID is available and logged.
