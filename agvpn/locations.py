"""Match the CLI status text to a known location.

Status text is free-form ("Helsinki, Finland", "Amsterdam in TUN mode",
"HELSINKI"), so matching by a plain substring can pick the wrong row — for
example ``York`` would match ``New York``.  This helper makes the order
explicit: exact city, exact country/ISO, then the longest substring match.
"""
from __future__ import annotations

import re

from .cli import Location


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _parts(text: str | None) -> list[str]:
    return [part for part in (_norm(p) for p in re.split(r"[,/]", text or "")) if part]


def _last_location(last) -> Location | None:
    if not last:
        return None
    try:
        return Location.from_dict(last)
    except Exception:
        return None


def _country_matches(loc: Location, country: str, iso: str) -> bool:
    return bool(
        (country and _norm(loc.country) == country)
        or (iso and _norm(loc.iso) == iso)
    )


def match_location(status_text: str | None, locations, last=None) -> Location | None:
    """Return the location described by ``status_text``, or ``None``.

    ``last`` is the stored last-location dict and is used for an empty status,
    and as a more specific answer when the status only names its country.
    """
    last_loc = _last_location(last)
    text = _norm(status_text)
    if not text:
        return last_loc

    candidates = [loc for loc in (locations or []) if loc is not None]
    parts = _parts(status_text)

    # 1. Exact city name (case-insensitive), whole status or a comma part.
    for loc in candidates:
        city = _norm(loc.city)
        if city and (city == text or city in parts):
            return loc
    if last_loc:
        city = _norm(last_loc.city)
        if city and (city == text or city in parts):
            return last_loc

    # 2. Exact country or ISO code.  Prefer the last location if it belongs
    #    to that country, because it also carries the city.
    for part in parts:
        for loc in candidates:
            if _country_matches(loc, part, part):
                if last_loc and _country_matches(last_loc, part, part):
                    return last_loc
                return loc
        if last_loc and _country_matches(last_loc, part, part):
            return last_loc

    # 3. Substring fallback: the longest city name is the most specific.
    best: Location | None = None
    best_len = 0
    for loc in candidates:
        city = _norm(loc.city)
        if city and city in text and len(city) > best_len:
            best, best_len = loc, len(city)
    if last_loc:
        city = _norm(last_loc.city)
        if city and city in text and len(city) > best_len:
            best = last_loc
    return best


def same_location(left: Location | None, right: Location | None) -> bool:
    """Compare rows by city + ISO, so a country-only match cannot collide."""
    if left is None or right is None:
        return False
    city_left, city_right = _norm(left.city), _norm(right.city)
    if not city_left or city_left != city_right:
        return False
    iso_left, iso_right = _norm(left.iso), _norm(right.iso)
    return not iso_left or not iso_right or iso_left == iso_right
