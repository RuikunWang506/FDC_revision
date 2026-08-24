"""Lightweight JSON-based i18n for Flask templates (en / zh)."""
from __future__ import annotations

import json
import os
from typing import Any

from flask import has_request_context, request, session

SUPPORTED_LOCALES = ("en", "zh")
DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "fdc_locale"
LOCALE_SESSION_KEY = "locale"

_LOCALES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "locales")
)

# mtime-aware cache so locales/*.json reloads without restarting the process.
_locale_mtime: dict[str, float] = {}
_locale_data: dict[str, dict[str, str]] = {}
_en_index: dict[str, str] = {}
_en_index_mtime: float | None = None


def _load_locale_file(locale: str) -> dict[str, str]:
    path = os.path.join(_LOCALES_DIR, f"{locale}.json")
    if not os.path.isfile(path):
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _locale_mtime.get(locale) == mtime and locale in _locale_data:
        return _locale_data[locale]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
        parsed = {str(k): str(v) for k, v in data.items() if v is not None}
    except (OSError, json.JSONDecodeError):
        parsed = {}
    _locale_mtime[locale] = mtime
    _locale_data[locale] = parsed
    return parsed


def normalize_locale(value: str | None) -> str:
    loc = (value or "").strip().lower()
    if loc.startswith("zh"):
        return "zh"
    if loc in SUPPORTED_LOCALES:
        return loc
    return DEFAULT_LOCALE


def get_locale() -> str:
    if has_request_context():
        query_loc = normalize_locale(request.args.get("lang"))
        if request.args.get("lang"):
            session[LOCALE_SESSION_KEY] = query_loc
            return query_loc
        if LOCALE_SESSION_KEY in session:
            return normalize_locale(session.get(LOCALE_SESSION_KEY))
        cookie_loc = request.cookies.get(LOCALE_COOKIE)
        if cookie_loc:
            return normalize_locale(cookie_loc)
    return DEFAULT_LOCALE


def set_locale(locale: str) -> str:
    loc = normalize_locale(locale)
    if has_request_context():
        session[LOCALE_SESSION_KEY] = loc
    return loc


def translate(key: str, **kwargs: Any) -> str:
    if not key:
        return ""
    locale = get_locale()
    text = _load_locale_file(locale).get(key)
    if text is None and locale != DEFAULT_LOCALE:
        text = _load_locale_file(DEFAULT_LOCALE).get(key)
    if text is None:
        text = key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


def _(key: str, **kwargs: Any) -> str:
    return translate(key, **kwargs)


def locale_label(locale: str | None = None) -> str:
    loc = normalize_locale(locale or get_locale())
    return _("lang.en") if loc == "en" else _("lang.zh")


def te(text: str, **kwargs: Any) -> str:
    """Translate using English UI text as lookup (gradual template migration)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    key = _en_text_index().get(raw)
    if key:
        return translate(key, **kwargs)
    return raw


def _en_text_index() -> dict[str, str]:
    global _en_index, _en_index_mtime
    path = os.path.join(_LOCALES_DIR, f"{DEFAULT_LOCALE}.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if mtime is not None and _en_index_mtime == mtime and _en_index:
        return _en_index
    en = _load_locale_file(DEFAULT_LOCALE)
    _en_index = {v: k for k, v in en.items() if v}
    _en_index_mtime = mtime
    return _en_index


def inject_i18n_context() -> dict[str, Any]:
    loc = get_locale()
    return {
        "locale": loc,
        "_": _,
        "te": te,
        "translate": translate,
        "supported_locales": SUPPORTED_LOCALES,
        "locale_label": locale_label(loc),
    }


def client_i18n_bundle(prefixes: tuple[str, ...] = ("common.", "js.")) -> dict[str, Any]:
    """Translations for static/js/fdc-i18n.js — scoped by key prefix to keep payloads small."""
    en = _load_locale_file("en")
    zh_map = _load_locale_file("zh")
    bundle: dict[str, Any] = {}
    en_index: dict[str, str] = {}
    for key, en_val in en.items():
        if not any(key.startswith(p) for p in prefixes):
            continue
        bundle[key] = {"en": en_val, "zh": zh_map.get(key, en_val)}
        en_index[en_val] = key
    bundle["_enIndex"] = en_index
    return bundle
