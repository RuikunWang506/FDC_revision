"""Staff smart search (v1): one box, categorized hits across OMS + app data."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

from flask import Blueprint, jsonify, render_template, request, session

from py.utilities.dashboard_permissions import DASHBOARD_TOOLS, tool_visible_for_user
from py.utilities.database_utils import (
    get_mysql_connection,
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.decorators import login_required
from py.utilities.i18n import _
from py.utilities.rate_limit import SlidingWindowLimiter

smart_search_bp = Blueprint("smart_search", __name__)

MIN_QUERY_LEN = 2
MAX_RESULTS = 8
SEARCH_TIMEOUT_SEC = 8
_SKU_RE = re.compile(r"^[A-Za-z]{1,4}\d{3,6}(?:[-\s][A-Za-z0-9]{1,8})?$")
_DOC_RE = re.compile(r"^\d{3,6}$")
_CONTAINER_RE = re.compile(r"^(?:[A-Za-z]{1,4}\d{3,}|[A-Z]{4}\d{6,7})$", re.I)
_YEAR_RE = re.compile(r"^20[2-3]\d$")
_search_limiter = SlidingWindowLimiter()

CategoryRunner = Callable[[str], List[Dict[str, str]]]


def looks_like_doc_number(query: str) -> bool:
    token = (query or "").strip()
    if not token or " " in token:
        return bool(re.search(r"\b\d{3,6}\b", token))
    if _YEAR_RE.match(token):
        return False
    return bool(_DOC_RE.match(token))


def looks_like_sku(query: str) -> bool:
    token = " ".join((query or "").strip().split())
    if not token:
        return False
    if _SKU_RE.match(token):
        return True
    return bool(re.search(r"[A-Za-z]", token) and re.search(r"\d", token))


def looks_like_container(query: str) -> bool:
    token = (query or "").strip().upper().replace(" ", "")
    if not token:
        return False
    return bool(_CONTAINER_RE.match(token))


def looks_like_address(query: str) -> bool:
    token = (query or "").strip()
    if len(token) < 3:
        return False
    if looks_like_sku(token) and " " not in token:
        return False
    if looks_like_container(token):
        return False
    if _DOC_RE.match(token) and len(token) in (5, 6):
        return True
    return bool(re.search(r"[A-Za-z]", token))


def looks_like_customer_text(query: str) -> bool:
    token = (query or "").strip()
    if len(token) < 2:
        return False
    if looks_like_sku(token) and " " not in token:
        return False
    if looks_like_container(token):
        return False
    return True


def should_run_category(category_id: str, query: str) -> bool:
    """Cheap intent gate so a 5-digit order does not scan image/SKU tables."""
    q = (query or "").strip()
    if len(q) < MIN_QUERY_LEN:
        return False
    if category_id == "tools":
        return True
    if category_id in {"orders", "invoices"}:
        return looks_like_doc_number(q)
    if category_id in {"skus", "list_creator", "stock", "images"}:
        return looks_like_sku(q)
    if category_id == "containers":
        return looks_like_container(q) or looks_like_sku(q)
    if category_id == "customers":
        return looks_like_customer_text(q)
    if category_id == "addresses":
        return looks_like_address(q)
    return False


def filter_nonempty_categories(categories: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cat in categories:
        items = [item for item in (cat.get("items") or []) if item]
        if not items:
            continue
        row = dict(cat)
        row["items"] = items
        row["count"] = len(items)
        out.append(row)
    return out


def _like_prefix(query: str) -> str:
    return f"{(query or '').strip()}%"


def _like_contains(query: str) -> str:
    return f"%{(query or '').strip()}%"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def _item(
    title: str,
    href: str,
    subtitle: str = "",
    badge: str = "",
) -> Dict[str, str]:
    return {
        "title": title,
        "href": href,
        "subtitle": subtitle,
        "badge": badge,
    }


def _close(conn) -> None:
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _sql_rows(connect, sql: str, params: Sequence[Any]) -> List[tuple]:
    conn = None
    try:
        conn = connect()
        cursor = conn.cursor()
        cursor.execute(sql, list(params))
        rows = cursor.fetchall()
        cursor.close()
        return list(rows)
    except Exception:
        return []
    finally:
        _close(conn)


def _mysql_rows(sql: str, params: Sequence[Any]) -> List[tuple]:
    conn = None
    try:
        conn = get_mysql_connection()
        if conn is None:
            return []
        cursor = conn.cursor()
        cursor.execute(sql, list(params))
        rows = cursor.fetchall()
        cursor.close()
        return list(rows)
    except Exception:
        return []
    finally:
        _close(conn)


def _search_orders(query: str) -> List[Dict[str, str]]:
    q = query.strip()
    like = _like_prefix(q)
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20)))),
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(o.cus_id AS VARCHAR(20)))),
            o.ord_amt,
            LTRIM(RTRIM(CAST(o.status AS VARCHAR(20)))),
            LTRIM(RTRIM(CAST(o.invs_num AS VARCHAR(20))))
        FROM orders o
        LEFT JOIN customer c ON o.cus_id = c.cus_id
        WHERE LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20)))) LIKE ?
        ORDER BY o.ord_num DESC
    """
    items: List[Dict[str, str]] = []
    seen = set()
    sources = []
    if len(q) == 4:
        sources = [(get_sql_server_connection_oms_ef, "EF")]
    elif len(q) == 5:
        sources = [(get_sql_server_connection_fdc, "FDC")]
    else:
        sources = [
            (get_sql_server_connection_fdc, "FDC"),
            (get_sql_server_connection_oms_ef, "EF"),
        ]
    for connect, badge in sources:
        for row in _sql_rows(connect, sql, (like,)):
            ord_num = _clean(row[0])
            if not ord_num or ord_num in seen:
                continue
            seen.add(ord_num)
            bits = [x for x in (_clean(row[1]), _money(row[3]), _clean(row[4])) if x]
            inv = _clean(row[5])
            if inv and inv not in {"0", "None"}:
                bits.append(f"Inv {inv}")
            items.append(
                _item(
                    f"Order {ord_num}",
                    f"/order_lookup/{quote(ord_num)}",
                    " · ".join(bits),
                    badge,
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_invoices(query: str) -> List[Dict[str, str]]:
    q = query.strip()
    like = _like_prefix(q)
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(CAST(i.invs_num AS VARCHAR(20)))),
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(i.cus_id AS VARCHAR(20)))),
            i.invs_amt,
            LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20))))
        FROM invoice i
        INNER JOIN customer c ON i.cus_id = c.cus_id
        LEFT JOIN orders o ON i.invs_num = o.invs_num
        WHERE LTRIM(RTRIM(CAST(i.invs_num AS VARCHAR(20)))) LIKE ?
        ORDER BY i.invs_num DESC
    """
    items: List[Dict[str, str]] = []
    seen = set()
    sources = []
    if len(q) == 4:
        sources = [(get_sql_server_connection_oms_ef, "EF")]
    elif len(q) == 5:
        sources = [(get_sql_server_connection_fdc, "FDC")]
    else:
        sources = [
            (get_sql_server_connection_fdc, "FDC"),
            (get_sql_server_connection_oms_ef, "EF"),
        ]
    for connect, badge in sources:
        for row in _sql_rows(connect, sql, (like,)):
            invs = _clean(row[0])
            if not invs or invs in seen:
                continue
            seen.add(invs)
            bits = [x for x in (_clean(row[1]), _money(row[3])) if x]
            ord_num = _clean(row[4])
            if ord_num:
                bits.append(f"Order {ord_num}")
            items.append(
                _item(
                    f"Invoice {invs}",
                    f"/invoice_lookup/{quote(invs)}",
                    " · ".join(bits),
                    badge,
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_customers(query: str) -> List[Dict[str, str]]:
    like = _like_contains(query)
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20)))),
            LTRIM(RTRIM(c.CITY)),
            LTRIM(RTRIM(c.STATE))
        FROM customer c
        WHERE c.cus_nm LIKE ? OR LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20)))) LIKE ?
        ORDER BY c.cus_nm
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, (like, like)):
            name = _clean(row[0])
            cus_id = _clean(row[1])
            key = f"{badge}:{cus_id or name}"
            if not (name or cus_id) or key in seen:
                continue
            seen.add(key)
            loc = ", ".join(x for x in (_clean(row[2]), _clean(row[3])) if x)
            subtitle = " · ".join(x for x in (f"ID {cus_id}" if cus_id else "", loc) if x)
            href = f"/customer_details/{quote(cus_id)}" if cus_id else "/customer"
            items.append(_item(name or cus_id, href, subtitle, badge))
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_addresses(query: str) -> List[Dict[str, str]]:
    like = _like_contains(query)
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20)))),
            LTRIM(RTRIM(c.ADDRESS)),
            LTRIM(RTRIM(c.CITY)),
            LTRIM(RTRIM(c.STATE)),
            LTRIM(RTRIM(c.ZIP)),
            LTRIM(RTRIM(c.SHP_ADDRESS)),
            LTRIM(RTRIM(c.SHP_CITY)),
            LTRIM(RTRIM(c.SHP_STATE)),
            LTRIM(RTRIM(c.SHP_ZIP))
        FROM customer c
        WHERE c.ADDRESS LIKE ? OR c.ADDRESS2 LIKE ? OR c.CITY LIKE ? OR c.ZIP LIKE ?
           OR c.SHP_ADDRESS LIKE ? OR c.SHP_ADDRESS2 LIKE ? OR c.SHP_CITY LIKE ? OR c.SHP_ZIP LIKE ?
        ORDER BY c.cus_nm
    """
    params = (like,) * 8
    items: List[Dict[str, str]] = []
    seen = set()
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, params):
            name = _clean(row[0])
            cus_id = _clean(row[1])
            key = f"{badge}:{cus_id or name}"
            if not (name or cus_id) or key in seen:
                continue
            seen.add(key)
            bill = ", ".join(x for x in (_clean(row[2]), _clean(row[3]), _clean(row[4]), _clean(row[5])) if x)
            ship = ", ".join(x for x in (_clean(row[6]), _clean(row[7]), _clean(row[8]), _clean(row[9])) if x)
            addr = bill or ship
            items.append(
                _item(
                    name or cus_id,
                    f"/customer_details/{quote(cus_id)}" if cus_id else "/customer",
                    addr,
                    badge,
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_skus(query: str) -> List[Dict[str, str]]:
    like = _like_prefix(query.strip())
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(i.PROD_CD)),
            LTRIM(RTRIM(i.ALT_CD)),
            LTRIM(RTRIM(i.UNIT_COLOR)),
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD LIKE ? OR i.ALT_CD LIKE ?
        ORDER BY i.ALT_CD, i.PROD_CD
    """
    items: List[Dict[str, str]] = []
    seen = set()
    locations = _sku_locations([query.strip()])
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, (like, like)):
            sku = _clean(row[0])
            if not sku or sku in seen:
                continue
            seen.add(sku)
            style = _clean(row[1])
            color = _clean(row[2])
            avail = row[3]
            loc = locations.get(sku.upper()) or locations.get(style.upper(), "")
            bits = [x for x in (
                f"Style {style}" if style else "",
                color,
                f"Avail {avail}" if avail is not None else "",
                f"Loc {loc}" if loc else "",
            ) if x]
            items.append(
                _item(
                    sku,
                    f"/product_detail?search={quote(sku)}",
                    " · ".join(bits),
                    badge,
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _sku_locations(tokens: Sequence[str]) -> Dict[str, str]:
    token = " ".join((tokens[0] if tokens else "").split())
    if not token:
        return {}
    rows = _mysql_rows(
        "SELECT SKU, Location FROM products WHERE SKU LIKE %s LIMIT 20",
        (_like_prefix(token),),
    )
    out: Dict[str, str] = {}
    for sku, loc in rows:
        key = _clean(sku).upper()
        if key and key not in out:
            out[key] = _clean(loc)
    return out


def _search_containers(query: str) -> List[Dict[str, str]]:
    raw = query.strip().upper()
    variants = {raw, raw.replace(" ", "")}
    if raw.startswith("FT") and len(raw) > 2:
        variants.add("F" + raw[2:])
    elif raw.startswith("F") and not raw.startswith("FT"):
        variants.add("FT" + raw[1:])
    items: List[Dict[str, str]] = []
    seen = set()

    try:
        from py.Toolkits.boat import load_latest_cached_boat_payload, normalize_container_id

        payload = load_latest_cached_boat_payload() or {}
        needle = raw.replace(" ", "")
        for row in payload.get("data") or []:
            cid = normalize_container_id(row.get("container"))
            if not cid or needle not in cid.replace(" ", ""):
                continue
            if cid in seen:
                continue
            seen.add(cid)
            mode = _clean(row.get("action_mode"))
            eta = _clean(row.get("eta"))
            bits = [x for x in (mode.title() if mode else "", f"ETA {eta}" if eta else "") if x]
            items.append(
                _item(
                    cid,
                    f"/container_search?container_id={quote(cid)}",
                    " · ".join(bits) or "Container Hub",
                    "Boat",
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    except Exception:
        pass

    like = _like_prefix(next(iter(sorted(variants, key=len))))
    sql = """
        SELECT TOP 8
            LTRIM(RTRIM(pl.pur_num)),
            COUNT(DISTINCT pl.prod_cd),
            MAX(CASE WHEN ISNULL(pl.hdl_chg, 0) > 0 THEN 1 ELSE 0 END)
        FROM plog pl
        WHERE pl.pur_num LIKE ?
          AND (ISNULL(pl.hdl_chg, 0) > 0 OR ISNULL(pl.log_qty, 0) > 0)
        GROUP BY LTRIM(RTRIM(pl.pur_num))
        ORDER BY LTRIM(RTRIM(pl.pur_num)) DESC
    """
    for row in _sql_rows(get_sql_server_connection_fdc, sql, (like,)):
        cid = _clean(row[0])
        if not cid or cid in seen:
            continue
        seen.add(cid)
        status = "Landed" if row[2] else "Ocean"
        items.append(
            _item(
                cid,
                f"/container_search?container_id={quote(cid)}",
                f"{status} · {int(row[1] or 0)} SKUs",
                "FDC",
            )
        )
        if len(items) >= MAX_RESULTS:
            break
    return items


def _search_list_creator(query: str) -> List[Dict[str, str]]:
    like = _like_prefix(query.strip())
    sql = """
        SELECT DISTINCT TOP 8 LTRIM(RTRIM(i.ALT_CD))
        FROM inv i
        WHERE i.ALT_CD LIKE ? OR i.PROD_CD LIKE ?
        ORDER BY LTRIM(RTRIM(i.ALT_CD))
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for connect in (get_sql_server_connection_fdc, get_sql_server_connection_oms_ef):
        for row in _sql_rows(connect, sql, (like, like)):
            style = _clean(row[0])
            if not style or style in seen:
                continue
            seen.add(style)
            items.append(
                _item(
                    style,
                    f"/v7_listcreator/{quote(style)}",
                    "Open in List Creator V7",
                    "V7",
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_stock(query: str) -> List[Dict[str, str]]:
    like = _like_prefix(query.strip())
    rows = _mysql_rows(
        """
        SELECT sku, product_name, quantity
        FROM internalcheck.internal_inventory
        WHERE sku LIKE %s
        ORDER BY sku
        LIMIT 8
        """,
        (like,),
    )
    items: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        sku = _clean(row[0])
        if not sku or sku in seen:
            continue
        seen.add(sku)
        name = _clean(row[1])
        qty = row[2]
        bits = [x for x in (name, f"Internal {qty}" if qty is not None else "") if x]
        items.append(
            _item(
                sku,
                "/internal_stock_check",
                " · ".join(bits),
                "Stock",
            )
        )
    return items


def _search_images(query: str) -> List[Dict[str, str]]:
    like = _like_prefix(query.strip().upper())
    rows = _mysql_rows(
        """
        SELECT sku, source, COUNT(*) AS n
        FROM image_assets
        WHERE is_active = 1 AND (sku LIKE %s OR parent_sku LIKE %s)
        GROUP BY sku, source
        ORDER BY
            CASE source
                WHEN 'big_picture' THEN 0
                WHEN 'catalog' THEN 1
                ELSE 2
            END,
            sku
        LIMIT 16
        """,
        (like, like),
    )
    grouped: Dict[str, Dict[str, int]] = {}
    for sku, source, count in rows:
        key = _clean(sku).upper()
        if not key:
            continue
        grouped.setdefault(key, {})
        grouped[key][_clean(source) or "other"] = int(count or 0)
    items: List[Dict[str, str]] = []
    for sku, sources in list(grouped.items())[:MAX_RESULTS]:
        bits = []
        if sources.get("big_picture"):
            bits.append(f"{sources['big_picture']} big")
        if sources.get("catalog"):
            bits.append(f"{sources['catalog']} catalog")
        extra = sum(n for src, n in sources.items() if src not in {"big_picture", "catalog"})
        if extra:
            bits.append(f"{extra} other")
        items.append(
            _item(
                sku,
                f"/master_image_viewer/{quote(sku)}",
                " · ".join(bits) or "Style images",
                "DAM",
            )
        )
    return items


def _search_tools(query: str) -> List[Dict[str, str]]:
    needle = query.strip().lower()
    items: List[Dict[str, str]] = []
    from py.utilities.dashboard_permissions import DASHBOARD_TOOL_NAV

    for tool in DASHBOARD_TOOLS:
        tool_id = tool.get("id") or ""
        title = tool.get("title") or ""
        category = tool.get("category") or ""
        hay = " ".join((tool_id, title, category)).lower()
        if needle not in hay:
            continue
        nav = DASHBOARD_TOOL_NAV.get(tool_id) or {}
        href = nav.get("href") or f"/{tool_id}"
        items.append(_item(title, href, category, "Tool"))
        if len(items) >= MAX_RESULTS:
            break
    return items


_CATEGORY_SPECS: List[Dict[str, Any]] = [
    {
        "id": "orders",
        "label_key": "smart_search.cat.orders",
        "icon": "fa-file-invoice",
        "tool_ids": ("order_lookup",),
        "href": "/order_lookup",
        "runner": _search_orders,
    },
    {
        "id": "invoices",
        "label_key": "smart_search.cat.invoices",
        "icon": "fa-file-invoice-dollar",
        "tool_ids": ("invoice_lookup",),
        "href": "/invoice_lookup",
        "runner": _search_invoices,
    },
    {
        "id": "customers",
        "label_key": "smart_search.cat.customers",
        "icon": "fa-users",
        "tool_ids": ("customer",),
        "href": "/customer",
        "runner": _search_customers,
    },
    {
        "id": "addresses",
        "label_key": "smart_search.cat.addresses",
        "icon": "fa-map-marker-alt",
        "tool_ids": ("customer",),
        "href": "/customer",
        "runner": _search_addresses,
    },
    {
        "id": "skus",
        "label_key": "smart_search.cat.skus",
        "icon": "fa-barcode",
        "tool_ids": ("item_lookup",),
        "href": "/product_detail",
        "runner": _search_skus,
    },
    {
        "id": "containers",
        "label_key": "smart_search.cat.containers",
        "icon": "fa-ship",
        "tool_ids": ("boat", "fdcef_container_search"),
        "href": "/container_search",
        "runner": _search_containers,
    },
    {
        "id": "list_creator",
        "label_key": "smart_search.cat.list_creator",
        "icon": "fa-list-ol",
        "tool_ids": ("v7_listcreator",),
        "href": "/v7_listcreator",
        "runner": _search_list_creator,
    },
    {
        "id": "stock",
        "label_key": "smart_search.cat.stock",
        "icon": "fa-layer-group",
        "tool_ids": ("internal_stock_check",),
        "href": "/internal_stock_check",
        "runner": _search_stock,
    },
    {
        "id": "images",
        "label_key": "smart_search.cat.images",
        "icon": "fa-images",
        "tool_ids": ("master_image_viewer",),
        "href": "/master_image_viewer",
        "runner": _search_images,
    },
    {
        "id": "tools",
        "label_key": "smart_search.cat.tools",
        "icon": "fa-th-large",
        "tool_ids": (),
        "href": "/",
        "runner": _search_tools,
    },
]


def _user_can_category(spec: Dict[str, Any], username: str, perms: Optional[Dict[str, Any]]) -> bool:
    tool_ids = spec.get("tool_ids") or ()
    if not tool_ids:
        return True
    return any(tool_visible_for_user(perms, username, tool_id) for tool_id in tool_ids)


def run_smart_search(
    query: str,
    *,
    username: str = "",
    perms: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    q = (query or "").strip()
    started = time.perf_counter()
    if len(q) < MIN_QUERY_LEN:
        return {
            "query": q,
            "categories": [],
            "elapsed_ms": 0,
            "min_len": MIN_QUERY_LEN,
        }

    jobs: List[tuple[str, CategoryRunner]] = []
    for spec in _CATEGORY_SPECS:
        if not _user_can_category(spec, username, perms):
            continue
        if not should_run_category(spec["id"], q):
            continue
        jobs.append((spec["id"], spec["runner"]))

    raw: Dict[str, List[Dict[str, str]]] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
            futures = {pool.submit(runner, q): cat_id for cat_id, runner in jobs}
            try:
                for future in as_completed(futures, timeout=SEARCH_TIMEOUT_SEC):
                    cat_id = futures[future]
                    try:
                        raw[cat_id] = future.result() or []
                    except Exception:
                        raw[cat_id] = []
            except Exception:
                for future, cat_id in futures.items():
                    if cat_id not in raw:
                        raw[cat_id] = []

    categories = []
    for spec in _CATEGORY_SPECS:
        items = raw.get(spec["id"]) or []
        categories.append(
            {
                "id": spec["id"],
                "label": _(spec["label_key"]),
                "icon": spec["icon"],
                "href": spec["href"],
                "items": items,
            }
        )
    categories = filter_nonempty_categories(categories)
    return {
        "query": q,
        "categories": categories,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "min_len": MIN_QUERY_LEN,
        "planned": [cat_id for cat_id, _ in jobs],
    }


@smart_search_bp.route("/smart_search", methods=["GET"])
@login_required
def smart_search_page():
    return render_template(
        "smart_search.html",
        current_user=session.get("username"),
        initial_query=(request.args.get("q") or "").strip(),
    )


@smart_search_bp.route("/api/smart_search", methods=["GET"])
@login_required
def smart_search_api():
    username = session.get("username") or ""
    q = (request.args.get("q") or "").strip()
    limit_key = f"{username.lower()}:smart_search"
    if _search_limiter.too_many(limit_key, 40, 60):
        return jsonify(success=False, error=_("smart_search.rate_limited")), 429

    from py.utilities.auth import get_user_permissions
    from py.utilities.logging_setup import log_user_action

    perms = get_user_permissions(username)
    payload = run_smart_search(q, username=username, perms=perms)
    if q:
        log_user_action(
            f"***{username}***__@@@smart search@@@__ {q}",
            username,
        )
    return jsonify(success=True, **payload)
