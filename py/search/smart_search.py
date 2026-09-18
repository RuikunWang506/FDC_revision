"""Staff smart search (v1): one box, categorized hits across OMS + app data."""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import quote

import json

from flask import Blueprint, Response, jsonify, render_template, request, session, stream_with_context

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
MAX_RESULTS = 100
SEARCH_TIMEOUT_SEC = 12
MIN_PHONE_DIGITS = 3
_PHONE_COLS = ("c.PHONE", "c.PHONE_2", "c.PHONE_3", "c.PHONE_4")
_SKU_RE = re.compile(r"^[A-Za-z]{1,4}\d{3,6}(?:[-\s][A-Za-z0-9]{1,8})?$")
_DOC_RE = re.compile(r"^\d{3,6}$")
_CONTAINER_RE = re.compile(r"^(?:[A-Za-z]{1,4}\d{3,}|[A-Z]{4}\d{6,7})$", re.I)
_YEAR_RE = re.compile(r"^20[2-3]\d$")
_LOC_RE = re.compile(r"(?:^[A-Za-z]\d|L\d|[-/]|\b(?:BOT|TOP|PALLET|AISLE)\b)", re.I)
_WILDCARD_CHARS = frozenset("%_*?")
_search_limiter = SlidingWindowLimiter()

CategoryRunner = Callable[[str], List[Dict[str, str]]]


class SearchPattern:
    def __init__(
        self,
        raw: str,
        core: str,
        like: str,
        regex: str,
        digits: str,
        mode: str,
        has_wildcard: bool,
        is_regex: bool,
    ) -> None:
        self.raw = raw
        self.core = core
        self.like = like
        self.regex = regex
        self.digits = digits
        self.mode = mode
        self.has_wildcard = has_wildcard
        self.is_regex = is_regex

    def as_public(self) -> Dict[str, Any]:
        return {
            "raw": self.raw,
            "core": self.core,
            "like": self.like,
            "regex": self.regex,
            "digits": self.digits,
            "mode": self.mode,
            "has_wildcard": self.has_wildcard,
            "is_regex": self.is_regex,
        }


def wildcard_to_like(query: str) -> str:
    """Turn user wildcards into SQL LIKE: * → %, ? → _."""
    return (query or "").strip().replace("*", "%").replace("?", "_")


def like_to_regex(like: str) -> str:
    """Convert a SQL LIKE pattern to an anchored regular expression."""
    parts = ["^"]
    for ch in like or "":
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    parts.append("$")
    return "".join(parts)


def compile_search_pattern(query: str) -> SearchPattern:
    """Normalize typed input into LIKE + regex. `%3305%` matches T3305."""
    raw = (query or "").strip()
    digits = re.sub(r"\D", "", raw)
    is_regex = False
    expr = raw
    if raw.lower().startswith("re:"):
        is_regex = True
        expr = raw[3:].strip()
    elif raw.startswith("/") and raw.endswith("/") and len(raw) >= 3:
        is_regex = True
        expr = raw[1:-1]

    if is_regex:
        try:
            re.compile(expr)
            regex = expr
        except re.error:
            regex = re.escape(expr)
        runs = re.findall(r"[A-Za-z0-9]{2,}", expr)
        core = max(runs, key=len) if runs else re.sub(r"[^A-Za-z0-9]+", "", expr)
        like = f"%{core}%" if core else "%"
        return SearchPattern(
            raw=raw,
            core=core,
            like=like,
            regex=regex,
            digits=re.sub(r"\D", "", expr),
            mode="regex",
            has_wildcard=True,
            is_regex=True,
        )

    has_wild = any(ch in raw for ch in _WILDCARD_CHARS)
    like = wildcard_to_like(raw)
    if has_wild:
        core = re.sub(r"[%_*?\s]+", "", raw)
        mode = "wildcard"
        if like.startswith("%") and like.endswith("%") and like.count("%") == 2:
            mode = "contains"
        elif like.endswith("%") and not like.startswith("%"):
            mode = "starts"
        elif like.startswith("%") and not like.endswith("%"):
            mode = "ends"
        regex = like_to_regex(like)
        return SearchPattern(
            raw=raw,
            core=core,
            like=like,
            regex=regex,
            digits=digits,
            mode=mode,
            has_wildcard=True,
            is_regex=False,
        )

    return SearchPattern(
        raw=raw,
        core=raw,
        like=f"%{raw}%",
        regex=like_to_regex(f"%{raw}%"),
        digits=digits,
        mode="text",
        has_wildcard=False,
        is_regex=False,
    )


def looks_like_phone(query: str) -> bool:
    digits = compile_search_pattern(query).digits
    return 7 <= len(digits) <= 15


def pattern_is_searchable(query: str) -> bool:
    pat = compile_search_pattern(query)
    if pat.is_regex and pat.regex:
        return True
    if len(pat.core or "") >= MIN_QUERY_LEN:
        return True
    return len(pat.digits) >= 7


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
    pat = compile_search_pattern(query)
    token = (pat.core or query or "").strip()
    if looks_like_phone(query):
        return True
    if looks_like_money(query) and not re.search(r"[A-Za-z]", query or ""):
        return False
    if len(token) < 2:
        return False
    if looks_like_sku(token) and " " not in token and not pat.has_wildcard:
        return False
    if looks_like_container(token) and not pat.has_wildcard:
        return False
    return True


