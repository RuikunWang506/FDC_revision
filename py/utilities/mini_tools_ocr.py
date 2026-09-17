"""Server-side OCR for Mini Tools: crop, enhance, then Tesseract."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image

TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
)

PROJECT_TESSDATA = Path(__file__).resolve().parents[2] / "instance" / "tessdata"
MAX_OCR_SIDE = 2200
MIN_OCR_SIDE = 1400


def find_tesseract() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    for path in TESSERACT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def tessdata_dir() -> str:
    if (PROJECT_TESSDATA / "eng.traineddata").is_file():
        return str(PROJECT_TESSDATA)
    exe = find_tesseract()
    if exe:
        bundled = Path(exe).parent / "tessdata"
        if (bundled / "eng.traineddata").is_file():
            return str(bundled)
    return ""


def tesseract_available() -> bool:
    return bool(find_tesseract())


def map_lang(requested: str) -> str:
    raw = (requested or "eng").strip().lower().replace(" ", "")
    data_dir = Path(tessdata_dir()) if tessdata_dir() else None
    has_chi = bool(data_dir and (data_dir / "chi_sim.traineddata").is_file())
    if raw in {"chi", "chi_sim", "zh", "zh-cn", "chinese"}:
        return "chi_sim+eng" if has_chi else "eng"
    if "chi" in raw or "zh" in raw:
        return "chi_sim+eng" if has_chi else "eng"
    return "eng"


def _decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("Could not read that image.")
    return bgr


def _scale_for_ocr(bgr: np.ndarray) -> np.ndarray:
    h, w = bgr.shape[:2]
    side = min(h, w)
    if side < MIN_OCR_SIDE:
        scale = MIN_OCR_SIDE / float(side)
    elif max(h, w) > MAX_OCR_SIDE:
        scale = MAX_OCR_SIDE / float(max(h, w))
    else:
        scale = 1.0
    if abs(scale - 1.0) < 0.05:
        return bgr
    interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    return cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=interp)


def _best_rect(mask: np.ndarray, min_frac: float = 0.06) -> Optional[Tuple[int, int, int, int]]:
    h, w = mask.shape[:2]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = 0.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_frac * h * w:
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        if bw < 24 or bh < 24:
            continue
        aspect = bw / float(bh)
        fill = area / float(max(1, bw * bh))
        if fill < 0.45:
            continue
        booklet = 0.45 <= aspect <= 0.95 or 1.05 <= aspect <= 2.3
        score = area * (1.35 if booklet else 0.7)
        if score > best_score:
            best_score = score
            best = (x, y, bw, bh)
    return best


def _apply_rect(bgr: np.ndarray, rect: Tuple[int, int, int, int], pad_frac: float = 0.04) -> np.ndarray:
    h, w = bgr.shape[:2]
    x, y, bw, bh = rect
    pad = int(pad_frac * max(bw, bh))
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    crop = bgr[y0:y1, x0:x1]
    if crop.size == 0 or crop.shape[0] < 40 or crop.shape[1] < 40:
        return bgr
    return crop


def crop_document(bgr: np.ndarray) -> np.ndarray:
    """Crop to a navy booklet / dark label when it sits on a busy background."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    navy = cv2.inRange(hsv, (90, 20, 12), (140, 255, 165))
    rect = _best_rect(navy, min_frac=0.05)
    if rect:
        return _apply_rect(bgr, rect)
    dark = cv2.inRange(hsv, (0, 20, 8), (180, 255, 160))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    dark = cv2.bitwise_or(dark, cv2.threshold(gray, 90, 255, cv2.THRESH_BINARY_INV)[1])
    rect = _best_rect(dark, min_frac=0.08)
    if rect:
        return _apply_rect(bgr, rect)
    return bgr


