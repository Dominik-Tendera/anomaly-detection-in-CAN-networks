# -*- coding: utf-8 -*-
"""
A script for parsing and analyzing CAN logs using DBC files.

It supports two processing modes:
- 'speed': Faster, but consumes more RAM.
- 'safe': Slower, but more memory-efficient, ideal for very large files.

The script leverages multiprocessing to speed up operations.
"""
import argparse
import configparser
import heapq
import mmap
import multiprocessing as mp
import os
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime
from functools import partial
from multiprocessing import Pool

import cantools
from tqdm import tqdm

# Try to import psutil; if it fails, set it to None.
try:
    import psutil
except ImportError:
    psutil = None

# --- Global Constants ---
CONFIG_FILE = "config.ini"
TEMP_DIR = "parser_temp"
DEFAULT_CONFIG_CONTENT = """[Settings]
# Number of CPU cores to use. 0 means all available cores minus one.
cpu_cores = 0

# Processing mode.
# speed: Fastest, but uses more RAM. Recommended for most systems.
# safe: Slower, but uses significantly less RAM. Ideal for very large files or low-memory systems.
processing_mode = speed

# Output filename style.
# 0: Prepends the first log's name (e.g., 12345_SignalName.txt).
# 1: Uses only the signal name (e.g., SignalName.txt).
filename_style = 0

# Show real-time CPU and RAM usage next to the progress bar.
# true or false
show_resources = true

# Output selection.
# ask: Show a menu at startup.
# txt: Decode DBC signals and add technical plus readable diagnostics TXT files.
# asc: Export raw frames and error events to a Vector CANoe ASCII file.
# both: Produce TXT and ASC outputs.
output_format = ask
"""

MICROSECONDS_PER_DAY = 24 * 60 * 60 * 1000000
ASC_REORDER_WINDOW_US = 1500000

CAN_EVENT_FLAGS = (
    (0x01, "controller_error"),
    (0x02, "fifo_overrun"),
    (0x04, "state_change"),
    (0x08, "ring_drop"),
)

HAL_CAN_ERRORS = (
    (0x00000001, "warning"),
    (0x00000002, "error_passive"),
    (0x00000004, "bus_off"),
    (0x00000008, "stuff"),
    (0x00000010, "form"),
    (0x00000020, "ack"),
    (0x00000040, "bit_recessive"),
    (0x00000080, "bit_dominant"),
    (0x00000100, "crc"),
    (0x00000200, "rx_fifo0_overrun"),
    (0x00000400, "rx_fifo1_overrun"),
    (0x00000800, "tx0_arbitration_lost"),
    (0x00001000, "tx0_error"),
    (0x00002000, "tx1_arbitration_lost"),
    (0x00004000, "tx1_error"),
    (0x00008000, "tx2_arbitration_lost"),
    (0x00010000, "tx2_error"),
    (0x00020000, "timeout"),
    (0x00040000, "not_initialized"),
    (0x00080000, "not_ready"),
    (0x00100000, "not_started"),
    (0x00200000, "invalid_parameter"),
    (0x00400000, "invalid_callback"),
    (0x00800000, "internal"),
)

CAN_EVENT_DESCRIPTIONS = (
    (0x01, "błąd kontrolera CAN"),
    (0x02, "przepełnienie sprzętowego FIFO odbiorczego"),
    (0x04, "zmiana stanu kontrolera"),
    (0x08, "utrata ramki w buforze RAM loggera"),
)

HAL_CAN_ERROR_DESCRIPTIONS = (
    (0x00000001, "przekroczony próg ostrzegawczy"),
    (0x00000002, "kontroler w stanie error-passive"),
    (0x00000004, "kontroler odłączony od magistrali (bus-off)"),
    (0x00000008, "błąd synchronizacji bitów (stuff error)"),
    (0x00000010, "błąd formatu ramki (form error)"),
    (0x00000020, "brak potwierdzenia ramki (ACK error)"),
    (0x00000040, "błąd bitu recesywnego"),
    (0x00000080, "błąd bitu dominującego"),
    (0x00000100, "błąd sumy kontrolnej CRC"),
    (0x00000200, "przepełnienie RX FIFO0"),
    (0x00000400, "przepełnienie RX FIFO1"),
    (0x00000800, "utrata arbitrazu TX0"),
    (0x00001000, "błąd transmisji TX0"),
    (0x00002000, "utrata arbitrazu TX1"),
    (0x00004000, "błąd transmisji TX1"),
    (0x00008000, "utrata arbitrazu TX2"),
    (0x00010000, "błąd transmisji TX2"),
    (0x00020000, "przekroczony limit czasu"),
    (0x00040000, "kontroler niezainicjalizowany"),
    (0x00080000, "kontroler niegotowy"),
    (0x00100000, "kontroler nieuruchomiony"),
    (0x00200000, "nieprawidłowy parametr sterownika"),
    (0x00400000, "nieprawidłowe wywołanie zwrotne sterownika"),
    (0x00800000, "wewnętrzny błąd sterownika"),
)

CAN_STATE_DESCRIPTIONS = {
    "active": "aktywny (active)",
    "warning": "ostrzeżenie (warning)",
    "passive": "pasywny (error-passive)",
    "bus_off": "odłączony od magistrali (bus-off)",
}

CAN_ERROR_COUNTER_DESCRIPTIONS = (
    ("warning", "wejścia w stan warning"),
    ("passive", "wejścia w stan error-passive"),
    ("bus_off", "wejścia w stan bus-off"),
    ("stuff", "błędy stuff"),
    ("form", "błędy formatu"),
    ("ack", "błędy ACK"),
    ("bit_r", "błędy bitu recesywnego"),
    ("bit_d", "błędy bitu dominującego"),
    ("crc", "błędy CRC"),
    ("other", "inne błędy"),
)


class bcolors:
    """A class for coloring terminal text using ANSI escape codes."""

    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def find_dbc_files():
    """Finds and sorts .dbc files in the current directory."""
    return sorted(
        [f for f in os.listdir() if f.endswith(".dbc")], key=str.casefold
    )


def create_dir(dir_path):
    """Creates a directory if it does not exist."""
    os.makedirs(dir_path, exist_ok=True)


def get_log_files_from_folder():
    """Finds and sorts log files (.aclp, .log) in the current directory."""
    return sorted([f for f in os.listdir() if f.endswith((".aclp", ".log"))])


