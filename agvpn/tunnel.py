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
# Kept for backwards compatibility: this is the window ``_read_tail`` uses and
# the size of one backwards-scanning chunk.
READ_TAIL_BYTES = 64 * 1024
# How far back we are willing to look for the last endpoint line.  tunnel.log
# can contain megabytes of per-second DNS noise after the connect record, so
# the legacy 64 KB window was far too small.  The limit keeps the scan bounded
# even when the log is huge.
MAX_SCAN_BYTES = 4 * 1024 * 1024
SCAN_CHUNK_BYTES = READ_TAIL_BYTES
# A few bytes from just before the cache's read offset.  They are compared on
# every append to notice copy-truncate rotations that already grew past the old
# offset (inode and size alone cannot detect those).
ANCHOR_BYTES = 64

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


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _iter_lines_backwards(handle, size: int,
                          max_bytes: int = MAX_SCAN_BYTES,
                          chunk_size: int = SCAN_CHUNK_BYTES):
    """Yield lines from ``size`` towards the start of the file, newest first.

    At most ``max_bytes`` are inspected.  Chunks are read backwards and lines
    that straddle a chunk boundary are reassembled before they are yielded, so
    a split record is never returned as two malformed halves.  The oldest line
    is dropped when the limit falls inside it; a file shorter than the limit
    keeps its first line, and a window without any newline is yielded as-is
    (the same tolerant behaviour as ``_read_tail``).
    """
    limit = max(0, size - max_bytes)
    pos = size
    buffer = b""
    while pos > limit:
        start = max(limit, pos - chunk_size)
        handle.seek(start)
        chunk = handle.read(pos - start)
        if not chunk:
            break
        pos = start
        buffer = chunk + buffer
        while True:
            index = buffer.rfind(b"\n")
            if index < 0:
                break
            line = buffer[index + 1:]
            buffer = buffer[:index]
            yield line
    if not buffer:
        return
    if pos == 0:
        yield buffer
        return
    # The scan limit may start exactly on a line boundary; in that case the
    # leftover is a complete first line and must be kept.
    try:
        handle.seek(pos - 1)
        boundary = handle.read(1)
    except (OSError, ValueError):
        boundary = b""
    if boundary == b"\n":
        yield buffer


def _last_endpoint_in(raw: bytes) -> Endpoint | None:
    """Return the last endpoint in a chunk of complete log lines."""
    if not raw or b"Using endpoint:" not in raw:
        return None
    for line in reversed(raw.splitlines()):
        endpoint = parse_line(_decode(line))
        if endpoint is not None:
            return endpoint
    return None


