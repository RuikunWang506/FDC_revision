"""Temporary catalog-OCR color cache for V7 List Creator styles not yet in OMS.

When a snapshot is built for a style that is missing from OMS, color codes are
read from the catalog (or Dropbox) image and stored under
``instance/listcreator_ocr_colors/<STYLE>.json``. Once the style appears in OMS
the cache file is deleted so live inventory colors are used instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Iterable, Optional

from flask import current_app

_log = logging.getLogger(__name__)

_OCR_LOCK = threading.Lock()
_READER = None
_KNOWN_COLORS: Optional[set[str]] = None

_STYLE_IN_TOKEN_RE = re.compile(r"^[A-Z]{1,3}\d{3,5}[A-Z]*$")

_EXTRA_COLORS = {
    "BUR", "BURG", "CAMO", "LEO", "LEOPARD", "FLOWER", "MULTI", "MIX",
    "PRT", "PRINT", "NUDE", "CLR", "GOLD", "SILVER", "CHEETAH", "ZEBRA",
    "SNAKE", "CROC", "PYTHON", "IVORY", "CREAM", "NATURAL", "NAT",
    "OL", "CF", "BK", "BR", "DM", "WN", "NV", "GY", "PK", "RD", "WT",
}

_STOPWORDS = {
    "FDC", "PCS", "BOX", "QTY", "TEL", "FAX", "WWW", "COM", "INC", "USA",
    "LLC", "THE", "AND", "FOR", "NEW", "SIZE", "ITEM", "CODE", "STYLE",
    "COLOR", "DESC", "PAGE", "FROM", "WITH", "THIS", "THAT", "MADE",
    "CHINA", "EMAIL", "HTTP", "HTTPS", "NET", "ORG", "LTD", "CTN", "CTNS",
    "BAG", "BAGS", "SET", "SETS", "LBS", "INCH", "INCHES", "FASHION",
    "DISTRIBUTION", "CENTER", "INDUSTRY", "LAWSON", "PHONE", "SKU",
    "CO", "OF", "IN", "ON", "TO", "AT", "BY", "NO", "PC", "MM", "CM",
    "KG", "OZ", "LX", "DX", "HX", "LH", "WH", "HD", "DROP", "HANDLE",
}

_OCR_MAX_WIDTH = 1400
_OCR_CROP_SCALE = 3
_OCR_MIN_CONF_KNOWN = 0.40
_OCR_MIN_CONF_UNKNOWN = 0.72
_OCR_CACHE_VERSION = 2
_SIZE_SUFFIX_RE = re.compile(r"^([A-Z]{2,5})(?:I|/)([SML])$")


def _style_key(style: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (style or "").upper())


def _ocr_color_db_dir(app=None) -> str:
    root = (app or current_app).instance_path
    path = os.path.join(root, "listcreator_ocr_colors")
    os.makedirs(path, exist_ok=True)
    return path


def _ocr_color_db_path(style: str, app=None) -> str:
    key = _style_key(style) or "unknown"
    return os.path.join(_ocr_color_db_dir(app), f"{key}.json")


def load_temp_ocr_colors(style: str, app=None) -> Optional[list[str]]:
    path = _ocr_color_db_path(style, app=app)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("ocr_version") or 0) < _OCR_CACHE_VERSION:
        return None
    colors = data.get("colors")
    if not isinstance(colors, list):
        return None
    cleaned = [_normalize_color_token(c) for c in colors]
    cleaned = [c for c in cleaned if c]
    return cleaned or None


def save_temp_ocr_colors(
    style: str,
    colors: Iterable[str],
    *,
    source: str = "ocr",
    image_path: str = "",
    image_mtime: Optional[float] = None,
    app=None,
) -> None:
    cleaned = [_normalize_color_token(c) for c in colors]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return
    payload = {
        "style": _style_key(style),
        "colors": cleaned,
        "source": source,
        "ocr_version": _OCR_CACHE_VERSION,
        "image_path": image_path or "",
        "image_mtime": image_mtime,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = _ocr_color_db_path(style, app=app)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=0)
    os.replace(tmp, path)


def delete_temp_ocr_colors(style: str, app=None) -> None:
    path = _ocr_color_db_path(style, app=app)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _cache_matches_image(style: str, image_path: str, app=None) -> Optional[list[str]]:
    path = _ocr_color_db_path(style, app=app)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("ocr_version") or 0) < _OCR_CACHE_VERSION:
        return None
    colors = data.get("colors")
    if not isinstance(colors, list):
        return None
    cleaned = [_normalize_color_token(c) for c in colors]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return None
    cached_path = str(data.get("image_path") or "")
    cached_mtime = data.get("image_mtime")
    if image_path and cached_path and os.path.normcase(cached_path) != os.path.normcase(image_path):
        return None
    if image_path and os.path.isfile(image_path) and cached_mtime is not None:
        try:
            if abs(float(os.path.getmtime(image_path)) - float(cached_mtime)) > 1.0:
                return None
        except (OSError, TypeError, ValueError):
            return None
    return cleaned


def _known_color_abbrevs() -> set[str]:
    global _KNOWN_COLORS
    if _KNOWN_COLORS is not None:
        return _KNOWN_COLORS
    known = set(_EXTRA_COLORS)
    js_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "static", "js", "fdc_upc13_maps.js"
    )
    js_path = os.path.normpath(js_path)
    try:
        text = open(js_path, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"abbrev:\s*'([^']*)'", text):
            raw = (m.group(1) or "").strip().upper()
            for part in re.split(r"[/\s]+", raw):
                part = re.sub(r"[^A-Z]", "", part)
                if 2 <= len(part) <= 6:
                    known.add(part)
    except OSError:
        pass
    _KNOWN_COLORS = known
    return known


def _color_base(token: str) -> str:
    return (token or "").split("/", 1)[0]


def _is_known_color(token: str) -> bool:
    if not token:
        return False
    known = _known_color_abbrevs()
    if token in known:
        return True
    m = re.fullmatch(r"([A-Z]{2,5})/[SML]", token)
    return bool(m and m.group(1) in known)


def _normalize_color_token(token: str) -> str:
    token = (token or "").strip().upper()
    token = token.replace(" ", "")
    if not token:
        return ""
    token = re.sub(r"[^A-Z./]", "", token)
    token = re.sub(r"\.{2,}", ".", token).strip(".")
    token = re.sub(r"/{2,}", "/", token).strip("/")
    if not token:
        return ""
    size_m = _SIZE_SUFFIX_RE.fullmatch(token)
    if size_m:
        token = f"{size_m.group(1)}/{size_m.group(2)}"
    if not _is_known_color(token):
        collapsed = re.sub(r"(.)\1+", r"\1", token)
        if collapsed != token and _is_known_color(collapsed):
            token = collapsed
    if _STYLE_IN_TOKEN_RE.match(_color_base(token)):
        return ""
    if token in _STOPWORDS or _color_base(token) in _STOPWORDS:
        return ""
    if len(_color_base(token)) == 1:
        return ""
    if len(token) > 10:
        return ""
    return token


def _token_is_color(token: str, *, allow_unknown: bool) -> bool:
    token = _normalize_color_token(token)
    if not token:
        return False
    if _is_known_color(token):
        return True
    if not allow_unknown:
        return False
    if "." in token or "/" in token:
        return False
    return 2 <= len(token) <= 4 and token.isalpha()


def catalog_image_path_for_style(style: str, dropbox_images: Optional[dict] = None) -> Optional[str]:
    """Local file for a style: Dropbox snapshot path first, then DAM catalog_original."""
    if dropbox_images:
        candidates = []
        raw = (style or "").strip()
        if raw:
            candidates.extend([raw, raw.upper(), re.sub(r"-", "", raw.upper())])
        for key in candidates:
            path = dropbox_images.get(key) or dropbox_images.get(str(key).upper())
            if path and os.path.isfile(path):
                return path
    sku = (style or "").strip().upper()
    if not sku:
        return None
    try:
        from py.utilities.dam_utils import get_asset_by_sku

        asset = get_asset_by_sku(sku, canonical_only=True, source_filter="catalog_original")
        if asset and getattr(asset, "path", None) and os.path.isfile(asset.path):
            return asset.path
        no_dash = sku.replace("-", "")
        if no_dash != sku:
            asset = get_asset_by_sku(no_dash, canonical_only=True, source_filter="catalog_original")
            if asset and getattr(asset, "path", None) and os.path.isfile(asset.path):
                return asset.path
    except Exception:
        _log.debug("catalog_image_path_for_style DAM lookup failed for %s", sku, exc_info=True)
    return None


def _easyocr_reader():
    global _READER
    if _READER is not None:
        return _READER
    with _OCR_LOCK:
        if _READER is not None:
            return _READER
        import easyocr

        _READER = easyocr.Reader(["en"], gpu=False, verbose=False)
        return _READER


def _open_rgb(path: str):
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if img.width > _OCR_MAX_WIDTH:
        nh = max(1, int(round(img.height * _OCR_MAX_WIDTH / img.width)))
        img = img.resize((_OCR_MAX_WIDTH, nh), Image.Resampling.LANCZOS)
    return img


def _prepare_ocr_image(crop, scale: int | None = None):
    from PIL import Image, ImageEnhance

    if crop.width < 8 or crop.height < 8:
        return crop
    if scale is None:
        scale = _OCR_CROP_SCALE if crop.width <= 480 else 1
    im = crop
    if scale > 1:
        im = crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.LANCZOS,
        )
    gray = ImageEnhance.Contrast(im.convert("L")).enhance(2.2)
    return gray.convert("RGB")


def _read_crop(reader, crop, *, allow_unknown: bool, scale: int | None = None) -> list[str]:
    import numpy as np

    if crop.width < 8 or crop.height < 8:
        return []
    prepared = _prepare_ocr_image(crop, scale=scale)
    try:
        results = reader.readtext(
            np.array(prepared),
            detail=1,
            paragraph=False,
            allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ/",
        )
    except Exception:
        _log.debug("easyocr readtext failed", exc_info=True)
        return []
    hits: list[tuple[float, float, str]] = []
    for box, text, conf in results:
        try:
            conf_f = float(conf)
        except (TypeError, ValueError):
            conf_f = 0.0
        raw = (text or "").strip().upper()
        if not raw:
            continue
        try:
            y = float(box[0][1])
        except (TypeError, ValueError, IndexError):
            y = 0.0
        for piece in re.split(r"[^A-Z./]+", raw):
            token = _normalize_color_token(piece)
            if not token:
                continue
            min_conf = _OCR_MIN_CONF_KNOWN if _is_known_color(token) else _OCR_MIN_CONF_UNKNOWN
            if conf_f < min_conf:
                continue
            if _token_is_color(token, allow_unknown=allow_unknown):
                hits.append((y, -conf_f, token))
    hits.sort()
    out: list[str] = []
    seen = set()
    for _y, _c, token in hits:
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _read_right_column(reader, crop) -> list[str]:
    """OCR stacked color-swatch labels; bands keep OL/BUR readable and fast."""
    if crop.width < 8 or crop.height < 8:
        return []
    colors: list[str] = []
    seen = set()
    bw, bh = crop.size
    n_bands = 5
    for i in range(n_bands):
        y0 = int(bh * i / n_bands)
        y1 = int(bh * (i + 1) / n_bands)
        band = crop.crop((0, y0, bw, y1))
        for token in _read_crop(reader, band, allow_unknown=True, scale=3):
            if token not in seen:
                seen.add(token)
                colors.append(token)
    if len(colors) < 2:
        for token in _read_crop(reader, crop, allow_unknown=True, scale=2):
            if token not in seen:
                seen.add(token)
                colors.append(token)
    return colors


def ocr_colors_from_image(image_path: str) -> list[str]:
    """Return color codes in display order: hero/main color, then right-column variants."""
    if not image_path or not os.path.isfile(image_path):
        return []
    try:
        reader = _easyocr_reader()
        img = _open_rgb(image_path)
    except Exception:
        _log.warning("listcreator OCR could not open %s", image_path, exc_info=True)
        return []
    w, h = img.size
    # Hero color sits by the main photo (lower-right of the large bag), not the whole sheet.
    hero = img.crop((int(w * 0.38), int(h * 0.48), int(w * 0.70), int(h * 0.86)))
    right = img.crop((int(w * 0.74), int(h * 0.10), w, h))
    hero_colors = _read_crop(reader, hero, allow_unknown=False, scale=1)
    right_colors = _read_right_column(reader, right)
    if len(right_colors) < 2:
        wider = img.crop((int(w * 0.68), int(h * 0.10), w, h))
        right_colors = _read_right_column(reader, wider)
    ordered: list[str] = []
    seen = set()
    for token in hero_colors + right_colors:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def resolve_not_in_oms_colors(
    style: str,
    dropbox_images: Optional[dict] = None,
    app=None,
) -> Optional[list[str]]:
    """Colors for a not-in-OMS style, or None if there is no image / OCR found nothing.

    Caller should keep the existing BK placeholder when this returns None.
    """
    image_path = catalog_image_path_for_style(style, dropbox_images=dropbox_images)
    if not image_path:
        cached = load_temp_ocr_colors(style, app=app)
        return cached
    cached = _cache_matches_image(style, image_path, app=app)
    if cached:
        return cached
    colors = ocr_colors_from_image(image_path)
    if not colors:
        return load_temp_ocr_colors(style, app=app)
    mtime = None
    try:
        mtime = os.path.getmtime(image_path)
    except OSError:
        mtime = None
    save_temp_ocr_colors(
        style,
        colors,
        source="ocr",
        image_path=image_path,
        image_mtime=mtime,
        app=app,
    )
    _log.info(
        "listcreator OCR colors style=%s colors=%s image=%s",
        style,
        ",".join(colors),
        os.path.basename(image_path),
    )
    return colors
