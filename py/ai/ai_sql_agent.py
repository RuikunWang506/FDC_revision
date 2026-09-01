"""Natural-language question → validated read-only SQL → rows → short answer."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from py.ai import ollama_client
from py.ai.database.database_tools import execute_readonly_query
from py.ai.prompts import (
    SUMMARIZE_SYSTEM_PROMPT,
    sql_system_prompt,
    sql_user_prompt,
    summarize_user_prompt,
)
from py.ai.schema_retriever import retrieve_schema, schema_json_for_prompt
from py.ai.sql_validator import validate_sql

_JSON_BLOCK = re.compile(r'\{[\s\S]*\}')
_RANK_HINT = re.compile(r'最高|最多|top\s*\d+|销量|排名|合计|总数|多少', re.I)


def _sql_looks_weak(question: str, sql: str) -> bool:
    text = re.sub(r'\s+', ' ', sql or '').lower()
    if re.search(r'select(?:\s+top\s+\d+)?\s+\*', text):
        return True
    if _RANK_HINT.search(question or '') and 'group by' not in text:
        return True
    if re.search(r'销量|sold|sales', question or '', re.I) and re.search(r'\bin_stock\b|\bavail_qty\b', text):
        return True
    if re.search(r'\b(ord_num|cus_id)\s*=\s*1\b', text) and not re.search(r'\b1\b', question or ''):
        return True
    return False


def _parse_sql_payload(text: str) -> dict[str, Any]:
    raw = (text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?', '', raw).strip()
        raw = re.sub(r'```$', '', raw).strip()
    match = _JSON_BLOCK.search(raw)
    if match:
        raw = match.group(0)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Ollama JSON was not an object')
    return {
        'sql': str(data.get('sql') or '').strip(),
        'tables_used': [str(t) for t in (data.get('tables_used') or []) if t],
        'reason': str(data.get('reason') or '').strip(),
    }


def _log(payload: dict[str, Any]) -> None:
    question = (payload.get('question') or '')[:160]
    tables = ','.join(payload.get('tables_used') or [])[:120]
    sql = (payload.get('sql') or '')[:300]
    error = (payload.get('error') or '')[:180]
    try:
        from py.utilities.logging_setup import log_event

        log_event(
            'info' if payload.get('ok') else 'warning',
            'AI SQL query question={question} tables={tables} sql={sql} '
            'ollama_ms={ollama_ms} sql_ms={sql_ms} rows={rows} error={error}'.format(
                question=question,
                tables=tables,
                sql=sql,
                ollama_ms=payload.get('ollama_ms'),
                sql_ms=payload.get('sql_ms'),
                rows=payload.get('row_count'),
                error=error,
            ),
        )
    except Exception:
        pass
    try:
        from datetime import datetime
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / 'output' / 'logs' / 'ai_sql.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            'ts': datetime.now().isoformat(timespec='seconds'),
            'username': payload.get('username') or '',
            'question': payload.get('question') or '',
            'tables_used': payload.get('tables_used') or [],
            'sql': payload.get('sql') or '',
            'ollama_ms': payload.get('ollama_ms'),
            'sql_ms': payload.get('sql_ms'),
            'elapsed_ms': payload.get('elapsed_ms'),
            'row_count': payload.get('row_count'),
            'ok': bool(payload.get('ok')),
            'error': payload.get('error'),
        }
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + '\n')
    except Exception:
        pass


def run_ai_query(question: str, *, username: str = '') -> dict[str, Any]:
    question = (question or '').strip()
    started = time.perf_counter()
    result: dict[str, Any] = {
        'ok': False,
        'answer': '',
        'sql': '',
        'data': [],
        'tables_used': [],
        'reason': '',
        'error': None,
        'row_count': 0,
        'ollama_ms': 0,
        'sql_ms': 0,
        'question': question,
        'username': (username or '').strip(),
    }
    if not question:
        result['error'] = 'question is required'
        _log(result)
        return result

    retrieved = retrieve_schema(question)
    result['tables_used'] = list(retrieved.get('table_names') or [])
    schema_json = schema_json_for_prompt(retrieved)

    ollama_t0 = time.perf_counter()
    try:
        raw = ollama_client.chat(
            [
                {'role': 'system', 'content': sql_system_prompt()},
                {'role': 'user', 'content': sql_user_prompt(question, schema_json)},
            ],
            timeout=120,
            json_mode=True,
        )
        parsed = _parse_sql_payload(raw)
        if _sql_looks_weak(question, parsed.get('sql') or ''):
            raw = ollama_client.chat(
                [
                    {'role': 'system', 'content': sql_system_prompt()},
                    {'role': 'user', 'content': sql_user_prompt(question, schema_json)},
                    {
                        'role': 'user',
                        'content': (
                            'The previous SQL did not answer the question. '
                            'Do not SELECT *. Do not copy sample-row ids. '
                            'If this is a ranking/qty question, use GROUP BY, SUM, ORDER BY, and TOP N. JSON only.'
                        ),
                    },
                ],
                timeout=120,
                json_mode=True,
            )
            parsed = _parse_sql_payload(raw)
    except Exception as exc:
        result['error'] = f'Ollama SQL generation failed: {exc}'
        result['ollama_ms'] = int((time.perf_counter() - ollama_t0) * 1000)
        _log(result)
        return result
    result['ollama_ms'] = int((time.perf_counter() - ollama_t0) * 1000)
    result['sql'] = parsed.get('sql') or ''
    result['reason'] = parsed.get('reason') or ''
    if parsed.get('tables_used'):
        result['tables_used'] = parsed['tables_used']

    if not result['sql']:
        result['answer'] = result['reason'] or 'Schema is not enough to write SQL.'
        result['error'] = 'No SQL generated'
        _log(result)
        return result

    checked = validate_sql(result['sql'])
    if not checked.get('ok'):
        result['error'] = checked.get('error') or 'SQL rejected'
        result['answer'] = f"SQL was not executed: {result['error']}"
        _log(result)
        return result
    result['sql'] = checked['sql']

    sql_t0 = time.perf_counter()
    query = execute_readonly_query(result['sql'])
    result['sql_ms'] = int((time.perf_counter() - sql_t0) * 1000)
    if not query.get('ok'):
        result['error'] = query.get('error') or 'Query failed'
        result['answer'] = f"Query failed: {result['error']}"
        _log(result)
        return result

    rows = query.get('rows') or []
    result['data'] = rows
    result['row_count'] = int(query.get('row_count') or len(rows))
    result['sql'] = query.get('sql') or result['sql']

    sum_t0 = time.perf_counter()
    try:
        answer = ollama_client.chat(
            [
                {'role': 'system', 'content': SUMMARIZE_SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': summarize_user_prompt(question, result['sql'], rows, result['row_count']),
                },
            ],
            timeout=90,
            json_mode=False,
        )
        result['answer'] = answer
    except Exception:
        if rows:
            result['answer'] = f'Returned {result["row_count"]} rows.'
        else:
            result['answer'] = 'No rows.'
    result['ollama_ms'] += int((time.perf_counter() - sum_t0) * 1000)
    result['ok'] = True
    result['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
    _log(result)
    return result