def read_last_endpoint(path: str | Path | None = None) -> Endpoint | None:
    """Return the last endpoint recorded in ``tunnel.log``.

    Scans backwards from the end in bounded chunks (up to ``MAX_SCAN_BYTES``)
    instead of reading only a fixed 64 KB tail.  Missing file, permission
    errors, empty files and malformed lines all resolve to ``None`` — this
    helper never raises.
    """
    target = Path(path) if path is not None else default_log_path()
    try:
        with open(target, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            for raw in _iter_lines_backwards(handle, size):
                endpoint = parse_line(_decode(raw))
                if endpoint is not None:
                    return endpoint
    except (OSError, ValueError):
        return None
    return None


class EndpointCache:
    """Incremental cache for the last endpoint in ``tunnel.log``.

    The UI polls this often while the CLI keeps appending DNS/status noise, so
    only bytes added since the previous call are read.  A full tail scan (up to
    ``MAX_SCAN_BYTES``) happens only on the first call, after truncation or
    rotation, and when the read offset is no longer trustworthy.  If new data
    contains no ``Using endpoint`` line, the previously cached endpoint is
    kept — except after :meth:`invalidate`, which drops it.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._inode: tuple[int, int] | None = None
        self._offset = 0
        self._mtime_ns: int | None = None
        self._anchor = b""
        self._value: Endpoint | None = None
        self._initialized = False
        self._force_rescan = False

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_log_path()

    def invalidate(self) -> None:
        """Drop the cached endpoint until a fresh record is parsed.

        Called when the tunnel connects to another location.  Returning the
        previous tunnel's endpoint would attribute its ping to the new
        location, so ``get()`` answers ``None`` until a new ``Using endpoint``
        line appears.  The read offset is deliberately kept: the fresh record
        is in the appended bytes, so the next call can still be cheap.
        """
        with self._lock:
            self._value = None
            self._force_rescan = False

    def _reset(self) -> None:
        self._inode = None
        self._offset = 0
        self._mtime_ns = None
        self._anchor = b""
        self._value = None
        self._initialized = False
        self._force_rescan = False

    def _read_anchor(self, target: Path, offset: int) -> bytes:
        if offset <= 0:
            return b""
        start = max(0, offset - ANCHOR_BYTES)
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                return handle.read(offset - start)
        except (OSError, ValueError):
            return b""

    def _anchor_matches(self, target: Path, offset: int) -> bool:
        if offset <= 0:
            return True
        start = max(0, offset - ANCHOR_BYTES)
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                current = handle.read(offset - start)
        except (OSError, ValueError):
            return False
        return current == self._anchor

    def _tail_boundary(self, target: Path, size: int) -> int:
        """Offset after the last newline, so a partial last line is re-read.

        When the file ends with a newline (the normal case) this is ``size``.
        A long tail without any newline is treated as a complete line, which
        matches the tolerant behaviour of ``read_last_endpoint``.
        """
        if size <= 0:
            return 0
        window = min(size, READ_TAIL_BYTES)
        try:
            with open(target, "rb") as handle:
                handle.seek(size - window)
                data = handle.read(window)
        except (OSError, ValueError):
            return size
        if not data or data.endswith(b"\n"):
            return size
        index = data.rfind(b"\n")
        if index < 0:
            return size
        return size - (len(data) - index - 1)

    def _read_new(self, target: Path, offset: int, size: int) -> bytes:
        with open(target, "rb") as handle:
            handle.seek(offset)
            return handle.read(max(0, size - offset))

    def _full_scan(self, target: Path, size: int,
                   inode: tuple[int, int], mtime_ns: int) -> Endpoint | None:
        value = read_last_endpoint(target)
        offset = self._tail_boundary(target, size)
        self._inode = inode
        self._offset = offset
        self._mtime_ns = mtime_ns
        self._anchor = self._read_anchor(target, offset)
        self._value = value
        self._initialized = True
        self._force_rescan = False
        return value

    def get(self) -> Endpoint | None:
        target = self.path
        try:
            stat = target.stat()
        except (OSError, ValueError):
            with self._lock:
                self._reset()
            return None

        with self._lock:
            inode = (stat.st_dev, stat.st_ino)
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns

            # Inode change: rotation.  Smaller size: truncation.  Same size but
            # a new mtime: the file was rewritten in place.
            rewritten = self._initialized and (
                inode != self._inode
                or size < self._offset
                or (size == self._offset and mtime_ns != self._mtime_ns)
            )
            if not self._initialized or rewritten or self._force_rescan:
                return self._full_scan(target, size, inode, mtime_ns)

            if size == self._offset:
                return self._value

            # The file grew.  If the bytes just before our offset changed, a
            # copy-truncate rotation has already grown past the old offset;
            # size/inode alone cannot see that, so scan afresh.
            if not self._anchor_matches(target, self._offset):
                return self._full_scan(target, size, inode, mtime_ns)

            data = self._read_new(target, self._offset, size)
            if not data:
                # Changed between stat() and read(): retry on the next poll
                # instead of trusting a possibly stale offset.
                self._force_rescan = True
                return self._value

            split = data.rfind(b"\n")
            if split < 0:
                complete, tail = b"", data
            else:
                complete, tail = data[:split + 1], data[split + 1:]

            endpoint = _last_endpoint_in(complete)
            if endpoint is None and tail:
                # A last line without a trailing newline may still be a
                # complete record.  Read it but keep it uncommitted so a later
                # append can finish it.
                endpoint = parse_line(_decode(tail))
            if endpoint is not None and endpoint != self._value:
                self._value = endpoint

            if complete:
                self._offset += len(complete)
                self._anchor = (self._anchor + complete)[-ANCHOR_BYTES:]
            self._mtime_ns = mtime_ns
            return self._value
