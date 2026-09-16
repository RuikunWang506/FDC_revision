"""JSON API for the native FDC iOS app (not a website wrapper)."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
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

NATIVE_TOOL_IDS = frozenset(
    {
        "item_lookup",
        "bag_style_viewer",
        "v7_listcreator",
        "master_image_viewer",
        "image_upload",
        "order_lookup",
        "customer",
        "invoice_lookup",
        "loris_fedex",
        "fdcef_container_search",
        "print_center",
        "mobile_scanner",
    }
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_OCEAN_KEYS = {"\U0001f6a2", "ocean", "Unnamed: 7"}


def strip_html(value: Any) -> str:
    text = "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)
    return _HTML_TAG_RE.sub("", text).replace("\xa0", " ").strip()


def _origin() -> str:
    return (getattr(Config, "PUBLIC_APP_ORIGIN", None) or "https://www.fdcef.com").rstrip("/")


def _is_placeholder_image(path: Any) -> bool:
    text = str(path or "").strip().lower()
    return (not text) or ("no_image" in text)


def _abs_url(path: Any) -> str:
    text = str(path or "").strip()
    if _is_placeholder_image(text):
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if not text.startswith("/"):
        text = "/" + text
    return _origin() + text


def _dam_thumb(sku_or_style: str, width: int = 300) -> str:
    key = (sku_or_style or "").strip().upper()
    if not key:
        return ""
    from py.utilities.dam_utils import get_canonical_asset_url, search_assets_by_sku_pattern

    style = key.split()[0]
    for term in (key, style):
        url = get_canonical_asset_url(term, width=width, source_filter="catalog_original")
        if url:
            return url
    assets = search_assets_by_sku_pattern(style, source_filter=["catalog_original", "dropbox", "catalog"])
    if not assets:
        return ""
    assets = sorted(
        assets,
        key=lambda asset: 0 if getattr(asset, "source", "") == "catalog_original" else 1,
    )
    return f"/image/{assets[0].id}/{width}"


def _json_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
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
    if isinstance(value, bool):
        return value
    text = strip_html(value)
    return text


def _jsonify_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonify_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify_obj(v) for v in value]
    return _json_cell(value)


def _clean_inventory_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        name = str(key)
        if name in _OCEAN_KEYS or "\U0001f6a2" in name:
            name = "ocean"
        out[name] = _json_cell(value)
    return out


def _sum_column(rows: List[Dict[str, Any]], *names: str) -> float:
    total = 0.0
    for row in rows:
        for name in names:
            raw = row.get(name)
            if raw in (None, "", "N/A"):
                continue
            try:
                total += float(raw)
                break
            except (TypeError, ValueError):
                continue
    return total


def _flask_json(view_fn, *args, **kwargs):
    """Call an existing Flask view and normalize {ok, ...} JSON."""
    result = view_fn(*args, **kwargs)
    if isinstance(result, tuple):
        response = result[0]
        status = result[1] if len(result) > 1 else getattr(response, "status_code", 200)
    else:
        response = result
        status = getattr(response, "status_code", 200)
    data = response.get_json(silent=True) if hasattr(response, "get_json") else None
    if not isinstance(data, dict):
        return result
    if "ok" not in data:
        if "success" in data:
            data["ok"] = bool(data.get("success"))
        elif "error" in data and data.get("error"):
            data["ok"] = False
        else:
            data["ok"] = int(status) < 400
    return jsonify(data), status


def _records_from_html_table(html: str, extra_headers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not html or "<table" not in html.lower():
        return []
    wrapped = html
    if extra_headers:
        head = "".join(f"<th>{h}</th>" for h in extra_headers)
        wrapped = f"<table><thead><tr>{head}</tr></thead><tbody>{html}</tbody></table>"
    try:
        df = pd.read_html(StringIO(wrapped))[0]
    except Exception:
        return []
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
        excel_html, history_html, purchase_html, adjustment_html, _, user_input = generate_excel(
            user_input
        )
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
            rows = [_clean_inventory_row(rec) for rec in df.to_dict(orient="records")]

        images = _item_images(user_input, df)
        styles = _item_style_groups(query, rows, images)
        log_user_action(
            f"***{session.get('username')}***__@@@search SKU@@@__ {user_input} [mobile]",
            session.get("username"),
        )
        return jsonify(
            {
                "ok": True,
                "query": user_input,
                "styles": styles,
                "rows": rows,
                "history": _records_from_html_table(history_html or ""),
                "purchases": _records_from_html_table(purchase_html or ""),
                "adjustments": _records_from_html_table(adjustment_html or ""),
                "images": images,
                "totals": {
                    "stock": _sum_column(rows, "Stock"),
                    "sold": _sum_column(rows, "Sold"),
                    "avail": _sum_column(rows, "Avail"),
                    "ocean": _sum_column(rows, "ocean"),
                },
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile item_lookup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


def _item_style_groups(query: str, rows: List[Dict[str, Any]], images: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    try:
        from py.other.mobile_lite import format_measurements, get_color_from_abbr, get_product_data, is_light_color

        grouped = get_product_data(query) or {}
        styles = []
        for style, style_data in grouped.items():
            products = style_data.get("products") or []
            meas = format_measurements(products) if products else {}
            colors = []
            seen = set()
            for product in products:
                sku = str(product.get("sku") or "").strip().upper()
                color = sku.split()[-1] if " " in sku else ""
                if color and color not in seen:
                    seen.add(color)
                    colors.append(
                        {
                            "code": color,
                            "hex": get_color_from_abbr(color),
                            "light": is_light_color(color),
                        }
                    )
                if product.get("image_url"):
                    product["image_url"] = _abs_url(product.get("image_url"))
            raw_image = style_data.get("image_url")
            if _is_placeholder_image(raw_image):
                raw_image = images[0]["url"] if images else ""
            if _is_placeholder_image(raw_image):
                raw_image = _dam_thumb(style)
            styles.append(
                {
                    "style": style,
                    "image_url": _abs_url(raw_image),
                    "colors": colors,
                    "products": _jsonify_obj(products),
                    "measurements": {
                        "pc_weights": meas.get("pc_weights") or "",
                        "dimensions": str(meas.get("dimensions") or "").replace("<br>", "\n"),
                        "pkg_weights": meas.get("pkg_weights") or "",
                        "pkg_dimensions": str(meas.get("pkg_dimensions") or "").replace("<br>", "\n"),
                    },
                }
            )
        if styles:
            return styles
    except Exception as exc:
        current_app.logger.warning("mobile lite grouping failed, using excel rows: %s", exc)
    return _styles_from_excel_rows(rows, images)


def _excel_num(row: Dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _styles_from_excel_rows(rows: List[Dict[str, Any]], images: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        sku = str(row.get("SKU") or "").strip().upper()
        style = str(row.get("Style") or "").strip().upper().split()[0] if row.get("Style") else (sku.split()[0] if sku else "")
        if not style:
            style = sku or "UNKNOWN"
        bucket = grouped.setdefault(
            style,
            {"style": style, "image_url": "", "products": [], "colors": [], "_seen_colors": set()},
        )
        db = str(row.get("DB") or "").strip()
        if db.lower() in ("e_fas", "oms_ef"):
            db = "oms_EF"
        elif "fdc" in db.lower():
            db = "FDC"
        color = str(row.get("color") or "").strip().upper()
        if not color and " " in sku:
            color = sku.split()[-1]
        if color and color not in bucket["_seen_colors"]:
            bucket["_seen_colors"].add(color)
            bucket["colors"].append({"code": color, "hex": "#888888", "light": True})
        bucket["products"].append(
            {
                "db": db,
                "sku": sku,
                "style": style,
                "info": {
                    "cost": _excel_num(row, "cost"),
                    "price": _excel_num(row, "MSRP") / 2 if _excel_num(row, "MSRP") else 0,
                    "description": row.get("Descrip") or "",
                },
                "inventory": {
                    "stock": int(_excel_num(row, "Stock")),
                    "sold": int(_excel_num(row, "Sold")),
                    "available": int(_excel_num(row, "Avail")),
                    "incoming": int(_excel_num(row, "ocean")),
                    "case_qty": int(_excel_num(row, "CaseQty")),
                    "update_date": row.get("update dt") or "",
                },
                "package": {
                    "pc_weight": _excel_num(row, "pc_wt"),
                    "length": _excel_num(row, "len"),
                    "width": _excel_num(row, "wid"),
                    "height": _excel_num(row, "hei"),
                    "box_weight": _excel_num(row, "box_wt"),
                },
                "location": {
                    "current_location": row.get("Location") or "",
                    "update_date": row.get("update dt") or "",
                },
            }
        )
    out = []
    first_image = images[0]["url"] if images else ""
    for style, bucket in grouped.items():
        bucket.pop("_seen_colors", None)
        thumb = first_image if not _is_placeholder_image(first_image) else _dam_thumb(style)
        bucket["image_url"] = _abs_url(thumb)
        products = bucket["products"]
        weights = sorted({f"{p['package']['pc_weight']:.1f} lbs" for p in products if p["package"]["pc_weight"]})
        dims = sorted(
            {
                f"{p['package']['length']}\" × {p['package']['width']}\" × {p['package']['height']}\""
                for p in products
                if p["package"]["length"] or p["package"]["width"] or p["package"]["height"]
            }
        )
        box_w = sorted({f"{p['package']['box_weight']:.1f} lbs" for p in products if p["package"]["box_weight"]})
        bucket["measurements"] = {
            "pc_weights": ", ".join(weights),
            "dimensions": "\n".join(dims),
            "pkg_weights": ", ".join(box_w),
            "pkg_dimensions": "",
        }
        out.append(bucket)
    return out


def _item_images(user_input: str, df) -> List[Dict[str, str]]:
    try:
        from py.search.item_lookup import _looks_like_hash_sku, _resolve_image_search_terms
        from py.utilities.dam_utils import get_asset_url, search_assets_by_sku_pattern
        import py.utilities.models
        from py.utilities.models import ImageAsset

        search_terms, image_match_mode = _resolve_image_search_terms(user_input, df)
        origin = _origin()
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


@mobile_app_bp.route("/invoice_lookup", methods=["GET"])
@_json_login_required
@_require_tool("invoice_lookup")
def mobile_invoice_lookup():
    raw = (request.args.get("q") or request.args.get("invoice_number") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "Enter an invoice number"}), 400
    try:
        from py.order.invoice_lookup import generate_invoice
        from py.utilities.logging_setup import log_user_action

        preferred = (request.args.get("db") or "").strip() or None
        header_info, items_html = generate_invoice(raw, preferred_db=preferred)
        if not header_info:
            return jsonify({"ok": False, "error": f"No invoice {raw}"}), 404
        log_user_action(
            f"***{session.get('username')}***__@@@search invoice@@@__ {raw} [mobile]",
            session.get("username"),
        )
        return jsonify(
            {
                "ok": True,
                "invoice_number": header_info.get("invoice_number") or raw,
                "header": {str(k): _json_cell(v) for k, v in header_info.items()},
                "items": _records_from_html_table(items_html or ""),
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile invoice_lookup failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@mobile_app_bp.route("/customer", methods=["GET"])
@_json_login_required
@_require_tool("customer")
def mobile_customer_search():
    query = (request.args.get("q") or "").strip()
    try:
        from py.other.customer import fetch_customer_matches

        results, null_results, errors = fetch_customer_matches(query or None)
        return jsonify(
            {
                "ok": True,
                "query": query,
                "customers": _jsonify_obj(results),
                "without_orders": _jsonify_obj(null_results),
                "errors": errors,
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile customer search failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@mobile_app_bp.route("/customer/<customer_id>", methods=["GET"])
@_json_login_required
@_require_tool("customer")
def mobile_customer_details(customer_id):
    from py.other.customer import customer_details

    return _flask_json(customer_details, customer_id)


@mobile_app_bp.route("/bag_style/meta", methods=["GET"])
@_json_login_required
@_require_tool("bag_style_viewer")
def mobile_bag_style_meta():
    from py.other.bag_style_viewer import fetch_all_descriptions

    return jsonify({"ok": True, "descriptions": fetch_all_descriptions()})


@mobile_app_bp.route("/bag_style", methods=["GET"])
@_json_login_required
@_require_tool("bag_style_viewer")
def mobile_bag_style():
    description = (request.args.get("description") or request.args.get("q") or "").strip()
    stock_filter = (request.args.get("stock_filter") or "").strip() or None
    color_filter = (request.args.get("color_filter") or "").strip() or None
    hide_zero = (request.args.get("hide_zero") or "").strip().lower() in ("1", "true", "yes")
    if not description:
        return jsonify({"ok": False, "error": "Choose a description"}), 400
    try:
        from py.other.bag_style_viewer import resolve_bag_style_viewer_results

        results, err = resolve_bag_style_viewer_results(
            [description],
            [],
            stock_filter=stock_filter,
            color_filter=color_filter,
            hide_zero_skus=hide_zero,
        )
        if err:
            return jsonify({"ok": False, "error": err}), 400
        trimmed = []
        for item in results[:80]:
            row = _jsonify_obj(item)
            if isinstance(row, dict) and row.get("image"):
                row["image"] = _abs_url(row["image"])
            trimmed.append(row)
        return jsonify(
            {
                "ok": True,
                "description": description,
                "count": len(results),
                "styles": trimmed,
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile bag_style failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@mobile_app_bp.route("/bag_style/stock/<style>", methods=["GET"])
@_json_login_required
@_require_tool("bag_style_viewer")
def mobile_bag_style_stock(style):
    from py.other.bag_style_viewer import get_detailed_stock_for_style

    detailed = get_detailed_stock_for_style((style or "").upper())
    total_quantity = sum(item.get("quantity") or 0 for item in detailed)
    total_ocean = sum(item.get("ocean") or 0 for item in detailed)
    return jsonify(
        {
            "ok": True,
            "style": (style or "").upper(),
            "total_quantity": total_quantity,
            "total_ocean": total_ocean,
            "total": total_quantity + total_ocean,
            "details": _jsonify_obj(detailed),
        }
    )


@mobile_app_bp.route("/list_creator", methods=["GET"])
@_json_login_required
@_require_tool("v7_listcreator")
def mobile_list_creator():
    from py.other.bag_style_viewer import fetch_all_descriptions
    from py.Toolkits.V7listcreator import listcreator_html_jobs_api

    jobs_resp, status = _flask_json(listcreator_html_jobs_api)
    data = jobs_resp.get_json(silent=True) if hasattr(jobs_resp, "get_json") else {}
    if not isinstance(data, dict):
        data = {"ok": False, "error": "Could not load jobs"}
        status = 500
    data["descriptions"] = fetch_all_descriptions()
    data["ok"] = bool(data.get("ok", True))
    return jsonify(data), status


@mobile_app_bp.route("/list_creator/filter", methods=["POST"])
@_json_login_required
@_require_tool("v7_listcreator")
def mobile_list_creator_filter():
    from py.Toolkits.V7listcreator import quick_filter_styles

    return _flask_json(quick_filter_styles)


@mobile_app_bp.route("/images", methods=["GET"])
@_json_login_required
@_require_tool("master_image_viewer")
def mobile_master_images():
    sku = (request.args.get("q") or request.args.get("sku") or "").strip()
    if not sku:
        return jsonify({"ok": False, "error": "Enter a SKU or style"}), 400
    try:
        from py.other.master_image_viewer import find_images_by_sku
        from py.utilities.customer_purchase_access import (
            FEATURE_MASTER_IMAGE_VIEWER,
            json_forbidden_if_customer_purchase_denied,
        )

        denied = json_forbidden_if_customer_purchase_denied(
            session.get("username"), sku, FEATURE_MASTER_IMAGE_VIEWER
        )
        if denied:
            return denied
        results = find_images_by_sku(sku)
        if isinstance(results, dict) and results.get("error"):
            return jsonify({"ok": False, "error": results["error"]}), 400
        groups = []
        grouped = results if isinstance(results, dict) else {}
        for key, label in (
            ("dam_catalog", "Catalog"),
            ("dam_dropbox", "Dropbox"),
            ("dam_big_picture", "Big picture"),
            ("dam_other", "Other"),
            ("china_ftp", "China FTP"),
        ):
            images = []
            for asset in grouped.get(key) or []:
                if not isinstance(asset, dict):
                    continue
                images.append(
                    {
                        "id": asset.get("id") or asset.get("filename") or "",
                        "sku": asset.get("sku") or sku,
                        "filename": asset.get("filename") or "",
                        "source": asset.get("source") or key,
                        "url": _abs_url(asset.get("url")),
                        "full_url": _abs_url(asset.get("full_url") or asset.get("url")),
                    }
                )
            if images:
                groups.append({"id": key, "label": label, "images": images})
        return jsonify(
            {
                "ok": True,
                "sku": grouped.get("sku") or sku.upper(),
                "total": grouped.get("total_count") or sum(len(g["images"]) for g in groups),
                "groups": groups,
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile master images failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@mobile_app_bp.route("/upload_catalog", methods=["POST"])
@_json_login_required
@_require_tool("image_upload")
def mobile_upload_catalog():
    from py.utilities.image_upload_routes import upload_bulk_catalog

    return _flask_json(upload_bulk_catalog)


@mobile_app_bp.route("/upload_catalog/process", methods=["POST"])
@_json_login_required
@_require_tool("image_upload")
def mobile_upload_catalog_process():
    from py.utilities.image_upload_routes import process_bulk_catalog

    return _flask_json(process_bulk_catalog)


@mobile_app_bp.route("/loris/orders", methods=["GET"])
@_json_login_required
@_require_tool("loris_fedex")
def mobile_loris_orders():
    from py.loris.lori_automation import get_orders

    return _flask_json(get_orders)


@mobile_app_bp.route("/loris/orders/<po_number>", methods=["GET"])
@_json_login_required
@_require_tool("loris_fedex")
def mobile_loris_order_items(po_number):
    from py.loris.lori_automation import get_order_items

    return _flask_json(get_order_items, po_number)


@mobile_app_bp.route("/container", methods=["GET"])
@_json_login_required
@_require_tool("fdcef_container_search")
def mobile_container():
    container_id = (request.args.get("q") or request.args.get("container_id") or "").strip().upper()
    try:
        from product_detail_module.product_detail import (
            fetch_combined_inventory,
            get_db_connections,
            group_container_by_style,
        )

        ocean_containers = []
        arrived_containers = []
        latest_containers = []
        try:
            from py.Toolkits.boat import sidebar_container_lists

            ocean_containers, arrived_containers = sidebar_container_lists()
        except Exception as list_err:
            current_app.logger.warning("mobile container lists: %s", list_err)

        conn_fdc, conn_oms_ef = get_db_connections()
        container_data = []
        style_groups = []
        grand_totals = None
        po_date = None
        is_arrived = False
        boat_row = {}
        try:
            if conn_fdc:
                cursor = conn_fdc.cursor()
                cursor.execute(
                    """
                    SELECT DISTINCT TOP 5 PUR_NUM, LOG_DATE
                    FROM plog
                    WHERE PUR_NUM IS NOT NULL AND PUR_NUM != ''
                    AND PUR_NUM NOT LIKE 'L%'
                    ORDER BY LOG_DATE DESC
                    """
                )
                latest_containers = [row[0] for row in cursor.fetchall()]
                cursor.close()
            if not arrived_containers:
                arrived_containers = [
                    {"id": str(c or "").strip(), "eta": ""}
                    for c in latest_containers
                    if str(c or "").strip()
                ]
            if container_id and conn_fdc:
                cursor = conn_fdc.cursor()
                cursor.execute(
                    """
                    SELECT
                        pl.prod_cd as sku,
                        LEFT(pl.prod_cd, CHARINDEX(' ', pl.prod_cd + ' ') - 1) as style,
                        pl.pur_num as po_num,
                        RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR) AS po_date,
                        CASE WHEN ISNULL(pl.hdl_chg, 0) > 0 THEN pl.hdl_chg ELSE pl.log_qty END as container_qty,
                        ISNULL(i.RETAIL_PRS, 0) as price,
                        ISNULL(pl.hdl_chg, 0) as hdl_chg
                    FROM plog pl
                    LEFT JOIN dbo.inv i ON pl.prod_cd = i.PROD_CD
                    WHERE RTRIM(pl.pur_num) = ?
                    AND (ISNULL(pl.hdl_chg, 0) > 0 OR ISNULL(pl.log_qty, 0) > 0)
                    ORDER BY style, pl.prod_cd
                    """,
                    [container_id],
                )
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
                cursor.close()
                seen = set()
                for row in rows:
                    row_dict = dict(zip(columns, row))
                    sku = (row_dict.get("sku") or "").strip()
                    if not sku or sku in seen:
                        continue
                    seen.add(sku)
                    container_data.append(row_dict)
                    if float(row_dict.get("hdl_chg") or 0) > 0:
                        is_arrived = True
                    if not po_date:
                        po_date = row_dict.get("po_date")
                inventory = fetch_combined_inventory(list(seen), conn_fdc, conn_oms_ef)
                for item in container_data:
                    inv = inventory.get((item.get("sku") or "").strip(), {})
                    item["catalog"] = inv.get("catalog") or "N/A"
                    item["color"] = inv.get("color") or "-"
                    item["available"] = inv.get("stock", 0)
                    item["in_warehouse"] = inv.get("available", 0)
                    item["sold_reserved"] = inv.get("sold", 0)
                style_groups, grand_totals = group_container_by_style(container_data)
                for group in style_groups:
                    if group.get("image_url"):
                        group["image_url"] = _abs_url(group["image_url"])
            if container_id:
                try:
                    from py.Toolkits.boat import lookup_boat_hub_row

                    boat_row = lookup_boat_hub_row(container_id) or {}
                except Exception as boat_err:
                    current_app.logger.warning("mobile boat row: %s", boat_err)
        finally:
            if conn_fdc:
                conn_fdc.close()
            if conn_oms_ef:
                conn_oms_ef.close()

        return jsonify(
            {
                "ok": True,
                "container_id": container_id,
                "po_date": po_date,
                "is_arrived": is_arrived,
                "items": _jsonify_obj(container_data[:300]),
                "style_groups": _jsonify_obj(style_groups),
                "totals": _jsonify_obj(grand_totals) if grand_totals else None,
                "boat": _jsonify_obj(boat_row),
                "ocean": _jsonify_obj(ocean_containers)[:40],
                "arrived": _jsonify_obj(arrived_containers)[:40],
            }
        )
    except Exception as exc:
        current_app.logger.exception("mobile container failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@mobile_app_bp.route("/print/status", methods=["GET"])
@_json_login_required
@_require_tool("print_center")
def mobile_print_status():
    from py.other.print_center import print_center_status

    return _flask_json(print_center_status)


@mobile_app_bp.route("/print", methods=["POST"])
@_json_login_required
@_require_tool("print_center")
def mobile_print():
    from py.other.print_center import print_center_print

    return _flask_json(print_center_print)


@mobile_app_bp.route("/scan", methods=["POST"])
@_json_login_required
@_require_tool("mobile_scanner")
def mobile_scan():
    body = request.get_json(silent=True) or {}
    sku = str(body.get("sku") or "").strip()
    upc = str(body.get("upc") or "").strip()
    if sku:
        from py.other.mobile_scanner import mobile_scanner_resolve_sku

        return _flask_json(mobile_scanner_resolve_sku)
    if upc:
        from py.other.mobile_scanner import mobile_scanner_resolve_upc

        return _flask_json(mobile_scanner_resolve_upc)
    return jsonify({"ok": False, "error": "Scan a barcode or enter a SKU"}), 400

