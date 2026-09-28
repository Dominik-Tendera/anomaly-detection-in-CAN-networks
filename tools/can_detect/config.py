#!/usr/bin/env python3
"""Session configuration of the anomaly detector: loading, validation, copying.

A session is reproducible only if the settings it ran with are recoverable from
the files it left behind. This module is the single place where those settings
enter the program, and it enforces three rules that the rest of the package
relies on.

No silent defaults. A parameter absent from the file is an error naming that
parameter, and so is a value outside its allowed range; the session does not
start (criterion 11.2). This matters more than convenience: a detector that
quietly substitutes its own window length produces numbers that cannot be tied
to any recorded configuration, and a thesis table built from such numbers is not
defensible.

Recommended values are not defaults. `session.example.conf` carries the values
this plan starts from, 1 s for the analysis window and 3 for the standard
deviation multiplier, and those values reach the program only because they are
written in a file that somebody chose to use. Copying the example and editing it
is the intended way to start a session. Nothing in this module falls back to
them, and no constant here may ever be used as a fallback, because the moment a
fallback exists the guarantee above stops holding.

The copy kept with the results is byte for byte the file that was read
(criterion 11.3). The parsed values are reported next to it, but the copy itself
is never re-serialised: a round trip through a parser would silently drop
comments, which is where the reasoning behind a chosen threshold usually lives.
Checksums of the DBC files and of the baseline profile are recorded in the same
manifest, because criteria 3.7 and 9.5 compare them later to decide whether two
sessions are comparable at all.

File format, deliberately plain text so that it diffs and reviews well:

    # full line comments start with a hash in the first non blank column
    dbc_path = ../../generator/dbc/RTE_3.5_CAN1_CAR.dbc
    baseline_path = baseline/reference.profile
    window_s = 1.0

`dbc_path` may appear more than once and the order is kept; every other
parameter appears exactly once. Relative paths resolve against the directory of
the configuration file, so a configuration committed next to its inputs stays
valid after the repository is cloned somewhere else. A hash is a comment only at
the start of a line, never inside a value, because Windows paths and DBC names
are allowed to contain one.

Reading the file does not touch the paths it names. Existence and checksums of
the DBC files and of the baseline profile are resolved by `checksums()`, called
once when a session starts. Separating the two keeps an example configuration
verifiable before the profile it points at has been computed.

Usage:
    python3 -m can_detect.config tools/can_detect/session.example.conf
    python3 -m can_detect.config my_session.conf --checksums
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MODULE_DIR = Path(__file__).resolve().parent
EXAMPLE_CONFIG_PATH = MODULE_DIR / "session.example.conf"

_HASH_NAME = "sha256"
_HASH_CHUNK = 1 << 16


class ConfigError(ValueError):
    """Configuration cannot be used as given. The message names the parameter."""


@dataclass(frozen=True)
class _Scalar:
    """One numeric parameter: how to parse it, and the range it must fall in."""

    name: str
    kind: str  # "float" or "int"
    low: float
    high: float
    unit: str
    why: str  # reason for the bounds, quoted in the error message


# Bounds are sanity limits, not tuning. They exist to catch a typo such as a
# window of 1000 s or a seed of minus one before a session runs for a minute and
# produces a table nobody can interpret. The values inside the bounds are chosen
# per session and justified in the work log, see task 13.4.
_SCALARS: tuple[_Scalar, ...] = (
    _Scalar("window_s", "float", 0.001, 60.0, "s",
            "an analysis window shorter than a millisecond holds no frames at "
            "CAN periods of 10 to 100 ms, and one longer than a minute exceeds "
            "the length of a reference run"),
    _Scalar("sigma_multiplier", "float", 0.5, 10.0, "standard deviations",
            "below 0.5 the interval is narrower than the observed spread and "
            "every window alarms, above 10 no realistic deviation is reported"),
    _Scalar("missing_frame_multiplier", "float", 1.5, 100.0, "periods",
            "a multiplier at or below 1 would turn ordinary jitter into a "
            "missing frame alarm"),
    _Scalar("min_interval_observations", "int", 2, 1_000_000, "observations",
            "a standard deviation of the interframe interval needs at least "
            "two observations; the plan starts from 100"),
    _Scalar("burst_min_frames", "int", 2, 10_000, "frames",
            "a burst is a run of frames, so at least two, and a threshold "
            "above ten thousand cannot be reached inside one window"),
    _Scalar("burst_interval_factor", "float", 0.000001, 0.999999, "factor",
            "a short-burst interval must be strictly shorter than the "
            "expected period; one would classify ordinary traffic as a burst"),
    _Scalar("match_tolerance_s", "float", 0.0, 60.0, "s",
            "zero means an alarm has to fall inside the episode itself; a "
            "tolerance comparable to a whole reference run would match "
            "everything to everything"),
    _Scalar("random_seed", "int", 0, 2 ** 32 - 1, "",
            "the seed is written to the session report and has to survive a "
            "round trip through it, so it stays an unsigned 32 bit value"),
)

_SCALARS_BY_NAME = {scalar.name: scalar for scalar in _SCALARS}

# Repeatable, order preserving: several DBC files describe one bus.
_REPEATABLE = ("dbc_path",)
# Single valued path parameters.
_PATHS = ("baseline_path",)

KNOWN_PARAMETERS: tuple[str, ...] = (
    _REPEATABLE + _PATHS + tuple(scalar.name for scalar in _SCALARS))


@dataclass(frozen=True)
class FileDigest:
    """Identity of one input file, as recorded with the session results."""

    parameter: str
    path: Path
    algorithm: str
    digest: str
    size_bytes: int

    def as_dict(self) -> dict:
        return {
            "parameter": self.parameter,
            "path": str(self.path),
            "algorithm": self.algorithm,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class SessionConfig:
    """Validated session settings, plus the bytes they were read from.

    `raw_bytes` is the file exactly as found on disk. It is what `write_copy`
    stores next to the results, so the copy carries the comments as well as the
    values.
    """

    source_path: Path
    raw_bytes: bytes
    dbc_paths: tuple[Path, ...]
    baseline_path: Path
    window_s: float
    sigma_multiplier: float
    missing_frame_multiplier: float
    min_interval_observations: int
    burst_min_frames: int
    burst_interval_factor: float
    match_tolerance_s: float
    random_seed: int

    @property
    def raw_text(self) -> str:
        return self.raw_bytes.decode("utf-8")

    def values(self) -> dict:
        """Scalar parameters with their units, for the session report."""
        return {scalar.name: getattr(self, scalar.name) for scalar in _SCALARS}

    def units(self) -> dict:
        return {scalar.name: scalar.unit for scalar in _SCALARS}

    def input_files(self) -> tuple[tuple[str, Path], ...]:
        """Every file the session reads besides the trace, in report order."""
        pairs = [("dbc_path", path) for path in self.dbc_paths]
        pairs.append(("baseline_path", self.baseline_path))
        return tuple(pairs)

    def checksums(self) -> tuple[FileDigest, ...]:
        """Digests of the configuration itself and of every input file.

        Raises ConfigError naming the parameter when a file is missing or
        unreadable, so that a session stops before it produces results that
        cannot be tied to known inputs.
        """
        digests = [FileDigest("config", self.source_path, _HASH_NAME,
                              _digest_bytes(self.raw_bytes),
                              len(self.raw_bytes))]
        for parameter, path in self.input_files():
            digests.append(_digest_file(parameter, path))
        return tuple(digests)

    def manifest(self, include_checksums: bool = True) -> dict:
        """Everything about the configuration that goes into the session report.

        Covers criterion 11.3 for the values and the digests, and criterion 9.4
        for the seed, which is reported explicitly rather than left for a reader
        to find among the other parameters.
        """
        manifest: dict = {
            "source_path": str(self.source_path),
            "values": self.values(),
            "units": self.units(),
            "dbc_paths": [str(path) for path in self.dbc_paths],
            "baseline_path": str(self.baseline_path),
            "random_seed": self.random_seed,
        }
        if include_checksums:
            manifest["checksums"] = [digest.as_dict()
                                     for digest in self.checksums()]
        return manifest

    def write_copy(self, destination: Path) -> Path:
        """Store the configuration verbatim at `destination`.

        The file name is not decided here. Artefact names of a session come from
        can_detect/session.py, artefact `config`, so that one naming rule covers
        the trace, the events, the report and this copy:

            paths = session.open_session(output_dir, session_id)
            cfg.write_copy(paths.path("config"))

        Exclusive creation: an existing file is left untouched and the collision
        is reported, because overwriting it would destroy the only record of how
        an earlier session was run.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as handle:
                handle.write(self.raw_bytes)
        except FileExistsError as exc:
            raise ConfigError(
                f"configuration copy already exists: {destination}; "
                "an earlier session used this name and its copy is left "
                "untouched"
            ) from exc
        return destination


