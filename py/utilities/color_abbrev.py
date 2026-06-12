"""Normalize container / vendor color names to OMS abbreviations."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# Spelled-out or vendor labels → standard OMS color abbrev
SPELL_TO_ABBREV: dict[str, str] = {
    "BLACK": "BK",
    "BLUE": "BL",
    "BEIGE": "BG",
    "BROWN": "BR",
    "BLUSH PINK": "BP",
    "COFFEE": "CF",
    "CHOCOLATE": "CF",
    "COGNAC": "CG",
    "CORAL": "CO",
    "DENIM BLUE": "DM",
    "DARK PLUM": "DPL",
    "FUSCHIA": "FU",
    "FUCHSIA": "FU",
    "GREEN": "GN",
    "GREY": "GY",
    "GRAY": "GY",
    "KHAKI": "KH",
    "ASHY BLUE": "ABL",
    "LIGHT BLUE": "LBL",
    "LAVENDER": "LV",
    "MUSTARD": "MD",
    "MAUVE": "MV",
    "NAVY": "NV",
    "OLIVE": "OL",
    "ORANGE": "OR",
    "OFF WHITE": "OW",
    "PINK": "PK",
    "PLUM": "PL",
    "PURPLE": "PP",
    "RED": "RD",
    "TAN": "TN",
    "TAUPE": "TP",
    "TEAL": "TL",
    "TEQUISE": "TQ",
    "TURQUOISE": "TQ",
    "WINE": "WN",
    "WHITE": "WT",
    "YELLOW": "YL",
    "STONE": "ST",
    "L.STONE": "L.ST",
    "L STONE": "L.ST",
    "L.ST": "L.ST",
    "LTTN": "L.TN",
    "LIGHT TAN": "L.TN",
    "L.TN": "L.TN",
    "FLOWER": "FLOWER",
    "LARGE": "L",
}

# Already-valid abbrevs (uppercase keys)
KNOWN_ABBREVS = {
    "BK", "BL", "BG", "BR", "BP", "CF", "CG", "CO", "DM", "DPL", "FU", "GN", "GY",
    "KH", "ABL", "LBL", "LV", "MD", "MV", "NV", "OL", "OR", "OW", "PK", "PL", "PP",
    "RD", "TN", "TP", "TL", "TQ", "WN", "WT", "YL", "ST", "L.ST", "L.TN", "FLOWER",
    "SG", "NT", "TN/NT",
}


@lru_cache(maxsize=1)
def _desc_word_to_abbrev() -> dict[str, str]:
    """Build lookup from English words in fdc_upc13_maps.js COLOR_MAP descriptions."""
    mapping: dict[str, str] = {}
    js_path = Path(__file__).resolve().parents[2] / "static" / "js" / "fdc_upc13_maps.js"
    if not js_path.is_file():
        return mapping

    text = js_path.read_text(encoding="utf-8", errors="ignore")
    for match in re.finditer(
        r"abbrev:\s*'([^']+)'\s*,\s*desc:\s*'([^']*)'",
        text,
    ):
        abbrev = match.group(1).strip().upper()
        desc = match.group(2).strip().upper()
        if not abbrev:
            continue
        for word in re.findall(r"[A-Z]{2,}", desc):
            mapping.setdefault(word, abbrev)
        if desc:
            mapping.setdefault(desc, abbrev)
    return mapping


def normalize_color_to_abbrev(color: str) -> str:
    """Convert vendor/spelled-out color to OMS abbreviation when possible."""
    raw = re.sub(r"\s+", " ", str(color or "").strip())
    if not raw:
        return ""

    upper = raw.upper()
    if upper in KNOWN_ABBREVS:
        return upper
    if upper in SPELL_TO_ABBREV:
        return SPELL_TO_ABBREV[upper]

    desc_map = _desc_word_to_abbrev()
    if upper in desc_map:
        return desc_map[upper]

    # Single trailing English word in mixed desc, e.g. "111 BLACK"
    tokens = upper.split()
    if len(tokens) > 1 and tokens[-1] in SPELL_TO_ABBREV:
        return SPELL_TO_ABBREV[tokens[-1]]

    # Already looks like an abbrev (short alphanumeric)
    if len(upper) <= 6 and re.fullmatch(r"[A-Z0-9./]+", upper):
        return upper

    return upper
