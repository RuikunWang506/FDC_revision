"""Ollama client for the SQL agent. Reuses FDC AI host selection (remote GPU first)."""
from __future__ import annotations

import os
from typing import Any


def _float_env(name: str, default: float, *, low: float, high: float) -> float:
    raw = (os.getenv(name) or '').strip()
    if not raw:
        return default
    try:
        return max(low, min(high, float(raw)))
    except ValueError:
        return default


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    raw = (os.getenv(name) or '').strip()
    if not raw:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        return default


def _sampling_options() -> dict[str, Any]:
    """Sampling settings for SQL generation.

    Ollama's defaults are tuned for conversation: temperature 0.8 and a
    fresh random seed per request. For text-to-SQL that is a defect, not
    a feature. The same question produces a different query on different
    runs, so two people asking the same thing -- or the same person
    asking twice -- can get different numbers, with nothing to indicate
    which is right or that anything differed at all.

    It also made the evaluation suite unrepeatable, which is how this
    was found: a case that reported "No SQL generated" across a full run
    returned correct JSON when run again by hand, same model, same
    question. Until generation is deterministic, two runs cannot be
    compared and no change can be shown to have helped.

    Temperature 0 with a fixed seed makes a run reproducible. Both stay
    overridable, because deliberately sampling several candidates for
    one question is a reasonable thing to want later -- the point is
    that it should be a choice.
    """
    return {
        'temperature': _float_env('FDC_AI_TEMPERATURE', 0.0, low=0.0, high=2.0),
        'seed': _int_env('FDC_AI_SEED', 0, low=0, high=2**31 - 1),
    }


def chat(messages: list[dict[str, str]], *, timeout: int = 120, json_mode: bool = False) -> str:
    from py.utilities.fdc_ai_agent import _DEFAULT_TIMEOUT, _post_ollama, ai_config, check_llm_health

    health = check_llm_health()
    if not health.get('ok') or not health.get('model_ready'):
        raise RuntimeError(health.get('error') or health.get('hint') or 'Ollama is not ready')
    cfg = ai_config()
    payload: dict[str, Any] = {
        'model': health['model'],
        'messages': messages,
        'stream': False,
        'keep_alive': cfg['keep_alive'],
        'options': _sampling_options(),
    }
    if json_mode:
        payload['format'] = 'json'
    resp = _post_ollama(
        f"{health.get('base_url') or cfg['base_url']}/api/chat",
        payload,
        timeout=timeout or _DEFAULT_TIMEOUT,
    )
    data = resp.json() or {}
    msg = data.get('message') or {}
    text = str(msg.get('content') or '').strip()
    if text:
        return text
    raise RuntimeError('Ollama returned an empty chat response')
