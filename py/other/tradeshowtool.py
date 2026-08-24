"""Trade Show Tool — clean catalog sheets and impose 4-up, 5-up, or 1-up on US Letter."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import threading
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from PIL import Image, ImageDraw, ImageFont, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from werkzeug.utils import secure_filename

from py.utilities.setting import Config, login_required

tradeshowtool_bp = Blueprint("tradeshowtool", __name__)

INCH_PT = 72.0
LETTER_W_IN, LETTER_H_IN = 8.5, 11.0
# 4-in-1: each cell 5.3" × 3.8" in the upper-left of landscape Letter (11 × 8.5),
# leaving crop-able white on the right (0.4") and bottom (0.9").
CELL_W_IN, CELL_H_IN = 5.3, 3.8
# 5-in-1: two 3.3×4.8 portrait stacked left, three 4.8×3.3 landscape stacked
# right, packed to the upper-left of portrait Letter (8.5 × 11). Content is
# 8.1" × 9.9"; leftover white is on the right (0.4") and bottom (1.1").
FIVE_SHORT_IN, FIVE_LONG_IN = 3.3, 4.8
PRINT_DPI = 300

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_FILES = 2000
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
JOB_TTL_SEC = 24 * 60 * 60
HANDOFF_TTL_SEC = 180
UPLOAD_BATCH_HINT = 12

# Bold serif + bold sans is enough; extra faces made every sheet slow.
WINDOWS_FONTS = [
    r"C:\Windows\Fonts\timesbd.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]


def _available_fonts() -> List[str]:
    found = [p for p in WINDOWS_FONTS if os.path.isfile(p)]
    return found[:2] if found else found


def _open_rgb(src: Any) -> Image.Image:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "P":
        return img.convert("RGBA").convert("RGB")
    return img.convert("RGB")


def _make_glyph(ch: str, font_path: str, size: int) -> Optional[np.ndarray]:
    try:
        font = ImageFont.truetype(font_path, size)
    except OSError:
        return None
    dummy = Image.new("L", (8, 8), 0)
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), ch, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if tw < 3 or th < 5:
        return None
    im = Image.new("L", (tw + 4, th + 4), 0)
    ImageDraw.Draw(im).text((2 - bbox[0], 2 - bbox[1]), ch, font=font, fill=255)
    arr = np.array(im)
    ys, xs = np.where(arr > 20)
    if xs.size == 0:
        return None
    return arr[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]


_GLYPH_CACHE: Dict[Tuple[str, str, int], Optional[np.ndarray]] = {}


def _cached_glyph(ch: str, font_path: str, size: int) -> Optional[np.ndarray]:
    key = (ch, font_path, size)
    if key not in _GLYPH_CACHE:
        _GLYPH_CACHE[key] = _make_glyph(ch, font_path, size)
    return _GLYPH_CACHE[key]


def _glyph_font_sizes(img_h: int) -> List[int]:
    """Point sizes covering small E-Fashion footer `$9.00` and larger FDC `$15.00`."""
    sizes = [
        max(10, int(round(img_h * frac / 0.78)))
        for frac in (0.016, 0.020, 0.024, 0.028, 0.034, 0.042)
    ]
    out: List[int] = []
    for size in sizes:
        if not out or abs(size - out[-1]) >= 2:
            out.append(size)
    return out


def _nms_hits(
    hits: Sequence[Tuple[float, int, int, int, int]], max_keep: int = 16
) -> List[Tuple[float, int, int, int, int]]:
    ordered = sorted(hits, key=lambda h: h[0], reverse=True)
    kept: List[Tuple[float, int, int, int, int]] = []
    for score, x, y, gw, gh in ordered:
        cx, cy = x + gw / 2, y + gh / 2
        overlap = False
        for _s, kx, ky, kw, kh in kept:
            if abs(cx - (kx + kw / 2)) < max(gw, kw) * 1.35 and abs(
                cy - (ky + kh / 2)
            ) < max(gh, kh) * 1.2:
                overlap = True
                break
        if overlap:
            continue
        kept.append((score, x, y, gw, gh))
        if len(kept) >= max_keep:
            break
    return kept


def _char_score_at(
    inv: np.ndarray, x: int, y: int, gw: int, gh: int, ch: str
) -> float:
    """Best template score for `ch` inside the `$` hit box."""
    import cv2

    pad = 1
    y0, x0 = max(0, y - pad), max(0, x - pad)
    patch = inv[y0 : y + gh + pad, x0 : x + gw + pad].astype(np.float32)
    if patch.size == 0:
        return 0.0
    best = 0.0
    sizes = sorted({max(12, gh), max(14, int(gh * 1.3))})
    for font_path in _available_fonts()[:2]:
        for size in sizes:
            glyph = _cached_glyph(ch, font_path, size)
            if glyph is None:
                continue
            g = glyph.astype(np.float32)
            if g.shape[0] > patch.shape[0] or g.shape[1] > patch.shape[1]:
                continue
            res = cv2.matchTemplate(patch, g, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(res.max()))
    return best


def _vertical_bar_frac(inv: np.ndarray, x: int, y: int, gw: int, gh: int) -> float:
    """How much of the glyph height has ink on the center stem (`$` vs `S`)."""
    patch = inv[y : y + gh, x : x + gw]
    if patch.size == 0 or gh <= 0:
        return 0.0
    cx = gw // 2
    band = patch[:, max(0, cx - 1) : min(gw, cx + 2)]
    if band.size == 0:
        return 0.0
    return float((band.max(axis=1) > 70).mean())


def _local_cc_height_ratio(inv: np.ndarray, x: int, y: int, gw: int, gh: int) -> float:
    """Height of the local ink blob vs the `$` glyph. Large digits score >> 1."""
    import cv2

    h, w = inv.shape
    pad = max(8, int(gh * 2.2))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    crop = (inv[y0 : min(h, y + gh + pad), x0 : min(w, x + gw + pad)] > 70).astype(np.uint8)
    if crop.size == 0:
        return 9.0
    _n, labels, stats, _c = cv2.connectedComponentsWithStats(crop, 8)
    cx = min(crop.shape[1] - 1, max(0, (x - x0) + gw // 2))
    cy = min(crop.shape[0] - 1, max(0, (y - y0) + gh // 2))
    lab = int(labels[cy, cx])
    if lab == 0:
        near = labels[max(0, cy - 2) : cy + 3, max(0, cx - 2) : cx + 3]
        vals = near[near > 0]
        lab = int(np.bincount(vals).argmax()) if vals.size else 0
    if lab == 0:
        return 9.0
    return float(stats[lab][3]) / max(gh, 1)


def _isolated_on_left(inv: np.ndarray, x: int, y: int, gw: int, gh: int) -> bool:
    """True if `$` starts a token (`$12.00`), not a digit inside ES1377 / T3686.

    Thin vertical rules (sheet/cell borders) are ignored.
    """
    pad = max(2, gh // 5)
    look = max(3, int(gw * 2.2))
    y0, y1 = max(0, y - pad), min(inv.shape[0], y + gh + pad)
    x0 = max(0, x - look)
    strip = inv[y0:y1, x0:x]
    if strip.size == 0:
        return True
    ink_min = max(2, gh // 8)
    cols = (strip > 70).sum(axis=0)
    row_h = y1 - y0
    # Last column is often the `$` glyph fringe — skip it.
    for i in range(len(cols) - 2, -1, -1):
        if cols[i] < ink_min:
            continue
        col = strip[:, i]
        if cols[i] >= row_h * 0.85 or (col > 70).mean() >= 0.85:
            continue
        dist = (len(cols) - 1) - i
        return dist >= max(2, gw // 3)
    return True


def _dollar_hits(
    inv: np.ndarray,
    search: Tuple[int, int, int, int],
) -> List[Tuple[float, int, int, int, int]]:
    """All real `$` signs in `search` — not serif S in PCS/BOX, not slices of big digits."""
    import cv2

    x0, y0, x1, y1 = search
    region = inv[y0:y1, x0:x1]
    if region.size == 0:
        return []
    fonts = _available_fonts()
    if not fonts:
        return []
    region_f = region.astype(np.float32)
    raw: List[Tuple[float, int, int, int, int]] = []
    for font_path in fonts:
        for size in _glyph_font_sizes(inv.shape[0]):
            glyph = _cached_glyph("$", font_path, size)
            if glyph is None:
                continue
            gh, gw = glyph.shape
            if region.shape[0] < gh or region.shape[1] < gw:
                continue
            res = cv2.matchTemplate(region_f, glyph.astype(np.float32), cv2.TM_CCOEFF_NORMED)
            _min_v, max_v, _min_l, max_l = cv2.minMaxLoc(res)
            if max_v < 0.58:
                continue
            raw.append(
                (float(max_v), int(max_l[0]) + x0, int(max_l[1]) + y0, gw, gh)
            )
            # Extra peaks only when the best hit is not already decisive.
            if max_v < 0.78:
                ys, xs = np.where(res >= 0.62)
                if xs.size > 24:
                    flat = res.ravel()
                    top = np.argpartition(flat, -24)[-24:]
                    coords = [divmod(int(i), res.shape[1]) for i in top]
                else:
                    coords = list(zip(ys.tolist(), xs.tolist()))
                for ry, rx in coords:
                    score = float(res[ry, rx])
                    if score < 0.62:
                        continue
                    raw.append((score, int(rx) + x0, int(ry) + y0, gw, gh))
    kept: List[Tuple[float, int, int, int, int]] = []
    for score, x, y, gw, gh in _nms_hits(raw, max_keep=8):
        if y < int(inv.shape[0] * 0.78):
            continue
        if _vertical_bar_frac(inv, x, y, gw, gh) < 0.32:
            continue
        if _local_cc_height_ratio(inv, x, y, gw, gh) > 1.55:
            continue
        box = _extend_price_box(inv, x, y, gw, gh)
        extra = box[2] - (x - box[0]) - gw
        # Need at least one digit after `$`; cap so we don't swallow a whole line.
        if extra < gw * 1.15 or extra > gw * 12:
            continue
        if not _isolated_on_left(inv, x, y, gw, gh):
            continue
        # Serif `$` on some E-Fashion sheets scores like `S`; trust digit extension.
        if extra < gw * 2.5:
            s_score = _char_score_at(inv, x, y, gw, gh, "S")
            if score - s_score < 0.04:
                continue
        kept.append((score, x, y, gw, gh))
    return kept


def _extend_price_box(
    inv: np.ndarray, x: int, y: int, gw: int, gh: int
) -> Tuple[int, int, int, int]:
    """Grow the `$` box rightward to cover whatever amount follows (`$15.00`, `$8`, `$12.50`, …)."""
    h, w = inv.shape
    pad_y = max(2, gh // 6)
    y0 = max(0, y - pad_y)
    y1 = min(h, y + gh + pad_y)
    max_extra = min(w - (x + gw), int(gh * 8.5))
    row = inv[y0:y1, x + gw : x + gw + max_extra]
    if row.size == 0:
        return x, y0, gw, y1 - y0
    col = (row > 70).sum(axis=0)
    ink_min = max(2, gh // 8)
    gap = 0
    end = 0
    max_gap = max(4, int(gh * 0.55))
    for i, v in enumerate(col):
        if v >= ink_min:
            end = i + 1
            gap = 0
        else:
            gap += 1
            if gap > max_gap and end > 0:
                break
    extra = min(max_extra, end + max(2, gh // 5))
    x0 = max(0, x - max(2, gw // 4))
    return x0, y0, (x + gw + extra) - x0, y1 - y0


def _tesseract_price_boxes(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Optional Tesseract pass: boxes whose text looks like `$15.00`."""
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return []
    h, w = rgb.shape[:2]
    try:
        data = pytesseract.image_to_data(Image.fromarray(rgb), output_type=Output.DICT)
    except Exception:
        return []
    boxes: List[Tuple[int, int, int, int]] = []
    n = len(data.get("text") or [])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 30:
            continue
        left, top = int(data["left"][i]), int(data["top"][i])
        width, height = int(data["width"][i]), int(data["height"][i])
        # Skip header phone / address band.
        if top < int(h * 0.16):
            continue
        if re.search(r"\$\s*\d", text) or re.fullmatch(r"\$[\d.,]+", text):
            boxes.append((left, top, width, height))
            continue
        if text == "$" or text.startswith("$"):
            # Merge following token on the same line (e.g. "$" + "15.00").
            nx = left + width
            ny = top + height // 2
            extra_w, extra_h = 0, 0
            for j in range(i + 1, min(i + 4, n)):
                t2 = (data["text"][j] or "").strip()
                if not t2:
                    continue
                if not re.search(r"[\d.,]", t2):
                    break
                l2, t2y = int(data["left"][j]), int(data["top"][j])
                w2, h2 = int(data["width"][j]), int(data["height"][j])
                if abs((t2y + h2 / 2) - ny) > height * 0.8:
                    break
                if l2 < nx - 4:
                    break
                extra_w = (l2 + w2) - left
                extra_h = max(height, t2y + h2 - top)
                nx = l2 + w2
            boxes.append((left, top, extra_w or width, extra_h or height))
    return boxes