def _clahe_gray(bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _foil_mask(bgr: np.ndarray) -> np.ndarray:
    """Gold / light lettering on a dark cover, including shadowed foil."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (5, 15, 35), (50, 255, 255))
    pale = cv2.inRange(hsv, (0, 0, 130), (55, 110, 255))
    mask = cv2.bitwise_or(gold, pale)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
    return cv2.bitwise_not(mask)


def preprocess_variants(bgr: np.ndarray) -> List[Tuple[str, np.ndarray]]:
    gray = _clahe_gray(bgr)
    return [
        ("foil", _foil_mask(bgr)),
        ("invert", cv2.bitwise_not(gray)),
        ("clahe", gray),
    ]


SHORT_WORDS = frozenset(
    {"of", "in", "to", "on", "or", "at", "no", "id", "po", "sku", "us", "uk"}
)


def extract_words(data: Dict[str, Any], min_conf: float = 55.0) -> List[Dict[str, Any]]:
    words: List[Dict[str, Any]] = []
    texts = data.get("text") or []
    for i, raw in enumerate(texts):
        token = str(raw or "").strip()
        if not token:
            continue
        try:
            conf = float((data.get("conf") or [0])[i])
        except (TypeError, ValueError, IndexError):
            continue
        letters = sum(ch.isalpha() for ch in token)
        if conf < min_conf or letters < 2:
            continue
        if len(token) <= 2 and token.lower() not in SHORT_WORDS:
            continue
        words.append(
            {
                "text": token,
                "conf": conf,
                "x": int((data.get("left") or [0])[i] or 0),
                "y": int((data.get("top") or [0])[i] or 0),
            }
        )
    return words


def merge_words(words: Sequence[Dict[str, Any]], y_tol: int = 28) -> str:
    if not words:
        return ""
    ordered = sorted(words, key=lambda w: (w["y"], w["x"]))
    kept: List[Dict[str, Any]] = []
    seen = set()
    for word in ordered:
        key = word["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(word)
    if not kept:
        return ""
    lines: List[List[str]] = []
    current: List[str] = [kept[0]["text"]]
    line_y = kept[0]["y"]
    for word in kept[1:]:
        if abs(word["y"] - line_y) <= y_tol:
            current.append(word["text"])
        else:
            lines.append(current)
            current = [word["text"]]
            line_y = word["y"]
    lines.append(current)
    return "\n".join(" ".join(part) for part in lines).strip()


def _configure_pytesseract() -> Any:
    import pytesseract

    exe = find_tesseract()
    if not exe:
        raise RuntimeError("Tesseract is not installed on this server.")
    pytesseract.pytesseract.tesseract_cmd = exe
    return pytesseract


def _tess_config(psm: int) -> str:
    parts = [f"--oem 3 --psm {psm}"]
    data = tessdata_dir()
    if data:
        os.environ["TESSDATA_PREFIX"] = data
        parts.append(f"--tessdata-dir {data}")
    return " ".join(parts)


def ocr_numpy(bgr: np.ndarray, lang: str = "eng") -> Dict[str, Any]:
    pytesseract = _configure_pytesseract()
    from pytesseract import Output

    cropped = crop_document(bgr)
    scaled = _scale_for_ocr(cropped)
    lang = map_lang(lang)
    collected: List[Dict[str, Any]] = []
    tried: List[str] = []
    for name, variant in preprocess_variants(scaled):
        pil = Image.fromarray(variant)
        for psm in (6, 11):
            config = _tess_config(psm)
            try:
                data = pytesseract.image_to_data(
                    pil, lang=lang, config=config, output_type=Output.DICT
                )
            except Exception:
                tried.append(f"{name}/psm{psm}:err")
                continue
            words = extract_words(data)
            tried.append(f"{name}/psm{psm}:{len(words)}")
            collected.extend(words)
        strong = [w for w in collected if w["conf"] >= 70 and len(w["text"]) >= 4]
        if len(strong) >= 3:
            break
    text = merge_words(collected)
    confs = [w["conf"] for w in collected]
    return {
        "text": text,
        "confidence": round(sum(confs) / len(confs), 1) if confs else 0.0,
        "variant": "merged",
        "psm": 0,
        "engine": "tesseract",
        "lang": lang,
        "tried": tried,
        "words": len(collected),
    }


def ocr_image_bytes(data: bytes, lang: str = "eng") -> Dict[str, Any]:
    return ocr_numpy(_decode_image(data), lang=lang)


def ocr_status_payload() -> Dict[str, Any]:
    langs: Sequence[str] = ()
    data = tessdata_dir()
    if data:
        langs = tuple(
            p.stem
            for p in Path(data).glob("*.traineddata")
            if p.stem not in {"osd", "equ"}
        )
    return {
        "available": tesseract_available(),
        "langs": list(langs),
    }