def get_log_files_from_dialog():
    """Opens a file dialog to select log files."""
    print(
        f"{bcolors.OKCYAN}No log files found. Opening file selection dialog...{bcolors.ENDC}"
    )
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        print(
            f"{bcolors.FAIL}File dialog is unavailable: {error}. "
            f"Place logs next to the parser or pass their paths on the "
            f"command line.{bcolors.ENDC}"
        )
        return []

    try:
        root = tk.Tk()
        root.withdraw()
        files = filedialog.askopenfilenames(
            title="Select log files",
            filetypes=(("Log files", "*.log *.aclp"), ("All files", "*.*")),
        )
        root.destroy()
        return list(files)
    except tk.TclError as error:
        print(
            f"{bcolors.FAIL}File dialog is unavailable: {error}. "
            f"Place logs next to the parser or pass their paths on the "
            f"command line.{bcolors.ENDC}"
        )
        return []


def load_config():
    """Loads configuration from an .ini file or creates a default one if it doesn't exist."""
    config = configparser.ConfigParser()

    if not os.path.exists(CONFIG_FILE):
        print(
            f"{bcolors.OKCYAN}{CONFIG_FILE} not found. Creating a new one with default settings...{bcolors.ENDC}"
        )
        with open(CONFIG_FILE, "w", encoding="utf-8") as configfile:
            configfile.write(DEFAULT_CONFIG_CONTENT)

    try:
        config.read(CONFIG_FILE)
        print(
            f"{bcolors.OKGREEN}Successfully loaded settings from {CONFIG_FILE}.{bcolors.ENDC}"
        )
    except configparser.Error as e:
        print(
            f"{bcolors.FAIL}CONFIGURATION ERROR: {e}. Using default settings.{bcolors.ENDC}"
        )
        config.read_string(DEFAULT_CONFIG_CONTENT)

    settings = {
        "cpu_cores": config.getint("Settings", "cpu_cores", fallback=0),
        "processing_mode": config.get(
            "Settings", "processing_mode", fallback="speed"
        ).lower(),
        "filename_style": config.getint("Settings", "filename_style", fallback=0),
        "show_resources": config.getboolean("Settings", "show_resources", fallback=True)
        if psutil
        else False,
        "output_format": config.get(
            "Settings", "output_format", fallback="txt"
        ).lower(),
    }

    print(f"{bcolors.HEADER}--- Current Configuration ---{bcolors.ENDC}")
    for key, value in settings.items():
        print(f"{bcolors.OKBLUE}{key:<20}: {value}{bcolors.ENDC}")
    print(f"{bcolors.HEADER}---------------------------{bcolors.ENDC}")
    return settings


def format_bytes(byte_count):
    """Formats a byte count into a human-readable string (KB, MB, GB)."""
    if byte_count is None:
        return "N/A"
    power = 1024
    n = 0
    power_labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T"}
    while byte_count >= power and n < len(power_labels) - 1:
        byte_count /= power
        n += 1
    return f"{byte_count:.2f} {power_labels[n]}B"


def parse_arguments():
    """Parses optional command-line overrides while keeping double-click use simple."""
    parser = argparse.ArgumentParser(description="Parse STM32 CAN logger files")
    parser.add_argument(
        "--format",
        choices=("txt", "asc", "both"),
        dest="output_format",
        help="output format; overrides config.ini",
    )
    parser.add_argument(
        "logs",
        nargs="*",
        help="optional .log/.aclp files; otherwise the current directory is scanned",
    )
    return parser.parse_args()


def select_output_format(configured_format, command_line_format=None):
    """Selects legacy TXT, CANoe ASC, or both outputs."""
    if command_line_format:
        return command_line_format
    if configured_format in ("txt", "asc", "both"):
        return configured_format
    if configured_format != "ask":
        print(
            f"{bcolors.WARNING}Unknown output_format '{configured_format}'. "
            f"Using TXT.{bcolors.ENDC}"
        )
        return "txt"
    if not sys.stdin or not sys.stdin.isatty():
        print(
            f"{bcolors.WARNING}No interactive console available. "
            f"Using TXT output.{bcolors.ENDC}"
        )
        return "txt"

    choices = {"1": "txt", "2": "asc", "3": "both"}
    while True:
        print(f"{bcolors.HEADER}--- Select output ---{bcolors.ENDC}")
        print("1: Decoded signal TXT + technical and readable CAN diagnostics")
        print("2: Vector CANoe ASC")
        print("3: Both TXT and CANoe ASC")
        selected = input("Selection [1-3]: ").strip()
        if selected in choices:
            return choices[selected]
        print(f"{bcolors.WARNING}Enter 1, 2, or 3.{bcolors.ENDC}")


def format_time_hms(microseconds):
    """Formats a microseconds-since-midnight timestamp used by logger records."""
    microseconds %= MICROSECONDS_PER_DAY
    hours, remainder = divmod(microseconds, 3600000000)
    minutes, remainder = divmod(remainder, 60000000)
    seconds, remainder = divmod(remainder, 1000000)
    milliseconds, micros = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}:"
        f"{milliseconds:03d}:{micros:03d}"
    )


def parse_frame_record(line):
    """Parses legacy fixed-width and v2 DLC-preserving CAN frame records."""
    if not line or line.startswith("#"):
        return None

    parts = line.split()
    if len(parts) != 4:
        return None

    timestamp_text, frame_id_text, data_text, bus_text = parts
    try:
        timestamp = int(timestamp_text, 10)
        frame_id = int(frame_id_text, 16)
        bus = int(bus_text, 10)
        if data_text == "0":
            encoded_data = b""
        else:
            if (
                len(data_text) == 0
                or len(data_text) > 16
                or (len(data_text) % 2) != 0
            ):
                return None
            encoded_data = bytes.fromhex(data_text)
    except ValueError:
        return None

    if timestamp < 0 or not (0 <= frame_id <= 0x7FF) or bus < 1:
        return None

    return {
        "timestamp": timestamp,
        "frame_id": frame_id,
        "bus": bus,
        "dlc": len(encoded_data),
        # The firmware writes the uint64 representation, so bytes are reversed
        # back to their original order on the CAN wire.
        "data": encoded_data[::-1],
        "data_text": data_text,
    }


