"""Honest ping data: read the endpoint the CLI actually connected to.

`adguardvpn-cli list-locations` only exposes AdGuard's *estimate* for every
location.  The real measurement for the active tunnel is written by the CLI
core to ``tunnel.log``::

    22.11.2026 00:24:34.822055 INFO  [112410] VPNCORE pinger_handler:
        [0] Using endpoint: name=cache-25.smart-pixl.net,
        address=201.8.95.149:443, relay=none, ping=231ms

This module only reads that log.  It never performs network requests, ICMP or
TCP probes, and it never invents a value: anything missing or malformed is
``None``.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import util

DEFAULT_LOG_PATH = util.CLI_DATA_DIR / "tunnel.log"
READ_TAIL_BYTES = 64 * 1024

# The line is logged by the CLI core as one record.  Keep the pattern tolerant
# to extra spaces and to missing optional pieces so that one odd line does not
# hide a valid one above it.
ENDPOINT_RE = re.compile(
    r"Using endpoint:\s*"
    r"name=(?P<name>[^,]+?)\s*,\s*"
    r"address=(?P<address>[^,]+?)\s*,\s*"
    r"relay=(?P<relay>[^,]+?)\s*,\s*"
    r"ping=(?P<ping>\d+)\s*ms"
)
TIMESTAMP_RE = re.compile(
    r"^(?P<ts>\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)


@dataclass(frozen=True)
class Endpoint:
    """The endpoint selected by the CLI for the current/last tunnel."""

    name: str
    address: str
    relay: str
    ping: int
    measured_at: datetime | None = None

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.name} ({self.address}) {self.ping} мс"


def default_log_path() -> Path:
    return Path(os.environ.get(
        "AGVPN_TUNNEL_LOG",
        str(DEFAULT_LOG_PATH),
    ))


def _parse_timestamp(text: str) -> datetime | None:
    text = (text or "").strip()
    for fmt in ("%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def parse_line(line: str) -> Endpoint | None:
    """Parse a single ``Using endpoint`` log line, or return ``None``."""
    if not line or "Using endpoint:" not in line:
        return None
    match = ENDPOINT_RE.search(line)
    if not match:
        return None
    try:
        ping = int(match.group("ping"))
    except (TypeError, ValueError):
        return None
    ts_match = TIMESTAMP_RE.match(line)
    measured_at = _parse_timestamp(ts_match.group("ts")) if ts_match else None
    return Endpoint(
        name=match.group("name").strip(),
        address=match.group("address").strip(),
        relay=match.group("relay").strip(),
        ping=ping,
        measured_at=measured_at,
    )


def _read_tail(path: Path, max_bytes: int = READ_TAIL_BYTES) -> str:
    """Read at most ``max_bytes`` from the end of the file.

    A partial first line is dropped only when the window really starts inside
    it; a window that starts on a line boundary keeps its first line, and a
    window without any newline is returned as-is.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - max_bytes)
        boundary = b"\n"
        if start > 0:
            handle.seek(start - 1)
            boundary = handle.read(1)
        handle.seek(start)
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    # Drop a partial first line only when the window really starts inside it.
    # If the previous byte is a newline, start is already on a line boundary.
    # If the window has no newline at all, keep the text as-is.
    if start > 0 and boundary != b"\n":
        newline = text.find("\n")
        if newline >= 0:
            text = text[newline + 1:]
    return text


def read_last_endpoint(path: str | Path | None = None) -> Endpoint | None:
    """Return the last endpoint recorded in ``tunnel.log``.

    Missing file, permission errors, empty files and malformed lines all
    resolve to ``None`` — this helper never raises.
    """
    target = Path(path) if path is not None else default_log_path()
    try:
        text = _read_tail(target)
    except (OSError, ValueError):
        return None
    for line in reversed(text.splitlines()):
        endpoint = parse_line(line)
        if endpoint is not None:
            return endpoint
    return None


class EndpointCache:
    """Cache ``read_last_endpoint`` keyed by ``(mtime, size)``.

    The UI polls often; re-reading the tail only when the log actually changed
    keeps that cheap.  ``invalidate()`` forces a fresh read on the next call,
    e.g. right after a connect/disconnect.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._key: tuple[int, int] | None = None
        self._value: Endpoint | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_log_path()

    def invalidate(self) -> None:
        with self._lock:
            self._key = None
            self._value = None

    def get(self) -> Endpoint | None:
        target = self.path
        try:
            stat = target.stat()
        except (OSError, ValueError):
            with self._lock:
                self._key = None
                self._value = None
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if key == self._key:
                return self._value
        value = read_last_endpoint(target)
        with self._lock:
            self._key = key
            self._value = value
        return value
