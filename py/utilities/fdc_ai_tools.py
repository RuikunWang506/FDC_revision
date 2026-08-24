"""Read-only allowlisted tools for the FDC local AI assistant."""
from __future__ import annotations

import re
from typing import Any

from py.utilities.database_utils import get_mysql_connection, get_sql_server_connection_readonly
from py.utilities.setting import Config

_MAX_ROWS = 25
_MAX_CUSTOMERS = 30
_SKU_RE = re.compile(r'\b([A-Za-z]{1,4}\d{3,6}(?:-[A-Za-z0-9]{1,8})?)\b')
_WRITE_RE = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|MERGE|GRANT|REVOKE|CREATE|REPLACE|EXEC|EXECUTE)\b',
    re.I,
)

TOOL_NAMES = (
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
    'search_help',
    'get_help_article',
    'list_online_users',
)

TOOL_METADATA = {
    'get_sku_inventory': {
        'description': 'Read-only FDC + EF stock, available, incoming for one SKU',
        'args': 'sku',
    },
    'get_sku_locations': {
        'description': 'Read-only warehouse locations for one SKU from MySQL products',
        'args': 'sku',
    },
    'get_sku_customers': {
        'description': (
            'Which customers bought a style/SKU (FDC + EF ord_log). '
            'Use for 谁买过 / who bought / customers, not for warehouse stock.'
        ),
        'args': 'sku',
    },
    'get_sku_ocean': {
        'description': 'Ocean/in-transit qty from plog (same as Product Detail / Boat), plus matching /boat containers',
        'args': 'sku',
    },
    'list_ocean_styles': {
        'description': (
            'Count distinct styles with open ocean qty (plog). '
            'Use for 多少个款有在海 / 其他款 / which styles are on the water, not a single SKU.'
        ),
        'args': '',
    },
    'list_received_styles': {
        'description': (
            'Styles received into the warehouse in a month from plog (hdl_chg > 0). '
            'Use for 入库 / 收货 / received in June, and follow-ups 分别是什么.'
        ),
        'args': 'year, month',
    },
    'list_ocean_containers': {
        'description': 'Containers currently Ocean on /boat (cached hub rows)',
        'args': '',
    },
    'get_container': {
        'description': 'One /boat container row by FDC container or BOL',
        'args': 'container',
    },
    'get_customer_purchases': {
        'description': 'What a customer bought (same source as /customer). Name or customer ID.',
        'args': 'query',
    },
    'get_invoice': {
        'description': 'Invoice header + lines from /invoice_lookup (5-digit FDC, 4-digit EF)',
        'args': 'invoice',
    },
    'get_order': {
        'description': 'Sales order header + lines from /order_lookup (5-digit FDC, 4-digit EF)',
        'args': 'order',
    },
    'search_help': {'description': 'Search FDC help articles', 'args': 'query'},
    'get_help_article': {'description': 'Load one help article by slug', 'args': 'slug'},
    'list_online_users': {'description': 'Who is currently online and how long', 'args': ''},
}


def extract_skus(text: str) -> list[str]:
    found = []
    seen = set()
    for m in _SKU_RE.finditer(text or ''):
        sku = m.group(1).strip().upper().replace('-', ' ')
        sku = ' '.join(sku.split())
        if sku in seen:
            continue
        seen.add(sku)
        found.append(sku)
        if len(found) >= 5:
            break
    return found


_DOC_RE = re.compile(r'\b(\d{4,5})\b')
_ISO_CONTAINER_RE = re.compile(r'\b([A-Z]{4}\d{6,7})\b', re.I)
_FDC_CONTAINER_RE = re.compile(r'\b(FT?\d{5,6})\b', re.I)
_YEAR_RE = re.compile(r'^20[2-3]\d$')
_ZH_MONTHS = {
    '一': 1,
    '二': 2,
    '三': 3,
    '四': 4,
    '五': 5,
    '六': 6,
    '七': 7,
    '八': 8,
    '九': 9,
    '十': 10,
    '十一': 11,
    '十二': 12,
}
_EN_MONTHS = {
    'january': 1,
    'jan': 1,
    'february': 2,
    'feb': 2,
    'march': 3,
    'mar': 3,
    'april': 4,
    'apr': 4,
    'may': 5,
    'june': 6,
    'jun': 6,
    'july': 7,
    'jul': 7,
    'august': 8,
    'aug': 8,
    'september': 9,
    'sep': 9,
    'sept': 9,
    'october': 10,
    'oct': 10,
    'november': 11,
    'nov': 11,
    'december': 12,
    'dec': 12,
}


def extract_year_month(text: str, default_year: int | None = None) -> tuple[int, int] | None:
    from datetime import datetime as dt

    raw = text or ''
    low = raw.lower()
    year = default_year or dt.now().year
    ym = re.search(r'(20[2-3]\d)\s*[-/年.]?\s*(1[0-2]|0?[1-9])\s*月?', raw)
    if ym:
        return int(ym.group(1)), int(ym.group(2))
    if '去年' in raw:
        year -= 1
    zh = re.search(r'(十二|十一|[一二三四五六七八九十]|1[0-2]|0?[1-9])\s*月', raw)
    if zh:
        token = zh.group(1)
        month = _ZH_MONTHS.get(token) or int(token)
        return year, month
    for name, month in _EN_MONTHS.items():
        if re.search(rf'\b{name}\b', low):
            y = re.search(r'(20[2-3]\d)', raw)
            return (int(y.group(1)) if y else year), month
    return None