def _remove_price(rgb: np.ndarray) -> Tuple[np.ndarray, int]:
    """White-out `$…` price tokens. Returns (image, boxes_removed)."""
    import cv2

    out = rgb.copy()
    gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
    inv = 255 - gray
    h, w = gray.shape
    boxes: List[Tuple[int, int, int, int]] = []

    # Price lives with the style block (lower-left / lower-center), not the header.
    search = (0, int(h * 0.55), int(w * 0.72), h)
    for _score, x, y, gw, gh in _dollar_hits(inv, search):
        bx, by, bw, bh = _extend_price_box(inv, x, y, gw, gh)
        boxes.append((bx - 2, by - 2, bw + 4, bh + 4))

    # Tesseract is much slower; only try it when the `$` matcher found nothing.
    if not boxes:
        for bx, by, bw, bh in _tesseract_price_boxes(out):
            boxes.append((bx - 2, by - 2, bw + 4, bh + 4))

    if not boxes:
        return out, 0

    # Dedup overlapping boxes.
    kept: List[Tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda b: b[2] * b[3], reverse=True):
        x, y, bw, bh = box
        overlap = False
        for kx, ky, kw, kh in kept:
            ix0, iy0 = max(x, kx), max(y, ky)
            ix1, iy1 = min(x + bw, kx + kw), min(y + bh, ky + kh)
            if ix1 > ix0 and iy1 > iy0:
                overlap = True
                break
        if not overlap:
            kept.append(box)

    for x, y, bw, bh in kept:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(w, x + bw), min(h, y + bh)
        out[y0:y1, x0:x1] = 255
    return out, len(kept)


def _char_like_components(
    binary: np.ndarray, img_w: int, img_h: int, ox: int, oy: int
) -> List[Tuple[int, int, int, int]]:
    """Character-sized connected components in a binary (ink=255) crop."""
    import cv2

    num, _labels, stats, _cents = cv2.connectedComponentsWithStats(binary, 8)
    out: List[Tuple[int, int, int, int]] = []
    min_h = max(8, int(img_h * 0.014))
    max_h = max(18, int(img_h * 0.040))
    min_w = 3
    max_w = max(16, int(img_w * 0.048))
    max_area = int(img_w * img_h * 0.003)
    for i in range(1, num):
        x, y, bw, bh, area = stats[i]
        if area < 18 or area > max_area:
            continue
        if bh < min_h or bh > max_h:
            continue
        if bw < min_w or bw > max_w:
            continue
        # Skip extremely flat specks (bag weave). Digit groups can be wider than tall.
        if bh > 0 and bw > bh * 3.6:
            continue
        out.append((x + ox, y + oy, bw, bh))
    return out


def _cluster_rows(
    boxes: Sequence[Tuple[int, int, int, int]], gap: int
) -> List[List[Tuple[int, int, int, int]]]:
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: b[1] + b[3] / 2)
    rows: List[List[Tuple[int, int, int, int]]] = [[ordered[0]]]
    for box in ordered[1:]:
        cy = box[1] + box[3] / 2
        prev = rows[-1]
        prev_cy = sum(b[1] + b[3] / 2 for b in prev) / len(prev)
        if abs(cy - prev_cy) <= gap:
            prev.append(box)
        else:
            rows.append([box])
    return rows


