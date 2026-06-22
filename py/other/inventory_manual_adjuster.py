"""Inventory Manual Adjuster — transfer between DBs or add/remove on one DB."""
from __future__ import annotations

import re
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from product_detail_module.product_detail import search_product_data_with_history
from py.utilities.inventory_adjustment import (
    DEFAULT_ADJUSTED_BY,
    execute_inventory_adjustments,
    execute_inventory_transfer,
)
from py.utilities.setting import login_required

inventory_manual_adjuster_bp = Blueprint("inventory_manual_adjuster", __name__)


def parse_search_terms(search_input: str) -> list[str]:
    """Split comma/newline/semicolon separated styles or SKUs."""
    terms = []
    seen = set()
    for part in re.split(r"[,;\n]+", (search_input or "").lower()):
        term = part.strip()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def search_product_data_multi(search_input: str) -> tuple[list[dict], list[str], list[str]]:
    """
    Search multiple styles/SKUs in one request.
    Returns (style_groups, matched_terms, not_found_terms).
    """
    terms = parse_search_terms(search_input)
    if not terms:
        return [], [], []

    groups_by_style: dict[str, dict] = {}
    matched: list[str] = []
    not_found: list[str] = []

    for term in terms:
        product_data = search_product_data_with_history(term)
        if not product_data:
            not_found.append(term)
            continue
        matched.append(term)
        for group in product_data:
            style_key = (group.get("style") or "").strip()
            if style_key and style_key not in groups_by_style:
                groups_by_style[style_key] = group

    return list(groups_by_style.values()), matched, not_found


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _db_metrics(db_data, exists: bool) -> dict:
    if not exists:
        return {"stock": None, "available": None, "sold": None}
    db_data = db_data or {}
    return {
        "stock": _num(db_data.get("stock")),
        "available": _num(db_data.get("available")),
        "sold": _num(db_data.get("sold")),
    }


def serialize_style_group(style_group: dict) -> dict:
    parent = style_group.get("parent_info") or {}
    colors = []
    any_fdc = False
    any_efas = False
    total_fdc_avail = 0
    total_efas_avail = 0

    for color in style_group.get("colors") or []:
        fdc_raw = color.get("fdc")
        efas_raw = color.get("efas")
        fdc_exists = fdc_raw is not None
        efas_exists = efas_raw is not None
        fdc = _db_metrics(fdc_raw, fdc_exists)
        efas = _db_metrics(efas_raw, efas_exists)

        if fdc_exists:
            any_fdc = True
            total_fdc_avail += fdc["available"] or 0
        if efas_exists:
            any_efas = True
            total_efas_avail += efas["available"] or 0

        colors.append(
            {
                "sku": (color.get("sku") or "").strip(),
                "color": (color.get("color") or "").strip(),
                "fdc_exists": fdc_exists,
                "fdc_stock": fdc["stock"],
                "fdc_available": fdc["available"],
                "fdc_sold": fdc["sold"],
                "efas_exists": efas_exists,
                "efas_stock": efas["stock"],
                "efas_available": efas["available"],
                "efas_sold": efas["sold"],
            }
        )

    return {
        "style": (style_group.get("style") or "").strip(),
        "parent_info": {
            "style": (parent.get("style") or "").strip(),
            "container": (parent.get("container") or "").strip(),
            "case_qty": parent.get("case_qty"),
            "box_wt": _float(parent.get("box_wt")),
            "pc_wt": _float(parent.get("pc_wt")),
            "msrp": _float(parent.get("msrp")),
            "cost": _float(parent.get("cost")),
            "last_activity": parent.get("last_activity") or "N/A",
            "create_dt": parent.get("create_dt") or "N/A",
        },
        "description": style_group.get("description") or "N/A",
        "colors": colors,
        "totals": {
            "fdc_available": total_fdc_avail if any_fdc else None,
            "efas_available": total_efas_avail if any_efas else None,
            "combined_available": (
                (total_fdc_avail if any_fdc else 0) + (total_efas_avail if any_efas else 0)
                if any_fdc or any_efas
                else None
            ),
        },
    }