def extract_doc_number(text: str) -> str:
    for m in _DOC_RE.finditer(text or ''):
        num = m.group(1)
        if _YEAR_RE.match(num):
            continue
        return num
    return ''


def extract_container_id(text: str) -> str:
    raw = text or ''
    iso = _ISO_CONTAINER_RE.search(raw.upper())
    if iso:
        return iso.group(1).upper()
    fdc = _FDC_CONTAINER_RE.search(raw.upper())
    if fdc:
        return fdc.group(1).upper()
    return ''


def extract_customer_query(text: str) -> str:
    raw = text or ''
    m = re.search(r'what did (.+?) buy', raw, flags=re.I)
    if m:
        return m.group(1).strip(' ?？,.')
    q = re.sub(
        r'(买了什么|买过哪些款|买过什么|买过哪些|采购历史|purchase history|what has .+ bought)',
        ' ',
        raw,
        flags=re.I,
    )
    q = re.sub(r'(客户|客人|customer)\s*(?:id|#)?\s*', ' ', q, flags=re.I)
    return ' '.join(q.split()).strip(' ?？,.')


def _oms_db_for_doc(number: str) -> tuple[str, str]:
    n = (number or '').strip()
    if len(n) == 4:
        return 'database_oms_ef', 'E_FAS'
    return 'database_fdc', 'FDC'


def _iter_oms_dbs():
    cfg = Config.SQL_SERVER_CONFIG
    return (
        (cfg['database_fdc'], 'FDC'),
        (cfg['database_oms_ef'], 'E_FAS'),
    )


def _assert_select_only(sql: str) -> None:
    compact = ' '.join((sql or '').split())
    if _WRITE_RE.search(compact):
        raise ValueError('Refusing non-SELECT SQL')
    if not compact.upper().lstrip().startswith('SELECT'):
        raise ValueError('Only SELECT is allowed')


def _fetch_sku_inventory(sku: str) -> dict[str, Any]:
    sku = (sku or '').strip()
    if not sku:
        return {'ok': False, 'error': 'sku is required'}
    sql = """
        SELECT
            i.PROD_CD AS SKU,
            i.ALT_CD AS Style,
            i.DESCRIP AS Description,
            ISNULL(id.in_stock, 0) AS Stock,
            ISNULL(id.ORDER_QTY, 0) AS Ordered,
            ISNULL((id.in_stock - id.order_qty), 0) AS Available,
            ISNULL(id.on_order_qty, 0) AS Incoming,
            ISNULL(i.UNIT_COLOR, '') AS Color
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD = ?
    """
    _assert_select_only(sql)
    rows = []
    cfg = Config.SQL_SERVER_CONFIG
    for db_key, label in (('database_fdc', 'FDC'), ('database_oms_ef', 'E_FAS')):
        conn = None
        try:
            conn = get_sql_server_connection_readonly(cfg[db_key])
            cur = conn.cursor()
            cur.execute(sql, (sku,))
            fetched = cur.fetchall()
            if not fetched:
                like_sql = """
                    SELECT TOP 25
                        i.PROD_CD AS SKU,
                        i.ALT_CD AS Style,
                        i.DESCRIP AS Description,
                        ISNULL(id.in_stock, 0) AS Stock,
                        ISNULL(id.ORDER_QTY, 0) AS Ordered,
                        ISNULL((id.in_stock - id.order_qty), 0) AS Available,
                        ISNULL(id.on_order_qty, 0) AS Incoming,
                        ISNULL(i.UNIT_COLOR, '') AS Color
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE i.PROD_CD LIKE ?
                """
                _assert_select_only(like_sql)
                cur.execute(like_sql, (sku + '%',))
                fetched = cur.fetchall()
            for row in fetched[:_MAX_ROWS]:
                rows.append(
                    {
                        'database': label,
                        'sku': str(row[0] or '').strip(),
                        'style': str(row[1] or '').strip(),
                        'description': str(row[2] or '').strip(),
                        'stock': int(row[3] or 0),
                        'ordered': int(row[4] or 0),
                        'available': int(row[5] or 0),
                        'incoming': int(row[6] or 0),
                        'color': str(row[7] or '').strip(),
                    }
                )
        except Exception as exc:
            rows.append({'database': label, 'error': str(exc)})
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    if not rows:
        return {'ok': False, 'error': f'SKU {sku} not found'}
    return {'ok': True, 'sku': sku, 'rows': rows}