def _rightmost_x_cluster(
    boxes: Sequence[Tuple[int, int, int, int]], gap: int
) -> List[Tuple[int, int, int, int]]:
    """Keep the rightmost x-cluster (quantity digits), drop color-code letters."""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: b[0] + b[2] / 2)
    clusters: List[List[Tuple[int, int, int, int]]] = [[ordered[0]]]
    for box in ordered[1:]:
        cx = box[0] + box[2] / 2
        prev = clusters[-1]
        prev_cx = max(b[0] + b[2] / 2 for b in prev)
        if cx - prev_cx <= gap:
            prev.append(box)
        else:
            clusters.append([box])
    return clusters[-1]


def _caption_tokens(
    boxes: Sequence[Tuple[int, int, int, int]], gap: int
) -> List[List[Tuple[int, int, int, int]]]:
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: b[0])
    tokens: List[List[Tuple[int, int, int, int]]] = [[ordered[0]]]
    for box in ordered[1:]:
        prev = tokens[-1]
        prev_r = max(b[0] + b[2] for b in prev)
        if box[0] - prev_r <= gap:
            prev.append(box)
        else:
            tokens.append([box])
    return tokens


def _token_box(
    token: Sequence[Tuple[int, int, int, int]], pad: int
) -> Tuple[int, int, int, int]:
    xs = min(b[0] for b in token)
    ys = min(b[1] for b in token)
    xe = max(b[0] + b[2] for b in token)
    ye = max(b[1] + b[3] for b in token)
    return xs - pad, ys - pad, (xe - xs) + 2 * pad, (ye - ys) + 2 * pad


def _token_is_qty(token: Sequence[Tuple[int, int, int, int]]) -> bool:
    """3–4 digit captions, or 2-digit groups that are narrower than color letters."""
    if not token:
        return False
    xs = min(b[0] for b in token)
    xe = max(b[0] + b[2] for b in token)
    h = max(b[3] for b in token)
    w = xe - xs
    if h <= 0:
        return False
    n = len(token)
    aspect = w / h
    mean_aw = sum(b[2] / max(b[3], 1) for b in token) / n
    if n >= 3:
        return True
    if n == 1 and aspect >= 1.70:
        return True
    return n == 2 and aspect >= 1.15 and mean_aw <= 0.68


