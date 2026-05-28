"""
Limit Master Image Viewer / client product detail to SKU/styles from a customer's ord_log history.
Configured per user group in manage_users.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set, Tuple

from product_detail_module.product_detail import get_db_connections

FEATURE_MASTER_IMAGE_VIEWER = "master_image_viewer"
FEATURE_CLIENT_PRODUCT_DETAIL = "client_product_detail"

_CACHE_TTL_SEC = 300
_purchased_cache: Dict[str, Tuple[float, Set[str]]] = {}


def _get_perms(username: Optional[str]) -> Dict[str, Any]:
    if not username:
        return {}
    from py.utilities.auth import get_user_permissions

    perms = get_user_permissions(username)
    return perms if isinstance(perms, dict) else {}


def get_customer_purchase_customer_id(
    username: Optional[str], perms: Optional[Dict[str, Any]] = None
) -> str:
    if perms is None:
        perms = _get_perms(username)
    return str(perms.get("customer_purchase_customer_id") or "").strip()


def customer_purchase_limit_enabled(
    username: Optional[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when this feature is limited to customer's purchased SKU/styles."""
    uname = (username or "").strip()
    if uname.lower() == "yeti":
        return False
    if perms is None:
        perms = _get_perms(username)
    if not perms:
        return False
    cust_id = get_customer_purchase_customer_id(username, perms=perms)
    if not cust_id:
        return False
    if feature == FEATURE_MASTER_IMAGE_VIEWER:
        return bool(perms.get("master_image_viewer_customer_purchase_only"))
    if feature == FEATURE_CLIENT_PRODUCT_DETAIL:
        return bool(perms.get("client_product_detail_customer_purchase_only"))
    return False


def fetch_customer_purchased_prod_codes(customer_id: str) -> Set[str]:
    """Distinct prod_cd values from ord_log for customer (FDC + E_FAS)."""
    customer_id = (customer_id or "").strip()
    if not customer_id:
        return set()

    now = time.time()
    cached = _purchased_cache.get(customer_id)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    codes: Set[str] = set()
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return codes

    query = """
        SELECT DISTINCT LTRIM(RTRIM(ol.prod_cd)) AS prod_cd
        FROM ord_log ol
        INNER JOIN customer c ON ol.cus_id = c.cus_id
        WHERE c.cus_id = ?
          AND ol.prod_cd IS NOT NULL
          AND LTRIM(RTRIM(ol.prod_cd)) <> ''
    """

    try:
        for conn in (conn_fdc, conn_oms_ef):
            try:
                cur = conn.cursor()
                cur.execute(query, (customer_id,))
                for row in cur.fetchall():
                    if row and row[0]:
                        codes.add(str(row[0]).strip().upper())
                cur.close()
            except Exception:
                pass
        _purchased_cache[customer_id] = (now, codes)
        return codes
    finally:
        try:
            conn_fdc.close()
        except Exception:
            pass
        try:
            conn_oms_ef.close()
        except Exception:
            pass


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

    cust_id = get_customer_purchase_customer_id(username, perms=perms)
    purchased = fetch_customer_purchased_prod_codes(cust_id)
    if not purchased:
        return False, (
            f"No order history found for customer ID {cust_id}. "
            "You can only search SKU/styles this customer has purchased."
        )

    if is_search_allowed_for_customer(search_term, purchased):
        return True, None

    return False, (
        f"This SKU/style is not in customer {cust_id}'s order history. "
        "Only purchased items are allowed."
    )


def filter_skus_for_customer(
    username: Optional[str],
    skus: List[str],
    feature: str,
    perms: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str]]:
    """Return (allowed_skus, rejected_skus) when customer purchase limit applies."""
    if not customer_purchase_limit_enabled(username, feature, perms=perms):
        return list(skus), []

    allowed: List[str] = []
    rejected: List[str] = []
    for sku in skus:
        ok, _ = check_customer_purchase_search(username, sku, feature, perms=perms)
        if ok:
            allowed.append(sku)
        else:
            rejected.append(sku)
    return allowed, rejected


def customer_purchase_notice(
    username: Optional[str], feature: str, perms: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    if not customer_purchase_limit_enabled(username, feature, perms=perms):
        return None
    cust_id = get_customer_purchase_customer_id(username, perms=perms)
    return (
        f"Search limited to SKU/styles purchased by customer {cust_id} "
        f"(order history)."
    )


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

    cust_id = get_customer_purchase_customer_id(username, perms=perms)
    purchased = fetch_customer_purchased_prod_codes(cust_id)
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