def _fetch_sku_customers(sku: str) -> dict[str, Any]:
    """Aggregate customers who ordered this style/SKU from FDC + E-Fashion ord_log."""
    sku = (sku or '').strip()
    if not sku:
        return {'ok': False, 'error': 'sku is required'}
    sql = """
        SELECT TOP 40
            LTRIM(RTRIM(ISNULL(c.cus_nm, ol.cus_id))) AS customer_name,
            LTRIM(RTRIM(ol.cus_id)) AS customer_id,
            SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)) AS qty,
            COUNT(DISTINCT ol.ord_num) AS orders,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            CAST(YEAR(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR) AS last_date,
            MAX(CAST(ol.olg_dt AS INT)) AS last_olg
        FROM dbo.ord_log ol
        LEFT JOIN dbo.customer c ON ol.cus_id = c.cus_id
        WHERE ol.prod_cd LIKE ?
        GROUP BY LTRIM(RTRIM(ISNULL(c.cus_nm, ol.cus_id))), LTRIM(RTRIM(ol.cus_id))
        ORDER BY SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)) DESC
    """
    _assert_select_only(sql)
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    cfg = Config.SQL_SERVER_CONFIG
    for db_key, label in (('database_fdc', 'FDC'), ('database_oms_ef', 'E_FAS')):
        conn = None
        try:
            conn = get_sql_server_connection_readonly(cfg[db_key])
            cur = conn.cursor()
            cur.execute(sql, (sku + '%',))
            for row in cur.fetchall():
                name = str(row[0] or '').strip()
                cid = str(row[1] or '').strip()
                qty = int(row[2] or 0)
                orders = int(row[3] or 0)
                last_date = str(row[4] or '').strip()
                last_olg = int(row[5] or 0)
                key = (cid or name).upper()
                if not key:
                    continue
                entry = merged.get(key)
                if entry is None:
                    entry = {
                        'customer_name': name or cid,
                        'customer_id': cid,
                        'qty': 0,
                        'orders': 0,
                        'last_date': last_date,
                        'last_olg': last_olg,
                        'fdc_qty': 0,
                        'efas_qty': 0,
                    }
                    merged[key] = entry
                entry['qty'] += qty
                entry['orders'] += orders
                if label == 'FDC':
                    entry['fdc_qty'] += qty
                else:
                    entry['efas_qty'] += qty
                if last_olg > int(entry.get('last_olg') or 0):
                    entry['last_date'] = last_date
                    entry['last_olg'] = last_olg
        except Exception as exc:
            errors.append(f'{label}: {exc}')
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    customers = sorted(merged.values(), key=lambda c: int(c.get('qty') or 0), reverse=True)
    truncated = len(customers) > _MAX_CUSTOMERS
    customers = customers[:_MAX_CUSTOMERS]
    for c in customers:
        c.pop('last_olg', None)
    if not customers and errors:
        return {'ok': False, 'sku': sku, 'error': '; '.join(errors)}
    return {
        'ok': True,
        'sku': sku,
        'customers': customers,
        'customer_count': len(customers),
        'total_qty': sum(int(c.get('qty') or 0) for c in customers),
        'truncated': truncated,
        'errors': errors,
    }


