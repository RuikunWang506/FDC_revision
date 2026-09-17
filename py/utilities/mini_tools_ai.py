"""Ollama helpers for Mini Tools: vision OCR plus text jobs on officeyeti / local."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from PIL import Image

VISION_TIMEOUT = 180
TEXT_TIMEOUT = 90
VISION_MAX_SIDE = 1280

_VISION_HINTS = (
    "qwen2.5vl",
    "qwen2-vl",
    "qwen-vl",
    "llava",
    "bakllava",
    "minicpm-v",
    "moondream",
)
_TEXT_HINTS = (
    "qwen3",
    "qwen2.5",
    "dolphin",
    "llama",
    "mistral",
    "gemma",
    "phi",
)
_EXTRA_HOSTS = (
    "http://192.168.254.20:11434",
    "http://100.106.12.45:11434",
    "http://127.0.0.1:11434",
)

AI_TASKS = frozenset(
    {"translate_en", "translate_zh", "cleanup", "extract", "vendor_draft", "tracking"}
)

_probe_cache: Dict[str, Any] = {"until": 0.0, "rows": []}


def _clean_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def ollama_hosts() -> List[str]:
    from py.utilities.fdc_ai_agent import _ollama_candidates

    out: List[str] = []
    for url in list(_ollama_candidates()) + list(_EXTRA_HOSTS):
        clean = _clean_url(url)
        if clean and clean not in out:
            out.append(clean)
    return out


def _host_models(base_url: str, timeout: float = 3.0) -> List[str]:
    resp = requests.get(f"{base_url}/api/tags", timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {base_url} returned {resp.status_code}")
    return [m.get("name") for m in (resp.json() or {}).get("models") or [] if m.get("name")]


def _is_vision(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in _VISION_HINTS)


def _pick_named(models: List[str], hints: Tuple[str, ...], *, vision: bool) -> str:
    ranked: List[str] = []
    for hint in hints:
        for model in models:
            low = model.lower()
            if hint in low and _is_vision(model) == vision and model not in ranked:
                ranked.append(model)
    return ranked[0] if ranked else ""


def _probe_hosts() -> List[Dict[str, Any]]:
    now = time.time()
    cached = _probe_cache.get("rows") or []
    if cached and now < float(_probe_cache.get("until") or 0):
        return cached
    rows: List[Dict[str, Any]] = []
    for host in ollama_hosts():
        try:
            models = _host_models(host)
        except Exception:
            continue
        rows.append(
            {
                "host": host,
                "models": models,
                "vision": _pick_named(models, _VISION_HINTS, vision=True),
                "text": _pick_named(models, _TEXT_HINTS, vision=False)
                or next((m for m in models if not _is_vision(m)), ""),
                "remote": "127.0.0.1" not in host and "localhost" not in host.lower(),
            }
        )
    _probe_cache["rows"] = rows
    _probe_cache["until"] = now + 30
    return rows


def pick_vision() -> Tuple[str, str]:
    rows = _probe_hosts()
    for prefer_remote in (True, False):
        for row in rows:
            if bool(row.get("remote")) != prefer_remote:
                continue
            if row.get("vision"):
                return str(row["host"]), str(row["vision"])
    return "", ""


def pick_text() -> Tuple[str, str]:
    rows = _probe_hosts()
    for prefer_remote in (True, False):
        for row in rows:
            if bool(row.get("remote")) != prefer_remote:
                continue
            if row.get("text"):
                return str(row["host"]), str(row["text"])
    return "", ""


def ai_status_payload() -> Dict[str, Any]:
    vision_host, vision_model = pick_vision()
    text_host, text_model = pick_text()
    return {
        "available": bool(vision_model or text_model),
        "vision": bool(vision_model),
        "text": bool(text_model),
        "vision_model": vision_model,
        "text_model": text_model,
        "vision_host": urlparse(vision_host).hostname or "",
        "text_host": urlparse(text_host).hostname or "",
    }


def _jpeg_b64(data: bytes) -> str:
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    side = max(w, h)
    if side > VISION_MAX_SIDE:
        scale = VISION_MAX_SIDE / float(side)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _chat(host: str, model: str, messages: List[Dict[str, Any]], timeout: int) -> str:
    resp = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": os.getenv("FDC_AI_KEEP_ALIVE") or "10m",
            "options": {"temperature": 0.1},
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        snippet = (resp.text or "")[:240]
        raise RuntimeError(f"Ollama {urlparse(host).hostname} returned {resp.status_code}: {snippet}")
    data = resp.json() or {}
    msg = data.get("message") or {}
    text = str(msg.get("content") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty response.")
    return text


def vision_ocr(image_bytes: bytes, lang: str = "eng") -> Dict[str, Any]:
    host, model = pick_vision()
    if not host or not model:
        raise RuntimeError("No vision model is available on Ollama.")
    if "chi" in (lang or "").lower() or "zh" in (lang or "").lower():
        lang_note = "The image may mix English and Simplified Chinese. Keep original wording."
    else:
        lang_note = "Prefer English if the document is English."
    prompt = (
        "You are reading a warehouse / shipping / ID photo. "
        "Transcribe every readable word in reading order. "
        f"{lang_note} "
        "If it is a passport or ID, include document type and country. "
        "Do not describe the photo, background, or hand. "
        "Do not invent numbers you cannot see. Return plain text only."
    )
    text = _chat(
        host,
        model,
        [
            {
                "role": "user",
                "content": prompt,
                "images": [_jpeg_b64(image_bytes)],
            }
        ],
        VISION_TIMEOUT,
    )
    return {
        "text": text.strip(),
        "engine": "ollama-vision",
        "model": model,
        "host": urlparse(host).hostname or host,
        "confidence": None,
    }


def _strip_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json|text)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def _task_prompt(task: str, text: str) -> Tuple[str, bool]:
    body = (text or "").strip()
    if task == "translate_en":
        return (
            "Translate the following warehouse/office text into clear US English. "
            "Keep SKUs, POs, quantities, and names unchanged. Return only the translation.\n\n"
            + body,
            False,
        )
    if task == "translate_zh":
        return (
            "把下面仓库/办公室文字译成简洁的简体中文。SKU、PO、数量、人名保持原样。只返回译文。\n\n"
            + body,
            False,
        )
    if task == "cleanup":
        return (
            "Clean this OCR or messy note into readable lines. Fix obvious OCR errors, "
            "keep numbers and SKUs, do not invent data. Return only the cleaned text.\n\n"
            + body,
            False,
        )
    if task == "tracking":
        return (
            "From this shipping email or note, extract tracking number, carrier, "
            "order/PO if present, and destination. Plain text, labeled lines. "
            "If missing, write Unknown.\n\n" + body,
            False,
        )
    if task == "vendor_draft":
        return (
            "Write a short professional vendor email in English, then a 简体中文 version. "
            "Use the facts below. Do not invent prices or dates.\n\n" + body,
            False,
        )
    return (
        "Extract purchase-order / invoice / packing-list fields from this text. "
        "Return JSON only with keys: po, vendor, date, currency, "
        "lines (array of {sku, qty, desc, price}), notes. "
        "Use empty strings when unknown. Do not invent SKUs.\n\n" + body,
        True,
    )


def run_text_task(task: str, text: str, image_bytes: Optional[bytes] = None) -> Dict[str, Any]:
    if task not in AI_TASKS:
        raise RuntimeError("Unknown AI task.")
    source = (text or "").strip()
    if image_bytes and not source:
        vision = vision_ocr(image_bytes)
        source = vision.get("text") or ""
        if not source:
            raise RuntimeError("Vision model did not read any text from the photo.")
    if not source:
        raise RuntimeError("Paste text or attach a photo.")
    prompt, want_json = _task_prompt(task, source)
    host, model = pick_text()
    if not host or not model:
        raise RuntimeError("No text model is available on Ollama.")
    raw = _chat(host, model, [{"role": "user", "content": prompt}], TEXT_TIMEOUT)
    cleaned = _strip_fence(raw)
    payload: Dict[str, Any] = {
        "text": cleaned,
        "task": task,
        "model": model,
        "host": urlparse(host).hostname or host,
        "source": source if image_bytes else "",
    }
    if want_json:
        try:
            payload["data"] = json.loads(cleaned)
            payload["text"] = json.dumps(payload["data"], ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            payload["data"] = None
    return payload