def load_config(path: Path | str) -> SessionConfig:
    """Read and validate a session configuration file.

    Every parameter has to be present and inside its range; nothing is filled in
    from a default (criterion 11.2). The paths named in the file are not opened
    here, see `SessionConfig.checksums`.
    """
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"configuration file cannot be read: {source} ({exc})") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"configuration file is not valid UTF-8: {source} ({exc})") from exc

    entries = _parse(text, source)
    _reject_unknown(entries, source)

    base = source.parent
    dbc_values = entries.get("dbc_path", [])
    if not dbc_values:
        raise ConfigError(_missing("dbc_path", source))
    dbc_paths = tuple(_resolve(base, "dbc_path", line, value)
                      for line, value in dbc_values)

    baseline_line, baseline_value = _single("baseline_path", entries, source)
    baseline_path = _resolve(base, "baseline_path", baseline_line,
                             baseline_value)

    scalars = {}
    for scalar in _SCALARS:
        line, value = _single(scalar.name, entries, source)
        scalars[scalar.name] = _scalar_value(scalar, line, value)

    return SessionConfig(
        source_path=source,
        raw_bytes=raw,
        dbc_paths=dbc_paths,
        baseline_path=baseline_path,
        **scalars,
    )


def _parse(text: str, source: Path) -> dict[str, list[tuple[int, str]]]:
    """Split the file into parameter occurrences, keeping line numbers."""
    entries: dict[str, list[tuple[int, str]]] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(
                f"{source}:{number}: line is neither a comment nor a "
                f"parameter assignment: {raw_line!r}")
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            raise ConfigError(f"{source}:{number}: parameter name is empty")
        if not value:
            raise ConfigError(
                f"parameter '{name}' has an empty value at {source}:{number}")
        entries.setdefault(name, []).append((number, value))
    return entries


