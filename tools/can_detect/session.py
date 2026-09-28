"""Session identifiers and artefact names for an analysis run.

Every run of the analysis writes several files that belong together: the trace,
the ground truth of the injected episodes, the anomaly events, the session
report and the verbatim copy of the configuration. They are tied to each other
by one string, the session identifier, built from the date and the time at which
the run started.

That identifier comes from the clock of the analysing machine and its only job
is to name files. It is deliberately not the time base of anything else. The
detection rules take their time from the records of the device, because the
statistics records arrive every 100 ms and carry the device clock forward even
when no frame is received; a rule fed from the host clock would give one answer
in the live mode and another in the replay mode. To keep the two apart the
function below says wall clock in its name and the formatted value never enters
a record, a feature vector or an event field.

The format matches the one rpi_receiver/can_stream_rx.py already uses for its
--session default, so a trace captured by the receiver and the artefacts written
next to it by the analysis share one identifier without translation.

An identifier that is already taken is an error, never an overwrite. A second
run started within the same second, or a rerun with an identifier passed by
hand, would otherwise silently replace the trace the earlier results were
computed from, and the session report of the earlier run would point at data
that no longer exists.

Usage:
    from can_detect.session import wall_clock_session_id, open_session

    session_id = wall_clock_session_id(datetime.now())
    paths = open_session(Path("captures"), session_id)
    paths.path("events").write_text(...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

# Date and time of the start of the run, local time, seconds resolution. Same
# format as the --session default of rpi_receiver/can_stream_rx.py.
SESSION_ID_FORMAT = "%Y%m%d_%H%M%S"

# Collision detection compares the identifier against file names, so the
# identifier has to be usable as part of one: no directory separators, no shell
# or glob metacharacters, nothing that a file system would rewrite.
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

# The artefacts of one session. The trace and the text log keep the names the
# receiver already writes, so a capture taken by rpi_receiver/can_stream_rx.py
# is reached through the same mapping.
ARTEFACT_SUFFIXES: Mapping[str, str] = MappingProxyType({
    "trace": ".canbin",
    "text_log": ".log",
    "ground_truth": ".truth.json",
    "events": ".events.jsonl",
    "report": ".report.json",
    "config": ".config.txt",
})


class SessionIdError(ValueError):
    """An identifier that cannot be used in a file name."""


class SessionIdCollision(FileExistsError):
    """Files carrying the identifier already exist, so the run must not start.

    The existing files are listed rather than counted, because the point of the
    error is to let the operator see what would have been overwritten.
    """

    def __init__(self, session_id: str, existing: list[Path]) -> None:
        self.session_id = session_id
        self.existing = list(existing)
        shown = ", ".join(path.name for path in self.existing[:5])
        if len(self.existing) > 5:
            shown += f", and {len(self.existing) - 5} more"
        super().__init__(
            f"session identifier {session_id!r} is already in use by: {shown}; "
            f"existing files were left untouched")


def wall_clock_session_id(started: datetime) -> str:
    """Format the start time of the run as a session identifier.

    The argument is the reading of the clock of the analysing machine, passed in
    rather than read inside, so that a caller can name a session after a capture
    it is reanalysing and so that the formatting is testable. This value names
    files; it is not a time base for any detection rule, where time comes from
    the device records alone.
    """
    if not isinstance(started, datetime):
        raise TypeError("started has to be a datetime")
    return started.strftime(SESSION_ID_FORMAT)


def validate_session_id(session_id: str) -> str:
    """Return the identifier, or raise if it cannot appear in a file name."""
    if not isinstance(session_id, str):
        raise TypeError("session_id has to be a string")
    if not SESSION_ID_PATTERN.match(session_id):
        raise SessionIdError(
            f"session identifier {session_id!r} is not usable in a file name: "
            f"allowed are letters, digits, underscore, dot and hyphen, "
            f"starting with a letter or a digit")
    return session_id


def artefact_name(session_id: str, kind: str) -> str:
    """Name of one artefact of the session, identifier first."""
    validate_session_id(session_id)
    try:
        suffix = ARTEFACT_SUFFIXES[kind]
    except KeyError:
        known = ", ".join(sorted(ARTEFACT_SUFFIXES))
        raise KeyError(f"unknown artefact {kind!r}, known are: {known}") from None
    return f"{session_id}{suffix}"


def colliding_paths(output_dir: Path, session_id: str) -> list[Path]:
    """Existing entries of the directory whose name carries the identifier.

    The whole name is searched, not only the artefact suffixes of this module,
    because criterion 11.5 asks about any file named after the identifier. A
    ground truth file written by the generator, or an export added later by
    hand, counts as a collision just as much as a trace does.
    """
    validate_session_id(session_id)
    directory = Path(output_dir)
    if not directory.is_dir():
        return []
    return sorted(entry for entry in directory.iterdir()
                  if session_id in entry.name)


@dataclass(frozen=True)
class SessionPaths:
    """Where the artefacts of one session go."""

    session_id: str
    output_dir: Path

    def path(self, kind: str) -> Path:
        return self.output_dir / artefact_name(self.session_id, kind)

    def all_paths(self) -> dict[str, Path]:
        return {kind: self.path(kind) for kind in ARTEFACT_SUFFIXES}


def open_session(output_dir: Path, session_id: str,
                 create_dir: bool = True) -> SessionPaths:
    """Claim an identifier in a directory and return the artefact paths.

    Raises SessionIdCollision when anything in the directory already carries the
    identifier. Nothing is written or removed in that case: the earlier results
    stay readable and the operator decides whether to keep them or to pass a
    different identifier.
    """
    validate_session_id(session_id)
    directory = Path(output_dir)
    existing = colliding_paths(directory, session_id)
    if existing:
        raise SessionIdCollision(session_id, existing)
    if create_dir:
        directory.mkdir(parents=True, exist_ok=True)
    return SessionPaths(session_id=session_id, output_dir=directory)
