"""
Limit Master Image Viewer / client product detail to SKU/styles from a customer's ord_log history.
Customer IDs are configured per user (FDC and e-fashion lists); feature toggles stay on the user group.
Optional: include all approved styles from cus_invt; manual approved style/SKU list per user.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from product_detail_module.product_detail import get_db_connections

FEATURE_MASTER_IMAGE_VIEWER = "master_image_viewer"
FEATURE_CLIENT_PRODUCT_DETAIL = "client_product_detail"

_CACHE_TTL_SEC = 300
_purchased_cache: Dict[str, Tuple[float, Set[str]]] = {}
_company_names_cache: Dict[str, Tuple[float, List[str]]] = {}

EXTERNAL_USER_LIMIT_EN = "External User — function limited."
EXTERNAL_USER_LIMIT_ZH = "外部用户 · 部分功能受限"

MSG_NO_DB_EN = "Invalid style — no data in database."
MSG_NO_DB_ZH = "型号错误，数据库无数据"
MSG_NOT_PURCHASED_EN = "Style not purchased — sorry."
MSG_NOT_PURCHASED_ZH = "本型号有数据但是未采购过。"


def _get_perms(username: Optional[str]) -> Dict[str, Any]:
    if not username:
        return {}
    from py.utilities.auth import get_user_permissions

    perms = get_user_permissions(username)
    return perms if isinstance(perms, dict) else {}


def parse_customer_id_list(raw: Optional[str]) -> List[str]:
    """Split comma-separated customer IDs; drop blanks; preserve order."""
    if not raw:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for piece in str(raw).split(","):
        cid = piece.strip()
        if not cid:
            continue
        key = cid.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(cid)
    return out


def parse_manual_style_list(raw: Optional[str]) -> List[str]:
    """Split manual styles/SKUs on commas, semicolons, or newlines."""
    if not raw:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for piece in re.split(r"[,;\r\n]+", str(raw)):
        token = piece.strip().upper()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _fetch_user_purchase_fields(username: Optional[str]) -> Dict[str, Any]:
    uname = (username or "").strip()
    if not uname:
        return {}
    from py.utilities.auth import get_db_connection

    connection = get_db_connection()
    if not connection:
        return {}
    try:
        cur = connection.cursor(dictionary=True)
        cur.execute(
            """
            SELECT customer_purchase_fdc_ids,
                   customer_purchase_efashion_ids,
                   customer_purchase_include_approved_styles,
                   customer_purchase_manual_styles,
                   master_image_viewer_customer_purchase_only,
                   client_product_detail_customer_purchase_only,
                   master_image_viewer_hide_path
            FROM users
            WHERE username = %s
            LIMIT 1
            """,
            (uname,),
        )
        row = cur.fetchone() or {}
        cur.close()
        return row
    except Exception:
        return {}
    finally:
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass


def get_user_customer_purchase_ids(
    username: Optional[str], perms: Optional[Dict[str, Any]] = None
) -> Tuple[List[str], List[str]]:
    """
    Return (fdc_ids, efashion_ids) for the user.
    Legacy fallback: group-level customer_purchase_customer_id applies to both DBs.
    """
    row = _fetch_user_purchase_fields(username)
    fdc_raw = str(row.get("customer_purchase_fdc_ids") or "").strip()
    ef_raw = str(row.get("customer_purchase_efashion_ids") or "").strip()
    fdc_ids = parse_customer_id_list(fdc_raw)
    ef_ids = parse_customer_id_list(ef_raw)
    if fdc_ids or ef_ids:
        return fdc_ids, ef_ids

    if perms is None:
        perms = _get_perms(username)
    legacy = str(perms.get("customer_purchase_customer_id") or "").strip()
    if legacy:
        legacy_ids = parse_customer_id_list(legacy)
        return legacy_ids, list(legacy_ids)
    return [], []


def user_includes_approved_styles(username: Optional[str]) -> bool:
    row = _fetch_user_purchase_fields(username)
    return bool(row.get("customer_purchase_include_approved_styles"))


def get_user_manual_approved_styles(username: Optional[str]) -> List[str]:
    row = _fetch_user_purchase_fields(username)
    return parse_manual_style_list(row.get("customer_purchase_manual_styles"))


def user_has_customer_purchase_ids(
    username: Optional[str], perms: Optional[Dict[str, Any]] = None
) -> bool:
    fdc_ids, ef_ids = get_user_customer_purchase_ids(username, perms=perms)
    if fdc_ids or ef_ids:
        return True
    if get_user_manual_approved_styles(username):
        return True
    return False


def external_user_limit_message() -> str:
    return f"{EXTERNAL_USER_LIMIT_EN} {EXTERNAL_USER_LIMIT_ZH}"


def customer_purchase_no_data_message() -> str:
    return f"{MSG_NO_DB_EN}\n{MSG_NO_DB_ZH}"


def customer_purchase_not_purchased_message() -> str:
    return f"{MSG_NOT_PURCHASED_EN}\n{MSG_NOT_PURCHASED_ZH}"


def _product_exists_in_inventory(search_term: str) -> bool:
    """True when SKU/style returns rows from FDC or e-fashion inv."""
    term = (search_term or "").strip()
    if not term:
        return False
    try:
        from product_detail_module.product_detail import search_product_data

        return bool(search_product_data(term))
    except Exception:
        return False


def denied_search_message_for_term(search_term: str) -> str:
    """User-facing deny reason: not purchased vs not in database."""
    if _product_exists_in_inventory(search_term):
        return customer_purchase_not_purchased_message()
    return customer_purchase_no_data_message()


def _fetch_company_name(customer_id: str, conn) -> Optional[str]:
    if not conn or not customer_id:
        return None
    query = """
        SELECT TOP 1 LTRIM(RTRIM(c.CUS_NM)) AS cus_nm
        FROM customer c
        WHERE LTRIM(RTRIM(c.CUS_ID)) = LTRIM(RTRIM(?))
    """
    try:
        cur = conn.cursor()
        cur.execute(query, (customer_id,))
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            name = str(row[0]).strip()
            return name if name else None
    except Exception:
        pass
    return None


def fetch_customer_company_names(username: Optional[str]) -> List[str]:
    """Company names (CUS_NM) for the user's configured FDC / e-fashion customer IDs."""
    uname = (username or "").strip()
    if not uname:
        return []

    cache_key = uname.lower()
    now = time.time()
    cached = _company_names_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return list(cached[1])

    fdc_ids, ef_ids = get_user_customer_purchase_ids(username)
    names: List[str] = []
    seen: Set[str] = set()
    conn_fdc, conn_oms_ef = get_db_connections()
    try:
        for cid in fdc_ids:
            nm = _fetch_company_name(cid, conn_fdc)
            if nm:
                key = nm.upper()
                if key not in seen:
                    seen.add(key)
                    names.append(nm)
        for cid in ef_ids:
            nm = _fetch_company_name(cid, conn_oms_ef)
            if nm:
                key = nm.upper()
                if key not in seen:
                    seen.add(key)
                    names.append(nm)
    finally:
        for conn in (conn_fdc, conn_oms_ef):
            if not conn:
                continue
            try:
                conn.close()
            except Exception:
                pass

    _company_names_cache[cache_key] = (now, names)
    return names