def _reject_unknown(entries: dict[str, list[tuple[int, str]]],
                    source: Path) -> None:
    """A misspelled parameter is an error, not a parameter that is absent.

    Without this check a typo would be reported as the correct parameter
    missing, which sends the reader looking for the wrong problem, and any
    parameter dropped from a later version of the format would pass unnoticed.
    """
    for name, occurrences in entries.items():
        if name not in KNOWN_PARAMETERS:
            line = occurrences[0][0]
            known = ", ".join(sorted(KNOWN_PARAMETERS))
            raise ConfigError(
                f"unknown parameter '{name}' at {source}:{line}; "
                f"known parameters: {known}")


def _single(name: str, entries: dict[str, list[tuple[int, str]]],
            source: Path) -> tuple[int, str]:
    occurrences = entries.get(name, [])
    if not occurrences:
        raise ConfigError(_missing(name, source))
    if len(occurrences) > 1:
        lines = ", ".join(str(line) for line, _ in occurrences)
        raise ConfigError(
            f"parameter '{name}' is given more than once at {source}, "
            f"lines {lines}; only {', '.join(_REPEATABLE)} may repeat")
    return occurrences[0]


def _missing(name: str, source: Path) -> str:
    hint = ""
    scalar = _SCALARS_BY_NAME.get(name)
    if scalar is not None:
        hint = (f"; allowed range {_format(scalar, scalar.low)} to "
                f"{_format(scalar, scalar.high)}"
                f"{' ' + scalar.unit if scalar.unit else ''}")
    return (f"parameter '{name}' is missing from {source}; no default value is "
            f"applied{hint}")


def _resolve(base: Path, name: str, line: int, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve()
    except OSError as exc:
        raise ConfigError(
            f"parameter '{name}' at line {line} is not a usable path: "
            f"{value!r} ({exc})") from exc


def _scalar_value(scalar: _Scalar, line: int, value: str) -> float | int:
    try:
        parsed = int(value) if scalar.kind == "int" else float(value)
    except ValueError as exc:
        expected = "an integer" if scalar.kind == "int" else "a number"
        raise ConfigError(
            f"parameter '{scalar.name}' at line {line} is not {expected}: "
            f"{value!r}") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ConfigError(
            f"parameter '{scalar.name}' at line {line} is not a finite "
            f"number: {value!r}")
    if not scalar.low <= parsed <= scalar.high:
        unit = f" {scalar.unit}" if scalar.unit else ""
        raise ConfigError(
            f"parameter '{scalar.name}' is out of range: {value}{unit}, "
            f"allowed {_format(scalar, scalar.low)} to "
            f"{_format(scalar, scalar.high)}{unit}; {scalar.why}")
    return parsed


def _format(scalar: _Scalar, bound: float) -> str:
    return str(int(bound)) if scalar.kind == "int" else str(bound)


def _digest_bytes(payload: bytes) -> str:
    return hashlib.new(_HASH_NAME, payload).hexdigest()


def _digest_file(parameter: str, path: Path) -> FileDigest:
    digest = hashlib.new(_HASH_NAME)
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ConfigError(
            f"file named by parameter '{parameter}' cannot be read: {path} "
            f"({exc})") from exc
    return FileDigest(parameter, path, _HASH_NAME, digest.hexdigest(), size)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a session configuration file and print it as the "
                    "session report will record it.")
    parser.add_argument("config", type=Path, help="configuration file to read")
    parser.add_argument("--checksums", action="store_true",
                        help="also read the DBC files and the baseline profile "
                             "and report their digests")
    args = parser.parse_args(argv)

    try:
        session = load_config(args.config)
        manifest = session.manifest(include_checksums=args.checksums)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
