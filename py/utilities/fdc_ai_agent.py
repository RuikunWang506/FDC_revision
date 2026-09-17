"""Local Ollama agent for FDC help chat."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from py.utilities.fdc_ai_tools import (
    TOOL_METADATA,
    TOOL_NAMES,
    execute_tool,
    extract_container_id,
    extract_customer_query,
    extract_doc_number,
    extract_skus,
    extract_year_month,
)
from py.utilities.setting import Config

_DEFAULT_TIMEOUT = 120
_CUDA_MARKERS = (
    'ptx',
    'cuda error',
    'unsupported toolchain',
    '0xc0000409',
    'ggml-cuda',
    'out of memory',
    'llama-server process has terminated',
)
_TEXT_MODEL_HINTS = (
    'llama',
    'qwen',
    'phi',
    'gemma',
    'mistral',
    'tinyllama',
    'orca',
    'vicuna',
    'yi',
    'deepseek',
)
_VISION_HINTS = (
    'llava',
    'bakllava',
    'moondream',
    'minicpm-v',
    'qwen2.5vl',
    'qwen2-vl',
    'qwen-vl',
)

# GTX 10-series / Pascal often crash newer Ollama CUDA builds. After the first
# CUDA/PTX failure on THIS machine, keep using CPU so we do not keep killing llama-server.
_force_cpu = False
_host_cache: dict[str, Any] = {'until': 0.0, 'health': None}


def _clean_url(url: str) -> str:
    return (url or '').strip().rstrip('/')


def _is_loopback(url: str) -> bool:
    low = (url or '').lower()
    return '127.0.0.1' in low or 'localhost' in low


def ai_config() -> dict[str, str]:
    primary = _clean_url(
        getattr(Config, 'FDC_AI_BASE_URL', None) or os.getenv('FDC_AI_BASE_URL') or 'http://127.0.0.1:11434'
    )
    fallback = _clean_url(getattr(Config, 'FDC_AI_FALLBACK_URL', None) or os.getenv('FDC_AI_FALLBACK_URL') or '')
    if not fallback and not _is_loopback(primary):
        fallback = 'http://127.0.0.1:11434'
    return {
        'provider': (getattr(Config, 'FDC_AI_PROVIDER', None) or os.getenv('FDC_AI_PROVIDER') or 'ollama').strip().lower(),
        'base_url': primary or 'http://127.0.0.1:11434',
        'fallback_url': fallback,
        'model': (getattr(Config, 'FDC_AI_MODEL', None) or os.getenv('FDC_AI_MODEL') or '').strip(),
        'keep_alive': getattr(Config, 'FDC_AI_KEEP_ALIVE', None) or os.getenv('FDC_AI_KEEP_ALIVE') or '10m',
    }


def _ollama_candidates() -> list[str]:
    cfg = ai_config()
    out: list[str] = []
    for url in (cfg.get('base_url'), cfg.get('fallback_url')):
        clean = _clean_url(url or '')
        if clean and clean not in out:
            out.append(clean)
    return out or ['http://127.0.0.1:11434']


def _pick_model(models: list[str], preferred: str) -> str:
    if preferred:
        for m in models:
            if m == preferred or m.startswith(preferred + ':'):
                return m
        return preferred
    text = [
        m
        for m in models
        if any(h in m.lower() for h in _TEXT_MODEL_HINTS)
        and not any(v in m.lower() for v in _VISION_HINTS)
    ]
    if text:
        return text[0]
    return models[0] if models else 'llama3.2'


def _inference_from_ps(base_url: str, model: str) -> str:
    import requests

    env = (os.getenv('FDC_AI_NUM_GPU') or '').strip()
    if env == '0' or (_force_cpu and _is_loopback(base_url)):
        return 'cpu'
    try:
        resp = requests.get(f'{base_url}/api/ps', timeout=2)
        rows = (resp.json() or {}).get('models') or [] if resp.status_code == 200 else []
    except Exception:
        rows = []
    model_key = (model or '').split(':')[0].lower()
    for row in rows:
        name = str(row.get('name') or row.get('model') or '').lower()
        if model_key and model_key not in name:
            continue
        size = float(row.get('size') or 0)
        vram = float(row.get('size_vram') or 0)
        if vram <= 0:
            return 'cpu'
        if size and vram >= size * 0.85:
            return 'gpu'
        return 'mixed'
    return 'gpu' if not _is_loopback(base_url) else 'cpu'


def _ollama_options(base_url: str = '') -> dict[str, Any]:
    opts: dict[str, Any] = {'temperature': 0.2}
    env = (os.getenv('FDC_AI_NUM_GPU') or '').strip()
    if _is_loopback(base_url) and (_force_cpu or env == '0'):
        opts['num_gpu'] = 0
    elif env != '' and _is_loopback(base_url):
        try:
            opts['num_gpu'] = int(env)
        except ValueError:
            pass
    return opts


def _is_cuda_fail(text: str) -> bool:
    low = (text or '').lower()
    return any(m in low for m in _CUDA_MARKERS)


def _response_error_text(resp) -> str:
    try:
        data = resp.json()
        err = data.get('error') if isinstance(data, dict) else None
        if err:
            return str(err)
    except Exception:
        pass
    try:
        return (resp.text or '')[:800]
    except Exception:
        return f'HTTP {getattr(resp, "status_code", "?")}'


def _health_for_url(base_url: str) -> dict[str, Any]:
    import requests

    cfg = ai_config()
    resp = requests.get(f'{base_url}/api/tags', timeout=3)
    if resp.status_code != 200:
        raise RuntimeError(f'Ollama returned {resp.status_code} from {base_url}')
    models = [m.get('name') for m in (resp.json().get('models') or []) if m.get('name')]
    model = _pick_model(models, cfg['model'])
    model_ok = any(model == m or m.startswith(f'{model}:') for m in models) if models else False
    hint = None
    if not model_ok:
        hint = f'ollama pull {model}' if model else f'No models on {base_url}'
    elif any(v in model.lower() for v in _VISION_HINTS):
        hint = 'Vision model in use. For better text chat: ollama pull llama3.2'
    return {
        'ok': True,
        'provider': cfg['provider'],
        'model': model,
        'base_url': base_url,
        'models': models,
        'model_ready': model_ok,
        'inference': _inference_from_ps(base_url, model),
        'hint': hint,
    }


def check_llm_health() -> dict[str, Any]:
    import time

    cfg = ai_config()
    now = time.time()
    cached = _host_cache.get('health')
    if cached and cached.get('ok') and cached.get('model_ready') and now < float(_host_cache.get('until') or 0):
        return cached
    errors: list[str] = []
    last: dict[str, Any] | None = None
    for url in _ollama_candidates():
        try:
            health = _health_for_url(url)
        except Exception as exc:
            errors.append(f'{url}: {exc}')
            continue
        last = health
        if health.get('ok') and health.get('model_ready'):
            _host_cache['health'] = health
            _host_cache['until'] = now + 30
            return health
    if last:
        return last
    return {
        'ok': False,
        'provider': cfg['provider'],
        'error': '; '.join(errors) or 'Ollama unreachable',
        'hint': f"Tried {', '.join(_ollama_candidates())}",
    }


def _post_ollama(url: str, payload: dict[str, Any], timeout: int):
    """POST to Ollama. Prefer the GPU host; only force CPU on loopback CUDA crashes."""
    import requests
    from urllib.parse import urlparse

    global _force_cpu
    parsed = urlparse(url)
    primary = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
    path = parsed.path or '/api/generate'
    hosts: list[str] = []
    for host in [primary, *_ollama_candidates()]:
        if host and host not in hosts:
            hosts.append(host)

    payload = dict(payload)
    extra = dict(payload.get('options') or {})
    last_error: Exception | None = None
    for host in hosts:
        options = {**_ollama_options(host), **extra}
        if not _is_loopback(host):
            options.pop('num_gpu', None)
        body = dict(payload)
        body['options'] = options
        try:
            resp = requests.post(f'{host}{path}', json=body, timeout=timeout)
        except Exception as exc:
            last_error = exc
            continue
        if resp.status_code == 200:
            return resp
        err = _response_error_text(resp)
        if _is_loopback(host) and _is_cuda_fail(err):
            _force_cpu = True
            try:
                from py.utilities.logging_setup import log_event

                log_event('warning', f'Ollama GPU failed ({err[:180]}); retrying on CPU')
            except Exception:
                pass
            retry = dict(body)
            retry['options'] = {**options, 'num_gpu': 0}
            try:
                retry_resp = requests.post(f'{host}{path}', json=retry, timeout=timeout)
            except Exception as exc:
                last_error = exc
                continue
            if retry_resp.status_code == 200:
                return retry_resp
            last_error = RuntimeError(_response_error_text(retry_resp))
            continue
        last_error = RuntimeError(err)
    if last_error:
        raise last_error
    raise RuntimeError('Ollama unreachable')


def wake_local_ai(background: bool = True) -> None:
    def _wake() -> None:
        health = check_llm_health()
        if not health.get('ok') or not health.get('model'):
            return
        try:
            _post_ollama(
                f"{health.get('base_url') or ai_config()['base_url']}/api/generate",
                {
                    'model': health['model'],
                    'prompt': 'ready',
                    'stream': False,
                    'keep_alive': ai_config()['keep_alive'],
                },
                timeout=90,
            )
        except Exception:
            pass

    if background:
        import threading

        threading.Thread(target=_wake, daemon=True, name='fdc-ai-wake').start()
        return
    _wake()


def _system_prompt(username: str) -> str:
    tools = '\n'.join(
        f"- {name}: {TOOL_METADATA.get(name, {}).get('description', '')}"
        for name in TOOL_NAMES
    )
    return (
        'You are the FDC Toolkit help assistant. Answer in the user\'s language '
        '(English or 中文). Be concise. You only know this company\'s warehouse, '
        'SKU, customer-order, shipping, and help-center tools.\n'
        f'Staff user: {username or "staff"}.\n'
        'Database access is READ-ONLY. Never claim you updated, deleted, or inserted data.\n'
        'If the user asks 谁买过 / who bought a SKU, use get_sku_customers, NOT stock.\n'
        'If the user asks what a customer bought (客户买了什么), use get_customer_purchases.\n'
        'If the user asks 在海 / ocean for one SKU, use get_sku_ocean. '
        'If they ask how many styles / 多少个款 / 其他款 are on the water, use list_ocean_styles, '
        'not the previous SKU.\n'
        'If the user asks 入库 / 收货 / received in a month, use list_received_styles. '
        'Follow-ups like 分别是什么 should list those received styles, not ocean tools.\n'
        'If the user asks 发票 / invoice or 订单 / order with a number, use get_invoice / get_order.\n'
        'Warehouses are FDC and EF. Never say 艺发; use EF.\n'
        f'Tools:\n{tools}\n'
    )


_BUYER_ZH = ('谁买', '买过', '哪些客', '哪个客', '买家', '卖给谁', '谁下过', '出过给')
_BUYER_EN = (
    'who bought',
    'who ordered',
    'sold to',
    'buyers',
    'which customer',
    'which customers',
    'purchasers',
    'who has bought',
    'who purchased',
)


def _wants_customers(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if _wants_customer_history(text):
        return False
    if any(k in text for k in _BUYER_ZH):
        return True
    if any(k in low for k in _BUYER_EN):
        return True
    if extract_skus(text) and any(k in text for k in ('客户', '客人')):
        return True
    if extract_skus(text) and re.search(r'\bcustomers?\b', low):
        return True
    return False


def _wants_customer_history(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if extract_skus(text) and any(k in text for k in ('谁买', '买过的客户')):
        return False
    if any(k in text for k in ('买了什么', '买过哪些', '买过什么', '采购历史')):
        return True
    if any(k in low for k in ('what did', 'bought what', 'purchase history', 'what has')):
        return True
    if re.search(r'(客户|客人)\s*\d{4,6}', text) and not extract_skus(text):
        return True
    if ('客户' in text or '客人' in text) and any(k in text for k in ('买过', '买了', '哪些款')):
        return not bool(extract_skus(text))
    return False


_LIST_FOLLOWUP = (
    '分别是什么',
    '分别是',
    '哪几个',
    '哪些款',
    '列出',
    '叫什么',
    '什么款',
    '多少个款',
    '几个款',
    'which ones',
    'list them',
    'what are they',
    'names',
)


def _wants_received(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if any(k in text for k in ('怎么', '如何')) or 'how do i' in low or 'how to' in low:
        return False
    return any(
        k in text or k in low
        for k in ('入库', '收货', '进仓', '到仓', 'received in', 'landed in', 'warehouse in')
    )


def _latest_lookup_topic(history: list[dict[str, str]] | None) -> str:
    for item in reversed(history or []):
        content = str(item.get('content') or '')
        if _wants_received(content) or '入库的款' in content:
            return 'received'
        if _wants_ocean(content) or '在海数量的款' in content or '货柜中心' in content:
            return 'ocean'
    return ''


def _wants_received_lookup(message: str, history: list[dict[str, str]] | None = None) -> bool:
    if _wants_received(message):
        return True
    text = message or ''
    low = text.lower()
    if not any(k in text or k in low for k in _LIST_FOLLOWUP):
        return False
    return _latest_lookup_topic(history) == 'received'


def _year_month_from_chat(message: str, history: list[dict[str, str]] | None = None) -> tuple[int, int] | None:
    found = extract_year_month(message)
    if found:
        return found
    for item in reversed(history or []):
        found = extract_year_month(str(item.get('content') or ''))
        if found:
            return found
    return None


def _wants_ocean(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if any(k in text for k in ('怎么', '如何')) or 'how do i' in low or 'how to' in low:
        return False
    return any(
        k in text or k in low
        for k in ('在海', '海运', 'ocean', 'on the water', '哪些柜', '柜在海')
    )


def _ocean_in_history(history: list[dict[str, str]] | None) -> bool:
    for item in reversed(history or []):
        content = str(item.get('content') or '')
        if _wants_ocean(content) or 'plog' in content.lower() or '货柜中心' in content:
            return True
    return False


def _wants_ocean_overview(message: str, history: list[dict[str, str]] | None = None) -> bool:
    text = message or ''
    low = text.lower()
    keys = (
        '多少个款',
        '几个款',
        '哪些款',
        '其他款',
        '别的款',
        '还有哪些款',
        '有在海数量',
        '分别是什么',
        '分别是',
        'how many styles',
        'which styles',
        'other styles',
        'other style',
        'another style',
        '导出',
        'excel',
        'xlsx',
        '下载表格',
        '给我妈',
        'which ones',
        'list them',
        'what are they',
    )
    if _wants_received_lookup(message, history):
        return False
    if not any(k in text or k in low for k in keys):
        return False
    if extract_skus(text) and not any(k in text for k in ('多少个款', '哪些款', '其他款', '别的款')):
        return False
    return _wants_ocean(text) or _latest_lookup_topic(history) == 'ocean'


def _wants_invoice(message: str) -> bool:
    text = message or ''
    low = text.lower()
    return '发票' in text or bool(re.search(r'\binvoices?\b', low))


def _wants_order(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if 'order lookup' in low and ('how' in low or '怎么' in text):
        return False
    return '订单' in text or bool(re.search(r'\borders?\b', low)) or '查单' in text


def _wants_stock(message: str) -> bool:
    text = message or ''
    low = text.lower()
    return any(
        k in text or k in low
        for k in (
            'stock',
            '库存',
            'available',
            '可用',
            'location',
            '库位',
            'where is',
            '在哪',
            '仓位',
            'on hand',
            '颜色',
            '有货',
        )
    )


def _wants_catalog_dump(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if _wants_ocean(text) or _wants_ocean_overview(text):
        return False
    return any(
        k in text or k in low
        for k in (
            '所有style',
            '所有 style',
            '所有款',
            '全部款',
            '全部style',
            'all styles',
            'every style',
            '不止',
            '所有库存',
            '整个仓库',
            '所有颜色',
            '全部颜色',
        )
    )


def _catalog_dump_reply(message: str) -> str:
    if _prefer_zh(message):
        return (
            '不能一次列出**所有款**。聊天只适合单个 style（例如 `M1141`）。\n'
            '全库请用 Product Detail、Bag Style Viewer，或 Item Lookup。\n'
            '查库存/客户一般几秒；只有走本地模型时才会到大约 **2 分钟**超时。'
        )
    return (
        'I cannot dump **every style** in chat. Ask one style (example: `M1141`).\n'
        'For the full catalog use Product Detail, Bag Style Viewer, or Item Lookup.\n'
        'SKU lookups usually finish in a few seconds; the local model can take up to **2 minutes**.'
    )


def _wants_in_stock_colors(message: str) -> bool:
    text = message or ''
    low = text.lower()
    if '有库存的颜色' in text or '有货的颜色' in text or '哪些颜色有' in text:
        return True
    return '颜色' in text and any(k in text or k in low for k in ('库存', '有货', 'stock'))


def _skus_from_history(history: list[dict[str, str]] | None) -> list[str]:
    for item in reversed(history or []):
        found = extract_skus(str(item.get('content') or ''))
        if found:
            return found
    return []


def _collect_tools(message: str, history: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    traces = []
    skus = extract_skus(message)
    cust_hist = _wants_customer_history(message)
    buyer = _wants_customers(message)
    ocean = _wants_ocean(message)
    ocean_overview = _wants_ocean_overview(message, history)
    received_lookup = _wants_received_lookup(message, history)
    stock = _wants_stock(message)
    colors = _wants_in_stock_colors(message)
    invoice_ask = _wants_invoice(message)
    order_ask = _wants_order(message)
    how_to = any(
        k in (message or '').lower() or k in (message or '')
        for k in ('help', '帮助', 'how do i', 'how to', '怎么', '如何', 'how does')
    )
    if ocean_overview or received_lookup:
        skus = []
    elif not skus and not how_to and (stock or buyer or ocean or colors):
        skus = _skus_from_history(history)
    doc = extract_doc_number(message)
    container_id = extract_container_id(message)
    live_lookup = False
    if container_id:
        skus = [
            s
            for s in skus
            if s.replace(' ', '').upper() != container_id.replace(' ', '').upper()
            and s.replace(' ', '').upper() != container_id.replace('FT', 'F', 1).upper()
        ]

    if cust_hist:
        query = extract_customer_query(message) or doc
        traces.append(
            {
                'tool': 'get_customer_purchases',
                'args': {'query': query},
                'result': execute_tool('get_customer_purchases', {'query': query}),
            }
        )
        live_lookup = True
    if invoice_ask and doc:
        traces.append(
            {
                'tool': 'get_invoice',
                'args': {'invoice': doc},
                'result': execute_tool('get_invoice', {'invoice': doc}),
            }
        )
        live_lookup = True
    if order_ask and doc:
        traces.append(
            {
                'tool': 'get_order',
                'args': {'order': doc},
                'result': execute_tool('get_order', {'order': doc}),
            }
        )
        live_lookup = True
    if received_lookup:
        ym = _year_month_from_chat(message, history)
        if not ym:
            traces.append(
                {
                    'tool': 'list_received_styles',
                    'args': {},
                    'result': {'ok': False, 'error': 'Need a month, for example 六月 or 2026-06'},
                }
            )
        else:
            year, month = ym
            traces.append(
                {
                    'tool': 'list_received_styles',
                    'args': {'year': year, 'month': month},
                    'result': execute_tool('list_received_styles', {'year': year, 'month': month}),
                }
            )
        live_lookup = True
    if ocean_overview:
        traces.append(
            {
                'tool': 'list_ocean_styles',
                'args': {'exclude': _skus_from_history(history)},
                'result': execute_tool('list_ocean_styles', {'exclude': _skus_from_history(history)}),
            }
        )
        live_lookup = True
    elif container_id and (ocean or '柜' in (message or '') or 'container' in message.lower()):
        traces.append(
            {
                'tool': 'get_container',
                'args': {'container': container_id},
                'result': execute_tool('get_container', {'container': container_id}),
            }
        )
        live_lookup = True
    if ocean and not ocean_overview and not skus and not container_id:
        traces.append(
            {
                'tool': 'list_ocean_containers',
                'args': {},
                'result': execute_tool('list_ocean_containers', {}),
            }
        )
        live_lookup = True
    if skus:
        for sku in skus:
            if buyer:
                traces.append(
                    {
                        'tool': 'get_sku_customers',
                        'args': {'sku': sku},
                        'result': execute_tool('get_sku_customers', {'sku': sku}),
                    }
                )
                live_lookup = True
            if ocean:
                traces.append(
                    {
                        'tool': 'get_sku_ocean',
                        'args': {'sku': sku},
                        'result': execute_tool('get_sku_ocean', {'sku': sku}),
                    }
                )
                live_lookup = True
            if stock or colors or (not buyer and not ocean and not cust_hist):
                traces.append(
                    {
                        'tool': 'get_sku_inventory',
                        'args': {'sku': sku},
                        'result': execute_tool('get_sku_inventory', {'sku': sku}),
                    }
                )
                traces.append(
                    {
                        'tool': 'get_sku_locations',
                        'args': {'sku': sku},
                        'result': execute_tool('get_sku_locations', {'sku': sku}),
                    }
                )
                live_lookup = True
    elif buyer and not cust_hist:
        traces.append(
            {
                'tool': 'get_sku_customers',
                'args': {'sku': ''},
                'result': {'ok': False, 'error': 'Need a style/SKU, for example M1141'},
            }
        )
        live_lookup = True

    low = message.lower()
    wants_help = any(
        k in low or k in message
        for k in ('help', '帮助', 'how do i', 'how to', '怎么', '如何', 'how does')
    )
    if (not live_lookup) and not skus and any(
        k in low or k in message
        for k in (
            'container hub',
            '货柜',
            'loris',
            'audit',
            '审计',
            '4seller',
            'dam',
            'v7',
            '展会',
            'tradeshow',
            '缺货',
        )
    ):
        wants_help = True
    if wants_help and not buyer and not cust_hist and not ocean and not invoice_ask and not order_ask:
        help_res = execute_tool('search_help', {'query': message[:80]})
        traces.append({'tool': 'search_help', 'args': {'query': message[:80]}, 'result': help_res})
        slugs = [a.get('slug') for a in (help_res.get('results') or []) if a.get('slug')]
        if slugs:
            traces.append(
                {
                    'tool': 'get_help_article',
                    'args': {'slug': slugs[0]},
                    'result': execute_tool('get_help_article', {'slug': slugs[0]}),
                }
            )
    if any(k in low for k in ('online', '在线', 'who is on')):
        traces.append({'tool': 'list_online_users', 'args': {}, 'result': execute_tool('list_online_users', {})})
    return traces


def _chat_ollama(messages: list[dict[str, str]], model: str, base_url: str) -> str:
    cfg = ai_config()
    resp = _post_ollama(
        f'{base_url}/api/chat',
        {'model': model, 'messages': messages, 'stream': False, 'keep_alive': cfg['keep_alive']},
        timeout=_DEFAULT_TIMEOUT,
    )
    data = resp.json()
    msg = data.get('message') or {}
    text = str(msg.get('content') or '').strip()
    if text:
        return text

    parts = []
    for m in messages:
        role = m.get('role') or 'user'
        parts.append(f"{role.upper()}:\n{m.get('content') or ''}")
    prompt = '\n\n'.join(parts) + '\n\nASSISTANT:\n'
    resp = _post_ollama(
        f'{base_url}/api/generate',
        {'model': model, 'prompt': prompt, 'stream': False, 'keep_alive': cfg['keep_alive']},
        timeout=_DEFAULT_TIMEOUT,
    )
    return str((resp.json() or {}).get('response') or '').strip()


def _prefer_zh(message: str) -> bool:
    return bool(re.search(r'[\u4e00-\u9fff]', message or ''))


def _norm_sku(value: str) -> str:
    return ' '.join(str(value or '').upper().replace('-', ' ').split())


def _wh_label(code: str, zh: bool = False) -> str:
    raw = str(code or '').strip().upper().replace('-', '_').replace('（艺发）', '')
    if raw in ('E_FAS', 'EFAS', 'E_FASHION', 'EFASHION', '艺发', 'EF'):
        return 'EF'
    return 'FDC'


def _color_code(sku: str, explicit: str = '') -> str:
    if explicit:
        return str(explicit).strip().upper()
    parts = str(sku or '').split()
    return parts[-1].upper() if len(parts) > 1 else ''


_COLOR_NAMES = {
    'BK': 'BLACK',
    'BR': 'BROWN',
    'OL': 'OLIVE',
    'TN': 'TAN',
    'WN': 'WINE',
    'BG': 'BEIGE',
    'BL': 'BLUE',
    'WT': 'WHITE',
    'GY': 'GREY',
    'RD': 'RED',
    'NV': 'NAVY',
    'PK': 'PINK',
    'GN': 'GREEN',
    'KH': 'KHAKI',
    'CF': 'COFFEE',
}


def _color_label(sku: str, explicit: str = '') -> str:
    code = _color_code(sku, '')
    name = str(explicit or '').strip()
    if name and len(name) <= 4 and name.replace('.', '').isalpha() and not code:
        code = name.upper()
        name = ''
    if not name or name.upper() == code.upper():
        name = _COLOR_NAMES.get(code) or name
    if code and name and name.upper() != code.upper():
        return f'{code} {name}'
    return code or name or str(sku or '').strip()


def _qty_bits(row: dict[str, Any] | None, zh: bool) -> str:
    if not row:
        return '—'
    stock = int(row.get('stock') or 0)
    avail = int(row.get('available') or 0)
    incoming = int(row.get('incoming') or 0)
    ordered = int(row.get('ordered') or 0)
    if zh:
        bits = [f'库存 {stock}', f'可用 {avail}']
        if incoming:
            bits.append(f'在途 {incoming}')
        if ordered:
            bits.append(f'已订 {ordered}')
    else:
        bits = [f'stock {stock}', f'avail {avail}']
        if incoming:
            bits.append(f'incoming {incoming}')
        if ordered:
            bits.append(f'ordered {ordered}')
    return ', '.join(bits)


def _has_structured_lookup(traces: list[dict[str, Any]]) -> bool:
    for t in traces:
        tool = t.get('tool')
        result = t.get('result') or {}
        if tool in (
            'get_sku_inventory',
            'get_sku_locations',
            'get_sku_customers',
            'get_sku_ocean',
            'list_ocean_styles',
            'list_received_styles',
            'list_ocean_containers',
            'get_container',
            'get_customer_purchases',
            'get_invoice',
            'get_order',
            'list_online_users',
        ):
            return True
        if tool == 'get_help_article' and result.get('ok') and result.get('summary'):
            return True
        if tool == 'search_help' and result.get('ok') and result.get('results'):
            return True
    return False


def _format_tool_reply(traces: list[dict[str, Any]], message: str = '') -> str:
    if not traces:
        return ''
    zh = _prefer_zh(message)
    by_sku: dict[str, dict[str, Any]] = {}
    extras: list[str] = []

    def bucket(sku: str) -> dict[str, Any]:
        key = _norm_sku(sku) or sku
        entry = by_sku.get(key)
        if entry is None:
            entry = {'label': key, 'fdc': None, 'efas': None, 'locations': '', 'color': '', 'errors': []}
            by_sku[key] = entry
        return entry

    for t in traces:
        result = t.get('result') or {}
        tool = t.get('tool')
        if tool == 'get_sku_inventory':
            if not result.get('ok'):
                if result.get('error'):
                    extras.append(str(result.get('error')))
                continue
            for row in result.get('rows') or []:
                if row.get('error'):
                    extras.append(f"{row.get('database')}: {row.get('error')}")
                    continue
                entry = bucket(str(row.get('sku') or ''))
                if row.get('database') == 'FDC':
                    entry['fdc'] = row
                else:
                    entry['efas'] = row
                if row.get('color') and not entry.get('color'):
                    entry['color'] = str(row.get('color') or '').strip()
        elif tool == 'get_sku_locations':
            if not result.get('ok'):
                if result.get('error'):
                    extras.append(str(result.get('error')))
                continue
            if result.get('locations'):
                entry = bucket(str(result.get('sku') or ''))
                entry['locations'] = str(result.get('locations') or '')
            for m in result.get('matches') or []:
                entry = bucket(str(m.get('sku') or ''))
                entry['locations'] = str(m.get('locations') or '')
        elif tool == 'get_sku_customers':
            sku_label = str(result.get('sku') or (t.get('args') or {}).get('sku') or '').strip()
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'No customer orders found'))
                continue
            customers = result.get('customers') or []
            if zh:
                block = [f'**{sku_label or "SKU"}** 买过的客户（FDC / EF 订单，按数量）']
            else:
                block = [f'**{sku_label or "SKU"}** customers (FDC / EF orders, by qty)']
            if not customers:
                block.append('没有找到客户订单。' if zh else 'No customer orders found.')
            else:
                for c in customers:
                    name = str(c.get('customer_name') or c.get('customer_id') or '').strip()
                    cid = str(c.get('customer_id') or '').strip()
                    qty = int(c.get('qty') or 0)
                    orders = int(c.get('orders') or 0)
                    last = str(c.get('last_date') or '').strip()
                    fdc_q = int(c.get('fdc_qty') or 0)
                    ef_q = int(c.get('efas_qty') or 0)
                    label = f'[{name}](/customer?search_id={cid})' if cid else name
                    db_bits = []
                    if fdc_q:
                        db_bits.append(f'FDC {fdc_q}')
                    if ef_q:
                        db_bits.append(_wh_label('E_FAS') + f' {ef_q}')
                    if zh:
                        line = f'- {label} — **{qty}** 件 · {orders} 单'
                        if last:
                            line += f' · 最近 {last}'
                    else:
                        line = f'- {label} — **{qty}** pcs · {orders} orders'
                        if last:
                            line += f' · last {last}'
                    if db_bits:
                        line += ' · ' + ', '.join(db_bits)
                    block.append(line)
                total_qty = int(result.get('total_qty') or 0)
                count = int(result.get('customer_count') or len(customers))
                if zh:
                    block.append(f'\n共 **{count}** 家客户，合计 **{total_qty}** 件。')
                    if result.get('truncated'):
                        block.append('仅显示数量最多的客户。')
                else:
                    block.append(f'\n**{count}** customers, **{total_qty}** pcs total.')
                    if result.get('truncated'):
                        block.append('Showing the largest buyers only.')
            for err in result.get('errors') or []:
                block.append(str(err))
            extras.append('\n'.join(block))
        elif tool == 'get_sku_ocean':
            sku_label = str(result.get('sku') or (t.get('args') or {}).get('sku') or '').strip()
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'No ocean qty'))
                continue
            total = int(result.get('total_ocean') or 0)
            if zh:
                block = [f'**{sku_label or "SKU"}** 在海（plog / 货柜中心）合计 **{total}**']
            else:
                block = [f'**{sku_label or "SKU"}** ocean (plog / Boat) total **{total}**']
            by_sku_ocean: dict[str, dict[str, int]] = {}
            for row in result.get('rows') or []:
                key = str(row.get('sku') or '').strip()
                entry = by_sku_ocean.setdefault(key, {'FDC': 0, 'E_FAS': 0})
                db = str(row.get('database') or '')
                entry[db] = entry.get(db, 0) + int(row.get('ocean_qty') or 0)
            for sku_key, bits in by_sku_ocean.items():
                parts_q = []
                if bits.get('FDC'):
                    parts_q.append(f"FDC {bits['FDC']}")
                if bits.get('E_FAS'):
                    parts_q.append(_wh_label('E_FAS') + f" {bits['E_FAS']}")
                if parts_q:
                    block.append(f"- **{sku_key}**: {', '.join(parts_q)}")
            for c in result.get('containers') or []:
                po = c.get('container') or c.get('po')
                line = f"- {po}"
                if c.get('status'):
                    line += f" · {c.get('status')}"
                if c.get('eta'):
                    line += f" · ETA {c.get('eta')}"
                block.append(line)
            if not by_sku_ocean and not (result.get('containers') or []):
                block.append('没有在海数量。' if zh else 'No open ocean qty.')
            extras.append('\n'.join(block))
        elif tool == 'list_ocean_styles':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'No ocean styles'))
                continue
            count = int(result.get('style_count') or 0)
            sku_n = int(result.get('sku_count') or 0)
            total = int(result.get('total_ocean') or 0)
            if zh:
                block = [
                    f'有在海数量的款：**{count}** 个（{sku_n} 个 SKU，合计 **{total}** 件）',
                    '数量最多的款：',
                ]
            else:
                block = [
                    f'Styles with ocean qty: **{count}** ({sku_n} SKUs, **{total}** pcs)',
                    'Largest styles:',
                ]
            excluded = [str(x).split()[0].upper() for x in (result.get('excluded') or []) if x]
            for row in result.get('styles') or []:
                style = str(row.get('style') or '').strip()
                qty = int(row.get('ocean_qty') or 0)
                parts_q = []
                if row.get('fdc_qty'):
                    parts_q.append(f"FDC {int(row.get('fdc_qty') or 0)}")
                if row.get('efas_qty'):
                    parts_q.append(_wh_label('E_FAS') + f" {int(row.get('efas_qty') or 0)}")
                bit = f"- **{style}**: **{qty}**"
                if parts_q:
                    bit += ' · ' + ', '.join(parts_q)
                block.append(bit)
            if excluded:
                if zh:
                    block.append(f"{', '.join(excluded)} 没有在海；以上是其他有在海数量的款。")
                else:
                    block.append(f"{', '.join(excluded)} has no ocean qty; listing other styles that do.")
            if result.get('truncated'):
                block.append('聊天里只列数量最多的 15 个款。' if zh else 'Chat shows the top 15 styles only.')
            export_path = result.get('export_path') or '/api/fdc-ai/export/ocean-styles'
            if zh:
                block.append(f'全部 {count} 款可导出 Excel 给别人：[下载在海款式.xlsx]({export_path})')
            else:
                block.append(f'Download all {count} styles: [ocean-styles.xlsx]({export_path})')
            for err in result.get('errors') or []:
                block.append(str(err))
            extras.append('\n'.join(block))
        elif tool == 'list_received_styles':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'No received styles'))
                continue
            year = int(result.get('year') or 0)
            month = int(result.get('month') or 0)
            count = int(result.get('style_count') or 0)
            total = int(result.get('total_qty') or 0)
            if zh:
                block = [
                    f'**{year}年{month}月** 入库的款：**{count}** 个（合计 **{total}** 件）',
                    '款式：',
                ]
            else:
                block = [
                    f'Styles received **{year}-{month:02d}**: **{count}** (**{total}** pcs)',
                    'Styles:',
                ]
            for row in result.get('styles') or []:
                style = str(row.get('style') or '').strip()
                qty = int(row.get('qty') or 0)
                parts_q = []
                if row.get('fdc_qty'):
                    parts_q.append(f"FDC {int(row.get('fdc_qty') or 0)}")
                if row.get('efas_qty'):
                    parts_q.append(_wh_label('E_FAS') + f" {int(row.get('efas_qty') or 0)}")
                bit = f'- **{style}**: **{qty}**'
                if parts_q:
                    bit += ' · ' + ', '.join(parts_q)
                block.append(bit)
            if not (result.get('styles') or []):
                block.append('这个月没有入库记录。' if zh else 'No receive records for that month.')
            if result.get('truncated'):
                block.append('仅列出数量最多的款。' if zh else 'Showing the largest styles only.')
            for err in result.get('errors') or []:
                block.append(str(err))
            extras.append('\n'.join(block))
        elif tool == 'list_ocean_containers':
            rows = result.get('containers') or []
            if zh:
                block = [f'**/boat** 在海柜子 **{result.get("count") or len(rows)}**']
            else:
                block = [f'**/boat** ocean containers **{result.get("count") or len(rows)}**']
            for c in rows:
                line = f"- **{c.get('container')}**"
                if c.get('eta'):
                    line += f" · ETA {c.get('eta')}"
                if c.get('bol'):
                    line += f" · BOL {c.get('bol')}"
                block.append(line)
            if result.get('truncated'):
                block.append('仅显示部分柜子，完整列表见 /boat。' if zh else 'Showing a subset; full list on /boat.')
            extras.append('\n'.join(block))
        elif tool == 'get_container':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'Container not found'))
                continue
            cid = result.get('container')
            line = f"**{cid}** — {result.get('status') or ''}"
            if result.get('eta'):
                line += f" · ETA {result.get('eta')}"
            if result.get('etd'):
                line += f" · ETD {result.get('etd')}"
            if result.get('bol'):
                line += f" · BOL {result.get('bol')}"
            extras.append(line + '\n[/boat](/boat)')
        elif tool == 'get_customer_purchases':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'Customer not found'))
                continue
            name = result.get('customer_name') or result.get('customer_id')
            cid = result.get('customer_id') or ''
            path = result.get('path') or '/customer'
            if zh:
                block = [
                    f"**{name}** (`{cid}`) 买过的款（[/customer]({path})）",
                    f"合计 **{result.get('total_qty') or 0}** 件 · {result.get('style_count') or 0} 个 SKU",
                ]
            else:
                block = [
                    f"**{name}** (`{cid}`) purchased styles ([/customer]({path}))",
                    f"**{result.get('total_qty') or 0}** pcs · {result.get('style_count') or 0} SKUs",
                ]
            for s in result.get('styles') or []:
                sku = s.get('sku')
                qty = int(s.get('qty') or 0)
                last = s.get('last_date') or ''
                line = f"- **{sku}** — **{qty}**"
                if last:
                    line += f" · {last}"
                block.append(line)
            extras.append('\n'.join(block))
        elif tool == 'get_invoice':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'Invoice not found'))
                continue
            inv = result.get('invoice')
            path = result.get('path') or f'/invoice_lookup/{inv}'
            block = [
                f"**Invoice {inv}** ({_wh_label(str(result.get('database') or ''))}) — [{inv}]({path})",
                f"{result.get('customer_name')} (`{result.get('customer_id')}`) · {result.get('invoice_date')} · ${float(result.get('amount') or 0):,.2f}",
            ]
            if result.get('order_number'):
                block.append(f"Order {result.get('order_number')}")
            for item in (result.get('items') or [])[:15]:
                block.append(f"- {item.get('sku')} × {item.get('qty')}")
            extras.append('\n'.join(block))
        elif tool == 'get_order':
            if not result.get('ok'):
                extras.append(str(result.get('error') or 'Order not found'))
                continue
            num = result.get('order_number')
            path = result.get('path') or f'/order_lookup/{num}'
            block = [
                f"**Order {num}** ({_wh_label(str(result.get('database') or ''))}) — [{num}]({path})",
                f"{result.get('customer_name')} (`{result.get('customer_id')}`) · {result.get('order_date')} · ${float(result.get('amount') or 0):,.2f}",
            ]
            if result.get('invoice_number'):
                block.append(f"Invoice {result.get('invoice_number')}")
            elif zh:
                block.append('未开发票')
            else:
                block.append('not invoiced')
            for item in (result.get('items') or [])[:15]:
                block.append(f"- {item.get('sku')} × {item.get('qty')}")
            extras.append('\n'.join(block))
        elif tool == 'list_online_users' and result.get('ok'):
            users = result.get('users') or []
            if users:
                extras.append(
                    ('在线：' if zh else 'Online: ')
                    + ', '.join(f"{u.get('username')} ({u.get('elapsed')})" for u in users)
                )
            else:
                extras.append('目前没有其他人在线。' if zh else 'No one else is online.')
        elif tool == 'get_help_article' and result.get('ok'):
            title = result.get('title') or result.get('slug') or 'Help'
            path = result.get('path') or ''
            summary = str(result.get('summary') or '').strip()
            if zh:
                block = [f'**{title}**']
            else:
                block = [f'**{title}**']
            if summary:
                block.append(summary)
            if path:
                block.append(f'[{title}]({path})' if not zh else f'[打开说明]({path})')
            extras.append('\n'.join(block))
        elif tool == 'search_help' and result.get('ok'):
            titles = [a.get('title') for a in (result.get('results') or []) if a.get('title')]
            if titles and not any(x.get('tool') == 'get_help_article' for x in traces):
                extras.append(('帮助文章：' if zh else 'Help articles: ') + '; '.join(titles[:5]))
        elif not result.get('ok') and result.get('error'):
            extras.append(str(result.get('error')))

    parts: list[str] = []
    if by_sku:
        queried = ''
        for t in traces:
            if t.get('tool') == 'get_sku_inventory':
                queried = str((t.get('args') or {}).get('sku') or '')
                break
        if queried:
            if zh:
                parts.append(f'**{queried}** 颜色库存 — FDC / EF')
            else:
                parts.append(f'**{queried}** colors — FDC / EF')
        fdc_on_hand = 0
        efas_on_hand = 0
        fdc_parts = []
        bins: list[str] = []
        in_stock_only = _wants_in_stock_colors(message)
        listed = 0
        for entry in by_sku.values():
            fdc = entry.get('fdc') or {}
            efas = entry.get('efas') or {}
            fdc_stock = int((fdc or {}).get('stock') or 0)
            efas_stock = int((efas or {}).get('stock') or 0)
            fdc_avail = int((fdc or {}).get('available') or 0)
            efas_avail = int((efas or {}).get('available') or 0)
            fdc_on_hand += fdc_stock
            efas_on_hand += efas_stock
            loc = entry.get('locations') or ''
            for token in [b.strip() for b in str(loc).split(',') if b.strip()]:
                if token not in bins:
                    bins.append(token)
            has_qty = fdc_stock > 0 or efas_stock > 0 or fdc_avail > 0 or efas_avail > 0
            if in_stock_only and not has_qty:
                continue
            listed += 1
            color = _color_label(str(entry.get('label') or ''), str(entry.get('color') or ''))
            sku_label = entry.get('label') or ''
            if zh:
                line = (
                    f"- **{color}** (`{sku_label}`): "
                    f"{_wh_label('FDC')} {_qty_bits(entry.get('fdc'), True)} · "
                    f"{_wh_label('E_FAS')} {_qty_bits(entry.get('efas'), True)}"
                )
            else:
                line = (
                    f"- **{color}** (`{sku_label}`): "
                    f"{_wh_label('FDC')} {_qty_bits(entry.get('fdc'), False)} · "
                    f"{_wh_label('E_FAS')} {_qty_bits(entry.get('efas'), False)}"
                )
            parts.append(line)
            if fdc_stock:
                fdc_parts.append(f"{sku_label} {fdc_stock}")
        if in_stock_only and listed == 0:
            parts.append('这些颜色目前两边都没有库存。' if zh else 'No colors currently have on-hand stock.')
        if bins:
            if zh:
                parts.append(f'仓位（货位，不是颜色）：{", ".join(bins)}')
            else:
                parts.append(f'Warehouse bins (not colors): {", ".join(bins)}')
        if zh:
            parts.append(f'\nFDC 在库合计 **{fdc_on_hand}**' + (f'（{"，".join(fdc_parts)}）' if fdc_parts else ''))
            parts.append(
                f'EF 在库合计 **{efas_on_hand}**。'
                '可用数 = 库存 - 已订；可用大于库存时通常是负的已订（退单/冲减）。'
            )
        else:
            parts.append(f'\nFDC on-hand total **{fdc_on_hand}**' + (f' ({", ".join(fdc_parts)})' if fdc_parts else ''))
            parts.append(
                f'EF on-hand total **{efas_on_hand}**. '
                'Available = stock - ordered; available above stock usually means a negative ordered qty (credit/return).'
            )
    parts.extend(extras)
    return '\n'.join(parts).strip()


def run_agent_turn(message: str, history: list[dict[str, str]] | None = None, context: dict[str, str] | None = None) -> dict[str, Any]:
    context = context or {}
    username = context.get('username') or ''
    history = list(history or [])[-16:]
    if _wants_catalog_dump(message):
        reply = _catalog_dump_reply(message)
        health = check_llm_health()
        new_history = history + [
            {'role': 'user', 'content': message},
            {'role': 'assistant', 'content': reply},
        ]
        return {
            'ok': True,
            'reply': reply,
            'tool_trace': [],
            'health': health,
            'history': new_history[-20:],
        }
    traces = _collect_tools(message, history=history)
    health = check_llm_health()
    if _force_cpu and _is_loopback(str(health.get('base_url') or '')):
        health = dict(health)
        health['inference'] = 'cpu'
    structured = _format_tool_reply(traces, message)

    # SKU / customer lookups are formatted from DB so a GPU crash cannot blank the answer.
    if _has_structured_lookup(traces) and structured:
        reply = structured
    elif health.get('ok') and health.get('model_ready'):
        tool_blob = json.dumps(traces, ensure_ascii=False, default=str)[:4000]
        messages = [{'role': 'system', 'content': _system_prompt(username)}]
        messages.extend(history)
        user_content = message
        if traces:
            user_content = (
                f'{message}\n\nRead-only tool results (JSON):\n{tool_blob}\n'
                'Answer using these facts. Do not invent stock numbers.'
            )
        messages.append({'role': 'user', 'content': user_content})
        try:
            reply = _chat_ollama(messages, health['model'], health['base_url']) or structured or 'No response from the local model.'
        except Exception:
            reply = structured or (
                'The local model could not answer (GPU CUDA is incompatible on this PC; CPU retry failed). '
                'SKU lookups still work if you paste a style like M1141.'
            )
            if health.get('hint') and 'llama3.2' in str(health.get('hint')):
                reply += f"\n\n{health.get('hint')}"
    else:
        reply = structured or 'Ollama is offline. I can still look up a SKU if you paste one (example: M1141 BK).'
        if not structured and health.get('hint'):
            reply += f"\n\n{health.get('hint')}"

    new_history = history + [
        {'role': 'user', 'content': message},
        {'role': 'assistant', 'content': reply},
    ]
    return {
        'ok': True,
        'reply': reply,
        'tool_trace': traces,
        'health': health,
        'history': new_history[-20:],
    }


def starter_suggestions() -> list[str]:
    return [
        'M1141 谁买过',
        'M1141 在海多少',
        'MACY\'S BACKSTAGE 买了什么',
        'Who is online?',
    ]
