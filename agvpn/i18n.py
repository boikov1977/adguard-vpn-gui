"""Minimal gettext setup for the AdGuard VPN GUI.

Russian is the source language: every msgid in the code base is a Russian
string.  When no compiled catalog is available (the normal case for a source
checkout) :func:`_` simply returns the msgid unchanged, so the application
keeps working entirely in Russian.

The module deliberately does not call :func:`locale.setlocale` and does not
import Gtk: it is also used by the standalone ``askpass.py`` helper.
"""
from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "adguard-vpn-gui"

PKG_DIR = Path(__file__).resolve().parent
PKG_LOCALE_DIR = PKG_DIR / "locale"
SYSTEM_LOCALE_DIR = Path("/usr/share/locale")

_NULL = gettext.NullTranslations()
_state: dict[str, object] = {"key": None, "translation": _NULL}

_ENV_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
_SKIP_LOCALES = {"c", "posix"}


def localedirs() -> tuple[Path, ...]:
    """Locale directories to search, in priority order."""
    dirs: list[Path] = []
    override = os.environ.get("AGVPN_GUI_LOCALEDIR", "").strip()
    if override:
        dirs.append(Path(override))
    dirs.append(PKG_LOCALE_DIR)
    dirs.append(SYSTEM_LOCALE_DIR)
    result: list[Path] = []
    for path in dirs:
        if path not in result:
            result.append(path)
    return tuple(result)


def normalize_locale(value: str | None) -> str | None:
    """Turn ``en_US.UTF-8`` / ``ru-RU@euro`` into ``en_US`` / ``ru_RU``.

    ``C`` and ``POSIX`` mean "no translation" and return ``None``.
    """
    text = (value or "").strip()
    if not text:
        return None
    # Encoding and modifier are irrelevant to gettext's directory lookup.
    text = text.split("@", 1)[0].split(".", 1)[0].replace("-", "_")
    if not text or text.lower() in _SKIP_LOCALES:
        return None
    return text


def languages() -> tuple[str, ...]:
    """Explicit language preference list derived from the environment.

    ``LANGUAGE`` wins and may contain a colon-separated list (``en:ru``).
    For every locale the full form comes first and the bare language second,
    e.g. ``en_US`` then ``en``.
    """
    raw: list[str] = []
    language = os.environ.get("LANGUAGE", "")
    if language:
        raw.extend(language.split(":"))
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            raw.append(value)

    result: list[str] = []
    for item in raw:
        normalized = normalize_locale(item)
        if normalized is None:
            continue
        candidates = [normalized]
        base = normalized.split("_", 1)[0]
        if base and base != normalized:
            candidates.append(base)
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return tuple(result)


def _translation_key() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(str(path) for path in localedirs()),
        languages(),
    )


def _translations():
    """Return the best matching catalog, cached by the current environment."""
    key = _translation_key()
    if _state["key"] == key:
        return _state["translation"]
    langs = list(key[1])
    translation = _NULL
    if langs:
        for directory in localedirs():
            try:
                translation = gettext.translation(
                    DOMAIN, localedir=str(directory), languages=langs,
                    fallback=False,
                )
                break
            except (OSError, IOError, ValueError):
                continue
            except Exception:
                continue
    _state["key"] = key
    _state["translation"] = translation
    return translation


def _(message: str) -> str:
    """Translate a Russian source string."""
    return _translations().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a counted phrase, choosing the catalog's plural form."""
    return _translations().ngettext(singular, plural, n)


__all__ = [
    "DOMAIN", "PKG_LOCALE_DIR", "SYSTEM_LOCALE_DIR",
    "languages", "localedirs", "normalize_locale", "_", "ngettext",
]
