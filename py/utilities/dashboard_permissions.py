"""
Home dashboard tool cards — ids match templates/index.html data-tool attributes.
Permissions JSON on users.permissions:
  tools: list[str] | omitted → full dashboard access
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Blueprint name -> dashboard tool id (must match DASHBOARD_TOOLS ids). Used by routes.before_request.
# Legacy tk_user password session (master_image_viewer only); prefer client group login.
TK_PASSWORD_BLUEPRINTS = frozenset({"master_image_viewer"})

# Client portal tool ids (same access as legacy TK password users).
CLIENT_PORTAL_TOOL_IDS = frozenset({"clientview", "master_image_viewer"})

# auth blueprint routes that are not covered by TOOL_BY_BLUEPRINT (blueprint-wide).
ENDPOINT_TOOL: Dict[str, str] = {
    "auth.manage_users": "manage_users",
    "auth.edit_user": "manage_users",
    "auth.delete_user": "manage_users",
    "auth.external_service_log": "manage_users",
    "auth.download_customer_approved_styles": "manage_users",
}

# Unauthenticated LAN probe / session handoff for Trade Show Tool (large local uploads).
PUBLIC_ENDPOINTS = frozenset(
    {
        "tradeshowtool.lan_ping",
        "tradeshowtool.lan_ping_png",
        "tradeshowtool.lan_claim",
        "whatnot_live.overlay",
        "whatnot_live.overlay_alias",
        "whatnot_live.camera",
        "whatnot_live.stream",
        "whatnot_live.ingest",
        "whatnot_live.public_state",
        "whatnot_live.webrtc_signal",
    }
)

TOOL_BY_BLUEPRINT: Dict[str, str] = {
    "item_lookup": "item_lookup",
    "invoice_lookup": "invoice_lookup",
    "customer": "customer",
    "order_lookup": "order_lookup",
    "order_processor": "order_processor",
    "external_stock_check": "external_stock_check",
    "internal_stock_check": "internal_stock_check",
    "inventory_manual_adjuster": "inventory_manual_adjuster",
    "four_seller": "four_seller",
    "location_massive_audit": "location_massive_update",
    "outofstock_check": "oosc",
    "dam_sync": "dam",
    "image_upload": "image_upload",
    "master_image_viewer": "master_image_viewer",
    "clientview": "clientview",
    "bag_style_viewer": "bag_style_viewer",
    "burlington_highlight": "burlington_highlight",
    "v7_listcreator": "v7_listcreator",
    "lori_automation": "loris_fedex",
    "mobile_scanner": "mobile_scanner",
    "burlington_label_processor": "burlington_label_processor",
    "print_center": "print_center",
    "tradeshowtool": "tradeshowtool",
    "boat_service": "boat",
    "whmap": "warehousemap",
    "master_audit": "audit",
    "vendor_app": "vendor_app",
    "container": "fdcef_container_search",
    "whatnot_live": "whatnot_live",
}

# Single source of truth for admin UI + home filtering (order = display order)
DASHBOARD_TOOLS: List[Dict[str, str]] = [
    {"id": "item_lookup", "title": "SKU/STYLE Lookup", "category": "Catalog"},
    {"id": "clientview", "title": "Client Portal", "category": "Catalog"},
    {"id": "bag_style_viewer", "title": "Bag Style Viewer", "category": "Catalog"},
    {"id": "v7_listcreator", "title": "List Creator V7", "category": "Catalog"},
    {"id": "tradeshowtool", "title": "Trade Show Tool", "category": "Catalog"},
    {"id": "master_image_viewer", "title": "Master Image Viewer", "category": "Catalog"},
    {"id": "dam", "title": "DAM System", "category": "Catalog"},
    {"id": "image_upload", "title": "Smart Image Upload", "category": "Catalog"},
    {"id": "burlington_highlight", "title": "Burlington-highlight", "category": "Catalog"},
    {"id": "order_lookup", "title": "Sales Orders", "category": "Sales"},
    {"id": "order_processor", "title": "Order Processor", "category": "Sales"},
    {"id": "customer", "title": "Customer Analytics", "category": "Sales"},
    {"id": "invoice_lookup", "title": "Invoice Lookup", "category": "Sales"},
    {"id": "four_seller", "title": "4Seller Stock Sync", "category": "Sales"},
    {"id": "whatnot_live", "title": "Whatnot Live", "category": "Sales"},
    {"id": "external_stock_check", "title": "External Stock Check", "category": "Inventory"},
    {"id": "internal_stock_check", "title": "Internal Stock Check", "category": "Inventory"},
    {"id": "inventory_manual_adjuster", "title": "Inventory Manual Adjuster", "category": "Inventory"},
    {"id": "oosc", "title": "Out of Stock Check", "category": "Inventory"},
    {"id": "location_massive_update", "title": "Massive Location Update", "category": "Inventory"},
    {"id": "boat", "title": "Shipping Intelligence", "category": "Warehouse"},
    {"id": "fdcef_container_search", "title": "FDCEF container search", "category": "Warehouse"},
    {"id": "warehousemap", "title": "Warehouse Map", "category": "Warehouse"},
    {"id": "loris_fedex", "title": "Loris Automation", "category": "Warehouse"},
    {"id": "print_center", "title": "打印中心", "category": "Warehouse"},
    {"id": "mobile_scanner", "title": "Mobile Barcode Scanner", "category": "Warehouse"},
    {"id": "burlington_label_processor", "title": "Burlington Label Processor", "category": "Warehouse"},
    {"id": "audit", "title": "Master Audit", "category": "Admin"},
    {"id": "manage_users", "title": "User Management", "category": "Admin"},
    {"id": "vendor_app", "title": "Vendor App", "category": "Admin"},
]

# Navbar shortcuts for starred home cards (hrefs match templates/index.html).
DASHBOARD_TOOL_NAV: Dict[str, Dict[str, str]] = {
    "item_lookup": {"href": "/item_lookup", "label": "SKU"},
    "invoice_lookup": {"href": "/invoice_lookup", "label": "Invoice"},
    "customer": {"href": "/customer", "label": "Customers"},
    "order_lookup": {"href": "/order_lookup", "label": "Orders"},
    "order_processor": {"href": "/order_processor", "label": "Processor"},
    "external_stock_check": {"href": "/external_stock_check", "label": "Ext Stock"},
    "internal_stock_check": {"href": "/internal_stock_check", "label": "Int Stock"},
    "inventory_manual_adjuster": {"href": "/inventory_manual_adjuster", "label": "Inv Adjust"},
    "four_seller": {"href": "/4seller", "label": "4Seller"},
    "location_massive_update": {"href": "/location_massive_update", "label": "Locations"},
    "oosc": {"href": "/oosc", "label": "OOS"},
    "dam": {"href": "/dam", "label": "DAM"},
    "image_upload": {"href": "/image_upload", "label": "Upload"},
    "master_image_viewer": {"href": "/master_image_viewer", "label": "Images"},
    "clientview": {"href": "/clientview/product_detail", "label": "Client PD"},
    "client_product_detail": {"href": "/clientview/product_detail", "label": "Client PD"},
    "bag_style_viewer": {"href": "/bag_style_viewer", "label": "Bag Style"},
    "burlington_highlight": {"href": "/burlington_highlight", "label": "Burl Highlight"},
    "v7_listcreator": {"href": "/v7_listcreator", "label": "V7 List"},
    "v6_listcreator": {"href": "/v7_listcreator", "label": "V7 List"},
    "loris_fedex": {"href": "/lori_automation/loris_fedex", "label": "Loris"},
    "mobile_scanner": {"href": "/mobile_scanner", "label": "Scanner"},
    "burlington_label_processor": {"href": "/burlington_label_processor", "label": "Burl Labels"},
    "tradeshowtool": {"href": "/tradeshowtool", "label": "Trade Show"},
    "print_center": {"href": "/print_center", "label": "Print"},
    "whatnot_live": {"href": "/whatnot_live", "label": "Whatnot"},
    "boat": {"href": "/boat", "label": "Boat"},
    "fdcef_container_search": {"href": "/container_search", "label": "Containers"},
    "warehousemap": {"href": "/warehousemap", "label": "Whs Map"},
    "audit": {"href": "/audit", "label": "Audit"},
    "manage_users": {"href": "/manage_users", "label": "Users"},
    "vendor_app": {"href": "/vendor_app", "label": "Vendor"},
}


WHATNOT_LIVE_USERS = frozenset({"yeti", "elaine"})


def tool_visible_for_user(perms: Optional[Dict[str, Any]], username: str, tool_id: str) -> bool:
    """yeti always sees everything; missing tools key = unrestricted."""
    uname = (username or "").strip().lower()
    if tool_id == "whatnot_live":
        return uname in WHATNOT_LIVE_USERS
    if uname == "yeti":
        return True
    if not perms:
        return True
    tools = perms.get("tools")
    if tools is None:
        return True
    if not isinstance(tools, list):
        return True
    if tool_id in tools:
        return True
    # Legacy permission id after rename from switch_number
    aliases = {
        "inventory_manual_adjuster": "switch_number",
        "switch_number": "inventory_manual_adjuster",
    }
    legacy = aliases.get(tool_id)
    return legacy in tools if legacy else False


def _wants_json_forbidden_response() -> bool:
    """Prefer JSON 403 for API-like requests so fetch/XHR does not follow HTML redirect."""
    from flask import request

    p = request.path or ""
    if p.startswith("/api/") or "/api/" in p:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    try:
        if request.accept_mimetypes.best_match(["application/json", "text/html"]) == "application/json":
            return True
    except Exception:
        pass
    return False


def enforce_dashboard_tool_access():
    """
    Flask before_request: block users who lack permission for the dashboard tool
    that owns the current blueprint (direct URL access, not only hidden cards).
    """
    from flask import jsonify, redirect, request, session, flash, url_for

    if request.method == "OPTIONS":
        return None
    ep = request.endpoint
    if ep in PUBLIC_ENDPOINTS:
        return None
    # Task Scheduler curl uses X-DAM-Cron-Secret on /dam/cron/* — no browser session.
    if (request.path or "").startswith("/dam/cron/"):
        return None
    if ep == "static" or (ep is not None and ep.endswith(".static")):
        return None
    bp = request.blueprint
    tool_id = TOOL_BY_BLUEPRINT.get(bp) if bp else None
    if not tool_id and ep:
        tool_id = ENDPOINT_TOOL.get(ep)
    if not tool_id:
        return None
    uname = session.get("username")
    if not uname:
        if session.get("tk_user") and bp in TK_PASSWORD_BLUEPRINTS:
            return None
        session["next"] = request.url
        return redirect(url_for("auth.login"))
    if tool_id == "manage_users" and uname.strip().lower() != "yeti":
        if _wants_json_forbidden_response():
            return (
                jsonify(
                    error="Forbidden",
                    message="You do not have access to user management.",
                ),
                403,
            )
        flash("Access denied. Only yeti can manage users.", "warning")
        return redirect(url_for("home"))
    from py.utilities.auth import get_user_permissions

    perms = get_user_permissions(uname)
    if tool_visible_for_user(perms, uname, tool_id):
        return None
    if _wants_json_forbidden_response():
        return (
            jsonify(
                error="Forbidden",
                message="You do not have access to this application.",
            ),
            403,
        )
    flash(
        "You do not have access to that application. Ask an administrator to enable it on your account.",
        "warning",
    )
    return redirect(url_for("home"))


def get_dashboard_favorites(username: str) -> List[str]:
    """Starred dashboard tool ids for this user (request-cached)."""
    import json

    from flask import g, has_request_context

    from py.utilities.database_utils import get_mysql_connection

    uname = (username or "").strip()
    if not uname:
        return []
    cache = None
    if has_request_context():
        cache = getattr(g, "_fdc_dashboard_favorites", None)
        if cache is None:
            cache = {}
            g._fdc_dashboard_favorites = cache
        if uname in cache:
            return cache[uname]
    out: List[str] = []
    conn = None
    cursor = None
    try:
        conn = get_mysql_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("SELECT favorites FROM users WHERE username = %s", (uname,))
            row = cursor.fetchone()
            raw = json.loads(row[0]) if row and row[0] else []
            if isinstance(raw, list):
                out = [str(x).strip() for x in raw if str(x).strip()]
    except Exception:
        out = []
    finally:
        try:
            if cursor is not None:
                cursor.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    if cache is not None:
        cache[uname] = out
    return out


def navbar_items_from_favorites(favorite_ids: List[str], tool_allowed_fn) -> List[Dict[str, str]]:
    """Resolve starred tool ids to navbar href/label, skipping tools the user cannot open."""
    items: List[Dict[str, str]] = []
    seen = set()
    for tool_id in favorite_ids or []:
        nav = DASHBOARD_TOOL_NAV.get(tool_id)
        if not nav:
            continue
        acl_id = "clientview" if tool_id == "client_product_detail" else tool_id
        try:
            if tool_allowed_fn and not tool_allowed_fn(acl_id) and not tool_allowed_fn(tool_id):
                continue
        except Exception:
            continue
        href = nav.get("href") or ""
        if not href or href in seen:
            continue
        seen.add(href)
        items.append({"id": tool_id, "href": href, "label": nav.get("label") or tool_id})
    return items
