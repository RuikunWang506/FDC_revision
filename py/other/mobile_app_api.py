"""JSON API for the native FDC iOS app (not a website wrapper)."""
from __future__ import annotations

import math
import re
from functools import wraps
from io import StringIO
from typing import Any, Dict, List, Optional

import pandas as pd
from flask import Blueprint, current_app, jsonify, request, session

from py.utilities.csrf import get_csrf_token
from py.utilities.dashboard_permissions import DASHBOARD_TOOLS, tool_visible_for_user
from py.utilities.rate_limit import auth_limiter, client_limit_key, request_client_ip
from py.utilities.setting import Config

mobile_app_bp = Blueprint("mobile_app", __name__, url_prefix="/api/mobile")

NATIVE_TOOL_IDS = frozenset({"item_lookup", "order_lookup"})
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: Any) -> str:
    text = "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)
    return _HTML_TAG_RE.sub("", text).replace("\xa0", " ").strip()


def _json_cell(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    text = strip_html(value)
    return text


def _records_from_html_table(html: str, extra_headers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not html:
        return []
    wrapped = html
    if extra_headers:
        head = "".join(f"<th>{h}</th>" for h in extra_headers)
        wrapped = f"<table><thead><tr>{head}</tr></thead><tbody>{html}</tbody></table>"
    df = pd.read_html(StringIO(wrapped))[0]
    out = []
    for row in df.to_dict(orient="records"):
        out.append({str(k): _json_cell(v) for k, v in row.items()})
    return out


def tools_payload(username: str, perms: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from py.utilities.module_loader import tool_id_available

    tools = []
    for spec in DASHBOARD_TOOLS:
        tool_id = spec["id"]
        if not tool_visible_for_user(perms, username, tool_id):
            continue
        if not tool_id_available(current_app, tool_id):
            continue
        tools.append(
            {
                "id": tool_id,
                "title": spec["title"],
                "category": spec["category"],
                "native": tool_id in NATIVE_TOOL_IDS,
            }
        )
    return tools


def _session_user() -> Optional[str]:
    name = (session.get("username") or "").strip()
    return name or None


def _user_payload(username: str) -> Dict[str, Any]:
    from py.utilities.auth import get_user_permissions

    perms = session.get("user_permissions") or get_user_permissions(username) or {}
    return {
        "username": username,
        "csrf": get_csrf_token(),
        "tools": tools_payload(username, perms if isinstance(perms, dict) else {}),
    }


def _json_login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _session_user():
            return jsonify({"ok": False, "error": "Login required"}), 401
        return fn(*args, **kwargs)

    return wrapped


def _require_tool(tool_id: str):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            username = _session_user()
            if not username:
                return jsonify({"ok": False, "error": "Login required"}), 401
            from py.utilities.auth import get_user_permissions

            perms = session.get("user_permissions") or get_user_permissions(username) or {}
            if not tool_visible_for_user(perms if isinstance(perms, dict) else {}, username, tool_id):
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            return fn(*args, **kwargs)

        return wrapped

    return decorator


@mobile_app_bp.route("/session", methods=["GET"])
def mobile_session():
    username = _session_user()
    if not username:
        return jsonify({"ok": True, "user": None, "csrf": get_csrf_token()})
    return jsonify({"ok": True, **_user_payload(username)})


@mobile_app_bp.route("/login", methods=["POST"])
def mobile_login():
    from py.utilities.auth import (
        check_credentials,
        get_user_permissions,
        log_user_activity,
        update_last_login,
    )
    from py.utilities.logging_setup import log_event

    body = request.get_json(silent=True) or {}
    username = str(body.get("username") or request.form.get("username") or "").strip()
    password = str(body.get("password") or request.form.get("password") or "")
    remember = bool(body.get("remember") or request.form.get("remember"))
    client_ip = request_client_ip()
    login_user_key = client_limit_key(client_ip, f"login:{(username or 'unknown').lower()}")
    login_ip_key = client_limit_key(client_ip, "login")
    if auth_limiter.is_limited(login_user_key, 8, 600) or auth_limiter.is_limited(login_ip_key, 30, 600):
        return jsonify({"ok": False, "error": "Too many login attempts. Wait a few minutes."}), 429
    if not username or not password or not check_credentials(username, password):
        auth_limiter.record(login_user_key)
        auth_limiter.record(login_ip_key)
        log_event(
            "WARNING",
            f'RED FAILED_LOGIN mobile: Username: "{username}", IP: {client_ip}',
        )
        return jsonify({"ok": False, "error": "Invalid username or password"}), 401

    update_last_login(username)
    session["username"] = username
    session["logged_in"] = True
    session["user_permissions"] = get_user_permissions(username)
    session.permanent = remember
    if username.strip().lower() == "yetimaster":
        try:
            from py.utilities.master_mode import enable_master_session

            enable_master_session()
        except Exception:
            pass
    log_user_activity(username)
    return jsonify({"ok": True, **_user_payload(username)})


@mobile_app_bp.route("/logout", methods=["POST"])
def mobile_logout():
    try:
        from py.utilities.online_presence import mark_offline

        mark_offline(session.get("presence_session_id") or "")
    except Exception:
        pass
    session.clear()
    return jsonify({"ok": True, "csrf": get_csrf_token()})


@mobile_app_bp.route("/item_lookup", methods=["GET"])
@_json_login_required
@_require_tool("item_lookup")
def mobile_item_lookup():
    query = (request.args.get("q") or request.args.get("user_input") or "").strip()
    if not query:
        return jsonify({"ok": False, "error": "Enter a SKU or style"}), 400
    try:
        from py.search.item_lookup import fetch_locations
        from py.utilities.SQL_connector import generate_excel
        from py.utilities.logging_setup import log_user_action

        user_input = query.lower().strip().rstrip(",")
        excel_html, _, _, _, _, user_input = generate_excel(user_input)
        rows: List[Dict[str, Any]] = []
        df = None
        if excel_html:
            df = pd.read_html(StringIO(excel_html))[0]
            if "SKU" in df.columns:
                skus = df["SKU"].astype(str).str.lower().tolist()
                location_dict = fetch_locations(skus)
                df["Location"] = df["SKU"].astype(str).str.lower().map(location_dict).fillna("")
            if "DB" in df.columns:
                df["DB"] = (
                    df["DB"]
                    .astype(str)
                    .str.lower()
                    .replace({"oms_ef": "E_FAS", "omsdata_fdc": "FDC"})
                )
            rows = [{str(k): _json_cell(v) for k, v in rec.items()} for rec in df.to_dict(orient="records")]

        images = _item_images(user_input, df)
        log_user_action(
            f"***{session.get('username')}***__@@@search SKU@@@__ {user_input} [mobile]",
            session.get("username"),
        )
        return jsonify({"ok": True, "query": user_input, "rows": rows, "images": images})
    except Exception as exc:
        current_app.logger.exception("mobile item_lookup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


def _item_images(user_input: str, df) -> List[Dict[str, str]]:
    try:
        from py.search.item_lookup import _looks_like_hash_sku, _resolve_image_search_terms
        from py.utilities.dam_utils import get_asset_url, search_assets_by_sku_pattern
        import py.utilities.models
        from py.utilities.models import ImageAsset

        search_terms, image_match_mode = _resolve_image_search_terms(user_input, df)
        origin = (getattr(Config, "PUBLIC_APP_ORIGIN", None) or "https://www.fdcef.com").rstrip("/")
        images: List[Dict[str, str]] = []
        db = py.utilities.models.SessionLocal()
        try:
            seen = set()
            for term in search_terms:
                sku_filter = (
                    ImageAsset.sku.ilike(term)
                    if image_match_mode == "exact"
                    else ImageAsset.sku.ilike(f"{term}%")
                )
                assets = (
                    db.query(ImageAsset)
                    .filter(
                        sku_filter,
                        ImageAsset.is_active == True,  # noqa: E712
                        ImageAsset.source.in_(["catalog_original", "dropbox"]),
                    )
                    .order_by(ImageAsset.version)
                    .limit(24)
                    .all()
                )
                for asset in assets:
                    if _looks_like_hash_sku(asset.sku) or asset.filename in seen:
                        continue
                    seen.add(asset.filename)
                    path = f"/image/{asset.id}/300"
                    images.append(
                        {
                            "sku": asset.sku or "",
                            "filename": asset.filename or "",
                            "url": origin + path,
                        }
                    )
                    if len(images) >= 24:
                        return images
        finally:
            db.close()
        if images:
            return images
        for term in search_terms:
            for asset in search_assets_by_sku_pattern(term, source_filter=["catalog_original", "dropbox"])[:12]:
                url = get_asset_url(asset.id)
                if url and not url.startswith("http"):
                    url = origin + url
                images.append({"sku": asset.sku or "", "filename": asset.filename or "", "url": url or ""})
        return images[:24]
    except Exception as exc:
        current_app.logger.debug("mobile item images skipped: %s", exc)
        return []


ORDER_ITEM_COLUMNS = [
    "line",
    "SKU",
    "Descrip",
    "Qty",
    "price",
    "per_box",
    "Boxes",
    "Location",
    "Stock",
    "Weight",
]


@mobile_app_bp.route("/order_lookup", methods=["GET"])
@_json_login_required
@_require_tool("order_lookup")
def mobile_order_lookup():
    raw = (request.args.get("q") or request.args.get("order_number") or request.args.get("po") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "Enter an order or PO number"}), 400
    try:
        from py.order.order_search import find_order_number_by_po, generate_order
        from py.utilities.logging_setup import log_user_action

        order_number = raw
        if not raw.isdigit():
            found = find_order_number_by_po(raw)
            if not found:
                return jsonify({"ok": False, "error": f"No order found for PO: {raw}"}), 404
            order_number = found
        session["order_number"] = order_number
        header_info, items_html, _, items_total_qty, items_total_boxes, items_total_weight = generate_order(
            order_number
        )
        if not header_info:
            return jsonify({"ok": False, "error": f"No data for order {order_number}"}), 404
        items = _records_from_html_table(items_html or "", ORDER_ITEM_COLUMNS)
        log_user_action(
            f"***{session.get('username')}***__@@@search order@@@__ {order_number} [mobile]",
            session.get("username"),
        )
        return jsonify(
            {
                "ok": True,
                "order_number": order_number,
                "header": {str(k): _json_cell(v) for k, v in (header_info or {}).items()},
                "items": items,
                "totals": {
                    "qty": items_total_qty,
                    "boxes": items_total_boxes,
                    "weight": items_total_weight,
                },
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile order_lookup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