def parse_diagnostic_record(line):
    """Parses a #TYPE key=value logger diagnostics record."""
    if not line.startswith("#"):
        return None
    parts = line.split()
    if not parts or len(parts[0]) < 2:
        return None

    fields = {}
    for token in parts[1:]:
        key, separator, value = token.partition("=")
        if separator and key:
            fields[key] = value
    try:
        timestamp = int(fields["t"], 10)
    except (KeyError, ValueError):
        return None
    return {
        "type": parts[0][1:],
        "timestamp": timestamp,
        "fields": fields,
        "raw": line,
    }


def parse_mask(value, definitions):
    """Returns symbolic names for a hexadecimal or decimal bit mask."""
    try:
        mask = int(value, 0)
    except (TypeError, ValueError):
        return "invalid_mask"
    names = [name for bit, name in definitions if mask & bit]
    known_mask = 0
    for bit, unused_name in definitions:
        known_mask |= bit
    unknown = mask & ~known_mask
    if unknown:
        names.append(f"unknown_0x{unknown:X}")
    return "|".join(names) if names else "none"


def format_diagnostic_record(record):
    """Creates a lossless, human-readable diagnostics line."""
    fields = record["fields"]
    annotations = []
    if "flags" in fields:
        annotations.append(f"events={parse_mask(fields['flags'], CAN_EVENT_FLAGS)}")
    if "last" in fields:
        annotations.append(f"last_errors={parse_mask(fields['last'], HAL_CAN_ERRORS)}")
    annotation_text = " ".join(annotations)
    if annotation_text:
        annotation_text += " | "
    return (
        f"{record['timestamp']} {format_time_hms(record['timestamp'])} "
        f"{record['type']} {annotation_text}{record['raw']}\n"
    )


def parse_numeric_field(record, field_name, default=0):
    """Reads a decimal or hexadecimal diagnostic field without raising."""
    try:
        return int(record["fields"].get(field_name, str(default)), 0)
    except (TypeError, ValueError):
        return default


def mask_descriptions(value, definitions):
    """Returns friendly descriptions for all known and unknown mask bits."""
    try:
        mask = int(value, 0)
    except (TypeError, ValueError):
        return ["nieprawidłowa wartość maski"]

    descriptions = [description for bit, description in definitions if mask & bit]
    known_mask = 0
    for bit, unused_description in definitions:
        known_mask |= bit
    unknown = mask & ~known_mask
    if unknown:
        descriptions.append(f"nieznane bity 0x{unknown:X}")
    return descriptions


def format_counter(value):
    """Formats integer counters with spaces as thousands separators."""
    return f"{value:,}".replace(",", " ")


def scan_diagnostic_file(filename):
    """Finds only # records without parsing every normal CAN frame."""
    records = []
    with open(filename, "rb") as source:
        if os.fstat(source.fileno()).st_size == 0:
            return records

        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            start = 0 if contents[0:1] == b"#" else -1
            while True:
                if start < 0:
                    marker = contents.find(b"\n#")
                    if marker < 0:
                        break
                    start = marker + 1

                end = contents.find(b"\n", start)
                if end < 0:
                    end = len(contents)
                line = contents[start:end].rstrip(b"\r").decode(
                    "utf-8", errors="replace"
                )
                record = parse_diagnostic_record(line)
                if record is not None:
                    records.append(record)

                if end >= len(contents):
                    break
                marker = contents.find(b"\n#", end)
                if marker < 0:
                    break
                start = marker + 1
    return records