def looks_like_money(query: str) -> bool:
    """Order totals like 3784, or unit prices like 12.5 / $12.50."""
    token = (query or "").strip().replace(",", "").replace("$", "")
    if looks_like_phone(query):
        return False
    if re.fullmatch(r"\d{1,4}\.\d{1,2}", token):
        return True
    return bool(re.fullmatch(r"\d{3,6}", token))


def money_value(query: str) -> Optional[float]:
    if not looks_like_money(query):
        return None
    token = (query or "").strip().replace(",", "").replace("$", "")
    try:
        return float(token)
    except ValueError:
        return None


def looks_like_style_digits(query: str) -> bool:
    """3–4 digit fragments usually mean a style (M1141), not a street address."""
    token = (query or "").strip()
    return bool(re.fullmatch(r"\d{3,4}", token))


def looks_like_style_word(query: str) -> bool:
    """Words like satchel / crossbody — style descriptions, not SKU codes."""
    pat = compile_search_pattern(query)
    token = (pat.core or query or "").strip()
    if len(token) < 4:
        return False
    if looks_like_sku(token) or looks_like_doc_number(token) or looks_like_phone(query):
        return False
    if looks_like_container(token):
        return False
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z \-/]{2,}", token))


def looks_like_location(query: str) -> bool:
    token = (query or "").strip()
    if len(token) < 2:
        return False
    if looks_like_sku(token):
        return True
    return bool(_LOC_RE.search(token))


def should_run_category(category_id: str, query: str) -> bool:
    """Cheap intent gate so a 5-digit order does not scan image/SKU tables."""
    if not pattern_is_searchable(query):
        return False
    pat = compile_search_pattern(query)
    q = (pat.core or query or "").strip()
    phone = looks_like_phone(query)
    wildcard_sku = pat.has_wildcard and bool(re.search(r"\d", q)) and len(q) >= 3
    if category_id == "tools":
        return not pat.has_wildcard
    money = looks_like_money(query)
    if category_id in {"orders", "invoices", "order_processor"}:
        if phone and not looks_like_doc_number(q) and not money:
            return False
        return looks_like_doc_number(q) or looks_like_customer_text(query) or money
    style_digits = looks_like_style_digits(q)
    if category_id == "skus":
        return looks_like_sku(q) or wildcard_sku or looks_like_style_word(query) or style_digits or (
            money and "." in (query or "")
        )
    if category_id == "descriptions":
        return looks_like_style_word(query) or looks_like_sku(q) or wildcard_sku or style_digits
    if category_id == "item_lookup":
        return looks_like_sku(q) or wildcard_sku or looks_like_style_word(query) or style_digits
    if category_id in {"list_creator", "stock", "images"}:
        return looks_like_sku(q) or wildcard_sku or style_digits
    if category_id == "bag_styles":
        return looks_like_sku(q) or looks_like_container(q) or wildcard_sku or looks_like_style_word(query) or style_digits
    if category_id == "containers":
        return looks_like_container(q) or looks_like_sku(q) or wildcard_sku
    if category_id in {"customers", "customer_skus"}:
        return looks_like_customer_text(query) or phone or looks_like_doc_number(q)
    if category_id == "addresses":
        return looks_like_address(q) or phone
    if category_id == "locations":
        return looks_like_location(q)
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
    pat = compile_search_pattern(query)
    if pat.has_wildcard or pat.is_regex:
        return pat.like
    return f"{(pat.core or query or '').strip()}%"


def _like_contains(query: str) -> str:
    pat = compile_search_pattern(query)
    if pat.has_wildcard or pat.is_regex:
        return pat.like
    return f"%{(pat.core or query or '').strip()}%"


def _sql_digits_expr(column: str) -> str:
    expr = f"LTRIM(RTRIM(CAST(ISNULL({column}, '') AS VARCHAR(40))))"
    for ch in ("-", "(", ")", " ", ".", "/", "+"):
        expr = f"REPLACE({expr}, '{ch}', '')"
    return expr


def _phone_digits_match_sql(query: str) -> tuple[str, List[str]]:
    """Match PHONE* after stripping punctuation so 626 hits (626) 555-1212."""
    digits = compile_search_pattern(query).digits
    if len(digits) < MIN_PHONE_DIGITS:
        return "", []
    dlike = f"%{digits}%"
    clauses = [f"{_sql_digits_expr(col)} LIKE ?" for col in _PHONE_COLS]
    return "(" + " OR ".join(clauses) + ")", [dlike] * len(_PHONE_COLS)


def _customer_id_match_sql(query: str) -> tuple[str, List[str]]:
    """Digit-only queries match customer ID (01141), not street numbers like 11410."""
    token = re.sub(r"\D", "", (query or "").strip())
    id_expr = "LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20))))"
    variants = {token, token.lstrip("0") or "0", token.zfill(4), token.zfill(5), token.zfill(6)}
    clauses = [f"{id_expr} = ?"] * len(variants)
    clauses.append(f"{id_expr} LIKE ?")
    params = list(variants) + [f"%{token}"]
    return "(" + " OR ".join(clauses) + ")", params