def build_portal_home_context(username: Optional[str]) -> Dict[str, Any]:
    """Greeting copy for external users on the tool-picker home page."""
    names = fetch_customer_company_names(username)
    if len(names) == 1:
        company_label = names[0]
    elif names:
        company_label = " · ".join(names)
    else:
        company_label = ""

    if company_label:
        greeting_en = f"Greetings, {company_label}"
        greeting_zh = f"{company_label}，您好"
    else:
        greeting_en = "Greetings"
        greeting_zh = "您好"

    limits_on = (
        customer_purchase_limit_enabled(username, FEATURE_MASTER_IMAGE_VIEWER)
        or customer_purchase_limit_enabled(username, FEATURE_CLIENT_PRODUCT_DETAIL)
    )

    return {
        "portal_greeting_en": greeting_en,
        "portal_greeting_zh": greeting_zh,
        "portal_company_name": company_label,
        "portal_show_limit_notice": limits_on,
        "portal_limit_en": EXTERNAL_USER_LIMIT_EN,
        "portal_limit_zh": EXTERNAL_USER_LIMIT_ZH,
    }


def format_customer_purchase_ids_label(
    username: Optional[str], perms: Optional[Dict[str, Any]] = None
) -> str:
    fdc_ids, ef_ids = get_user_customer_purchase_ids(username, perms=perms)
    parts: List[str] = []
    if fdc_ids:
        parts.append("FDC: " + ", ".join(fdc_ids))
    if ef_ids:
        parts.append("e-fashion: " + ", ".join(ef_ids))
    if user_includes_approved_styles(username):
        parts.append("approved styles (cus_invt)")
    manual = get_user_manual_approved_styles(username)
    if manual:
        parts.append(f"manual ({len(manual)})")
    return "; ".join(parts) if parts else "(not configured)"


