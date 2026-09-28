# STM32 USB-CAN Logger

**Author:** kubak  
**Created:** May 10, 2023

---

## Overview

This project implements a USB-to-CAN data logger on an STM32 microcontroller. When a USB flash drive (“pen”) is connected, the system:

1. Enumerates and mounts the drive via the FATFS library.
2. Creates a timestamped log file on the drive.
3. Receives CAN frames from two CAN buses.
4. Buffers incoming frames in a ring buffer.
5. Periodically writes CAN data to the log file.
6. Outputs diagnostic and debug messages over UART.

The main application loop continuously services USB host events, manages pen (drive) state transitions, processes CAN/UART I/O, and drives LEDs for heartbeat.

---

## Key Features

- **USB Host / FATFS integration**  
  Auto-mounts/dismounts FAT filesystem on USB flash drives; creates new log files every minute with datetime-based names.

- **Dual-CAN support**  
  Uses CAN1 and CAN2 interfaces (500 kbps or 1Mbps with change of resistor) to capture frames; each Rx callback pushes frames into a lock-free ring buffer.

- **Buffered logging**  
  Incoming frames are parsed, timestamped, and stored in RAM. When buffer thresholds or timers trigger, data is flushed to the USB drive.

- **Binary record stream towards a Raspberry Pi**  
  USART1 is a data channel carrying every received frame with a microsecond
  timestamp, the DLC, the channel number and a sequence number, plus the
  diagnostic counters and bus events. Default speed 2 000 000 baud, transmitted
  by DMA. USART2 stays the text debug channel and accepts the commands.
  See `CAN_STREAM_PROTOCOL.md` for the wire format, `tools/rpi_receiver/` for the
  receiving side and `tests/host/` for the tests of the format.

- **UART debug interface**  
  The debug channel (`huart2`) transmits human-readable status messages without
  blocking the main loop and accepts configuration commands.

- **Real-Time Clock (RTC) management**  
  Supports host commands over UART to set date/time; uses RTC to timestamp the log files.

- **LED heartbeat**  
  Blinks an on-board LED at 600 ms intervals to show the system is alive.


# CAN PARSER included (Python)

## Overview

This standalone Python script takes raw CAN‑frame log files (with extensions `.log` or `.aclp`) produced by the STM32 logger, decodes each frame using a user‑supplied DBC database, and writes out per‑signal text files. It then merges those per‑signal files into consolidated outputs and cleans up temporary data.  

**It is important that user should include his own current .dbc file containing definitions of can frames captured in .log file that will be parsed**

Key stages:

1. **Discover log files** (`.log`, `.aclp`) in the current directory.  
2. **Load up to two** `.dbc` file(s) (CAN database) present in the directory.  
3. **Parallel parse** each log:
   - Read each line’s timestamp, CAN ID, raw data, and channel number.
   - Detect and mark any invalid characters (nulls or non‑alphanumeric) as “corruption warnings.”
   - Decode the raw 8‑byte CAN payload into named signals via `cantools`.
   - Buffer each signal’s time/value history in memory.  
   - Write per‑signal text files to `parser_temp_files/` named `<logname>.<signal>_<channel>.txt`.  

4. **Merge** all per‑signal files by suffix (signal name + channel) into a single file per group under a directory matching the lowest numeric log prefix.  
5. **Clean up** the `parser_temp_files/` directory.

## Parser output modes

Set `output_format` in `CAN_PARSER/config.ini`, select a mode in the startup
menu, or override it with `--format txt|asc|both`:

- `txt` retains the existing per-signal TXT output and writes two diagnostics
  files. `CAN_diagnostics.txt` is the lossless technical record, while the
  Polish `CAN_report.txt` gives a plain-language result, final CAN1/CAN2
  counters, logger-buffer usage and descriptions of individual CAN events.
- `asc` exports raw CAN frames and aggregated `ErrorFrame` markers to a Vector
  CANoe-compatible ASCII trace. A DBC is not required for this mode.
- `both` produces both result sets.

The parser accepts legacy frame records with a fixed 16-character data field
and frame format v2, where the data-field length preserves the actual DLC. The
four original columns are retained, so older parser executables can still read
new frame records; they ignore diagnostics and pad shorter payloads to eight
bytes as before.

Log paths can also be passed directly, which is useful when logs are not in
the parser directory:

```bash
PARSER.exe --format both 202608102353.log 202608102354.log
```

---

## Prerequisites

- **Python 3.7+**  
- **cantools**  
- **tqdm**  

Install dependencies via pip:

```bash
pip install cantools tqdm