def _footer_row_qty_boxes(gray: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """E-Fashion footer: numbers in `BK 365` / `411 BK` rows. Leave color letters and PCS/BOX."""
    import cv2

    h, w = gray.shape[:2]
    y0, x0 = int(h * 0.68), int(w * 0.34)
    crop = gray[y0:h, x0:w]
    if crop.size == 0:
        return []
    binary = cv2.adaptiveThreshold(
        crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 17, 8
    )
    binary = cv2.medianBlur(binary, 3)
    min_h = max(5, int(h * 0.012))
    max_h = max(12, int(h * 0.040))
    min_w, max_w = 2, max(14, int(w * 0.045))
    num, _labels, stats, _c = cv2.connectedComponentsWithStats(binary, 8)
    comps: List[Tuple[int, int, int, int]] = []
    for i in range(1, num):
        x, y, bw, bh, area = stats[i]
        if area < 8 or bh < min_h or bh > max_h or bw < min_w or bw > max_w:
            continue
        comps.append((x + x0, y + y0, bw, bh))
    if len(comps) < 3:
        return []
    row_gap = max(5, int(h * 0.016))
    x_gap = max(8, int(w * 0.016))
    pad = max(1, int(h * 0.004))
    boxes: List[Tuple[int, int, int, int]] = []
    for row in _cluster_rows(comps, row_gap):
        tokens = _caption_tokens(row, x_gap)
        if len(tokens) < 3:
            continue
        xs = min(b[0] for tok in tokens for b in tok)
        xe = max(b[0] + b[2] for tok in tokens for b in tok)
        # FDC color column is a narrow vertical strip; E-Fashion captions span the footer.
        if (xe - xs) < w * 0.14:
            continue
        qty_tokens = [tok for tok in tokens if _token_is_qty(tok)]
        if not qty_tokens:
            # BK 365 / 411 BK usually alternate letter-pair and number.
            for tok in tokens:
                n = len(tok)
                xs = min(b[0] for b in tok)
                xe = max(b[0] + b[2] for b in tok)
                ht = max(b[3] for b in tok)
                if ht and (xe - xs) / ht >= 1.55:
                    qty_tokens.append(tok)
                elif n >= 3:
                    qty_tokens.append(tok)
        for tok in qty_tokens:
            boxes.append(_token_box(tok, pad))
    return boxes


def _tesseract_qty_boxes(rgb: np.ndarray) -> List[Tuple[int, int, int, int]]:
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return []
    h, w = rgb.shape[:2]
    x0 = int(w * 0.70)
    y0 = int(h * 0.16)
    crop = rgb[y0:h, x0:w]
    try:
        data = pytesseract.image_to_data(Image.fromarray(crop), output_type=Output.DICT)
    except Exception:
        return []
    boxes: List[Tuple[int, int, int, int]] = []
    n = len(data.get("text") or [])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not re.fullmatch(r"\d{1,4}", text):
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0.0
        if conf < 35:
            continue
        left, top = int(data["left"][i]) + x0, int(data["top"][i]) + y0
        width, height = int(data["width"][i]), int(data["height"][i])
        # Far-right numbers only; skip header leftovers and mid-sheet digits.
        if left < int(w * 0.88):
            continue
        if top < int(h * 0.16):
            continue
        boxes.append((left - 2, top - 2, width + 4, height + 4))
    return boxes


def _remove_quantity(rgb: np.ndarray) -> Tuple[np.ndarray, int]:
    """White-out right-column quantity numbers (e.g. 60 / 150 / 148). Leave BK/BR/BG."""
    import cv2

    out = rgb.copy()
    h, w = out.shape[:2]
    gray = cv2.cvtColor(out, cv2.COLOR_RGB2GRAY)
    x0 = int(w * 0.70)
    y0 = int(h * 0.16)
    crop = gray[y0:h, x0:w]
    binary = cv2.adaptiveThreshold(
        crop, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 21, 10
    )
    binary = cv2.medianBlur(binary, 3)
    # Join split strokes of a single digit / adjacent digits without merging color codes.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, int(w * 0.006)), 1))
    binary = cv2.dilate(binary, kernel, iterations=1)

    comps = _char_like_components(binary, w, h, x0, y0)
    # Quantities sit at the far right of the color column; color codes (BK/BR/BG) are left of them.
    far = [b for b in comps if (b[0] + b[2] / 2) >= w * 0.92]
    row_gap = max(10, int(h * 0.028))
    x_gap = max(14, int(w * 0.022))
    qty_boxes: List[Tuple[int, int, int, int]] = []
    for row in _cluster_rows(far, row_gap):
        cluster = _rightmost_x_cluster(row, x_gap)
        if not cluster:
            continue
        mean_x = sum(b[0] + b[2] / 2 for b in cluster) / len(cluster)
        right_edge = max(b[0] + b[2] for b in cluster)
        # Right-aligned digit groups (2–4 chars). Drop bag-texture specks and color codes.
        if mean_x < w * 0.93:
            continue
        if right_edge < w * 0.95:
            continue
        xs = min(b[0] for b in cluster)
        ys = min(b[1] for b in cluster)
        xe = max(b[0] + b[2] for b in cluster)
        ye = max(b[1] + b[3] for b in cluster)
        union_w, union_h = xe - xs, ye - ys
        # 2–4 digit quantities, possibly merged into 1 blob after dilation.
        if union_h <= 0 or union_w < union_h * 0.9:
            continue
        if union_w > union_h * 4.2:
            continue
        pad = max(2, int((ye - ys) * 0.18))
        qty_boxes.append((xs - pad, ys - pad, (xe - xs) + 2 * pad, (ye - ys) + 2 * pad))

    if not qty_boxes:
        qty_boxes.extend(_footer_row_qty_boxes(gray))

    if not qty_boxes:
        qty_boxes.extend(_tesseract_qty_boxes(out))

    if not qty_boxes:
        return out, 0

    for x, y, bw, bh in qty_boxes:
        x0b, y0b = max(0, x), max(0, y)
        x1b, y1b = min(w, x + bw), min(h, y + bh)
        out[y0b:y1b, x0b:x1b] = 255
    return out, len(qty_boxes)


def process_catalog_image(
    src: Any, remove_price: bool, remove_quantity: bool
) -> Tuple[Image.Image, Dict[str, int]]:
    img = _open_rgb(src)
    rgb = np.array(img)
    stats = {"price_boxes": 0, "qty_boxes": 0}
    if remove_price:
        rgb, n = _remove_price(rgb)
        stats["price_boxes"] = n
    if remove_quantity:
        rgb, n = _remove_quantity(rgb)
        stats["qty_boxes"] = n
    return Image.fromarray(rgb), stats


def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    canvas_im = Image.new("RGB", (box_w, box_h), (255, 255, 255))
    if img.width <= 0 or img.height <= 0:
        return canvas_im
    scale = min(box_w / img.width, box_h / img.height)
    nw = max(1, int(round(img.width * scale)))
    nh = max(1, int(round(img.height * scale)))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas_im.paste(resized, ((box_w - nw) // 2, (box_h - nh) // 2))
    return canvas_im


def _fit_contain_oriented(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """Fit into the cell, rotating 90° when the sheet does not match the cell."""
    if img.width <= 0 or img.height <= 0:
        return _fit_contain(img, box_w, box_h)
    img_land = img.width >= img.height
    box_land = box_w >= box_h
    if img_land != box_land:
        img = img.transpose(Image.Transpose.ROTATE_90)
    return _fit_contain(img, box_w, box_h)


def compose_letter_pages(
    images: Sequence[Image.Image], layout: str
) -> List[Image.Image]:
    """Return 300-DPI RGB pages: 4-in-1, 5-in-1, or 1-in-1 US Letter."""
    layout = _layout_key(layout)
    if layout == "1in1":
        page_w = int(round(LETTER_W_IN * PRINT_DPI))
        page_h = int(round(LETTER_H_IN * PRINT_DPI))
        margin = int(round(0.25 * PRINT_DPI))
        inner_w, inner_h = page_w - 2 * margin, page_h - 2 * margin
        pages: List[Image.Image] = []
        for img in images:
            page = Image.new("RGB", (page_w, page_h), (255, 255, 255))
            fitted = _fit_contain(img, inner_w, inner_h)
            page.paste(fitted, (margin, margin))
            pages.append(page)
        return pages or [Image.new("RGB", (page_w, page_h), (255, 255, 255))]

    if layout == "5in1":
        page_w = int(round(LETTER_W_IN * PRINT_DPI))
        page_h = int(round(LETTER_H_IN * PRINT_DPI))
        short_px = int(round(FIVE_SHORT_IN * PRINT_DPI))
        long_px = int(round(FIVE_LONG_IN * PRINT_DPI))
        pages = []
        batch: List[Image.Image] = []
        for img in images:
            batch.append(img)
            if len(batch) == 5:
                pages.append(_compose_five(batch, page_w, page_h, short_px, long_px))
                batch = []
        if batch:
            pages.append(_compose_five(batch, page_w, page_h, short_px, long_px))
        return pages or [Image.new("RGB", (page_w, page_h), (255, 255, 255))]

    # 4-in-1 on landscape Letter: 11" wide × 8.5" tall.
    page_w = int(round(LETTER_H_IN * PRINT_DPI))  # 11"
    page_h = int(round(LETTER_W_IN * PRINT_DPI))  # 8.5"
    cell_w = int(round(CELL_W_IN * PRINT_DPI))
    cell_h = int(round(CELL_H_IN * PRINT_DPI))
    pages = []
    batch: List[Image.Image] = []
    for img in images:
        batch.append(img)
        if len(batch) == 4:
            pages.append(_compose_four(batch, page_w, page_h, cell_w, cell_h))
            batch = []
    if batch:
        pages.append(_compose_four(batch, page_w, page_h, cell_w, cell_h))
    return pages or [Image.new("RGB", (page_w, page_h), (255, 255, 255))]


def _compose_four(
    imgs: Sequence[Image.Image], page_w: int, page_h: int, cell_w: int, cell_h: int
) -> Image.Image:
    page = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    # PDF/print origin is bottom-left; PIL origin is top-left. Place 2×2 in the
    # upper-left of the page so leftover white is on the right and bottom.
    positions = [
        (0, 0),
        (cell_w, 0),
        (0, cell_h),
        (cell_w, cell_h),
    ]
    for img, (x, y) in zip(imgs, positions):
        page.paste(_fit_contain(img, cell_w, cell_h), (x, y))
    return page


def _compose_five(
    imgs: Sequence[Image.Image], page_w: int, page_h: int, short_px: int, long_px: int
) -> Image.Image:
    """Two portrait 3.3×4.8 stacked left, three landscape 4.8×3.3 stacked right."""
    page = Image.new("RGB", (page_w, page_h), (255, 255, 255))
    port_w, port_h = short_px, long_px
    land_w, land_h = long_px, short_px
    # Packed to upper-left; leftover Letter white stays on the right and bottom.
    slots = [
        (0, 0, port_w, port_h),
        (0, port_h, port_w, port_h),
        (port_w, 0, land_w, land_h),
        (port_w, land_h, land_w, land_h),
        (port_w, land_h * 2, land_w, land_h),
    ]
    for img, (x, y, w, h) in zip(imgs, slots):
        page.paste(_fit_contain_oriented(img, w, h), (x, y))
    return page


def pages_to_pdf_bytes(pages: Sequence[Image.Image]) -> bytes:
    if not pages:
        raise ValueError("No pages to write.")
    buf = io.BytesIO()
    first = pages[0]
    page_w = first.width / PRINT_DPI * INCH_PT
    page_h = first.height / PRINT_DPI * INCH_PT
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    for page in pages:
        bio = io.BytesIO()
        page.save(bio, format="PNG")
        bio.seek(0)
        pw = page.width / PRINT_DPI * INCH_PT
        ph = page.height / PRINT_DPI * INCH_PT
        if abs(pw - page_w) > 0.5 or abs(ph - page_h) > 0.5:
            c.setPageSize((pw, ph))
        c.drawImage(ImageReader(bio), 0, 0, width=pw, height=ph, preserveAspectRatio=False, mask="auto")
        c.showPage()
    c.save()
    return buf.getvalue()


def pages_to_zip_bytes(pages: Sequence[Image.Image], prefix: str = "tradeshow") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(pages, start=1):
            img_buf = io.BytesIO()
            page.save(img_buf, format="PNG")
            zf.writestr(f"{prefix}_page_{i:03d}.png", img_buf.getvalue())
    buf.seek(0)
    return buf.getvalue()


def _is_image_filename(name: str) -> bool:
    ext = Path(name or "").suffix.lower()
    return ext in ALLOWED_EXTENSIONS


_PING_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _layout_key(layout: str) -> str:
    layout = (layout or "4in1").strip().lower()
    if layout in ("1in1", "1-in-1", "one"):
        return "1in1"
    if layout in ("5in1", "5-in-1", "five"):
        return "5in1"
    return "4in1"


def _export_key(export: str) -> str:
    e = (export or "").strip().lower()
    if e in ("zip", "replace"):
        return e
    return "pdf"


def _dropbox_rel_from_input(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"dropbox\\(.+)", raw, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip("\\/")
    return raw.replace("/", "\\").strip().strip("\\")


def _dropbox_input_value(rel: str) -> str:
    rel = (rel or "").strip().strip("\\")
    return ("dropbox\\" + rel) if rel else ""


def _safe_dropbox_dir(relative_raw: str) -> Tuple[Optional[Path], str]:
    """Resolve a subfolder under LOCAL_DROPBOX_DIR. Same rules as V7 list creator."""
    root = getattr(Config, "LOCAL_DROPBOX_DIR", None) or ""
    if not root:
        return None, "not_configured"
    try:
        base = Path(os.path.abspath(str(root))).resolve()
    except OSError:
        return None, "not_configured"
    rel = _dropbox_rel_from_input(relative_raw)
    parts = [p for p in rel.replace("/", "\\").split("\\") if p and p != "."]
    if any(p == ".." for p in parts):
        return None, "invalid"
    if not parts:
        return None, "invalid"
    try:
        dest = base.joinpath(*parts).resolve(strict=False)
        dest.relative_to(base)
    except (ValueError, OSError):
        return None, "invalid"
    if not dest.exists():
        return None, "not_found"
    if not dest.is_dir():
        return None, "not_dir"
    return dest, "ok"


def _dropbox_err_message(err: str) -> str:
    return {
        "not_configured": "Dropbox folder is not configured on this server.",
        "invalid": "Invalid Dropbox path. Pick a subfolder (not the Dropbox root).",
        "not_found": "Dropbox folder not found.",
        "not_dir": "That Dropbox path is not a folder.",
    }.get(err or "", "Invalid Dropbox folder.")


def _list_dropbox_image_names(dest: Path) -> List[str]:
    """Non-recursive image filenames in `dest` (subfolders are excluded)."""
    try:
        names = os.listdir(dest)
    except OSError:
        return []
    out: List[str] = []
    for name in names:
        if not _is_image_filename(name):
            continue
        try:
            if not (dest / name).is_file():
                continue
        except OSError:
            continue
        out.append(name)
    out.sort(key=lambda n: n.lower())
    return out


def _atomic_save_image(img: Image.Image, dest: Path) -> None:
    ext = dest.suffix.lower()
    tmp = dest.with_name(dest.name + ".tsing.tmp")
    try:
        if ext in (".jpg", ".jpeg"):
            img.save(tmp, format="JPEG", quality=95, subsampling=0)
        elif ext == ".png":
            img.save(tmp, format="PNG")
        elif ext == ".webp":
            img.save(tmp, format="WEBP", quality=90)
        elif ext in (".tif", ".tiff"):
            img.save(tmp, format="TIFF")
        elif ext == ".bmp":
            img.save(tmp, format="BMP")
        else:
            img.save(tmp, format="JPEG", quality=95, subsampling=0)
        os.replace(str(tmp), str(dest))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _init_clean_worker() -> None:
    """One OpenCV/BLAS thread per process so image jobs can occupy separate cores."""
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:
        pass


def _clean_worker_count(n_files: int) -> int:
    n_files = max(1, int(n_files))
    cpu = os.cpu_count() or 4
    # Leave one core for Flask / Dropbox / the OS.
    return max(1, min(n_files, max(1, cpu - 1)))


def _should_replace_file(
    price_n: int, qty_n: int, remove_price: bool, remove_quantity: bool
) -> bool:
    """Replace in Dropbox only when the selected clean-up actually changed something."""
    if not remove_price and not remove_quantity:
        return False
    if remove_price and remove_quantity:
        return price_n > 0 or qty_n > 0
    if remove_price:
        return price_n > 0
    return qty_n > 0


def _process_image_worker(
    payload: Tuple[int, str, str, bool, bool, str],
) -> Tuple[int, str, str, int, int, bool]:
    """Child-process worker: clean one catalog sheet and write it to `out`."""
    index, src, out, remove_price, remove_quantity, save_mode = payload
    try:
        img, stats = process_catalog_image(src, remove_price, remove_quantity)
        price_n = int(stats.get("price_boxes") or 0)
        qty_n = int(stats.get("qty_boxes") or 0)
        dest = Path(out)
        replaced = False
        if save_mode == "replace":
            if _should_replace_file(price_n, qty_n, remove_price, remove_quantity):
                _atomic_save_image(img, dest)
                replaced = True
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            img.save(dest, format="JPEG", quality=95, subsampling=0)
            replaced = True
        img.close()
        return index, "ok", "", price_n, qty_n, replaced
    except Exception as exc:
        return index, "err", str(exc) or "failed", 0, 0, False


def _clean_images_parallel(
    pairs: Sequence[Tuple[Path, Path]],
    *,
    remove_price: bool,
    remove_quantity: bool,
    export: str,
    cleaned_dir: Path,
    on_progress: Callable[[int, int, int], None],
) -> Tuple[List[Path], List[Dict[str, Any]]]:
    """Clean catalog sheets across processes. Returns output paths and per-file stats."""
    save_mode = "replace" if export == "replace" else "jpeg"
    tasks: List[Tuple[int, str, str, bool, bool, str]] = []
    outs: List[Path] = []
    for i, (src, dest) in enumerate(pairs):
        out = dest if save_mode == "replace" else (cleaned_dir / f"{i:05d}.jpg")
        outs.append(out)
        tasks.append((i, str(src), str(out), remove_price, remove_quantity, save_mode))

    total = len(tasks)
    workers = _clean_worker_count(total)
    errors: List[str] = []
    file_stats: List[Optional[Dict[str, Any]]] = [None] * total

    def _record(
        idx: int, status: str, err: str, price_n: int, qty_n: int, replaced: bool
    ) -> None:
        name = Path(tasks[idx][1]).name
        file_stats[idx] = {
            "file": name,
            "ok": status == "ok",
            "price_boxes": int(price_n or 0),
            "qty_boxes": int(qty_n or 0),
            "replaced": bool(replaced),
            "error": err or "",
        }
        if status != "ok":
            errors.append(f"{name}: {err}" if err else name)

    if workers <= 1 or total <= 1:
        _init_clean_worker()
        for i, task in enumerate(tasks, start=1):
            idx, status, err, price_n, qty_n, replaced = _process_image_worker(task)
            _record(idx, status, err, price_n, qty_n, replaced)
            on_progress(i, total, workers)
    else:
        def _drain(futures) -> None:
            nonlocal done
            for fut in as_completed(futures):
                idx, status, err, price_n, qty_n, replaced = fut.result()
                _record(idx, status, err, price_n, qty_n, replaced)
                done += 1
                on_progress(done, total, workers)

        done = 0
        try:
            with ProcessPoolExecutor(
                max_workers=workers, initializer=_init_clean_worker
            ) as pool:
                _drain([pool.submit(_process_image_worker, task) for task in tasks])
        except Exception:
            if done:
                raise
            _init_clean_worker()
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                _drain([pool.submit(_process_image_worker, task) for task in tasks])

    if errors:
        extra = f"{len(errors)} images failed. " if len(errors) > 1 else ""
        raise ValueError(extra + errors[0])
    return outs, [s for s in file_stats if s]


def _lan_origin() -> str:
    from py.utilities.setting import Config

    return (getattr(Config, "LAN_ORIGIN_HINT", "") or "http://192.168.254.30:5555").rstrip("/")


def _on_lan_host() -> bool:
    from py.utilities.lan_detection import host_is_private_lan

    return host_is_private_lan(request.host)


def _tool_root() -> Path:
    root = Path(current_app.instance_path) / "tradeshowtool"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _job_dir(job_id: str) -> Path:
    return _tool_root() / "jobs" / job_id


def _is_win_lock_err(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    win = getattr(exc, "winerror", None)
    return win in (5, 32)  # Access denied / sharing violation


_JOB_META_LOCK = threading.Lock()


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Dict[str, Any], *, optional: bool = False) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
        last_err: Optional[OSError] = None
        for attempt in range(8):
            try:
                os.replace(str(tmp), str(path))
                return
            except OSError as exc:
                last_err = exc
                if not _is_win_lock_err(exc):
                    raise
                time.sleep(0.04 * (attempt + 1))
        if optional:
            return
        if last_err is not None:
            raise last_err
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _load_job(job_id: str) -> Dict[str, Any]:
    meta_path = _job_dir(job_id) / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError("Job not found.")
    last_err: Optional[BaseException] = None
    for attempt in range(8):
        try:
            with _JOB_META_LOCK:
                return _read_json(meta_path)
        except (OSError, json.JSONDecodeError) as exc:
            last_err = exc
            if isinstance(exc, OSError) and not _is_win_lock_err(exc):
                raise
            time.sleep(0.04 * (attempt + 1))
    if last_err is not None:
        raise last_err
    raise FileNotFoundError("Job not found.")


def _save_job(job_id: str, meta: Dict[str, Any], *, optional: bool = False) -> None:
    with _JOB_META_LOCK:
        _write_json(_job_dir(job_id) / "meta.json", meta, optional=optional)


def _job_owned(meta: Dict[str, Any]) -> bool:
    return (meta.get("user") or "") == (session.get("username") or "")


def _cleanup_old_jobs() -> None:
    jobs = _tool_root() / "jobs"
    if not jobs.is_dir():
        return
    now = time.time()
    for path in jobs.iterdir():
        if not path.is_dir():
            continue
        try:
            age = now - path.stat().st_mtime
            if age > JOB_TTL_SEC:
                import shutil

                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _save_page_image(page: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    page.save(dest, format="PNG", optimize=False)


def _write_pdf_from_pages(page_paths: Sequence[Path], dest: Path) -> None:
    if not page_paths:
        raise ValueError("No pages to write.")
    first = Image.open(page_paths[0])
    page_w = first.width / PRINT_DPI * INCH_PT
    page_h = first.height / PRINT_DPI * INCH_PT
    first.close()
    c = canvas.Canvas(str(dest), pagesize=(page_w, page_h))
    for path in page_paths:
        img = Image.open(path).convert("RGB")
        bio = io.BytesIO()
        img.save(bio, format="JPEG", quality=92, optimize=True)
        bio.seek(0)
        pw = img.width / PRINT_DPI * INCH_PT
        ph = img.height / PRINT_DPI * INCH_PT
        img.close()
        if abs(pw - page_w) > 0.5 or abs(ph - page_h) > 0.5:
            c.setPageSize((pw, ph))
        c.drawImage(ImageReader(bio), 0, 0, width=pw, height=ph, preserveAspectRatio=False, mask="auto")
        c.showPage()
    c.save()


def _write_zip_from_pages(page_paths: Sequence[Path], dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, path in enumerate(page_paths, start=1):
            zf.write(path, arcname=f"tradeshow_page_{i:03d}.png")


def _job_source_files(job_id: str, meta: Dict[str, Any]) -> List[Tuple[Path, Path]]:
    """Return (read_path, replace_path) pairs. replace_path is the Dropbox file when replacing."""
    dropbox_raw = (meta.get("dropbox_path") or "").strip()
    dest_dir: Optional[Path] = None
    if dropbox_raw:
        dest_dir, err = _safe_dropbox_dir(dropbox_raw)
        if err != "ok" or dest_dir is None:
            raise ValueError(_dropbox_err_message(err))

    source = (meta.get("source") or "").strip().lower()
    if source == "dropbox":
        if dest_dir is None:
            raise ValueError("Select a Dropbox folder.")
        names = _list_dropbox_image_names(dest_dir)
        if not names:
            raise ValueError("No JPG/PNG images were found in that Dropbox folder.")
        if len(names) > MAX_FILES:
            raise ValueError(f"Too many images (max {MAX_FILES}).")
        pairs = []
        for name in names:
            fp = dest_dir / name
            pairs.append((fp, fp))
        return pairs

    inbox = _job_dir(job_id) / "inbox"
    if not inbox.is_dir():
        return []
    files = sorted(p for p in inbox.iterdir() if p.is_file())
    pairs = []
    for src in files:
        name = src.name
        orig = name.split("_", 1)[1] if "_" in name else name
        replace_path = (dest_dir / orig) if dest_dir is not None else src
        pairs.append((src, replace_path))
    return pairs


def _format_elapsed(sec: float) -> str:
    sec_i = max(0, int(round(sec)))
    if sec_i < 60:
        return f"{sec_i} sec"
    minutes, rem = divmod(sec_i, 60)
    if minutes < 60:
        return f"{minutes} min {rem} sec" if rem else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} hr {minutes} min"


def _build_job_report(
    *,
    total: int,
    pages: int,
    layout: str,
    export: str,
    remove_price: bool,
    remove_quantity: bool,
    cores: int,
    elapsed_sec: float,
    dropbox_path: str,
    filename: str,
    file_stats: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    price_boxes = sum(int(s.get("price_boxes") or 0) for s in file_stats)
    qty_boxes = sum(int(s.get("qty_boxes") or 0) for s in file_stats)
    price_hit = sum(1 for s in file_stats if int(s.get("price_boxes") or 0) > 0)
    qty_hit = sum(1 for s in file_stats if int(s.get("qty_boxes") or 0) > 0)
    replaced_count = sum(1 for s in file_stats if s.get("replaced"))
    skipped = [
        str(s.get("file") or "")
        for s in file_stats
        if s.get("ok") and not s.get("replaced")
    ]
    skipped = [n for n in skipped if n]
    no_price = [
        str(s.get("file") or "")
        for s in file_stats
        if remove_price and int(s.get("price_boxes") or 0) <= 0
    ]
    no_qty = [
        str(s.get("file") or "")
        for s in file_stats
        if remove_quantity and int(s.get("qty_boxes") or 0) <= 0
    ]
    no_price = [n for n in no_price if n]
    no_qty = [n for n in no_qty if n]
    out_pages = replaced_count if export == "replace" else pages
    return {
        "images": total,
        "pages": out_pages,
        "images_replaced": replaced_count,
        "images_skipped": len(skipped),
        "layout": layout,
        "export": export,
        "remove_price": bool(remove_price),
        "remove_quantity": bool(remove_quantity),
        "cores": cores,
        "elapsed_sec": round(float(elapsed_sec), 1),
        "elapsed": _format_elapsed(elapsed_sec),
        "dropbox_path": dropbox_path or "",
        "filename": filename or "",
        "price_boxes": price_boxes,
        "qty_boxes": qty_boxes,
        "images_price_cleared": price_hit,
        "images_qty_cleared": qty_hit,
        "images_skipped_list": skipped[:40],
        "images_skipped_more": max(0, len(skipped) - 40),
        "images_no_price": no_price[:40],
        "images_no_price_more": max(0, len(no_price) - 40),
        "images_no_qty": no_qty[:40],
        "images_no_qty_more": max(0, len(no_qty) - 40),
    }


def _run_job(app, job_id: str) -> None:
    with app.app_context():
        try:
            meta = _load_job(job_id)
            pages_dir = _job_dir(job_id) / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            pairs = _job_source_files(job_id, meta)
            layout = _layout_key(str(meta.get("layout") or "4in1"))
            export = _export_key(str(meta.get("export") or "pdf"))
            remove_price = bool(meta.get("remove_price"))
            remove_quantity = bool(meta.get("remove_quantity"))
            total = len(pairs)
            if total < 1:
                raise ValueError("No pictures were found.")
            t0 = time.time()
            meta["state"] = "running"
            meta["total"] = total
            meta["processed"] = 0
            meta["pages"] = 0
            cores = _clean_worker_count(total)
            meta["message"] = f"Processing 0/{total} ({cores} core" + ("s" if cores != 1 else "") + ")…"
            _save_job(job_id, meta)

            cleaned_dir = _job_dir(job_id) / "cleaned"
            cleaned_dir.mkdir(parents=True, exist_ok=True)

            def on_progress(done: int, n: int, workers: int) -> None:
                now = time.time()
                if done < n and now - getattr(on_progress, "_last", 0.0) < 0.45:
                    return
                on_progress._last = now
                try:
                    latest = _load_job(job_id)
                    latest["processed"] = done
                    if export == "replace":
                        latest["pages"] = done
                    core_label = f"{workers} core" + ("s" if workers != 1 else "")
                    verb = "Replacing" if export == "replace" else "Processing"
                    latest["message"] = f"{verb} {done}/{n} ({core_label})…"
                    _save_job(job_id, latest, optional=True)
                except OSError:
                    return

            out_paths, file_stats = _clean_images_parallel(
                pairs,
                remove_price=remove_price,
                remove_quantity=remove_quantity,
                export=export,
                cleaned_dir=cleaned_dir,
                on_progress=on_progress,
            )
            dropbox_label = (meta.get("dropbox_path") or "").strip()

            if export == "replace":
                replaced_n = sum(1 for s in file_stats if s.get("replaced"))
                skipped_n = total - replaced_n
                report = _build_job_report(
                    total=total,
                    pages=replaced_n,
                    layout=layout,
                    export=export,
                    remove_price=remove_price,
                    remove_quantity=remove_quantity,
                    cores=cores,
                    elapsed_sec=time.time() - t0,
                    dropbox_path=dropbox_label,
                    filename="",
                    file_stats=file_stats,
                )
                meta = _load_job(job_id)
                meta["state"] = "done"
                meta["output"] = ""
                meta["filename"] = ""
                meta["mime"] = ""
                meta["replaced"] = replaced_n
                meta["pages"] = replaced_n
                meta["report"] = report
                msg = (
                    f"Replaced {replaced_n} picture" + ("" if replaced_n == 1 else "s")
                    + f" in {dropbox_label or 'the Dropbox folder'}"
                )
                if skipped_n:
                    msg += f" ({skipped_n} skipped — nothing to clean)"
                msg += "."
                meta["message"] = msg
                _save_job(job_id, meta)
                return

            page_paths: List[Path] = []
            batch: List[Image.Image] = []
            per_page = 1
            if layout == "4in1":
                per_page = 4
                page_w = int(round(LETTER_H_IN * PRINT_DPI))
                page_h = int(round(LETTER_W_IN * PRINT_DPI))
            elif layout == "5in1":
                per_page = 5
                page_w = int(round(LETTER_W_IN * PRINT_DPI))
                page_h = int(round(LETTER_H_IN * PRINT_DPI))
            else:
                page_w = int(round(LETTER_W_IN * PRINT_DPI))
                page_h = int(round(LETTER_H_IN * PRINT_DPI))
            cell_w = int(round(CELL_W_IN * PRINT_DPI))
            cell_h = int(round(CELL_H_IN * PRINT_DPI))
            five_short = int(round(FIVE_SHORT_IN * PRINT_DPI))
            five_long = int(round(FIVE_LONG_IN * PRINT_DPI))
            margin = int(round(0.25 * PRINT_DPI))

            def flush_batch(imgs: List[Image.Image]) -> None:
                if not imgs:
                    return
                if layout == "5in1":
                    page = _compose_five(imgs, page_w, page_h, five_short, five_long)
                else:
                    page = _compose_four(imgs, page_w, page_h, cell_w, cell_h)
                dest = pages_dir / f"page_{len(page_paths) + 1:03d}.png"
                _save_page_image(page, dest)
                page_paths.append(dest)
                for im in imgs:
                    im.close()
                imgs.clear()

            def flush_one(img: Image.Image) -> None:
                page = Image.new("RGB", (page_w, page_h), (255, 255, 255))
                fitted = _fit_contain(img, page_w - 2 * margin, page_h - 2 * margin)
                page.paste(fitted, (margin, margin))
                dest = pages_dir / f"page_{len(page_paths) + 1:03d}.png"
                _save_page_image(page, dest)
                page_paths.append(dest)
                img.close()

            meta = _load_job(job_id)
            meta["message"] = "Laying out Letter pages…"
            _save_job(job_id, meta)

            for i, cleaned in enumerate(out_paths, start=1):
                img = _open_rgb(cleaned)
                if per_page == 1:
                    flush_one(img)
                else:
                    batch.append(img)
                    if len(batch) == per_page:
                        flush_batch(batch)
                if i == total or i % 8 == 0:
                    try:
                        latest = _load_job(job_id)
                        latest["processed"] = total
                        latest["pages"] = len(page_paths)
                        latest["message"] = f"Laying out pages ({len(page_paths)} so far)…"
                        _save_job(job_id, latest, optional=True)
                    except OSError:
                        pass

            if batch:
                flush_batch(batch)

            if not page_paths:
                raise ValueError("No pages were produced.")

            meta = _load_job(job_id)
            meta["message"] = "Building download…"
            meta["pages"] = len(page_paths)
            _save_job(job_id, meta)

            if export == "zip":
                out_path = _job_dir(job_id) / "tradeshow_pages.zip"
                _write_zip_from_pages(page_paths, out_path)
                filename = "tradeshow_pages.zip"
                mime = "application/zip"
            else:
                out_path = _job_dir(job_id) / "tradeshow_pages.pdf"
                _write_pdf_from_pages(page_paths, out_path)
                filename = "tradeshow_pages.pdf"
                mime = "application/pdf"

            meta = _load_job(job_id)
            meta["state"] = "done"
            meta["output"] = out_path.name
            meta["filename"] = filename
            meta["mime"] = mime
            meta["pages"] = len(page_paths)
            meta["report"] = _build_job_report(
                total=total,
                pages=len(page_paths),
                layout=layout,
                export=export,
                remove_price=remove_price,
                remove_quantity=remove_quantity,
                cores=cores,
                elapsed_sec=time.time() - t0,
                dropbox_path=dropbox_label,
                filename=filename,
                file_stats=file_stats,
            )
            meta["message"] = f"Done — {len(page_paths)} Letter page" + (
                "" if len(page_paths) == 1 else "s"
            )
            _save_job(job_id, meta)
        except Exception as exc:
            current_app.logger.exception("tradeshowtool job %s failed", job_id)
            try:
                meta = _load_job(job_id)
                meta["state"] = "error"
                meta["error"] = str(exc) or "Processing failed."
                meta["message"] = meta["error"]
                _save_job(job_id, meta)
            except Exception:
                pass


def _cors_ping(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@tradeshowtool_bp.route("/tradeshowtool/lan-ping", methods=["GET", "OPTIONS"])
def lan_ping():
    if request.method == "OPTIONS":
        return _cors_ping(current_app.response_class(status=204))
    return _cors_ping(jsonify(ok=True, app="fdc-tradeshow", lan=True))


@tradeshowtool_bp.route("/tradeshowtool/lan-ping.png")
def lan_ping_png():
    resp = send_file(io.BytesIO(_PING_PNG), mimetype="image/png")
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@tradeshowtool_bp.route("/tradeshowtool/lan-handoff", methods=["POST"])
@login_required
def lan_handoff():
    user = (session.get("username") or "").strip()
    if not user:
        return jsonify(ok=False, error="Not signed in."), 401
    token = secrets.token_urlsafe(24)
    folder = _tool_root() / "handoff"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"user": user, "exp": time.time() + HANDOFF_TTL_SEC}
    _write_json(folder / f"{token}.json", payload)
    lan = _lan_origin()
    return jsonify(
        ok=True,
        token=token,
        lan_origin=lan,
        lan_claim_url=f"{lan}/tradeshowtool/lan-claim?token={token}",
    )


@tradeshowtool_bp.route("/tradeshowtool/lan-claim")
def lan_claim():
    token = (request.args.get("token") or "").strip()
    if not token or "/" in token or "\\" in token:
        return redirect(url_for("auth.login"))
    path = _tool_root() / "handoff" / f"{token}.json"
    if not path.is_file():
        return redirect(url_for("auth.login"))
    data = _read_json(path)
    try:
        path.unlink()
    except OSError:
        pass
    user = (data.get("user") or "").strip()
    exp = float(data.get("exp") or 0)
    if not user or time.time() > exp:
        return redirect(url_for("auth.login"))
    session["username"] = user
    session.permanent = True
    return redirect(url_for("tradeshowtool.tradeshowtool_page"))


@tradeshowtool_bp.route("/tradeshowtool")
@login_required
def tradeshowtool_page():
    from py.utilities.lan_detection import host_is_private_lan

    return render_template(
        "tradeshowtool.html",
        current_user=session.get("username"),
        lan_origin=_lan_origin(),
        on_lan=host_is_private_lan(request.host),
        max_files=MAX_FILES,
        v7_local_dropbox_dir=getattr(Config, "LOCAL_DROPBOX_DIR", "") or "",
    )


@tradeshowtool_bp.route("/tradeshowtool/dropbox_folder", methods=["GET"])
@login_required
def dropbox_folder():
    """List image filenames in a server Dropbox subfolder (same path rules as V7)."""
    raw = (request.args.get("path") or "").strip()
    dest, err = _safe_dropbox_dir(raw)
    if err != "ok" or dest is None:
        code = 503 if err == "not_configured" else 400
        return jsonify(ok=False, error=_dropbox_err_message(err)), code
    names = _list_dropbox_image_names(dest)
    rel = _dropbox_rel_from_input(raw)
    return jsonify(
        ok=True,
        dropbox_path=_dropbox_input_value(rel),
        count=len(names),
        files=names[:12],
        more=max(0, len(names) - 12),
    )


@tradeshowtool_bp.route("/tradeshowtool/job/start", methods=["POST"])
@login_required
def job_start():
    _cleanup_old_jobs()
    layout = _layout_key(request.form.get("layout") or (request.json or {}).get("layout") or "4in1")
    export = _export_key(request.form.get("export") or (request.json or {}).get("export") or "pdf")
    src = request.form if request.form else (request.json or {})
    remove_price = str(src.get("remove_price") or "") in ("1", "true", "on", "yes")
    remove_quantity = str(src.get("remove_quantity") or "") in ("1", "true", "on", "yes")
    dropbox_raw = (src.get("dropbox_path") or src.get("list_input") or "").strip()
    source = "upload"
    uploaded = 0
    expected = int(src.get("expected") or 0)
    dropbox_path = ""
    if dropbox_raw:
        dest, err = _safe_dropbox_dir(dropbox_raw)
        if err != "ok" or dest is None:
            return jsonify(ok=False, error=_dropbox_err_message(err)), 400
        names = _list_dropbox_image_names(dest)
        if not names:
            return jsonify(ok=False, error="No JPG/PNG images were found in that Dropbox folder."), 400
        if len(names) > MAX_FILES:
            return jsonify(ok=False, error=f"Too many images (max {MAX_FILES})."), 400
        source = "dropbox"
        dropbox_path = _dropbox_input_value(_dropbox_rel_from_input(dropbox_raw))
        uploaded = len(names)
        expected = len(names)
        current_app.logger.info(
            "tradeshowtool dropbox folder %s (%s images) by %s",
            dropbox_path,
            expected,
            session.get("username"),
        )
    elif export == "replace":
        return jsonify(ok=False, error="Select a Dropbox folder to replace pictures in it."), 400
    if expected > MAX_FILES:
        return jsonify(ok=False, error=f"Too many images (max {MAX_FILES})."), 400
    job_id = uuid.uuid4().hex
    inbox = _job_dir(job_id) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": job_id,
        "user": session.get("username"),
        "state": "uploading" if source == "upload" else "queued",
        "source": source,
        "dropbox_path": dropbox_path,
        "layout": layout,
        "export": export,
        "remove_price": remove_price,
        "remove_quantity": remove_quantity,
        "expected": expected,
        "uploaded": uploaded,
        "processed": 0,
        "pages": 0,
        "bytes": 0,
        "message": (
            f"Using {uploaded} picture(s) from {dropbox_path}…"
            if source == "dropbox"
            else "Waiting for pictures…"
        ),
        "created": time.time(),
        "lan": _on_lan_host(),
    }
    _save_job(job_id, meta)
    return jsonify(ok=True, job_id=job_id, source=source, uploaded=uploaded)


@tradeshowtool_bp.route("/tradeshowtool/job/<job_id>/upload", methods=["POST"])
@login_required
def job_upload(job_id: str):
    try:
        meta = _load_job(job_id)
    except FileNotFoundError:
        return jsonify(ok=False, error="Job not found."), 404
    if not _job_owned(meta):
        return jsonify(ok=False, error="Not your job."), 403
    if meta.get("state") not in ("uploading",):
        return jsonify(ok=False, error="This job is no longer accepting files."), 400
    inbox = _job_dir(job_id) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    uploaded = int(meta.get("uploaded") or 0)
    total_bytes = int(meta.get("bytes") or 0)
    files = request.files.getlist("files") or request.files.getlist("folder")
    added = 0
    for fs in files:
        if fs is None or not (fs.filename or "").strip():
            continue
        rel = (fs.filename or "").replace("\\", "/")
        base = os.path.basename(rel)
        if not _is_image_filename(base):
            continue
        if uploaded + added >= MAX_FILES:
            return jsonify(ok=False, error=f"Too many images (max {MAX_FILES})."), 400
        # Stream to disk; do not keep the whole batch in RAM.
        seq = uploaded + added + 1
        safe = secure_filename(base) or f"image{seq}.jpg"
        dest = inbox / f"{seq:05d}_{safe}"
        size = 0
        with dest.open("wb") as out:
            while True:
                chunk = fs.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return jsonify(
                        ok=False,
                        error=f"{base} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.",
                    ), 400
                out.write(chunk)
        if size == 0:
            dest.unlink(missing_ok=True)
            continue
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            dest.unlink(missing_ok=True)
            return jsonify(ok=False, error="Total upload is too large (max 2 GB)."), 400
        added += 1
    meta = _load_job(job_id)
    meta["uploaded"] = uploaded + added
    meta["bytes"] = total_bytes
    meta["message"] = f"Uploaded {meta['uploaded']} picture(s)…"
    _save_job(job_id, meta)
    return jsonify(ok=True, uploaded=meta["uploaded"], added=added)


@tradeshowtool_bp.route("/tradeshowtool/job/<job_id>/run", methods=["POST"])
@login_required
def job_run(job_id: str):
    try:
        meta = _load_job(job_id)
    except FileNotFoundError:
        return jsonify(ok=False, error="Job not found."), 404
    if not _job_owned(meta):
        return jsonify(ok=False, error="Not your job."), 403
    if int(meta.get("uploaded") or 0) < 1 and (meta.get("source") or "") != "dropbox":
        return jsonify(ok=False, error="No pictures were uploaded."), 400
    if meta.get("state") not in ("uploading", "queued"):
        return jsonify(ok=False, error="Job already started."), 400
    meta["state"] = "queued"
    meta["message"] = "Starting…"
    _save_job(job_id, meta)
    app = current_app._get_current_object()
    threading.Thread(target=_run_job, args=(app, job_id), daemon=False).start()
    return jsonify(ok=True, job_id=job_id)


@tradeshowtool_bp.route("/tradeshowtool/job/<job_id>/status")
@login_required
def job_status(job_id: str):
    try:
        meta = _load_job(job_id)
    except FileNotFoundError:
        return jsonify(ok=False, error="Job not found."), 404
    except (OSError, json.JSONDecodeError):
        return jsonify(
            ok=True,
            state="running",
            uploaded=0,
            processed=0,
            pages=0,
            total=0,
            message="Processing…",
            error="",
        )
    if not _job_owned(meta):
        return jsonify(ok=False, error="Not your job."), 403
    return jsonify(
        ok=True,
        state=meta.get("state"),
        uploaded=meta.get("uploaded") or 0,
        processed=meta.get("processed") or 0,
        pages=meta.get("pages") or 0,
        total=meta.get("total") or meta.get("uploaded") or 0,
        message=meta.get("message") or "",
        error=meta.get("error") or "",
        lan=bool(meta.get("lan")),
        export=meta.get("export") or "pdf",
        dropbox_path=meta.get("dropbox_path") or "",
        replaced=meta.get("replaced") or 0,
        filename=meta.get("filename") or "",
        report=meta.get("report") or {},
    )


@tradeshowtool_bp.route("/tradeshowtool/job/<job_id>/download")
@login_required
def job_download(job_id: str):
    try:
        meta = _load_job(job_id)
    except FileNotFoundError:
        return jsonify(ok=False, error="Job not found."), 404
    if not _job_owned(meta):
        return jsonify(ok=False, error="Not your job."), 403
    if meta.get("state") != "done":
        return jsonify(ok=False, error="Not ready yet."), 400
    if (meta.get("export") or "") == "replace" or not (meta.get("output") or ""):
        return jsonify(ok=True, replaced=True, message=meta.get("message") or "Replaced in Dropbox folder.")
    out_name = meta.get("output") or ""
    path = _job_dir(job_id) / out_name
    if not path.is_file():
        return jsonify(ok=False, error="Output file is missing."), 404
    return send_file(
        path,
        mimetype=meta.get("mime") or "application/octet-stream",
        as_attachment=True,
        download_name=meta.get("filename") or out_name,
    )