def _user_purchase_limit_flag(
    username: Optional[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> bool:
    """Per-user limit toggles; NULL column falls back to legacy group permissions."""
    row = _fetch_user_purchase_fields(username)
    if feature == FEATURE_MASTER_IMAGE_VIEWER:
        col = row.get("master_image_viewer_customer_purchase_only")
        group_key = "master_image_viewer_customer_purchase_only"
    elif feature == FEATURE_CLIENT_PRODUCT_DETAIL:
        col = row.get("client_product_detail_customer_purchase_only")
        group_key = "client_product_detail_customer_purchase_only"
    else:
        return False
    if col is not None:
        return bool(col)
    if perms is None:
        perms = _get_perms(username)
    return bool(perms.get(group_key))


def customer_purchase_limit_enabled(
    username: Optional[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when this feature is limited to allowed SKU/styles for this user."""
    uname = (username or "").strip()
    if uname.lower() == "yeti":
        return False
    if not user_has_customer_purchase_ids(username, perms=perms):
        return False
    return _user_purchase_limit_flag(username, feature, perms=perms)


def _cache_key(
    fdc_ids: List[str],
    ef_ids: List[str],
    include_approved: bool,
    manual_styles: List[str],
) -> str:
    manual_key = "|".join(manual_styles)
    return (
        "fdc:" + "|".join(fdc_ids)
        + "||ef:" + "|".join(ef_ids)
        + "||appr:" + ("1" if include_approved else "0")
        + "||manual:" + manual_key
    )


def _fetch_prod_codes_for_ids(customer_ids: List[str], conn) -> Set[str]:
    codes: Set[str] = set()
    if not conn or not customer_ids:
        return codes

    query = """
        SELECT DISTINCT LTRIM(RTRIM(ol.prod_cd)) AS prod_cd
        FROM ord_log ol
        INNER JOIN customer c ON ol.cus_id = c.cus_id
        WHERE c.cus_id = ?
          AND ol.prod_cd IS NOT NULL
          AND LTRIM(RTRIM(ol.prod_cd)) <> ''
    """
    for customer_id in customer_ids:
        try:
            cur = conn.cursor()
            cur.execute(query, (customer_id,))
            for row in cur.fetchall():
                if row and row[0]:
                    codes.add(str(row[0]).strip().upper())
            cur.close()
        except Exception:
            pass
    return codes


def _fetch_approved_prod_codes_for_ids(customer_ids: List[str], conn) -> Set[str]:
    """Distinct prod_cd from cus_invt (customer approved style list)."""
    codes: Set[str] = set()
    if not conn or not customer_ids:
        return codes

    query = """
        SELECT DISTINCT LTRIM(RTRIM(ci.prod_cd)) AS prod_cd
        FROM cus_invt ci
        WHERE LTRIM(RTRIM(ci.cus_id)) = LTRIM(RTRIM(?))
          AND ci.prod_cd IS NOT NULL
          AND LTRIM(RTRIM(ci.prod_cd)) <> ''
    """
    for customer_id in customer_ids:
        try:
            cur = conn.cursor()
            cur.execute(query, (customer_id,))
            for row in cur.fetchall():
                if row and row[0]:
                    codes.add(str(row[0]).strip().upper())
            cur.close()
        except Exception:
            pass
    return codes


def _expand_manual_tokens_to_prod_codes(
    tokens: List[str], conn_fdc, conn_oms_ef
) -> Set[str]:
    """Add tokens directly; expand base styles to all SKUs via inv."""
    codes: Set[str] = set(tokens)
    style_only: List[str] = []
    for token in tokens:
        if " " not in token:
            style_only.append(token)

    if not style_only:
        return codes

    query = """
        SELECT DISTINCT LTRIM(RTRIM(i.PROD_CD)) AS prod_cd
        FROM dbo.inv i
        WHERE LTRIM(RTRIM(i.PROD_CD)) = ?
           OR LTRIM(RTRIM(i.ALT_CD)) = ?
    """
    for conn in (conn_fdc, conn_oms_ef):
        if not conn:
            continue
        for style in style_only:
            try:
                cur = conn.cursor()
                cur.execute(query, (style, style))
                for row in cur.fetchall():
                    if row and row[0]:
                        codes.add(str(row[0]).strip().upper())
                cur.close()
            except Exception:
                pass
    return codes


def fetch_customer_purchased_prod_codes(
    username: Optional[str], perms: Optional[Dict[str, Any]] = None
) -> Set[str]:
    """
    Allowed prod_cd set: ord_log purchases + optional cus_invt approved
    + manual approved styles/SKUs.
    """
    fdc_ids, ef_ids = get_user_customer_purchase_ids(username, perms=perms)
    include_approved = user_includes_approved_styles(username)
    manual_styles = get_user_manual_approved_styles(username)

    if not fdc_ids and not ef_ids and not include_approved and not manual_styles:
        return set()

    cache_key = _cache_key(fdc_ids, ef_ids, include_approved, manual_styles)
    now = time.time()
    cached = _purchased_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    codes: Set[str] = set()
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc and not conn_oms_ef:
        return codes

    try:
        if fdc_ids and conn_fdc:
            codes |= _fetch_prod_codes_for_ids(fdc_ids, conn_fdc)
        if ef_ids and conn_oms_ef:
            codes |= _fetch_prod_codes_for_ids(ef_ids, conn_oms_ef)
        if include_approved:
            if fdc_ids and conn_fdc:
                codes |= _fetch_approved_prod_codes_for_ids(fdc_ids, conn_fdc)
            if ef_ids and conn_oms_ef:
                codes |= _fetch_approved_prod_codes_for_ids(ef_ids, conn_oms_ef)
        if manual_styles:
            codes |= _expand_manual_tokens_to_prod_codes(
                manual_styles, conn_fdc, conn_oms_ef
            )
        _purchased_cache[cache_key] = (now, codes)
        return codes
    finally:
        for conn in (conn_fdc, conn_oms_ef):
            if not conn:
                continue
            try:
                conn.close()
            except Exception:
                pass


def fetch_approved_styles_rows(
    fdc_ids: List[str], ef_ids: List[str]
) -> List[Dict[str, str]]:
    """Rows for CSV download: prod_cd, customer_id, database."""
    rows: List[Dict[str, str]] = []
    conn_fdc, conn_oms_ef = get_db_connections()
    query = """
        SELECT DISTINCT
            LTRIM(RTRIM(ci.cus_id)) AS customer_id,
            LTRIM(RTRIM(ci.prod_cd)) AS prod_cd
        FROM cus_invt ci
        WHERE LTRIM(RTRIM(ci.cus_id)) = LTRIM(RTRIM(?))
          AND ci.prod_cd IS NOT NULL
          AND LTRIM(RTRIM(ci.prod_cd)) <> ''
        ORDER BY prod_cd
    """
    try:
        if fdc_ids and conn_fdc:
            for cid in fdc_ids:
                try:
                    cur = conn_fdc.cursor()
                    cur.execute(query, (cid,))
                    for customer_id, prod_cd in cur.fetchall():
                        rows.append(
                            {
                                "database": "FDC",
                                "customer_id": str(customer_id or "").strip(),
                                "prod_cd": str(prod_cd or "").strip(),
                            }
                        )
                    cur.close()
                except Exception:
                    pass
        if ef_ids and conn_oms_ef:
            for cid in ef_ids:
                try:
                    cur = conn_oms_ef.cursor()
                    cur.execute(query, (cid,))
                    for customer_id, prod_cd in cur.fetchall():
                        rows.append(
                            {
                                "database": "e-fashion",
                                "customer_id": str(customer_id or "").strip(),
                                "prod_cd": str(prod_cd or "").strip(),
                            }
                        )
                    cur.close()
                except Exception:
                    pass
    finally:
        for conn in (conn_fdc, conn_oms_ef):
            if not conn:
                continue
            try:
                conn.close()
            except Exception:
                pass
    return rows


def is_search_allowed_for_customer(search_term: str, purchased: Set[str]) -> bool:
    """Match SKU, style prefix, or style-only search against purchased prod_cd set."""
    if not search_term or not purchased:
        return False

    term = search_term.strip().upper().split(",")[0].strip()
    if not term:
        return False

    for pc in purchased:
        if term == pc:
            return True
        if pc.startswith(term + " ") or pc.startswith(term):
            return True
        style = pc.split()[0] if pc else ""
        if style and (term == style or style.startswith(term) or term.startswith(style)):
            return True
    return False


def check_customer_purchase_search(
    username: Optional[str],
    search_term: str,
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Returns (allowed, error_message).
    error_message is set when denied.
    """
    if not customer_purchase_limit_enabled(username, feature, perms=perms):
        return True, None

    purchased = fetch_customer_purchased_prod_codes(username, perms=perms)
    if not purchased:
        return False, denied_search_message_for_term(search_term)

    if is_search_allowed_for_customer(search_term, purchased):
        return True, None

    return False, denied_search_message_for_term(search_term)


def filter_skus_for_customer(
    username: Optional[str],
    skus: List[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str]]:
    """Return (allowed_skus, rejected_skus) when customer purchase limit applies."""
    allowed, skipped = filter_skus_for_customer_detailed(
        username, skus, feature, perms=perms
    )
    return allowed, [str(x.get("sku") or "") for x in skipped]


def filter_skus_for_customer_detailed(
    username: Optional[str],
    skus: List[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Return (allowed_skus, skipped) with per-SKU denial messages."""
    if not customer_purchase_limit_enabled(username, feature, perms=perms):
        return list(skus), []

    allowed: List[str] = []
    skipped: List[Dict[str, str]] = []
    purchased = fetch_customer_purchased_prod_codes(username, perms=perms)

    for sku in skus:
        term = str(sku or "").strip()
        if not term:
            continue
        if purchased and is_search_allowed_for_customer(term, purchased):
            allowed.append(term)
            continue
        reason, message = (
            ("not_purchased", customer_purchase_not_purchased_message())
            if _product_exists_in_inventory(term)
            else ("no_data", customer_purchase_no_data_message())
        )
        skipped.append({"sku": term.upper(), "reason": reason, "message": message})

    return allowed, skipped


def batch_all_skus_skipped_message(skipped: List[Dict[str, str]]) -> str:
    if not skipped:
        return customer_purchase_no_data_message()
    if len(skipped) == 1:
        return str(skipped[0].get("message") or customer_purchase_not_purchased_message())
    lines = [f"{s.get('sku', '?')}: {s.get('message', '')}" for s in skipped]
    return "\n".join(lines)


def customer_purchase_notice(
    username: Optional[str], feature: str, perms: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    if not customer_purchase_limit_enabled(username, feature, perms=perms):
        return None
    return external_user_limit_message()


def json_forbidden_if_customer_purchase_denied(
    username: Optional[str], search_term: str, feature: str
):
    """Flask JSON 403 response tuple, or None if allowed."""
    from flask import jsonify

    allowed, msg = check_customer_purchase_search(username, search_term, feature)
    if allowed:
        return None
    return jsonify({"error": msg, "success": False}), 403


def filter_product_data_for_customer(
    username: Optional[str],
    product_data: Optional[List[dict]],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> Optional[List[dict]]:
    """Keep only style/color rows whose SKU is in the customer's order history."""
    if not product_data or not customer_purchase_limit_enabled(
        username, feature, perms=perms
    ):
        return product_data

    purchased = fetch_customer_purchased_prod_codes(username, perms=perms)
    if not purchased:
        return None

    filtered: List[dict] = []
    for style_group in product_data:
        colors = []
        for color in style_group.get("colors") or []:
            sku = (color.get("sku") or "").strip()
            if sku and is_search_allowed_for_customer(sku, purchased):
                colors.append(color)
        if colors:
            row = dict(style_group)
            row["colors"] = colors
            filtered.append(row)
    return filtered if filtered else None