def _customer_match_sql(query: str) -> tuple[str, List[str]]:
    pat = compile_search_pattern(query)
    token = (pat.core or query or "").strip()
    if re.fullmatch(r"\d{3,6}", token) and not looks_like_phone(query) and not pat.has_wildcard:
        id_sql, id_params = _customer_id_match_sql(token)
        phone_sql, phone_params = _phone_digits_match_sql(query)
        if phone_sql:
            return f"({id_sql} OR {phone_sql})", id_params + phone_params
        return id_sql, id_params
    like = _like_contains(query)
    columns = (
        "c.cus_nm",
        "LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20))))",
        "c.PHONE",
        "c.PHONE_2",
        "c.PHONE_3",
        "c.PHONE_4",
        "c.EMAIL_ADR",
        "c.WEB_ADR",
        "c.ZIP",
        "c.SHP_ZIP",
        "c.ADDRESS",
        "c.ADDRESS2",
        "c.CITY",
        "c.STATE",
        "c.SHP_ADDRESS",
        "c.SHP_CITY",
        "c.SHP_STATE",
        "LTRIM(RTRIM(CAST(c.SALES_NUM AS VARCHAR(20))))",
    )
    clauses = [f"{col} LIKE ?" for col in columns]
    params: List[str] = [like] * len(columns)
    phone_sql, phone_params = _phone_digits_match_sql(query)
    if phone_sql:
        clauses.append(phone_sql)
        params.extend(phone_params)
    return "(" + " OR ".join(clauses) + ")", params