def collect_diagnostic_records(logs):
    """Collects diagnostics quickly and restores chronology across log files."""
    ordered_logs = sorted(logs, key=lambda value: os.path.basename(value))
    dated_logs = [
        (filename, log_datetime_from_filename(filename))
        for filename in ordered_logs
    ]
    known_dates = [value for unused_filename, value in dated_logs if value is not None]
    base_date = min(known_dates).date() if known_dates else None
    collected = []
    max_timestamp = None
    day_offset = 0
    sequence = 0

    for filename, file_datetime in dated_logs:
        file_day_offset = None
        if base_date is not None and file_datetime is not None:
            file_day_offset = (
                file_datetime.date() - base_date
            ).days * MICROSECONDS_PER_DAY

        for record in scan_diagnostic_file(filename):
            if file_day_offset is not None:
                absolute_timestamp = file_day_offset + record["timestamp"]
                if max_timestamp is not None:
                    if absolute_timestamp > max_timestamp + (
                        MICROSECONDS_PER_DAY // 2
                    ):
                        absolute_timestamp -= MICROSECONDS_PER_DAY
                    elif absolute_timestamp < max_timestamp - (
                        MICROSECONDS_PER_DAY // 2
                    ):
                        absolute_timestamp += MICROSECONDS_PER_DAY
            else:
                absolute_timestamp = day_offset + record["timestamp"]
                if (
                    max_timestamp is not None
                    and absolute_timestamp
                    < max_timestamp - (MICROSECONDS_PER_DAY // 2)
                ):
                    day_offset += MICROSECONDS_PER_DAY
                    absolute_timestamp += MICROSECONDS_PER_DAY

            sequence += 1
            collected.append((absolute_timestamp, sequence, record))
            if max_timestamp is None or absolute_timestamp > max_timestamp:
                max_timestamp = absolute_timestamp

    collected.sort(key=lambda item: (item[0], item[1]))
    return [
        (absolute_timestamp, record)
        for absolute_timestamp, unused_sequence, record in collected
    ]


def human_diagnostics_output_filename(logs, exitdir, filename_style):
    base_name = os.path.basename(logs[0]).split(".")[0]
    filename = (
        f"{base_name}_CAN_report.txt"
        if filename_style == 0
        else "CAN_report.txt"
    )
    return os.path.join(exitdir, filename)


def human_event_severity(record):
    """Classifies a CANEVENT for quick visual scanning."""
    fields = record["fields"]
    flags = parse_numeric_field(record, "flags")
    last_errors = parse_numeric_field(record, "last")
    state = fields.get("state", "unknown")
    if state == "bus_off" or last_errors & 0x00000004:
        return "KRYTYCZNY"
    if state == "passive" or flags & 0x01:
        return "BŁĄD"
    if state == "warning" or flags & (0x02 | 0x08):
        return "OSTRZEŻENIE"
    return "INFORMACJA"


def format_human_can_event(record):
    """Explains one CANEVENT in plain language without losing its raw value."""
    fields = record["fields"]
    bus = fields.get("bus", "?")
    flags = parse_numeric_field(record, "flags")
    event_descriptions = mask_descriptions(
        fields.get("flags", "0"), CAN_EVENT_DESCRIPTIONS
    )
    error_descriptions = mask_descriptions(
        fields.get("last", "0"), HAL_CAN_ERROR_DESCRIPTIONS
    )
    state = fields.get("state", "unknown")
    state_description = CAN_STATE_DESCRIPTIONS.get(
        state, f"nieznany ({state})"
    )

    lines = [
        f"[{format_time_hms(record['timestamp'])}] CAN{bus} - "
        f"{human_event_severity(record)}",
        "  Co się stało: "
        + (", ".join(event_descriptions) if event_descriptions else "brak flag zdarzeń"),
        f"  Stan kontrolera: {state_description}",
        f"  Liczniki błędów: REC={fields.get('rec', '?')}, "
        f"TEC={fields.get('tec', '?')}",
        "  Ostatnie błędy: "
        + (", ".join(error_descriptions) if error_descriptions else "brak"),
    ]

    if flags & 0x08:
        lines.append(
            f"  Ostatnie ID utracone w RAM: {fields.get('last_drop_id', 'nieznane')}"
        )
    if flags & 0x01:
        lines.append(
            "  ID uszkodzonej ramki: niedostępne - kontroler odrzuca "
            "ramkę, zanim jej ID można uznać za wiarygodne"
        )
    lines.append(f"  Surowy wpis: {record['raw']}")
    return "\n".join(lines)


def latest_diagnostic_records_by_bus(records, record_type):
    """Returns the newest record of a given type for every numeric CAN bus."""
    latest = {}
    for absolute_timestamp, record in records:
        if record["type"] != record_type:
            continue
        try:
            bus = int(record["fields"].get("bus", ""), 10)
        except ValueError:
            continue
        if bus not in latest or absolute_timestamp >= latest[bus][0]:
            latest[bus] = (absolute_timestamp, record)
    return latest


def diagnostics_have_problems(records):
    """Checks event flags and cumulative counters for recorded CAN problems."""
    canstat_problem_fields = (
        "read_err", "ext", "rtr", "fifo_full", "fifo_ovr", "ring_drop", "dlc_err"
    )
    error_counter_fields = tuple(
        field_name for field_name, unused_description in CAN_ERROR_COUNTER_DESCRIPTIONS
    )
    for unused_timestamp, record in records:
        if record["type"] == "CANEVENT" and parse_numeric_field(record, "flags"):
            return True
        if record["type"] == "CANSTAT" and any(
            parse_numeric_field(record, field_name)
            for field_name in canstat_problem_fields
        ):
            return True
        if record["type"] == "CANERR" and any(
            parse_numeric_field(record, field_name)
            for field_name in error_counter_fields
        ):
            return True
        if (
            record["type"] == "LOGGERSTAT"
            and parse_numeric_field(record, "ring_drop_total")
        ):
            return True
        if record["type"] == "LOGGERSTAT":
            capacity = parse_numeric_field(record, "ring_capacity")
            peak = parse_numeric_field(record, "ring_peak")
            if capacity and peak * 100 >= capacity * 80:
                return True
    return False


def format_human_diagnostics_summary(records):
    """Builds a compact final-state summary from cumulative logger records."""
    lines = []
    latest_canstat = latest_diagnostic_records_by_bus(records, "CANSTAT")
    latest_canerr = latest_diagnostic_records_by_bus(records, "CANERR")
    latest_canevent = latest_diagnostic_records_by_bus(records, "CANEVENT")
    buses = sorted(set(latest_canstat) | set(latest_canerr) | set(latest_canevent))

    if diagnostics_have_problems(records):
        lines.append("WYNIK: WYKRYTO PROBLEMY - szczegóły znajdują się poniżej.")
    else:
        lines.append("WYNIK: nie wykryto błędów CAN ani utraty ramek.")

    session_records = [item for item in records if item[1]["type"] == "SESSION"]
    if session_records:
        session = max(session_records, key=lambda item: item[0])[1]
        lines.extend(("", "KONFIGURACJA OSTATNIEJ SESJI:"))
        for bus in (1, 2):
            bitrate_name = f"can{bus}_bitrate"
            if bitrate_name not in session["fields"]:
                continue
            abom = session["fields"].get(f"abom{bus}", "?")
            abom_description = {"1": "włączony", "0": "wyłączony"}.get(
                abom, f"nieznany ({abom})"
            )
            lines.append(
                f"  CAN{bus}: {format_counter(parse_numeric_field(session, bitrate_name))} bit/s, "
                f"BTR={session['fields'].get(f'can{bus}_btr', '?')}, "
                f"auto bus-off recovery: {abom_description}"
            )
        lines.append(
            f"  Format ramek loggera: v{session['fields'].get('frame_format', '?')}"
        )

    for bus in buses:
        lines.append("")
        lines.append(f"CAN{bus}:")
        if bus in latest_canstat:
            record = latest_canstat[bus][1]
            fields = record["fields"]
            state = fields.get("state", "unknown")
            lines.extend(
                (
                    f"  Stan końcowy: {CAN_STATE_DESCRIPTIONS.get(state, state)}",
                    "  Ramki: odebrane {rx}, poprawne {valid}, zapisane do RAM {queued}".format(
                        rx=format_counter(parse_numeric_field(record, "rx")),
                        valid=format_counter(parse_numeric_field(record, "valid")),
                        queued=format_counter(parse_numeric_field(record, "queued")),
                    ),
                    f"  Bajty danych w poprawnych ramkach: "
                    f"{format_counter(parse_numeric_field(record, 'payload_bytes'))}",
                    "  Utrata: FIFO {fifo}, RAM {ring}, błędy odczytu {read}".format(
                        fifo=format_counter(parse_numeric_field(record, "fifo_ovr")),
                        ring=format_counter(parse_numeric_field(record, "ring_drop")),
                        read=format_counter(parse_numeric_field(record, "read_err")),
                    ),
                    "  Odrzucone formaty: extended {ext}, RTR {rtr}, błędny DLC {dlc}".format(
                        ext=format_counter(parse_numeric_field(record, "ext")),
                        rtr=format_counter(parse_numeric_field(record, "rtr")),
                        dlc=format_counter(parse_numeric_field(record, "dlc_err")),
                    ),
                    "  FIFO: sygnały full={full}, przepełnienia={overrun}, szczyt={peak}".format(
                        full=format_counter(parse_numeric_field(record, "fifo_full")),
                        overrun=format_counter(parse_numeric_field(record, "fifo_ovr")),
                        peak=format_counter(parse_numeric_field(record, "fifo_peak")),
                    ),
                    "  Szczyt liczników błędów: REC={rec}, TEC={tec}".format(
                        rec=parse_numeric_field(record, "rec_peak"),
                        tec=parse_numeric_field(record, "tec_peak"),
                    ),
                )
            )
            if parse_numeric_field(record, "ring_drop"):
                lines.append(
                    f"  Ostatnie znane ID utracone w RAM: "
                    f"{fields.get('last_drop_id', 'nieznane')}"
                )
        elif bus in latest_canevent:
            record = latest_canevent[bus][1]
            state = record["fields"].get("state", "unknown")
            lines.extend(
                (
                    f"  Stan z ostatniego zdarzenia: "
                    f"{CAN_STATE_DESCRIPTIONS.get(state, state)}",
                    f"  Liczniki z ostatniego zdarzenia: "
                    f"REC={record['fields'].get('rec', '?')}, "
                    f"TEC={record['fields'].get('tec', '?')}",
                )
            )
        if bus in latest_canerr:
            record = latest_canerr[bus][1]
            nonzero_errors = [
                f"{description}: {format_counter(parse_numeric_field(record, field_name))}"
                for field_name, description in CAN_ERROR_COUNTER_DESCRIPTIONS
                if parse_numeric_field(record, field_name)
            ]
            lines.append(
                "  Błędy protokołu/stanu: "
                + (", ".join(nonzero_errors) if nonzero_errors else "brak")
            )

    logger_records = [
        item for item in records if item[1]["type"] == "LOGGERSTAT"
    ]
    if logger_records:
        record = max(logger_records, key=lambda item: item[0])[1]
        peak = parse_numeric_field(record, "ring_peak")
        capacity = parse_numeric_field(record, "ring_capacity")
        usage = (100.0 * peak / capacity) if capacity else 0.0
        lines.extend(
            (
                "",
                "LOGGER:",
                f"  Maksymalne zapełnienie RAM: {format_counter(peak)}/"
                f"{format_counter(capacity)} ({usage:.1f}%)",
                "  Utracone ramki łącznie: "
                f"{format_counter(parse_numeric_field(record, 'ring_drop_total'))}",
            )
        )
    return "\n".join(lines)


def write_human_diagnostics_report(
    logs, exitdir, filename_style, diagnostic_records=None
):
    """Writes a concise Polish report explaining what happened on the CAN bus."""
    records = (
        collect_diagnostic_records(logs)
        if diagnostic_records is None
        else diagnostic_records
    )
    output_filename = human_diagnostics_output_filename(
        logs, exitdir, filename_style
    )
    can_events = [record for unused_timestamp, record in records if record["type"] == "CANEVENT"]

    # UTF-8 with BOM keeps Polish labels readable in Windows Notepad and
    # Windows PowerShell 5.1 while remaining normal UTF-8 for modern tools.
    with open(output_filename, "w", encoding="utf-8-sig") as output:
        output.write("RAPORT DIAGNOSTYCZNY CAN\n")
        output.write("========================\n")
        output.write("Pliki źródłowe:\n")
        for filename in logs:
            output.write(f"  - {os.path.basename(filename)}\n")
        output.write(f"Rekordy diagnostyczne: {len(records)}\n")
        if records:
            output.write(
                "Zakres czasu: "
                f"{format_time_hms(records[0][0])} - "
                f"{format_time_hms(records[-1][0])}\n"
            )
        output.write("\nPODSUMOWANIE\n")
        output.write("------------\n")
        if records:
            output.write(format_human_diagnostics_summary(records))
            output.write("\n")
        else:
            output.write("Brak rekordow diagnostycznych - to prawdopodobnie starszy log.\n")

        output.write("\nZDARZENIA CAN\n")
        output.write("-------------\n")
        if can_events:
            for index, record in enumerate(can_events):
                if index:
                    output.write("\n")
                output.write(format_human_can_event(record))
                output.write("\n")
        else:
            output.write("Brak wpisów CANEVENT.\n")

        output.write(
            "\nUWAGA\n"
            "-----\n"
            "Ten raport jest opisem pomocniczym. Surowe wartości pozostają "
            "w pliku *_CAN_diagnostics.txt i w logach z loggera.\n"
        )
    return output_filename, len(can_events)


def diagnostics_output_filename(logs, exitdir, filename_style):
    base_name = os.path.basename(logs[0]).split(".")[0]
    filename = (
        f"{base_name}_CAN_diagnostics.txt"
        if filename_style == 0
        else "CAN_diagnostics.txt"
    )
    return os.path.join(exitdir, filename)


def write_diagnostics_report(
    logs, exitdir, filename_style, diagnostic_records=None
):
    """Writes all diagnostic metadata in chronological timestamp order."""
    output_filename = diagnostics_output_filename(logs, exitdir, filename_style)
    records = (
        collect_diagnostic_records(logs)
        if diagnostic_records is None
        else diagnostic_records
    )
    record_count = 0
    with open(output_filename, "w", encoding="utf-8") as output:
        output.write(
            "# STM32 CAN logger diagnostics\n"
            "# timestamp_us time_hh:mm:ss:ms:us type decoded_fields | raw_record\n"
        )
        for unused_timestamp, record in records:
            output.write(format_diagnostic_record(record))
            record_count += 1
        if record_count == 0:
            output.write("# No diagnostic records found (legacy log).\n")
    return output_filename, record_count


def log_datetime_from_filename(filename):
    """Extracts YYYYMMDDHHMM[SS] from a logger filename when available."""
    base_name = os.path.basename(filename).split(".")[0]
    for length, date_format in ((14, "%Y%m%d%H%M%S"), (12, "%Y%m%d%H%M")):
        candidate = base_name[:length]
        if len(candidate) == length and candidate.isdigit():
            try:
                return datetime.strptime(candidate, date_format)
            except ValueError:
                pass
    return None


def format_asc_datetime(value):
    """Formats an English CANoe ASC header date independent of OS locale."""
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    return (
        f"{weekdays[value.weekday()]} {months[value.month - 1]} "
        f"{value.day:02d} {value:%H:%M:%S}.{value.microsecond // 1000:03d} "
        f"{value.year:04d}"
    )


def iter_chronological_log_records(logs):
    """Streams records with a small reorder window for delayed CANEVENT lines."""
    ordered_logs = sorted(logs, key=lambda value: os.path.basename(value))
    dated_logs = [(filename, log_datetime_from_filename(filename)) for filename in ordered_logs]
    known_dates = [value for unused_filename, value in dated_logs if value is not None]
    base_date = min(known_dates).date() if known_dates else None
    pending = []
    max_timestamp = None
    last_emitted_timestamp = None
    day_offset = 0
    sequence = 0

    for filename, file_datetime in dated_logs:
        file_day_offset = None
        if base_date is not None and file_datetime is not None:
            file_day_offset = (file_datetime.date() - base_date).days * MICROSECONDS_PER_DAY

        with open(filename, "r", encoding="utf-8", errors="replace") as source:
            for raw_line in source:
                line = raw_line.strip()
                record = parse_frame_record(line)
                kind = "frame"
                if record is None:
                    record = parse_diagnostic_record(line)
                    kind = "diagnostic"
                if record is None:
                    continue

                if file_day_offset is not None:
                    absolute_timestamp = file_day_offset + record["timestamp"]
                    if max_timestamp is not None:
                        if absolute_timestamp > max_timestamp + (MICROSECONDS_PER_DAY // 2):
                            absolute_timestamp -= MICROSECONDS_PER_DAY
                        elif absolute_timestamp < max_timestamp - (MICROSECONDS_PER_DAY // 2):
                            absolute_timestamp += MICROSECONDS_PER_DAY
                else:
                    absolute_timestamp = day_offset + record["timestamp"]
                    if (
                        max_timestamp is not None
                        and absolute_timestamp < max_timestamp - (MICROSECONDS_PER_DAY // 2)
                    ):
                        day_offset += MICROSECONDS_PER_DAY
                        absolute_timestamp += MICROSECONDS_PER_DAY

                sequence += 1
                heapq.heappush(
                    pending,
                    (absolute_timestamp, sequence, kind, record),
                )
                if max_timestamp is None or absolute_timestamp > max_timestamp:
                    max_timestamp = absolute_timestamp

                cutoff = max_timestamp - ASC_REORDER_WINDOW_US
                while pending and pending[0][0] <= cutoff:
                    emitted = heapq.heappop(pending)
                    if (
                        last_emitted_timestamp is not None
                        and emitted[0] < last_emitted_timestamp
                    ):
                        emitted = (last_emitted_timestamp,) + emitted[1:]
                    last_emitted_timestamp = emitted[0]
                    yield emitted

    while pending:
        emitted = heapq.heappop(pending)
        if (
            last_emitted_timestamp is not None
            and emitted[0] < last_emitted_timestamp
        ):
            emitted = (last_emitted_timestamp,) + emitted[1:]
        last_emitted_timestamp = emitted[0]
        yield emitted


def asc_output_filename(logs, exitdir):
    base_name = os.path.basename(logs[0]).split(".")[0]
    return os.path.join(exitdir, f"{base_name}_CANoe.asc")


def export_to_canoe_asc(logs, exitdir):
    """Exports frames and logger events to a CANoe-compatible ASCII trace."""
    output_filename = asc_output_filename(logs, exitdir)
    header_datetime = log_datetime_from_filename(sorted(logs)[0]) or datetime.now()
    first_timestamp = None
    frame_count = 0
    error_frame_count = 0
    diagnostic_count = 0

    with open(output_filename, "w", encoding="ascii", errors="replace", newline="\n") as output:
        asc_datetime = format_asc_datetime(header_datetime)
        output.write(f"date {asc_datetime}\n")
        output.write("base hex  timestamps absolute\n")
        output.write("internal events logged\n")
        output.write(f"Begin Triggerblock {asc_datetime}\n")

        for absolute_timestamp, unused_sequence, kind, record in iter_chronological_log_records(logs):
            if first_timestamp is None:
                first_timestamp = absolute_timestamp
                output.write(" 0.000000 Start of measurement\n")
            relative_seconds = (absolute_timestamp - first_timestamp) / 1000000.0

            if kind == "frame":
                data_text = " ".join(f"{byte:02X}" for byte in record["data"])
                identifier_text = f"{record['frame_id']:X}"
                message = (
                    f"{record['bus']}  {identifier_text}"
                    f"{' ' * max(1, 16 - len(identifier_text))}"
                    f"Rx   d {record['dlc']:x}"
                )
                if data_text:
                    message += f" {data_text}"
                output.write(f"{relative_seconds: 9.6f} {message}\n")
                frame_count += 1
                continue

            diagnostic_count += 1
            fields = record["fields"]
            if record["type"] == "CANEVENT":
                try:
                    flags = int(fields.get("flags", "0"), 0)
                    bus = int(fields.get("bus", "1"), 10)
                except ValueError:
                    flags = 0
                    bus = 1
                if flags & 0x01:
                    output.write(f"{relative_seconds: 9.6f} {bus}  ErrorFrame\n")
                    error_frame_count += 1

            detail = format_diagnostic_record(record).strip()
            output.write(f"{relative_seconds: 9.6f} LoggerEvent {detail}\n")

        if first_timestamp is None:
            output.write(" 0.000000 Start of measurement\n")
        output.write("End TriggerBlock\n")

    return output_filename, frame_count, error_frame_count, diagnostic_count


def extract_data_from_log(filename, db_list):
    """
    Parses a single log file, decodes CAN messages, and returns data buffers.

    Args:
        filename (str): Path to the log file.
        db_list (list): List of loaded cantools database objects.

    Returns:
        tuple: (base_filename, data_buffers, set_of_unknown_ids)
    """
    buffers = defaultdict(list)
    base_name = os.path.basename(filename).split(".")[0]
    unknown_ids = set()
    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = parse_frame_record(line)
                    if frame is None:
                        continue

                    ts_str = str(frame["timestamp"])
                    frame_id = frame["frame_id"]
                    can_ch = frame["bus"]

                    if not (1 <= can_ch <= len(db_list)):
                        continue

                    data_bytes = frame["data"]

                    try:
                        decoded_msg = db_list[can_ch - 1].decode_message(
                            frame_id, data_bytes, allow_truncated=True
                        )
                    except (KeyError, ValueError):
                        unknown_ids.add(frame_id)
                        continue

                    time_hms = format_time_hms(frame["timestamp"])

                    corruption_warning = " corruption_warning" if "\0" in line else ""

                    for signal_name, value in decoded_msg.items():
                        name_can = f"{signal_name}_{can_ch}"
                        value_str = (
                            format(value, ".10g")
                            if isinstance(value, float)
                            else str(value)
                        )
                        line_to_write = f"{ts_str} {time_hms} {value_str}{corruption_warning}\n"
                        buffers[name_can].append(line_to_write)
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(
            f"Unexpected error processing file {filename}: {type(e).__name__} - {e}"
        )
    return base_name, buffers, unknown_ids


def sort_and_write_worker(args):
    """A worker process for sorting and writing data for a single signal ('speed' mode)."""
    signal_key, log_sources, exitdir, filename_style = args
    all_lines = [
        line
        for basename in sorted(log_sources.keys(), key=int)
        for line in log_sources[basename]
    ]
    all_lines.sort(key=lambda line: int(line.split(" ", 1)[0]))

    if filename_style == 0:
        first_log_basename = sorted(log_sources.keys(), key=int)[0]
        output_filename = os.path.join(
            exitdir, f"{first_log_basename}_{signal_key}.txt"
        )
    else:
        output_filename = os.path.join(exitdir, f"{signal_key}.txt")

    try:
        with open(output_filename, "w", encoding="utf-8") as outfile:
            outfile.writelines(all_lines)
    except Exception as e:
        return f"Error writing file {output_filename}: {e}"
    return None


def merge_sorted_files_worker(args):
    """A worker process for merging sorted temporary files for a signal ('safe' mode)."""
    signal_key, temp_files, exitdir, filename_style, first_occurrence = args
    if filename_style == 0:
        first_log_basename = first_occurrence.get(signal_key, "00000")
        final_filename = f"{first_log_basename}_{signal_key}.txt"
    else:
        final_filename = f"{signal_key}.txt"

    final_filepath = os.path.join(exitdir, final_filename)
    open_files = [open(f, "r", encoding="utf-8") for f in temp_files]
    try:
        with open(final_filepath, "w", encoding="utf-8") as outfile:
            merged_lines = heapq.merge(
                *open_files, key=lambda line: int(line.split(" ", 1)[0])
            )
            outfile.writelines(merged_lines)
    finally:
        for f in open_files:
            f.close()


def chunkify(lst, n):
    """Splits a list into smaller chunks."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def update_pbar_postfix(pbar, process, last_update_time):
    """Updates the tqdm progress bar postfix with CPU and RAM usage."""
    current_time = time.time()
    if process and (current_time - last_update_time > 1):
        cpu = psutil.cpu_percent()
        mem_bytes = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                mem_bytes += child.memory_info().rss
            except psutil.NoSuchProcess:
                continue
        pbar.set_postfix_str(f"CPU: {cpu:5.1f}% | RAM: {format_bytes(mem_bytes)}")
        return current_time
    return last_update_time


def process_in_speed_mode(logs, db, config, cpu_usage, parent_process):
    """Processes logs in 'speed' mode (fast, high RAM usage)."""
    print(
        f"{bcolors.WARNING}Running in Speed Mode (fastest, uses more memory).{bcolors.ENDC}"
    )
    master_unknown_ids = set()

    # Step 1: Parse files and collect data in memory
    print(f"{bcolors.OKGREEN}Step 1/2: Parsing log files...{bcolors.ENDC}")
    final_data = defaultdict(lambda: defaultdict(list))
    with tqdm(total=len(logs), desc="Parsing files") as pbar:
        last_update_time = 0
        with Pool(processes=cpu_usage) as pool:
            process_func = partial(extract_data_from_log, db_list=db)
            for base_name, buffers, worker_ids in pool.imap_unordered(
                process_func, logs
            ):
                for name_can, lines in buffers.items():
                    final_data[name_can][base_name].extend(lines)
                master_unknown_ids.update(worker_ids)
                pbar.update(1)
                last_update_time = update_pbar_postfix(
                    pbar, parent_process, last_update_time
                )

    # Step 2: Sort and write data to files
    print(
        f"{bcolors.OKGREEN}Step 2/2: Processing and writing signal files...{bcolors.ENDC}"
    )
    tasks = [
        (signal_key, log_sources, config["exitdir"], config["filename_style"])
        for signal_key, log_sources in final_data.items()
    ]
    with tqdm(total=len(tasks), desc="Processing signals") as pbar:
        last_update_time = 0
        with Pool(processes=cpu_usage) as pool:
            for res in pool.imap_unordered(sort_and_write_worker, tasks):
                if res:
                    print(f"{bcolors.FAIL}{res}{bcolors.ENDC}")
                pbar.update(1)
                last_update_time = update_pbar_postfix(
                    pbar, parent_process, last_update_time
                )

    return master_unknown_ids


def process_in_safe_mode(logs, db, config, cpu_usage, parent_process):
    """Processes logs in 'safe' mode (slower, memory-efficient)."""
    from concurrent.futures import ThreadPoolExecutor

    chunk_size = 32  # A fixed, optimized chunk size
    print(
        f"{bcolors.OKGREEN}Running in Safe Mode (slower, memory-efficient).{bcolors.ENDC}"
    )
    master_unknown_ids = set()

    # Prepare temporary directory
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
    create_dir(TEMP_DIR)

    log_chunks = list(chunkify(logs, chunk_size))
    first_occurrence = {}
    temp_files_map = defaultdict(list)

    # Step 1: Parse files in chunks and write to temporary files
    print(
        f"{bcolors.OKGREEN}Step 1/3: Parsing files into temporary chunks...{bcolors.ENDC}"
    )
    with tqdm(total=len(logs), desc="Parsing files") as pbar:
        last_update_time = 0
        with Pool(processes=cpu_usage) as pool:
            process_func = partial(extract_data_from_log, db_list=db)
            for chunk in log_chunks:
                for base_name, buffers, worker_ids in pool.imap_unordered(
                    process_func, chunk
                ):
                    master_unknown_ids.update(worker_ids)
                    for name_can, lines in buffers.items():
                        if name_can not in first_occurrence:
                            first_occurrence[name_can] = base_name
                        lines.sort(key=lambda line: int(line.split(" ", 1)[0]))
                        temp_filepath = os.path.join(
                            TEMP_DIR, f"{name_can}_{base_name}.part"
                        )
                        with open(
                            temp_filepath, "w", encoding="utf-8"
                        ) as f:
                            f.writelines(lines)
                        temp_files_map[name_can].append(temp_filepath)
                    pbar.update(1)
                    last_update_time = update_pbar_postfix(
                        pbar, parent_process, last_update_time
                    )

    # Step 2: Merge the sorted temporary files
    print(f"{bcolors.OKGREEN}Step 2/3: Merging temporary files...{bcolors.ENDC}")
    merge_tasks = [
        (
            signal_key,
            temp_files,
            config["exitdir"],
            config["filename_style"],
            first_occurrence,
        )
        for signal_key, temp_files in temp_files_map.items()
    ]
    with ThreadPoolExecutor(max_workers=cpu_usage) as executor:
        list(
            tqdm(
                executor.map(merge_sorted_files_worker, merge_tasks),
                total=len(merge_tasks),
                desc="Merging signals",
            )
        )

    # Step 3: Clean up
    print(f"{bcolors.OKGREEN}Step 3/3: Cleaning up...{bcolors.ENDC}")
    shutil.rmtree(TEMP_DIR, ignore_errors=True)

    return master_unknown_ids


def main():
    """The main execution function of the script."""
    start_time = time.time()

    # Load configuration
    arguments = parse_arguments()
    config = load_config()
    output_format = select_output_format(
        config["output_format"], arguments.output_format
    )
    print(f"{bcolors.OKGREEN}Selected output: {output_format}.{bcolors.ENDC}")
    cpu_cores_config = config["cpu_cores"]
    cpu_usage = (
        max(1, mp.cpu_count() - 1)
        if cpu_cores_config == 0
        else cpu_cores_config
    )
    print(
        f"{bcolors.OKGREEN}Found {mp.cpu_count()} CPUs. Using {cpu_usage} for processing.{bcolors.ENDC}"
    )

    # Find log files
    logs = arguments.logs or get_log_files_from_folder()
    if not logs:
        logs = get_log_files_from_dialog()
        if not logs:
            print(f"{bcolors.FAIL}No log files selected. Exiting.{bcolors.ENDC}")
            sys.exit()
    logs = sorted(logs, key=lambda value: os.path.basename(value))

    # Create output directory
    log_prefixes = [
        int(os.path.basename(p).split(".")[0])
        for p in logs
        if os.path.basename(p).split(".")[0].isdigit()
    ]
    exitdir = str(min(log_prefixes)) if log_prefixes else "output"
    create_dir(exitdir)
    config["exitdir"] = exitdir  # Add to config for easy access in workers

    master_unknown_ids = set()
    if output_format in ("txt", "both"):
        diagnostic_records = collect_diagnostic_records(logs)
        diagnostics_file, diagnostics_count = write_diagnostics_report(
            logs,
            exitdir,
            config["filename_style"],
            diagnostic_records,
        )
        report_file, can_event_count = write_human_diagnostics_report(
            logs,
            exitdir,
            config["filename_style"],
            diagnostic_records,
        )
        print(
            f"{bcolors.OKGREEN}Diagnostics TXT: {diagnostics_file} "
            f"({diagnostics_count} records).{bcolors.ENDC}"
        )
        print(
            f"{bcolors.OKGREEN}Readable CAN report: {report_file} "
            f"({can_event_count} CAN events).{bcolors.ENDC}"
        )

        # DBC files are required only for decoded TXT signal output. Diagnostic
        # reports above intentionally remain available even if a DBC is absent.
        dbc_files = find_dbc_files()
        if not dbc_files:
            print(
                f"{bcolors.FAIL}No .dbc file(s) found. DBC files are required "
                f"for TXT signal decoding.{bcolors.ENDC}"
            )
            sys.exit()
        print(
            f"{bcolors.OKGREEN}Found DBC files: "
            f"{', '.join(dbc_files)}{bcolors.ENDC}"
        )
        db = [cantools.database.load_file(f) for f in dbc_files]

        parent_process = (
            psutil.Process(os.getpid()) if config["show_resources"] else None
        )
        if config["processing_mode"] == "speed":
            master_unknown_ids = process_in_speed_mode(
                logs, db, config, cpu_usage, parent_process
            )
        else:
            master_unknown_ids = process_in_safe_mode(
                logs, db, config, cpu_usage, parent_process
            )

    if output_format in ("asc", "both"):
        asc_file, frame_count, error_count, diagnostic_count = export_to_canoe_asc(
            logs, exitdir
        )
        print(
            f"{bcolors.OKGREEN}CANoe ASC: {asc_file} "
            f"({frame_count} frames, {error_count} ErrorFrame events, "
            f"{diagnostic_count} logger records).{bcolors.ENDC}"
        )

    # Summary and report of unknown IDs
    if master_unknown_ids:
        print(
            f"\n{bcolors.WARNING}-------------------- DBC VALIDATION WARNING --------------------{bcolors.ENDC}"
        )
        print(
            f"{bcolors.WARNING}Found {bcolors.BOLD}{len(master_unknown_ids)}{bcolors.ENDC}{bcolors.WARNING} message IDs in logs that are not defined in the DBC file(s).{bcolors.ENDC}"
        )
        print(
            f"{bcolors.OKCYAN}These messages were ignored. List of undefined IDs (hex):{bcolors.ENDC}"
        )
        hex_ids = sorted([hex(uid) for uid in master_unknown_ids])
        print(f"{bcolors.OKCYAN}{', '.join(hex_ids)}{bcolors.ENDC}")
        print(
            f"{bcolors.WARNING}----------------------------------------------------------------{bcolors.ENDC}"
        )

    print(f"\n{bcolors.BOLD}{bcolors.OKGREEN}Processing complete!{bcolors.ENDC}")
    print(f"Execution time: --- {time.time() - start_time:.2f} seconds ---")
    sys.exit()


if __name__ == "__main__":
    # Ensures compatibility with frozen applications (e.g., via PyInstaller) on Windows.
    mp.freeze_support()
    main()
