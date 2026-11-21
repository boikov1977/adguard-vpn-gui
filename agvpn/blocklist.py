"""UnRKN "sites that dislike VPN" list: download, normalize, cache (T-41).

The list contains domains that should bypass the tunnel in *general* exclusion
mode: the VPN protects everything else, while these sites are opened directly
(``ozon.ru`` is the canonical example).

Normalization rule requested by the user: if a brand has a ``.ru`` domain,
keep only ``.ru`` and drop its other zones (``.by``, ``.com``, ``.kz``,
``.su``, ...).  Brands without ``.ru`` are kept as-is — otherwise a service
whose primary domain is ``.com`` would be lost.

The module performs no network requests at import time and is safe to use from
headless tests.  Only :func:`fetch` touches the network, and only when the user
presses the button.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import util

log = util.get_logger("blocklist")

SOURCE_URL = "https://raw.githubusercontent.com/UnRKN/ru-blocklist/main/ru-blocklist.txt"
CACHE_FILE = util.CACHE_DIR / "ru-blocklist.json"
VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
BATCH_SIZE = 100

HOST_RE = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$"
)


class BlocklistError(Exception):
    """A download/parse failure with a human-readable message."""

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class Blocklist:
    domains: tuple[str, ...]
    raw_count: int
    fetched_at: datetime
    source: str = SOURCE_URL

    @property
    def count(self) -> int:
        return len(self.domains)


def _clean_line(raw: str) -> str | None:
    line = raw.strip().lstrip("\ufeff").strip()
    if not line or line.startswith(("#", "!", ";")):
        return None
    if line.lower().startswith("domain:"):
        line = line[7:].strip()
    if not line or any(char.isspace() for char in line):
        return None
    line = line.rstrip(".").lower()
    if not HOST_RE.match(line):
        return None
    return line


def unique_domains(lines) -> list[str]:
    """Parse, clean and deduplicate raw lines, preserving source order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in lines:
        domain = _clean_line(str(raw))
        if domain is None or domain in seen:
            continue
        seen.add(domain)
        result.append(domain)
    return result


def normalize_domains(domains) -> list[str]:
    """Keep only ``.ru`` for brands that have it; keep the rest untouched."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for domain in unique_domains(domains):
        base = domain.rsplit(".", 1)[0] if "." in domain else domain
        if base not in groups:
            groups[base] = []
            order.append(base)
        groups[base].append(domain)
    result: list[str] = []
    for base in order:
        group = groups[base]
        ru = [domain for domain in group if domain.endswith(".ru")]
        result.extend(ru if ru else group)
    return result


def parse_text(text: str) -> tuple[list[str], int]:
    """Return ``(normalized_domains, raw_unique_count)`` for a list file."""
    raw = unique_domains((text or "").splitlines())
    return normalize_domains(raw), len(raw)


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".", delete=False,
    )
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        os.chmod(handle.name, 0o600)
        handle.close()
        os.replace(handle.name, path)
    finally:
        try:
            if not handle.closed:
                handle.close()
        except Exception:
            pass


def load_cache(path: str | Path | None = None) -> Blocklist | None:
    """Read the last downloaded list.  Missing/corrupt cache means ``None``."""
    target = Path(path) if path is not None else CACHE_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        log.warning("Could not read blocklist cache", exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    domains = raw.get("domains")
    if not isinstance(domains, list):
        return None
    cleaned = [str(domain) for domain in domains if isinstance(domain, str)]
    cleaned = [domain for domain in cleaned if HOST_RE.match(domain)]
    if not cleaned:
        return None
    cleaned = normalize_domains(cleaned)
    if not cleaned:
        return None
    try:
        fetched_at = datetime.fromisoformat(str(raw.get("fetched_at")))
    except (TypeError, ValueError):
        fetched_at = datetime.now()
    try:
        raw_count = int(raw.get("raw_count") or len(cleaned))
    except (TypeError, ValueError):
        raw_count = len(cleaned)
    return Blocklist(
        domains=tuple(cleaned),
        raw_count=max(raw_count, len(cleaned)),
        fetched_at=fetched_at,
        source=str(raw.get("source") or SOURCE_URL),
    )


def save_cache(data: Blocklist, path: str | Path | None = None) -> None:
    target = Path(path) if path is not None else CACHE_FILE
    payload = {
        "version": VERSION,
        "source": data.source,
        "fetched_at": data.fetched_at.isoformat(timespec="seconds"),
        "raw_count": data.raw_count,
        "domains": list(data.domains),
    }
    _atomic_write(target, json.dumps(payload, ensure_ascii=False, indent=2))


def fetch(url: str = SOURCE_URL, timeout: float = 20) -> Blocklist:
    """Download and normalize the list.  Raises :class:`BlocklistError`."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"{util.APP_ID}/{util.APP_VERSION} (GTK3)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise BlocklistError(
            f"GitHub ответил ошибкой {exc.code}.",
            f"{url}\n{exc}",
        ) from exc
    except urllib.error.URLError as exc:
        raise BlocklistError(
            "Не удалось связаться с GitHub. Проверьте интернет-соединение.",
            f"{url}\n{exc.reason}",
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise BlocklistError(
            "Не удалось загрузить список: истекло время ожидания.",
            f"{url}\n{exc}",
        ) from exc
    if len(data) > MAX_BYTES:
        raise BlocklistError(
            "Список с GitHub оказался неожиданно большим.",
            f"{url}\n>{MAX_BYTES} bytes",
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlocklistError("Список с GitHub повреждён (не UTF-8).", str(exc)) from exc
    domains, raw_count = parse_text(text)
    if not domains:
        raise BlocklistError(
            "В загруженном файле не нашлось доменов.",
            f"{url}\n{len(data)} bytes",
        )
    return Blocklist(
        domains=tuple(domains),
        raw_count=raw_count,
        fetched_at=datetime.now(),
        source=url,
    )


def fetched_text(value: datetime | None) -> str:
    return value.strftime("%d.%m в %H:%M") if value else ""
