"""Local cache for the Container Import Google Sheet and processed /boat/data responses."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from io import StringIO
from typing import Any

import pandas as pd
import requests

GOOGLE_SHEETS_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv&gid=0"
)

_CACHE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "output", "boat_cache")
)
_SHEET_CSV = os.path.join(_CACHE_ROOT, "container_import_sheet.csv")
_META_JSON = os.path.join(_CACHE_ROOT, "cache_meta.json")
_RESPONSE_JSON = os.path.join(_CACHE_ROOT, "boat_data_response.json")
_RESPONSE_META_JSON = os.path.join(_CACHE_ROOT, "boat_data_response_meta.json")


def _ensure_cache_dir() -> None:
    os.makedirs(_CACHE_ROOT, exist_ok=True)


def _read_json(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    _ensure_cache_dir()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_remote_sheet_bytes(*, timeout: int = 25) -> bytes:
    response = requests.get(GOOGLE_SHEETS_URL, timeout=timeout)
    response.raise_for_status()
    return response.content


def _sheet_max_written_time(df: pd.DataFrame) -> str:
    """Best-effort max timestamp from column O (written time) for display."""
    col_name = None
    for candidate in ("written time", "Written time", "WRITTEN TIME"):
        if candidate in df.columns:
            col_name = candidate
            break
    if col_name is None and len(df.columns) >= 15:
        col_name = df.columns[14]
    if col_name is None:
        return ""

    values: list[datetime] = []
    for raw in df[col_name].dropna().astype(str):
        text = raw.strip()
        if not text or text.lower() in {"written time", "nan"}:
            continue
        for fmt in (
            "%m/%d/%Y %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y",
            "%Y-%m-%d",
        ):
            try:
                values.append(datetime.strptime(text, fmt))
                break
            except ValueError:
                continue
    if not values:
        return ""
    return max(values).strftime("%Y-%m-%d %H:%M:%S")


def load_sheet_dataframe(*, force_refresh: bool = False) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return sheet data, refreshing the on-disk CSV only when remote content changed."""
    _ensure_cache_dir()
    meta = _read_json(_META_JSON)

    remote_bytes = fetch_remote_sheet_bytes()
    remote_hash = content_hash(remote_bytes)

    if (
        not force_refresh
        and meta.get("content_hash") == remote_hash
        and os.path.isfile(_SHEET_CSV)
    ):
        df = pd.read_csv(_SHEET_CSV)
        return df, {
            "source": "cache",
            "content_hash": remote_hash,
            "cached_at": meta.get("cached_at", ""),
            "sheet_timestamp": meta.get("sheet_timestamp", ""),
            "row_count": int(meta.get("row_count") or len(df)),
        }

    df = pd.read_csv(StringIO(remote_bytes.decode("utf-8")))
    sheet_timestamp = _sheet_max_written_time(df)
    cached_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(_SHEET_CSV, "wb") as handle:
        handle.write(remote_bytes)

    new_meta = {
        "content_hash": remote_hash,
        "cached_at": cached_at,
        "sheet_timestamp": sheet_timestamp,
        "row_count": len(df),
        "source_url": GOOGLE_SHEETS_URL,
    }
    _write_json(_META_JSON, new_meta)

    return df, {
        "source": "live",
        "content_hash": remote_hash,
        "cached_at": cached_at,
        "sheet_timestamp": sheet_timestamp,
        "row_count": len(df),
    }


def build_boat_data_cache_key(*, sheet_hash: str, dropbox_keys: list[str]) -> str:
    dropbox_part = hashlib.sha256(
        ",".join(sorted(dropbox_keys)).encode("utf-8")
    ).hexdigest()[:16]
    return f"{sheet_hash}:{dropbox_part}"


def load_latest_cached_boat_payload() -> dict[str, Any] | None:
    """Return the last saved /boat/data payload even if the cache key changed."""
    if not os.path.isfile(_RESPONSE_JSON):
        return None
    with open(_RESPONSE_JSON, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("success"):
        meta = _read_json(_RESPONSE_META_JSON)
        payload["from_cache"] = True
        payload["cache_key"] = meta.get("cache_key", "")
        payload["cached_at"] = meta.get("cached_at", "")
        return payload
    return None


def load_cached_boat_data(cache_key: str) -> dict[str, Any] | None:
    meta = _read_json(_RESPONSE_META_JSON)
    if meta.get("cache_key") != cache_key:
        return None
    payload = load_latest_cached_boat_payload()
    if payload:
        payload["cache_key"] = cache_key
        payload["cached_at"] = meta.get("cached_at", "")
    return payload


def save_cached_boat_data(cache_key: str, payload: dict[str, Any]) -> None:
    _ensure_cache_dir()
    with open(_RESPONSE_JSON, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    _write_json(
        _RESPONSE_META_JSON,
        {
            "cache_key": cache_key,
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "row_count": len(payload.get("data") or []),
        },
    )


def invalidate_boat_data_response_cache() -> None:
    """Drop processed /boat/data cache after sheet/tracking updates."""
    for path in (_RESPONSE_JSON, _RESPONSE_META_JSON):
        if os.path.isfile(path):
            os.remove(path)