def _fetch_sku_locations(sku: str) -> dict[str, Any]:
    sku = (sku or '').strip()
    if not sku:
        return {'ok': False, 'error': 'sku is required'}
    sql = """
        SELECT SKU, GROUP_CONCAT(DISTINCT Location ORDER BY Location SEPARATOR ', ') AS locations
        FROM products
        WHERE LOWER(TRIM(SKU)) = %s AND Location IS NOT NULL AND Location != ''
        GROUP BY SKU
    """
    _assert_select_only(sql)
    conn = get_mysql_connection()
    if not conn:
        return {'ok': False, 'error': 'MySQL unavailable'}
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(sql, (sku.lower(),))
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT SKU, GROUP_CONCAT(DISTINCT Location ORDER BY Location SEPARATOR ', ') AS locations
                FROM products
                WHERE LOWER(SKU) LIKE %s AND Location IS NOT NULL AND Location != ''
                GROUP BY SKU
                LIMIT 10
                """,
                (f'{sku.lower()}%',),
            )
            matches = cursor.fetchall()
            if not matches:
                return {'ok': False, 'error': f'No location for {sku}'}
            return {
                'ok': True,
                'sku': sku,
                'matches': [{'sku': r[0], 'locations': r[1]} for r in matches],
            }
        return {'ok': True, 'sku': row[0], 'locations': row[1]}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn.is_connected():
                conn.close()
        except Exception:
            pass


_HELP_STOP = {
    'how', 'do', 'i', 'the', 'a', 'an', 'to', 'use', 'using', 'what', 'is', 'for',
    'my', 'in', 'of', 'and', 'or', 'can', 'we', 'you', 'this', 'that', 'page',
    'please', 'show', 'me', 'with',
}
_HELP_HINTS = (
    ('container hub', 'container hub'),
    ('货柜中心', '货柜中心'),
    ('货柜', '货柜中心'),
    ('客户分析', 'customer analytics'),
    ('客户查询', 'customer analytics'),
    ('调艺发', 'manual adjuster'),
    ('boat', 'container'),
    ('loris', 'loris'),
    ('贴纸', 'loris'),
    ('fedex', 'loris'),
    ('v7', 'v7'),
    ('list creator', 'v7'),
    ('audit', 'audit'),
    ('审计', '审计'),
    ('4seller', '4seller'),
    ('dam', 'dam'),
    ('master image', 'image'),
    ('图片', '图片'),
    ('客户分析', 'customer analytics'),
    ('客户查询', 'customer analytics'),
    ('tradeshow', 'trade'),
    ('展会', '展会'),
    ('调艺发', 'manual adjuster'),
    ('缺货', '缺货'),
    ('oosc', 'stock'),
    ('收藏', '收藏'),
    ('首页', '首页'),
    ('invoice', 'invoice'),
    ('发票', '发票'),
    ('order lookup', 'order'),
    ('订单', '订单'),
    ('warehouse map', 'warehouse'),
    ('仓位', '仓位'),
    ('栈板', '栈板'),
    ('pallet', 'pallet'),
    ('print center', 'print'),
    ('打印', '打印'),
    ('product detail', 'product'),
    ('产品详情', '产品'),
    ('sku lookup', 'sku'),
    ('存货', '存货'),
)


def _help_queries(raw: str) -> list[str]:
    text = (raw or '').strip()
    if not text:
        return []
    low = text.lower()
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = ' '.join((q or '').split())
        key = q.lower()
        if len(q) < 2 or key in seen:
            return
        seen.add(key)
        queries.append(q)

    for needle, term in _HELP_HINTS:
        if needle in text or needle in low:
            add(term)
    for tok in re.findall(r'[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{2,4}', text):
        if tok.lower() in _HELP_STOP:
            continue
        add(tok)
    if not queries:
        add(text[:40])
    return queries[:8]


def _search_help(query: str) -> dict[str, Any]:
    from py.utilities.help_content import article_summary, list_articles, seed_help_articles_if_needed

    q = (query or '').strip()
    if not q:
        return {'ok': False, 'error': 'query is required'}
    try:
        seed_help_articles_if_needed()
    except Exception:
        pass
    by_slug: dict[str, dict[str, Any]] = {}
    terms = _help_queries(q)
    raw_low = q.lower()
    for term in terms:
        for a in list_articles(search=term)[:12]:
            slug = str(a.get('slug') or '')
            if not slug or slug in by_slug:
                continue
            title = str(a.get('title') or '')
            title_zh = str(a.get('title_zh') or '')
            hay = f'{title} {title_zh} {slug.replace("-", " ")}'.lower()
            score = 0
            for t in terms:
                tl = t.lower()
                if tl in hay:
                    score += 25 if tl in f'{title} {title_zh}'.lower() else 12
            if slug in (
                'getting-started',
                'system-overview',
                'terminology-glossary',
                'page-home',
            ):
                score -= 15
            if any(k in raw_low or k in q for k in ('怎么', 'how', '如何')) and slug.startswith('page-'):
                score += 4
            by_slug[slug] = {
                'slug': slug,
                'title': a.get('title'),
                'category': a.get('category'),
                'summary': article_summary(a.get('body_html') or '', 280),
                '_score': score,
            }
    ranked = sorted(by_slug.values(), key=lambda r: int(r.get('_score') or 0), reverse=True)[:8]
    for r in ranked:
        r.pop('_score', None)
    return {'ok': True, 'query': q, 'results': ranked}


def _get_help_article(slug: str) -> dict[str, Any]:
    from py.utilities.help_content import article_summary, get_article_by_slug, seed_help_articles_if_needed

    try:
        seed_help_articles_if_needed()
    except Exception:
        pass
    art = get_article_by_slug((slug or '').strip())
    if not art:
        return {'ok': False, 'error': f'article {slug} not found'}
    body = (art.get('body_html') or '')[:4000]
    return {
        'ok': True,
        'slug': art.get('slug'),
        'title': art.get('title'),
        'summary': article_summary(body, 900),
        'path': f"/help/{art.get('slug')}",
    }


def _list_online_users() -> dict[str, Any]:
    from py.utilities.online_presence import list_online_users

    users = list_online_users()
    return {
        'ok': True,
        'count': len(users),
        'users': [
            {'username': u.get('username'), 'elapsed': u.get('elapsed_display')}
            for u in users
        ],
    }


def _norm_container(value: str) -> str:
    text = str(value or '').strip().upper()
    if text.startswith('FT') and text[2:].isdigit():
        return 'F' + text[2:]
    return text


def _boat_rows() -> list[dict[str, Any]]:
    try:
        from py.utilities.boat_sheet_cache import load_latest_cached_boat_payload

        payload = load_latest_cached_boat_payload() or {}
        return list(payload.get('data') or [])
    except Exception:
        return []


def _fetch_sku_ocean(sku: str) -> dict[str, Any]:
    from py.utilities.ocean_qty import fetch_ocean_pos_by_style_prefix, fetch_ocean_qty_by_style_prefix

    sku = (sku or '').strip()
    if not sku:
        return {'ok': False, 'error': 'sku is required'}
    qty_rows: list[dict[str, Any]] = []
    pos: list[str] = []
    errors: list[str] = []
    cfg = Config.SQL_SERVER_CONFIG
    for db_key, label in (('database_fdc', 'FDC'), ('database_oms_ef', 'E_FAS')):
        conn = None
        try:
            conn = get_sql_server_connection_readonly(cfg[db_key])
            qty_map = fetch_ocean_qty_by_style_prefix(conn, sku)
            po_map = fetch_ocean_pos_by_style_prefix(conn, sku)
            for sku_key, qty in sorted(qty_map.items()):
                qty_rows.append({'database': label, 'sku': sku_key, 'ocean_qty': int(qty or 0)})
            for style_pos in po_map.values():
                for po in style_pos:
                    if po and po not in pos:
                        pos.append(po)
        except Exception as exc:
            errors.append(f'{label}: {exc}')
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    boat_by_id = {}
    for row in _boat_rows():
        cid = _norm_container(str(row.get('container') or ''))
        bol = _norm_container(str(row.get('bol') or ''))
        if cid:
            boat_by_id[cid] = row
        if bol:
            boat_by_id.setdefault(bol, row)
    containers = []
    for po in pos:
        row = boat_by_id.get(_norm_container(po))
        item = {'po': po}
        if row:
            item.update(
                {
                    'container': row.get('container'),
                    'status': row.get('status_label') or row.get('status_key') or row.get('action_mode'),
                    'eta': row.get('eta') or '',
                    'etd': row.get('etd') or '',
                    'rcd': row.get('rcd') or '',
                    'bol': row.get('bol') or '',
                }
            )
        containers.append(item)
    total = sum(int(r.get('ocean_qty') or 0) for r in qty_rows)
    if not qty_rows and not containers and errors:
        return {'ok': False, 'sku': sku, 'error': '; '.join(errors)}
    return {
        'ok': True,
        'sku': sku,
        'total_ocean': total,
        'rows': qty_rows[:40],
        'containers': containers[:20],
        'errors': errors,
        'source': 'plog + /boat',
    }


def _wh_name(label: str) -> str:
    raw = str(label or '').strip().upper()
    if raw in ('E_FAS', 'EFAS', 'EF'):
        return 'EF'
    return 'FDC'


def _collect_ocean_inventory() -> dict[str, Any]:
    from py.utilities.ocean_qty import fetch_all_open_ocean_qty, style_from_sku

    merged: dict[str, dict[str, int]] = {}
    sku_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    cfg = Config.SQL_SERVER_CONFIG
    for db_key, label in (('database_fdc', 'FDC'), ('database_oms_ef', 'E_FAS')):
        conn = None
        try:
            conn = get_sql_server_connection_readonly(cfg[db_key])
            qty_map = fetch_all_open_ocean_qty(conn)
            for sku, qty in qty_map.items():
                qty_i = int(qty or 0)
                if qty_i <= 0:
                    continue
                style = style_from_sku(sku)
                if not style:
                    continue
                entry = merged.setdefault(style, {'FDC': 0, 'E_FAS': 0, 'sku_count': 0, 'ocean_qty': 0})
                entry[label] = entry.get(label, 0) + qty_i
                entry['ocean_qty'] += qty_i
                entry['sku_count'] += 1
                sku_rows.append(
                    {
                        'style': style,
                        'sku': sku,
                        'warehouse': _wh_name(label),
                        'ocean_qty': qty_i,
                    }
                )
        except Exception as exc:
            errors.append(f'{_wh_name(label)}: {exc}')
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1]['ocean_qty'], kv[0]))
    styles = [
        {
            'style': style,
            'ocean_qty': bits['ocean_qty'],
            'sku_count': bits['sku_count'],
            'fdc_qty': bits.get('FDC') or 0,
            'efas_qty': bits.get('E_FAS') or 0,
        }
        for style, bits in ranked
    ]
    sku_rows.sort(key=lambda r: (-int(r.get('ocean_qty') or 0), str(r.get('style') or ''), str(r.get('sku') or '')))
    return {
        'ok': True,
        'style_count': len(styles),
        'sku_count': len(sku_rows),
        'total_ocean': sum(s['ocean_qty'] for s in styles),
        'styles': styles,
        'sku_rows': sku_rows,
        'errors': errors,
        'source': 'plog',
        'path': '/boat',
        'export_path': '/api/fdc-ai/export/ocean-styles',
    }


def _summarize_ocean_styles(exclude_styles: list[str] | None = None) -> dict[str, Any]:
    from py.utilities.ocean_qty import style_from_sku

    data = _collect_ocean_inventory()
    exclude = {style_from_sku(s) for s in (exclude_styles or []) if style_from_sku(s)}
    others = [row for row in (data.get('styles') or []) if str(row.get('style') or '') not in exclude]
    out = dict(data)
    out['excluded'] = sorted(exclude)
    out['styles'] = others[:15]
    out['truncated'] = len(others) > 15
    out.pop('sku_rows', None)
    return out


def build_ocean_styles_xlsx() -> tuple[bytes, str]:
    from datetime import datetime
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    data = _collect_ocean_inventory()
    if not data.get('ok'):
        raise RuntimeError(data.get('error') or 'ocean export failed')
    stamp = datetime.now().strftime('%Y-%m-%d')
    filename = f'FDC在海款式_{stamp}.xlsx'
    wb = Workbook()
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='1F4E79')
    total_fill = PatternFill('solid', fgColor='D6EAF8')
    thin = Border(
        left=Side(style='thin', color='BFBFBF'),
        right=Side(style='thin', color='BFBFBF'),
        top=Side(style='thin', color='BFBFBF'),
        bottom=Side(style='thin', color='BFBFBF'),
    )

    def _paint(ws, headers, rows, widths):
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
        for row in rows:
            ws.append(row)
            for cell in ws[ws.max_row]:
                cell.border = thin
                if isinstance(cell.value, int):
                    cell.alignment = Alignment(horizontal='right')
        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = 'A2'
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width

    ws = wb.active
    ws.title = '按款汇总'
    ws['A1'] = f"有在海数量的款 {data.get('style_count') or 0} 个 · SKU {data.get('sku_count') or 0} · 合计 {data.get('total_ocean') or 0} 件 · {stamp}"
    ws.merge_cells('A1:E1')
    ws['A1'].font = Font(bold=True, size=13)
    ws.append(['款号', 'FDC在海', 'EF在海', '合计', 'SKU数'])
    for cell in ws[2]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center')
    for row in data.get('styles') or []:
        ws.append(
            [
                row.get('style'),
                int(row.get('fdc_qty') or 0),
                int(row.get('efas_qty') or 0),
                int(row.get('ocean_qty') or 0),
                int(row.get('sku_count') or 0),
            ]
        )
        for cell in ws[ws.max_row]:
            cell.border = thin
    last = ws.max_row + 1
    ws.append(
        [
            '合计',
            sum(int(r.get('fdc_qty') or 0) for r in (data.get('styles') or [])),
            sum(int(r.get('efas_qty') or 0) for r in (data.get('styles') or [])),
            int(data.get('total_ocean') or 0),
            int(data.get('sku_count') or 0),
        ]
    )
    for cell in ws[last]:
        cell.font = Font(bold=True)
        cell.fill = total_fill
        cell.border = thin
    ws.auto_filter.ref = f'A2:E{max(2, last - 1)}'
    ws.freeze_panes = 'A3'
    for idx, width in enumerate((14, 12, 12, 12, 10), start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width

    ws2 = wb.create_sheet('按SKU明细')
    _paint(
        ws2,
        ['款号', 'SKU', '仓库', '在海数量'],
        [
            [r.get('style'), r.get('sku'), r.get('warehouse'), int(r.get('ocean_qty') or 0)]
            for r in (data.get('sku_rows') or [])
        ],
        (14, 22, 10, 12),
    )
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue(), filename


def _oms_actual_date_sql(column: str) -> str:
    return f"DATEADD(DAY, DATEDIFF(DAY, '19000101', {column}) - 81088, '20230101')"


def _list_received_styles(year: int, month: int) -> dict[str, Any]:
    from calendar import monthrange
    from datetime import date

    from py.utilities.ocean_qty import style_from_sku

    year = int(year)
    month = int(month)
    if month < 1 or month > 12 or year < 2020 or year > 2035:
        return {'ok': False, 'error': 'Need a valid year/month, for example 2026-06'}
    start = date(year, month, 1)
    end_month = month + 1 if month < 12 else 1
    end_year = year if month < 12 else year + 1
    end = date(end_year, end_month, 1)
    rec_sql = _oms_actual_date_sql('pl.log_date')
    sql = f"""
        SELECT RTRIM(pl.prod_cd) AS sku,
               SUM(CAST(ISNULL(pl.hdl_chg, 0) AS INT)) AS qty
        FROM dbo.plog pl
        WHERE ISNULL(pl.hdl_chg, 0) > 0
          AND {rec_sql} >= ?
          AND {rec_sql} < ?
        GROUP BY RTRIM(pl.prod_cd)
    """
    _assert_select_only(sql)
    merged: dict[str, dict[str, int]] = {}
    errors: list[str] = []
    cfg = Config.SQL_SERVER_CONFIG
    for db_key, label in (('database_fdc', 'FDC'), ('database_oms_ef', 'E_FAS')):
        conn = None
        try:
            conn = get_sql_server_connection_readonly(cfg[db_key])
            cur = conn.cursor()
            cur.execute(sql, (start.isoformat(), end.isoformat()))
            for sku, qty in cur.fetchall() or []:
                qty_i = int(qty or 0)
                if qty_i <= 0:
                    continue
                style = style_from_sku(str(sku or ''))
                if not style:
                    continue
                entry = merged.setdefault(style, {'FDC': 0, 'E_FAS': 0, 'sku_count': 0, 'qty': 0})
                entry[label] = entry.get(label, 0) + qty_i
                entry['qty'] += qty_i
                entry['sku_count'] += 1
        except Exception as exc:
            errors.append(f'{label}: {exc}')
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    ranked = sorted(merged.items(), key=lambda kv: (-kv[1]['qty'], kv[0]))
    styles = [
        {
            'style': style,
            'qty': bits['qty'],
            'sku_count': bits['sku_count'],
            'fdc_qty': bits.get('FDC') or 0,
            'efas_qty': bits.get('E_FAS') or 0,
        }
        for style, bits in ranked
    ]
    return {
        'ok': True,
        'year': year,
        'month': month,
        'style_count': len(styles),
        'sku_count': sum(s['sku_count'] for s in styles),
        'total_qty': sum(s['qty'] for s in styles),
        'styles': styles[:80],
        'truncated': len(styles) > 80,
        'errors': errors,
        'source': 'plog receive',
        'days_in_month': monthrange(year, month)[1],
    }


def _list_ocean_containers() -> dict[str, Any]:
    ocean = []
    for row in _boat_rows():
        mode = str(row.get('action_mode') or row.get('status_key') or '').strip().lower()
        if mode not in ('ocean', 'port'):
            continue
        cid = str(row.get('container') or '').strip()
        if not cid or cid.upper().startswith('L'):
            continue
        ocean.append(
            {
                'container': cid,
                'status': row.get('status_label') or mode,
                'eta': row.get('eta') or '',
                'etd': row.get('etd') or '',
                'rcd': row.get('rcd') or '',
                'bol': row.get('bol') or '',
                'vessel': row.get('vessel_no') or '',
            }
        )
    return {
        'ok': True,
        'count': len(ocean),
        'containers': ocean[:25],
        'truncated': len(ocean) > 25,
        'path': '/boat',
    }


def _fetch_container(container_id: str) -> dict[str, Any]:
    want = _norm_container(container_id)
    if not want:
        return {'ok': False, 'error': 'container is required'}
    for row in _boat_rows():
        cid = _norm_container(str(row.get('container') or ''))
        bol = _norm_container(str(row.get('bol') or ''))
        if want not in {cid, bol}:
            continue
        return {
            'ok': True,
            'container': row.get('container'),
            'status': row.get('status_label') or row.get('status_key') or row.get('action_mode'),
            'eta': row.get('eta') or '',
            'etd': row.get('etd') or '',
            'rcd': row.get('rcd') or '',
            'bol': row.get('bol') or '',
            'vessel': row.get('vessel_no') or '',
            'in_oms': bool(row.get('in_oms')),
            'path': '/boat',
        }
    return {'ok': False, 'error': f'Container {container_id} not on /boat'}


def _fetch_customer_purchases(query: str) -> dict[str, Any]:
    q = (query or '').strip()
    if not q:
        return {'ok': False, 'error': 'customer name or ID is required'}
    from py.other.customer import resolve_customer_by_id, resolve_customer_by_name

    match = None
    if re.fullmatch(r'\d{4,6}', q):
        match = resolve_customer_by_id(q)
    if not match:
        match = resolve_customer_by_name(q)
    if not match:
        return {'ok': False, 'error': f'Customer {q} not found on /customer'}
    cid = str(match.get('cus_id') or '').strip()
    name = str(match.get('cus_nm') or cid).strip()
    sql = """
        SELECT TOP 40
            LTRIM(RTRIM(ol.prod_cd)) AS sku,
            SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)) AS qty,
            COUNT(DISTINCT ol.ord_num) AS orders,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            CAST(YEAR(DATEADD(DAY, MAX(CAST(ol.olg_dt AS INT)) - 81088, '20230101')) AS VARCHAR) AS last_date
        FROM dbo.ord_log ol
        WHERE LTRIM(RTRIM(ol.cus_id)) = ?
        GROUP BY LTRIM(RTRIM(ol.prod_cd))
        ORDER BY SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)) DESC
    """
    _assert_select_only(sql)
    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for db_name, label in _iter_oms_dbs():
        conn = None
        try:
            conn = get_sql_server_connection_readonly(db_name)
            cur = conn.cursor()
            cur.execute(sql, (cid,))
            for row in cur.fetchall():
                sku = str(row[0] or '').strip()
                if not sku:
                    continue
                qty = int(row[1] or 0)
                orders = int(row[2] or 0)
                last = str(row[3] or '').strip()
                entry = merged.get(sku)
                if entry is None:
                    entry = {'sku': sku, 'qty': 0, 'orders': 0, 'last_date': last, 'fdc_qty': 0, 'efas_qty': 0}
                    merged[sku] = entry
                entry['qty'] += qty
                entry['orders'] += orders
                if label == 'FDC':
                    entry['fdc_qty'] += qty
                else:
                    entry['efas_qty'] += qty
                if last and (not entry.get('last_date') or last > entry['last_date']):
                    # last_date is display only; keep newest-looking MM/DD/YYYY loosely
                    entry['last_date'] = last
        except Exception as exc:
            errors.append(f'{label}: {exc}')
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
    styles = sorted(merged.values(), key=lambda r: int(r.get('qty') or 0), reverse=True)[:30]
    return {
        'ok': True,
        'customer_id': cid,
        'customer_name': name,
        'styles': styles,
        'style_count': len(styles),
        'total_qty': sum(int(s.get('qty') or 0) for s in styles),
        'path': f'/customer?search_id={cid}',
        'errors': errors,
    }


def _fetch_invoice(invoice_number: str) -> dict[str, Any]:
    number = (invoice_number or '').strip()
    if not number:
        return {'ok': False, 'error': 'invoice number is required'}
    db_key, label = _oms_db_for_doc(number)
    cfg = Config.SQL_SERVER_CONFIG
    header_sql = """
        SELECT TOP 1
            i.INVS_NUM,
            LTRIM(RTRIM(i.CUS_ID)),
            LTRIM(RTRIM(c.CUS_NM)),
            i.INVS_AMT,
            o.ord_num,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            CAST(YEAR(DATEADD(DAY, CAST(i.INVS_DT AS INT) - 81088, '20230101')) AS VARCHAR)
        FROM dbo.invoice i
        LEFT JOIN dbo.customer c ON i.CUS_ID = c.CUS_ID
        LEFT JOIN dbo.orders o ON i.INVS_NUM = o.INVS_NUM
        WHERE i.INVS_NUM = ?
    """
    items_sql = """
        SELECT TOP 25
            LTRIM(RTRIM(il.PROD_CD)),
            CAST(ISNULL(il.PROD_QTY, 0) AS INT),
            ISNULL(il.UNIT_PRS, 0)
        FROM dbo.invt_log il
        WHERE il.INVS_NUM = ?
        ORDER BY il.COMM_LN
    """
    _assert_select_only(header_sql)
    _assert_select_only(items_sql)
    conn = None
    try:
        conn = get_sql_server_connection_readonly(cfg[db_key])
        cur = conn.cursor()
        cur.execute(header_sql, (number,))
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'error': f'Invoice {number} not found ({label})'}
        cur.execute(items_sql, (number,))
        items = [
            {'sku': str(r[0] or '').strip(), 'qty': int(r[1] or 0), 'price': float(r[2] or 0)}
            for r in cur.fetchall()
        ]
        return {
            'ok': True,
            'invoice': str(row[0]),
            'database': label,
            'customer_id': str(row[1] or '').strip(),
            'customer_name': str(row[2] or '').strip(),
            'amount': float(row[3] or 0),
            'order_number': str(row[4] or '').strip(),
            'invoice_date': str(row[5] or '').strip(),
            'items': items,
            'path': f'/invoice_lookup/{number}',
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _fetch_order(order_number: str) -> dict[str, Any]:
    number = (order_number or '').strip()
    if not number:
        return {'ok': False, 'error': 'order number is required'}
    db_key, label = _oms_db_for_doc(number)
    cfg = Config.SQL_SERVER_CONFIG
    header_sql = """
        SELECT TOP 1
            o.ord_num,
            LTRIM(RTRIM(o.cus_id)),
            LTRIM(RTRIM(c.cus_nm)),
            CASE WHEN o.INVS_NUM IS NULL OR o.INVS_NUM = 0 THEN '' ELSE CAST(o.INVS_NUM AS VARCHAR) END,
            LTRIM(RTRIM(o.PO_NUM)),
            o.ORD_AMT,
            LTRIM(RTRIM(o.STATUS)),
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2)
                + '/' +
            CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR)
        FROM dbo.orders o
        LEFT JOIN dbo.customer c ON o.cus_id = c.cus_id
        WHERE o.ord_num = ?
    """
    items_sql = """
        SELECT TOP 25
            LTRIM(RTRIM(ol.prod_cd)),
            CAST(ISNULL(ol.order_qty, 0) AS INT),
            ISNULL(ol.unit_prs, 0)
        FROM dbo.ord_log ol
        WHERE ol.ord_num = ?
        ORDER BY ol.COMM_LN
    """
    _assert_select_only(header_sql)
    _assert_select_only(items_sql)
    conn = None
    try:
        conn = get_sql_server_connection_readonly(cfg[db_key])
        cur = conn.cursor()
        cur.execute(header_sql, (number,))
        row = cur.fetchone()
        if not row:
            return {'ok': False, 'error': f'Order {number} not found ({label})'}
        cur.execute(items_sql, (number,))
        items = [
            {'sku': str(r[0] or '').strip(), 'qty': int(r[1] or 0), 'price': float(r[2] or 0)}
            for r in cur.fetchall()
        ]
        return {
            'ok': True,
            'order_number': str(row[0]),
            'database': label,
            'customer_id': str(row[1] or '').strip(),
            'customer_name': str(row[2] or '').strip(),
            'invoice_number': str(row[3] or '').strip(),
            'po': str(row[4] or '').strip(),
            'amount': float(row[5] or 0),
            'status': str(row[6] or '').strip(),
            'order_date': str(row[7] or '').strip(),
            'items': items,
            'path': f'/order_lookup/{number}',
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def execute_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    if name not in TOOL_NAMES:
        return {'ok': False, 'error': f'unknown tool {name}'}
    if name == 'get_sku_inventory':
        return _fetch_sku_inventory(str(args.get('sku') or ''))
    if name == 'get_sku_locations':
        return _fetch_sku_locations(str(args.get('sku') or ''))
    if name == 'get_sku_customers':
        return _fetch_sku_customers(str(args.get('sku') or ''))
    if name == 'get_sku_ocean':
        return _fetch_sku_ocean(str(args.get('sku') or ''))
    if name == 'list_ocean_styles':
        raw = args.get('exclude') or args.get('exclude_styles') or []
        if isinstance(raw, str):
            raw = [raw]
        return _summarize_ocean_styles([str(x) for x in raw])
    if name == 'list_received_styles':
        return _list_received_styles(int(args.get('year') or 0), int(args.get('month') or 0))
    if name == 'list_ocean_containers':
        return _list_ocean_containers()
    if name == 'get_container':
        return _fetch_container(str(args.get('container') or args.get('id') or ''))
    if name == 'get_customer_purchases':
        return _fetch_customer_purchases(str(args.get('query') or args.get('customer') or ''))
    if name == 'get_invoice':
        return _fetch_invoice(str(args.get('invoice') or args.get('number') or ''))
    if name == 'get_order':
        return _fetch_order(str(args.get('order') or args.get('number') or ''))
    if name == 'search_help':
        return _search_help(str(args.get('query') or args.get('q') or ''))
    if name == 'get_help_article':
        return _get_help_article(str(args.get('slug') or ''))
    if name == 'list_online_users':
        return _list_online_users()
    return {'ok': False, 'error': 'unhandled tool'}
