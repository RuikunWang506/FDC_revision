"""
Home dashboard tool cards — ids match templates/index.html data-tool attributes.
Permissions JSON on users.permissions:
  tools: list[str] | omitted → full dashboard access
  mail_account_ids: list[int] | omitted → all mailboxes
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Blueprint name -> dashboard tool id (must match DASHBOARD_TOOLS ids). Used by routes.before_request.
TOOL_BY_BLUEPRINT: Dict[str, str] = {
    "item_lookup": "item_lookup",
    "invoice_lookup": "invoice_lookup",
    "customer": "customer",
    "email": "fdc_mail",
    "order_lookup": "order_lookup",
    "order_processor": "order_processor",
    "external_stock_check": "external_stock_check",
    "internal_stock_check": "internal_stock_check",
    "four_seller": "four_seller",
    "location_massive_audit": "location_massive_update",
    "outofstock_check": "oosc",
    "dam_sync": "dam",
    "image_upload": "image_upload",
    "master_image_viewer": "master_image_viewer",
    "bag_style_viewer": "bag_style_viewer",
    "burlington_highlight": "burlington_highlight",
    "v7_listcreator": "v7_listcreator",
    "lori_automation": "loris_fedex",
    "mobile_scanner": "mobile_scanner",
    "burlington_label_processor": "burlington_label_processor",
    "boat_service": "boat",
    "whmap": "warehousemap",
    "master_audit": "audit",
    "vendor_app": "vendor_app",
    "container": "fdcef_container_search",
}

# Single source of truth for admin UI + home filtering (order = display order)
DASHBOARD_TOOLS: List[Dict[str, str]] = [
    {"id": "item_lookup", "title": "SKU/STYLE Lookup", "category": "Lookup"},
    {"id": "invoice_lookup", "title": "Invoice Lookup", "category": "Lookup"},
    {"id": "customer", "title": "Customer Analytics", "category": "Lookup"},
    {"id": "fdc_mail", "title": "Mail", "category": "Lookup"},
    {"id": "order_lookup", "title": "Sales Orders", "category": "Orders"},
    {"id": "order_processor", "title": "Order Processor", "category": "Orders"},
    {"id": "external_stock_check", "title": "External Stock Check", "category": "Inventory"},
    {"id": "internal_stock_check", "title": "Internal Stock Check", "category": "Inventory"},
    {"id": "four_seller", "title": "4Seller Stock Sync", "category": "Inventory"},
    {"id": "location_massive_update", "title": "Massive Location Update", "category": "Inventory"},
    {"id": "oosc", "title": "Out of Stock Check", "category": "Inventory"},
    {"id": "dam", "title": "DAM System", "category": "Media"},
    {"id": "image_upload", "title": "Smart Image Upload", "category": "Media"},
    {"id": "master_image_viewer", "title": "Master Image Viewer", "category": "Media"},
    {"id": "bag_style_viewer", "title": "Bag Style Viewer", "category": "Media"},
    {"id": "burlington_highlight", "title": "Burlington-highlight", "category": "Media"},
    {"id": "v7_listcreator", "title": "List Creator V7", "category": "Utilities"},
    {"id": "loris_fedex", "title": "Loris Automation", "category": "Utilities"},
    {"id": "mobile_scanner", "title": "Mobile Barcode Scanner", "category": "Utilities"},
    {"id": "burlington_label_processor", "title": "Burlington Label Processor", "category": "Utilities"},
    {"id": "boat", "title": "Shipping Intelligence", "category": "Logistics"},
    {"id": "fdcef_container_search", "title": "FDCEF container search", "category": "Logistics"},
    {"id": "warehousemap", "title": "Warehouse Map", "category": "Logistics"},
    {"id": "audit", "title": "Master Audit", "category": "Admin"},
    {"id": "manage_users", "title": "User Management", "category": "Admin"},
    {"id": "vendor_app", "title": "Vendor App", "category": "Admin"},
]


def tool_visible_for_user(perms: Optional[Dict[str, Any]], username: str, tool_id: str) -> bool:
    """yeti always sees everything; missing tools key = unrestricted."""
    if (username or "").strip().lower() == "yeti":
        return True
    if not perms:
        return True
    tools = perms.get("tools")
    if tools is None:
        return True
    if not isinstance(tools, list):
        return True
    return tool_id in tools


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
    if ep == "static" or (ep is not None and ep.endswith(".static")):
        return None
    bp = request.blueprint
    if not bp:
        return None
    tool_id = TOOL_BY_BLUEPRINT.get(bp)
    if not tool_id:
        return None
    uname = session.get("username")
    if not uname:
        session["next"] = request.url
        return redirect(url_for("auth.login"))
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


def mail_accounts_allowed_ids(perms: Optional[Dict[str, Any]], username: str) -> Optional[set]:
    """
    None = unrestricted (all mailboxes).
    set of int = only those account ids.
    """
    if (username or "").strip().lower() == "yeti":
        return None
    if not perms:
        return None
    raw = perms.get("mail_account_ids")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: set = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out