def _apply_regex_filter(items: List[Dict[str, str]], query: str) -> List[Dict[str, str]]:
    pat = compile_search_pattern(query)
    if not pat.is_regex:
        return items
    try:
        rx = re.compile(pat.regex, re.I)
    except re.error:
        return items
    kept = []
    for item in items:
        hay = " ".join(
            (
                item.get("title") or "",
                item.get("subtitle") or "",
                item.get("badge") or "",
                item.get("match_in") or "",
                item.get("match_text") or "",
            )
        )
        if rx.search(hay):
            kept.append(item)
    return kept


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def _money_plain(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def _item(
    title: str,
    href: str,
    subtitle: str = "",
    badge: str = "",
    match_in: str = "",
    match_text: str = "",
) -> Dict[str, str]:
    row = {
        "title": title,
        "href": href,
        "subtitle": subtitle,
        "badge": badge,
    }
    if match_in:
        row["match_in"] = match_in
    if match_text:
        row["match_text"] = match_text
    return row


def _text_matches(query: str, text: str) -> bool:
    pat = compile_search_pattern(query)
    hay = _clean(text)
    if not hay:
        return False
    if pat.is_regex:
        try:
            return bool(re.search(pat.regex, hay, re.I))
        except re.error:
            return False
    needle = (pat.core or query or "").strip()
    if not needle:
        return False
    if needle.lower() in hay.lower():
        return True
    qdigits = re.sub(r"\D", "", needle)
    hdigits = re.sub(r"\D", "", hay)
    if len(qdigits) >= MIN_PHONE_DIGITS and qdigits in hdigits:
        return True
    return False


def _pick_match(query: str, fields: Sequence[tuple[str, Any]]) -> tuple[str, str]:
    for label, value in fields:
        text = _clean(value)
        if text and _text_matches(query, text):
            return label, text
    return "", ""


def _with_match(
    item: Dict[str, str],
    query: str,
    fields: Sequence[tuple[str, Any]] = (),
) -> Dict[str, str]:
    if item.get("match_in") and item.get("match_text"):
        return item
    pairs = list(fields) + [
        ("Title", item.get("title")),
        ("Details", item.get("subtitle")),
        ("Found in", item.get("match_text")),
    ]
    field, text = _pick_match(query, pairs)
    if not field:
        return item
    out = dict(item)
    out["match_in"] = field
    out["match_text"] = text
    return out


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


def _oms_sources(query: str) -> List[tuple]:
    q = compile_search_pattern(query).core or query.strip()
    if looks_like_money(query):
        return [
            (get_sql_server_connection_fdc, "FDC"),
            (get_sql_server_connection_oms_ef, "EF"),
        ]
    if looks_like_doc_number(q) and " " not in q:
        if len(q) == 4:
            return [(get_sql_server_connection_oms_ef, "EF")]
        if len(q) == 5:
            return [(get_sql_server_connection_fdc, "FDC")]
    return [
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ]


def _amount_eq_sql(column: str) -> str:
    return f"ABS(CAST(ISNULL({column}, 0) AS DECIMAL(12,2)) - ?) < 0.01"


def _search_orders(query: str) -> List[Dict[str, str]]:
    q = query.strip()
    prefix = _like_prefix(q)
    contains = _like_contains(q)
    amt = money_value(q)
    where = [
        "LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20)))) LIKE ?",
        "LTRIM(RTRIM(CAST(o.po_num AS VARCHAR(40)))) LIKE ?",
        "c.cus_nm LIKE ?",
        "LTRIM(RTRIM(CAST(o.cus_id AS VARCHAR(20)))) LIKE ?",
    ]
    params: List[Any] = [prefix, contains, contains, contains]
    if amt is not None:
        where.append(_amount_eq_sql("o.ord_amt"))
        params.append(amt)
    order_sql = "o.ord_num DESC"
    if amt is not None:
        order_sql = f"CASE WHEN {_amount_eq_sql('o.ord_amt')} THEN 0 ELSE 1 END, o.ord_num DESC"
        params.append(amt)
    sql = f"""
        SELECT TOP 100
            LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20)))),
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(o.cus_id AS VARCHAR(20)))),
            o.ord_amt,
            LTRIM(RTRIM(CAST(o.status AS VARCHAR(20)))),
            LTRIM(RTRIM(CAST(o.invs_num AS VARCHAR(20)))),
            LTRIM(RTRIM(CAST(o.po_num AS VARCHAR(40))))
        FROM orders o
        LEFT JOIN customer c ON o.cus_id = c.cus_id
        WHERE {" OR ".join(where)}
        ORDER BY {order_sql}
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for connect, badge in _oms_sources(q):
        for row in _sql_rows(connect, sql, params):
            ord_num = _clean(row[0])
            if not ord_num or ord_num in seen:
                continue
            seen.add(ord_num)
            bits = [x for x in (_clean(row[1]), _money(row[3]), _clean(row[4])) if x]
            inv = _clean(row[5])
            po_num = _clean(row[6])
            if po_num:
                bits.insert(0, f"PO {po_num}")
            if inv and inv not in {"0", "None"}:
                bits.append(f"Inv {inv}")
            items.append(
                _with_match(
                    _item(
                        f"Order {ord_num}",
                        f"/order_lookup/{quote(ord_num)}",
                        " · ".join(bits),
                        badge,
                    ),
                    q,
                    (
                        ("Order total", _money_plain(row[3])),
                        ("Order", ord_num),
                        ("PO", po_num),
                        ("Customer", _clean(row[1])),
                        ("Customer ID", _clean(row[2])),
                        ("Invoice", inv),
                    ),
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    if amt is not None and len(items) < MAX_RESULTS:
        items.extend(_search_unit_prices(q, seen, MAX_RESULTS - len(items)))
    return items


def _search_unit_prices(
    query: str,
    seen_orders: Optional[set] = None,
    limit: int = MAX_RESULTS,
) -> List[Dict[str, str]]:
    amt = money_value(query)
    if amt is None or limit <= 0:
        return []
    sql = f"""
        SELECT TOP {int(limit)}
            LTRIM(RTRIM(CAST(ol.ord_num AS VARCHAR(20)))),
            LTRIM(RTRIM(ol.prod_cd)),
            ol.unit_prs,
            CAST(ISNULL(ol.order_qty, 0) AS INT),
            LTRIM(RTRIM(c.cus_nm))
        FROM ord_log ol
        LEFT JOIN customer c ON ol.cus_id = c.cus_id
        WHERE {_amount_eq_sql("ol.unit_prs")}
        ORDER BY ol.ord_num DESC
    """
    items: List[Dict[str, str]] = []
    seen = set(seen_orders or ())
    seen_lines = set()
    for connect, badge in _oms_sources(query):
        for row in _sql_rows(connect, sql, (amt,)):
            ord_num = _clean(row[0])
            sku = _clean(row[1])
            key = f"{ord_num}:{sku}:{_money(row[2])}"
            if not ord_num or key in seen_lines or ord_num in seen:
                continue
            seen_lines.add(key)
            seen.add(ord_num)
            bits = [x for x in (
                sku,
                f"Unit {_money(row[2])}",
                f"Qty {row[3]}" if row[3] is not None else "",
                _clean(row[4]),
            ) if x]
            items.append(
                _with_match(
                    _item(
                        f"Order {ord_num}",
                        f"/order_lookup/{quote(ord_num)}",
                        " · ".join(bits),
                        badge,
                    ),
                    query,
                    (("Unit price", _money_plain(row[2])), ("SKU", sku), ("Order", ord_num)),
                )
            )
            if len(items) >= limit:
                return items
    return items


def _search_invoices(query: str) -> List[Dict[str, str]]:
    q = query.strip()
    prefix = _like_prefix(q)
    contains = _like_contains(q)
    sql = f"""
        SELECT TOP 100
            LTRIM(RTRIM(CAST(i.invs_num AS VARCHAR(20)))),
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(i.cus_id AS VARCHAR(20)))),
            i.invs_amt,
            LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20))))
        FROM invoice i
        INNER JOIN customer c ON i.cus_id = c.cus_id
        LEFT JOIN orders o ON i.invs_num = o.invs_num
        WHERE LTRIM(RTRIM(CAST(i.invs_num AS VARCHAR(20)))) LIKE ?
           OR c.cus_nm LIKE ?
           OR LTRIM(RTRIM(CAST(i.cus_id AS VARCHAR(20)))) LIKE ?
           OR {_amount_eq_sql("i.invs_amt")}
        ORDER BY i.invs_num DESC
    """
    amt = money_value(q)
    params: List[Any] = [prefix, contains, contains, amt if amt is not None else -1]
    items: List[Dict[str, str]] = []
    seen = set()
    for connect, badge in _oms_sources(q):
        for row in _sql_rows(connect, sql, params):
            invs = _clean(row[0])
            if not invs or invs in seen:
                continue
            seen.add(invs)
            bits = [x for x in (_clean(row[1]), _money(row[3])) if x]
            ord_num = _clean(row[4])
            if ord_num:
                bits.append(f"Order {ord_num}")
            items.append(
                _with_match(
                    _item(
                        f"Invoice {invs}",
                        f"/invoice_lookup/{quote(invs)}",
                        " · ".join(bits),
                        badge,
                    ),
                    q,
                    (
                        ("Invoice total", _money_plain(row[3])),
                        ("Invoice", invs),
                        ("Customer", _clean(row[1])),
                        ("Customer ID", _clean(row[2])),
                        ("Order", ord_num),
                    ),
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_order_processor(query: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for row in _search_orders(query)[:MAX_RESULTS]:
        title = row.get("title") or ""
        ord_num = title.replace("Order ", "", 1).strip()
        if not ord_num:
            continue
        items.append(
            _item(
                f"Edit {ord_num}",
                f"/order_processor?order_number={quote(ord_num)}",
                row.get("subtitle") or "Order Processor",
                "Processor",
                match_in=row.get("match_in") or "",
                match_text=row.get("match_text") or "",
            )
        )
    return items


def has_valid_order(last_ord: Any) -> bool:
    token = _clean(last_ord)
    return bool(token) and token not in {"0", "None", "none"}


def _search_customers(query: str) -> List[Dict[str, str]]:
    where_sql, params = _customer_match_sql(query)
    sql = f"""
        SELECT TOP 100
            LTRIM(RTRIM(c.cus_nm)),
            LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20)))),
            LTRIM(RTRIM(c.CITY)),
            LTRIM(RTRIM(c.STATE)),
            LTRIM(RTRIM(c.PHONE)),
            LTRIM(RTRIM(c.PHONE_2)),
            LTRIM(RTRIM(c.PHONE_3)),
            LTRIM(RTRIM(c.EMAIL_ADR)),
            LTRIM(RTRIM(c.CUS_TYPE)),
            LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20))))
        FROM customer c
        LEFT JOIN (
            SELECT cus_id, MAX(ord_num) AS max_ord_num
            FROM orders
            GROUP BY cus_id
        ) mo ON c.cus_id = mo.cus_id
        LEFT JOIN orders o ON mo.cus_id = o.cus_id AND mo.max_ord_num = o.ord_num
        WHERE {where_sql}
        ORDER BY CASE
            WHEN o.ord_num IS NULL THEN 1
            WHEN LTRIM(RTRIM(CAST(o.ord_num AS VARCHAR(20)))) IN ('', '0') THEN 1
            ELSE 0
        END, c.cus_nm
    """
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
            loc = ", ".join(x for x in (_clean(row[2]), _clean(row[3])) if x)
            phone = _clean(row[4]) or _clean(row[5]) or _clean(row[6])
            last_ord = _clean(row[9])
            ordered = has_valid_order(last_ord)
            bits = [x for x in (
                f"ID {cus_id}" if cus_id else "",
                loc,
                phone,
                _clean(row[7]),
                f"Last order {last_ord}" if ordered else "",
            ) if x]
            href = f"/customer?search_id={quote(name or cus_id)}"
            row_item = _with_match(
                _item(name or cus_id, href, " · ".join(bits), badge),
                query,
                (
                    ("Customer ID", cus_id),
                    ("Customer", name),
                    ("Phone", phone),
                    ("Email", _clean(row[7])),
                ),
            )
            row_item["has_order"] = ordered
            items.append(row_item)
            if len(items) >= MAX_RESULTS:
                break
    return _apply_regex_filter(items, query)


def _search_customer_skus(query: str) -> List[Dict[str, str]]:
    where_sql, params = _customer_match_sql(query)
    sql = f"""
        SELECT TOP 100
            LTRIM(RTRIM(ol.prod_cd)),
            SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)),
            MAX(LTRIM(RTRIM(c.cus_nm))),
            MAX(LTRIM(RTRIM(CAST(c.cus_id AS VARCHAR(20)))))
        FROM ord_log ol
        JOIN customer c ON ol.cus_id = c.cus_id
        WHERE {where_sql}
        GROUP BY LTRIM(RTRIM(ol.prod_cd))
        ORDER BY SUM(CAST(ISNULL(ol.order_qty, 0) AS INT)) DESC
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, params):
            sku = _clean(row[0])
            if not sku or sku in seen:
                continue
            seen.add(sku)
            qty = row[1]
            name = _clean(row[2])
            cus_id = _clean(row[3]) if len(row) > 3 else ""
            bits = [x for x in (
                f"ID {cus_id}" if cus_id else "",
                name,
                f"Ordered {qty}" if qty is not None else "",
            ) if x]
            items.append(
                _with_match(
                    _item(
                        sku,
                        f"/product_detail?search={quote(sku)}",
                        " · ".join(bits),
                        badge,
                    ),
                    query,
                    (
                        ("Customer ID", cus_id),
                        ("Customer", name),
                        ("SKU", sku),
                    ),
                )
            )
            if len(items) >= MAX_RESULTS:
                break
    return _apply_regex_filter(items, query)


