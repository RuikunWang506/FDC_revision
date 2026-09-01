"""Ollama client for the SQL agent. Reuses FDC AI host selection (remote GPU first)."""
from __future__ import annotations

from typing import Any


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
