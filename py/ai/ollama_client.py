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

    Generation already ran at temperature 0.2, set in
    fdc_ai_agent._ollama_options and merged underneath whatever a caller
    passes. That is a deliberate, conservative value. What it never set
    is a seed, so every request sampled afresh and the same question
    could produce different SQL on different runs.

    A low temperature makes that rare enough to pass for stability, which
    is probably why it lasted: a setting that already reads 0.2 does not
    invite suspicion. Rare is not the same as absent, and the cases it
    flips are the borderline ones -- exactly the ones worth measuring.

    For text-to-SQL the sampling buys nothing. Two people asking the same
    question, or one person asking twice, can get different queries and
    different numbers, with nothing to indicate that anything differed.

    It also made the evaluation suite unrepeatable, which is how this was
    found: a case that reported "No SQL generated" across a full run
    returned correct JSON when run again by hand, same model, same
    prompt. Until that was fixed, no two eval runs could be compared, so
    no change could be shown to have helped.

    Temperature 0 with a fixed seed makes a run reproducible. Taking 0.2
    down to 0 is a change beyond adding the seed, and an intentional one:
    there is no question this agent answers better by guessing at. Both
    stay overridable for deliberate sampling experiments.
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
        # _post_ollama merges these over _ollama_options(host), so these win.
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