def _search_addresses(query: str) -> List[Dict[str, str]]:
    like = _like_contains(query)
    sql = """
        SELECT TOP 100
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
                _with_match(
                    _item(
                        name or cus_id,
                        f"/customer_details/{quote(cus_id)}" if cus_id else "/customer",
                        addr,
                        badge,
                    ),
                    query,
                    (
                        ("Address", _clean(row[2])),
                        ("City", _clean(row[3])),
                        ("ZIP", _clean(row[5])),
                        ("Ship address", _clean(row[6])),
                        ("Ship city", _clean(row[7])),
                        ("Ship ZIP", _clean(row[9])),
                        ("Customer", name),
                        ("Customer ID", cus_id),
                    ),
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_skus(query: str) -> List[Dict[str, str]]:
    amt = money_value(query)
    code_like = (
        _like_contains(query)
        if looks_like_style_word(query) or looks_like_style_digits(query) or amt is not None
        else _like_prefix(query.strip())
    )
    desc_like = _like_contains(query)
    where = "i.PROD_CD LIKE ? OR i.ALT_CD LIKE ? OR i.DESCRIP LIKE ?"
    params: List[Any] = [code_like, code_like, desc_like]
    if amt is not None:
        where += f" OR {_amount_eq_sql('i.WHOLE_PRS')} OR {_amount_eq_sql('i.RETAIL_PRS')}"
        params.extend([amt, amt])
    sql = f"""
        SELECT TOP 100
            LTRIM(RTRIM(i.PROD_CD)),
            LTRIM(RTRIM(i.ALT_CD)),
            LTRIM(RTRIM(i.UNIT_COLOR)),
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT),
            LTRIM(RTRIM(i.DESCRIP)),
            i.WHOLE_PRS,
            i.RETAIL_PRS
        FROM inv i
        LEFT JOIN inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE {where}
        ORDER BY i.ALT_CD, i.PROD_CD
    """
    items: List[Dict[str, str]] = []
    seen = set()
    locations = _sku_locations([query.strip()])
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, params):
            sku = _clean(row[0])
            if not sku or sku in seen:
                continue
            seen.add(sku)
            style = _clean(row[1])
            color = _clean(row[2])
            avail = row[3]
            descrip = _clean(row[4]) if len(row) > 4 else ""
            whole = _money(row[5]) if len(row) > 5 else ""
            retail = _money(row[6]) if len(row) > 6 else ""
            loc = locations.get(sku.upper()) or locations.get(style.upper(), "")
            bits = [x for x in (
                f"Style {style}" if style else "",
                descrip,
                color,
                f"WS {whole}" if whole else "",
                f"RTL {retail}" if retail else "",
                f"Avail {avail}" if avail is not None else "",
                f"Loc {loc}" if loc else "",
            ) if x]
            items.append(
                _with_match(
                    _item(
                        sku,
                        f"/product_detail?search={quote(sku)}",
                        " · ".join(bits),
                        badge,
                    ),
                    query,
                    (
                        ("SKU", sku),
                        ("Style", style),
                        ("Description", descrip),
                        ("Color", color),
                        ("Wholesale", _money_plain(row[5]) if len(row) > 5 else ""),
                        ("Retail", _money_plain(row[6]) if len(row) > 6 else ""),
                    ),
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
        SELECT TOP 100
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


def _search_item_lookup(query: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for row in _search_skus(query):
        sku = row.get("title") or ""
        if not sku:
            continue
        items.append(
            _item(
                sku,
                f"/item_lookup/{quote(sku)}",
                row.get("subtitle") or "Item Lookup",
                "SKU",
                match_in=row.get("match_in") or "",
                match_text=row.get("match_text") or "",
            )
        )
    return items


def _search_descriptions(query: str) -> List[Dict[str, str]]:
    like = _like_contains(query)
    items: List[Dict[str, str]] = []
    seen = set()
    for style, desc in _mysql_rows(
        """
        SELECT style, description
        FROM onlinedb.description
        WHERE description LIKE %s OR style LIKE %s
        ORDER BY description, style
        LIMIT 100
        """,
        (like, like),
    ):
        style_s = _clean(style)
        desc_s = _clean(desc)
        key = (style_s or desc_s).upper()
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(
            _with_match(
                _item(
                    desc_s or style_s,
                    f"/bag_style_viewer?style_q={quote(style_s)}" if style_s else "/bag_style_viewer",
                    f"Style {style_s}" if style_s else "Style description",
                    "Desc",
                ),
                query,
                (("Description", desc_s), ("Style", style_s)),
            )
        )
        if len(items) >= MAX_RESULTS:
            return items

    sql = """
        SELECT TOP 100
            LTRIM(RTRIM(i.ALT_CD)),
            LTRIM(RTRIM(i.PROD_CD)),
            LTRIM(RTRIM(i.DESCRIP))
        FROM inv i
        WHERE i.DESCRIP LIKE ?
        ORDER BY i.ALT_CD, i.PROD_CD
    """
    for connect, badge in (
        (get_sql_server_connection_fdc, "FDC"),
        (get_sql_server_connection_oms_ef, "EF"),
    ):
        for row in _sql_rows(connect, sql, (like,)):
            style = _clean(row[0])
            sku = _clean(row[1])
            desc = _clean(row[2])
            key = (sku or style or desc).upper()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                _with_match(
                    _item(
                        sku or style or desc,
                        f"/product_detail?search={quote(sku or style)}",
                        desc or style,
                        badge,
                    ),
                    query,
                    (("Description", desc), ("Style", style), ("SKU", sku)),
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_bag_styles(query: str) -> List[Dict[str, str]]:
    like = (
        _like_contains(query)
        if looks_like_style_word(query) or looks_like_style_digits(query)
        else _like_prefix(query.strip())
    )
    desc_like = _like_contains(query)
    sql = """
        SELECT TOP 100
            LTRIM(RTRIM(i.ALT_CD)),
            LTRIM(RTRIM(i.PROD_CD)),
            LTRIM(RTRIM(i.DESCRIP))
        FROM inv i
        WHERE i.ALT_CD LIKE ? OR i.PROD_CD LIKE ? OR i.DESCRIP LIKE ?
        ORDER BY
            CASE
                WHEN i.ALT_CD LIKE ? THEN 0
                WHEN i.PROD_CD LIKE ? THEN 1
                ELSE 2
            END,
            i.ALT_CD, i.PROD_CD
    """
    items: List[Dict[str, str]] = []
    seen = set()
    for style, desc in _mysql_rows(
        """
        SELECT style, description
        FROM onlinedb.description
        WHERE description LIKE %s OR style LIKE %s
        ORDER BY description
        LIMIT 100
        """,
        (desc_like, desc_like),
    ):
        style_s = _clean(style)
        if not style_s or style_s in seen:
            continue
        seen.add(style_s)
        desc_s = _clean(desc)
        items.append(
            _with_match(
                _item(
                    style_s,
                    f"/bag_style_viewer?style_q={quote(style_s)}",
                    desc_s or "Bag Style Viewer",
                    "Bag",
                ),
                query,
                (("Style", style_s), ("Description", desc_s)),
            )
        )
    if looks_like_container(query):
        cid = query.strip().upper()
        items.append(
            _with_match(
                _item(
                    cid,
                    f"/bag_style_viewer?container_q={quote(cid)}",
                    "Bag styles in this container",
                    "Bag",
                ),
                query,
                (("Container", cid),),
            )
        )
    for connect in (get_sql_server_connection_fdc, get_sql_server_connection_oms_ef):
        for row in _sql_rows(connect, sql, (like, like, desc_like, like, like)):
            style = _clean(row[0])
            if not style or style in seen:
                continue
            seen.add(style)
            sku = _clean(row[1]) if len(row) > 1 else ""
            descrip = _clean(row[2]) if len(row) > 2 else ""
            items.append(
                _with_match(
                    _item(
                        style,
                        f"/bag_style_viewer?style_q={quote(style)}",
                        " · ".join(x for x in (descrip, sku) if x) or "Bag Style Viewer",
                        "Bag",
                    ),
                    query,
                    (("Style", style), ("SKU", sku), ("Description", descrip)),
                )
            )
            if len(items) >= MAX_RESULTS:
                return items
    return items


def _search_locations(query: str) -> List[Dict[str, str]]:
    token = query.strip()
    like = _like_contains(token)
    items: List[Dict[str, str]] = []
    seen = set()
    for sku, loc in _mysql_rows(
        "SELECT SKU, Location FROM products WHERE SKU LIKE %s OR Location LIKE %s LIMIT 100",
        (_like_prefix(token), like),
    ):
        sku_s = _clean(sku)
        loc_s = _clean(loc)
        key = f"{sku_s}:{loc_s}"
        if not (sku_s or loc_s) or key in seen:
            continue
        seen.add(key)
        items.append(
            _with_match(
                _item(
                    loc_s or sku_s,
                    f"/product_detail?search={quote(sku_s or token)}",
                    f"{sku_s} · warehouse location" if sku_s else "Warehouse location",
                    "Loc",
                ),
                query,
                (("Location", loc_s), ("SKU", sku_s)),
            )
        )
        if len(items) >= MAX_RESULTS:
            break
    for (loc,) in _mysql_rows(
        "SELECT DISTINCT location FROM pallet_details WHERE location LIKE %s ORDER BY location LIMIT 100",
        (like,),
    ):
        loc_s = _clean(loc)
        if not loc_s or loc_s in seen:
            continue
        seen.add(loc_s)
        items.append(
            _with_match(
                _item(
                    loc_s,
                    f"/palletdetail/{quote(loc_s, safe='')}",
                    "Pallet detail",
                    "Pallet",
                ),
                query,
                (("Location", loc_s),),
            )
        )
        if len(items) >= MAX_RESULTS:
            break
    return items[:MAX_RESULTS]


def _search_list_creator(query: str) -> List[Dict[str, str]]:
    like = _like_prefix(query.strip())
    sql = """
        SELECT DISTINCT TOP 100 LTRIM(RTRIM(i.ALT_CD))
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
        LIMIT 100
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
                "/inventory_manual_adjuster",
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
        LIMIT 100
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
        items.append(
            _with_match(
                _item(title, href, category, "Tool"),
                query,
                (("Tool", title), ("Category", category), ("ID", tool_id)),
            )
        )
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
        "id": "order_processor",
        "label_key": "smart_search.cat.order_processor",
        "icon": "fa-edit",
        "tool_ids": ("order_processor",),
        "href": "/order_processor",
        "runner": _search_order_processor,
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
        "id": "customer_skus",
        "label_key": "smart_search.cat.customer_skus",
        "icon": "fa-shopping-bag",
        "tool_ids": ("customer",),
        "href": "/customer",
        "runner": _search_customer_skus,
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
        "id": "descriptions",
        "label_key": "smart_search.cat.descriptions",
        "icon": "fa-tag",
        "tool_ids": ("bag_style_viewer", "item_lookup"),
        "href": "/bag_style_viewer",
        "runner": _search_descriptions,
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
        "id": "item_lookup",
        "label_key": "smart_search.cat.item_lookup",
        "icon": "fa-search",
        "tool_ids": ("item_lookup",),
        "href": "/item_lookup",
        "runner": _search_item_lookup,
    },
    {
        "id": "bag_styles",
        "label_key": "smart_search.cat.bag_styles",
        "icon": "fa-shopping-bag",
        "tool_ids": ("bag_style_viewer",),
        "href": "/bag_style_viewer",
        "runner": _search_bag_styles,
    },
    {
        "id": "locations",
        "label_key": "smart_search.cat.locations",
        "icon": "fa-warehouse",
        "tool_ids": ("location_service", "location_massive_update", "warehousemap"),
        "href": "/locations",
        "runner": _search_locations,
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
        "tool_ids": ("inventory_manual_adjuster",),
        "href": "/inventory_manual_adjuster",
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


def _spec_by_id(cat_id: str) -> Optional[Dict[str, Any]]:
    for spec in _CATEGORY_SPECS:
        if spec["id"] == cat_id:
            return spec
    return None


def _jobs_for_query(
    query: str,
    username: str = "",
    perms: Optional[Dict[str, Any]] = None,
) -> List[tuple[str, CategoryRunner]]:
    jobs: List[tuple[str, CategoryRunner]] = []
    for spec in _CATEGORY_SPECS:
        if not _user_can_category(spec, username, perms):
            continue
        if not should_run_category(spec["id"], query):
            continue
        jobs.append((spec["id"], spec["runner"]))
    return jobs


def _category_payload(cat_id: str, items: List[Dict[str, str]], query: str) -> Dict[str, Any]:
    spec = _spec_by_id(cat_id) or {}
    filtered = [_with_match(item, query) for item in _apply_regex_filter(items or [], query)]
    return {
        "id": cat_id,
        "label": _(spec.get("label_key") or cat_id),
        "icon": spec.get("icon") or "fa-search",
        "href": spec.get("href") or "/",
        "items": filtered,
        "count": len(filtered),
    }


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def run_smart_search(
    query: str,
    *,
    username: str = "",
    perms: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    q = (query or "").strip()
    started = time.perf_counter()
    pattern = compile_search_pattern(q)
    if not pattern_is_searchable(q):
        return {
            "query": q,
            "categories": [],
            "elapsed_ms": 0,
            "min_len": MIN_QUERY_LEN,
            "pattern": pattern.as_public(),
        }

    jobs = _jobs_for_query(q, username, perms)

    raw: Dict[str, List[Dict[str, str]]] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {pool.submit(runner, q): cat_id for cat_id, runner in jobs}
            try:
                for future in as_completed(futures, timeout=SEARCH_TIMEOUT_SEC):
                    cat_id = futures[future]
                    try:
                        raw[cat_id] = [
                            _with_match(item, q)
                            for item in _apply_regex_filter(future.result() or [], q)
                        ]
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
        "pattern": pattern.as_public(),
    }


@smart_search_bp.route("/smart_search", methods=["GET"])
@smart_search_bp.route("/smart_search/<path:q>", methods=["GET"])
@login_required
def smart_search_page(q: str = ""):
    initial = (q or request.args.get("q") or "").strip()
    return render_template(
        "smart_search.html",
        current_user=session.get("username"),
        initial_query=initial,
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


@smart_search_bp.route("/api/smart_search/stream", methods=["GET"])
@login_required
def smart_search_stream():
    username = session.get("username") or ""
    q = (request.args.get("q") or "").strip()
    limit_key = f"{username.lower()}:smart_search"
    if _search_limiter.too_many(limit_key, 40, 60):
        return jsonify(success=False, error=_("smart_search.rate_limited")), 429

    from py.utilities.auth import get_user_permissions
    from py.utilities.logging_setup import log_user_action

    perms = get_user_permissions(username)
    pattern = compile_search_pattern(q)
    jobs = _jobs_for_query(q, username, perms) if pattern_is_searchable(q) else []
    planned = [
        {
            "id": cat_id,
            "label": _((_spec_by_id(cat_id) or {}).get("label_key") or cat_id),
            "icon": (_spec_by_id(cat_id) or {}).get("icon") or "fa-search",
        }
        for cat_id, _runner in jobs
    ]
    if q:
        log_user_action(f"***{username}***__@@@smart search@@@__ {q}", username)

    def generate():
        yield _sse(
            {
                "type": "start",
                "success": True,
                "query": q,
                "pattern": pattern.as_public(),
                "planned": planned,
            }
        )
        yield ": " + (" " * 2048) + "\n\n"
        if not jobs:
            yield _sse({"type": "done", "query": q, "count": 0})
            return
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            futures = {pool.submit(runner, q): cat_id for cat_id, runner in jobs}
            found = 0
            try:
                for future in as_completed(futures, timeout=SEARCH_TIMEOUT_SEC):
                    cat_id = futures[future]
                    try:
                        items = future.result() or []
                    except Exception:
                        items = []
                    payload = _category_payload(cat_id, items, q)
                    if payload["count"]:
                        found += 1
                    yield _sse({"type": "category", "query": q, "category": payload})
            except Exception:
                for future, cat_id in futures.items():
                    if future.done():
                        continue
                    yield _sse(
                        {
                            "type": "category",
                            "query": q,
                            "category": _category_payload(cat_id, [], q),
                        }
                    )
            yield _sse({"type": "done", "query": q, "count": found})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