def _search_response(search_input: str):
    current_user = session.get("username", "User")
    current_app.logger.info(
        "***%s*** inventory_manual_adjuster search: %s",
        current_user,
        search_input.lower(),
    )

    product_groups, matched, not_found = search_product_data_multi(search_input)
    if not product_groups:
        msg = "No products found"
        if not_found:
            msg = f"No products found for: {', '.join(not_found)}"
        return jsonify({"success": False, "error": msg, "not_found": not_found}), 404

    groups = [serialize_style_group(group) for group in product_groups]
    return jsonify(
        {
            "success": True,
            "groups": groups,
            "search": search_input,
            "matched": matched,
            "not_found": not_found,
        }
    )


@inventory_manual_adjuster_bp.route("/inventory_manual_adjuster", methods=["GET"])
@login_required
def inventory_manual_adjuster_page():
    return render_template(
        "inventory_manual_adjuster.html",
        current_user=session.get("username", "User"),
    )


@inventory_manual_adjuster_bp.route("/switch_number", methods=["GET"])
@login_required
def switch_number_redirect():
    return redirect(url_for("inventory_manual_adjuster.inventory_manual_adjuster_page"))


@inventory_manual_adjuster_bp.route("/api/inventory_manual_adjuster/search", methods=["POST"])
@login_required
def inventory_manual_adjuster_search():
    try:
        payload = request.get_json(silent=True) or {}
        search_input = str(payload.get("search_input", "")).strip()
        if not search_input:
            return jsonify({"success": False, "error": "Enter a style or SKU"}), 400
        return _search_response(search_input)
    except Exception as exc:
        current_app.logger.error("inventory_manual_adjuster search error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@inventory_manual_adjuster_bp.route("/api/switch_number/search", methods=["POST"])
@login_required
def switch_number_search_redirect():
    return inventory_manual_adjuster_search()


@inventory_manual_adjuster_bp.route("/api/inventory_manual_adjuster/transfer", methods=["POST"])
@login_required
def inventory_manual_adjuster_transfer():
    try:
        payload = request.get_json(silent=True) or {}
        direction = str(payload.get("direction", "")).strip().lower()
        transfers = payload.get("transfers") or []
        adjusted_by = str(payload.get("adjusted_by", "")).strip() or DEFAULT_ADJUSTED_BY

        if direction not in ("fdc_to_efas", "efas_to_fdc"):
            return jsonify({"success": False, "error": "Invalid transfer direction"}), 400
        if not isinstance(transfers, list):
            return jsonify({"success": False, "error": "Invalid transfers payload"}), 400

        current_user = session.get("username", "User")
        result = execute_inventory_transfer(transfers, direction, adjusted_by=adjusted_by)

        current_app.logger.info(
            "***%s*** inventory_manual_adjuster transfer %s: %s line(s), log %s -> %s",
            current_user,
            direction,
            len(result["lines"]),
            result["log_num_from"],
            result["log_num_to"],
        )

        return jsonify({"success": True, "result": result})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        current_app.logger.error("inventory_manual_adjuster transfer error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


@inventory_manual_adjuster_bp.route("/api/switch_number/transfer", methods=["POST"])
@login_required
def switch_number_transfer_redirect():
    return inventory_manual_adjuster_transfer()


@inventory_manual_adjuster_bp.route("/api/inventory_manual_adjuster/adjust", methods=["POST"])
@login_required
def inventory_manual_adjuster_adjust():
    try:
        payload = request.get_json(silent=True) or {}
        database = str(payload.get("database", "")).strip().upper()
        adjustments = payload.get("adjustments") or []
        adjusted_by = str(payload.get("adjusted_by", "")).strip() or DEFAULT_ADJUSTED_BY

        if database in ("EFAS", "E-FAS"):
            database = "E_FAS"
        if database not in ("FDC", "E_FAS"):
            return jsonify({"success": False, "error": "database must be FDC or E_FAS"}), 400
        if not isinstance(adjustments, list):
            return jsonify({"success": False, "error": "Invalid adjustments payload"}), 400

        current_user = session.get("username", "User")
        result = execute_inventory_adjustments(
            adjustments,
            database,
            adjusted_by=adjusted_by,
        )

        current_app.logger.info(
            "***%s*** inventory_manual_adjuster adjust %s: %s line(s), log %s",
            current_user,
            database,
            len(result["lines"]),
            result["log_num"],
        )

        return jsonify({"success": True, "result": result})
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        current_app.logger.error("inventory_manual_adjuster adjust error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
