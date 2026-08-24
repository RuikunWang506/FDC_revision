from flask import (
    current_app,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    Blueprint,
    send_file,
)
from py.utilities import SQL_connector
from py.other import audit_cache
from py.other.audit_pdf import build_audit_pdf
import pandas as pd
from urllib.parse import quote
import os
import io
import re
from datetime import datetime
from werkzeug.utils import secure_filename

master_audit_bp = Blueprint("master_audit", __name__, template_folder="templates")

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {"xlsx", "xls"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@master_audit_bp.record_once
def _start_cache_worker(state):
    audit_cache.start_audit_cache_worker(state.app)


def _require_login():
    if "username" not in session:
        session["next"] = request.url
        return redirect(url_for("auth.login"))
    return None


def _filter_cached_orders(payload, database_filter="both", start_date="", end_date=""):
    orders = payload.get("orders") or []
    start_date = (start_date or "").strip()
    end_date = (end_date or "").strip()
    db_wanted = None
    if database_filter == "fdc":
        db_wanted = "FDC"
    elif database_filter == "efashion":
        db_wanted = "E_FAS"

    filtered = []
    for order in orders:
        if db_wanted and order.get("database") != db_wanted:
            continue
        order_date = str(order.get("order_date") or "")
        if start_date and order_date < start_date:
            continue
        if end_date and order_date > end_date:
            continue
        filtered.append(order)
    return filtered


def _orders_data_from_cached(cached_orders):
    rows = []
    for order in cached_orders:
        rows.append(
            (
                order.get("database"),
                order.get("order_number"),
                order.get("order_date_display") or order.get("order_date"),
                order.get("customer_raw") or order.get("customer"),
                order.get("order_amount") or 0,
                order.get("handling_fee") or 0,
                order.get("misc_charge") or 0,
                order.get("order_total") or 0,
                order.get("items") or 0,
                order.get("qty") or 0,
            )
        )
    return rows


def _build_sold_out_html(orders_data, current_user):
    if not orders_data:
        return "<p>No orders to analyze.</p>", pd.DataFrame()

    order_sku_qty_rows = SQL_connector.get_order_sku_quantities_for_orders(orders_data)
    if not order_sku_qty_rows:
        return "<p>No SKUs found in the selected orders.</p>", pd.DataFrame()

    sku_qty_df = pd.DataFrame.from_records(
        order_sku_qty_rows,
        columns=["Database", "Order Number", "SKU", "Qty", "Revenue"],
    )
    all_skus = sku_qty_df["SKU"].dropna().astype(str).str.strip().unique().tolist()
    if not all_skus:
        return "<p>No SKUs found in the selected orders.</p>", pd.DataFrame()

    all_inventory = SQL_connector.get_sku_inventory_from_both_databases(all_skus, current_user)
    if not all_inventory:
        return "<p>No inventory data found for SKUs in the selected orders.</p>", pd.DataFrame()

    inventory_df = pd.DataFrame.from_records(
        all_inventory,
        columns=["Database", "SKU", "Style", "Description", "Stock", "Ordered", "Available", "Incoming", "Price"],
    )
    order_summary = (
        sku_qty_df.groupby("SKU")
        .agg({"Qty": "sum", "Order Number": lambda x: sorted(set(str(v) for v in x))})
        .reset_index()
        .rename(columns={"Qty": "Qty in Order"})
    )
    merged_df = inventory_df.merge(order_summary, on="SKU", how="left")
    merged_df["Qty in Order"] = merged_df["Qty in Order"].fillna(0)
    merged_df["Order Number"] = merged_df["Order Number"].fillna("").apply(
        lambda x: x if isinstance(x, list) else []
    )
    for col in ["Stock", "Ordered", "Available", "Incoming", "Price", "Qty in Order"]:
        merged_df[col] = pd.to_numeric(merged_df[col], errors="coerce").fillna(0)

    pivot_df = (
        merged_df.groupby(["Style", "SKU"])
        .agg(
            {
                "Stock": "sum",
                "Ordered": "sum",
                "Available": "sum",
                "Incoming": "sum",
                "Qty in Order": "sum",
                "Order Number": lambda x: sorted(
                    set([item for sublist in x for item in sublist if sublist])
                ),
            }
        )
        .reset_index()
    )
    sold_out_df = pivot_df[pivot_df["Available"] <= 0].copy()
    if sold_out_df.empty:
        return "<p>No sold out SKUs found in the selected orders.</p>", pd.DataFrame()

    def create_order_links(order_list):
        if not order_list:
            return ""
        links = []
        for order_num in order_list:
            links.append(
                f'<a href="https://www.fdcef.com/order_lookup/{order_num}" target="_blank">{order_num}</a>'
            )
        return ", ".join(links)

    sold_out_df["Order Numbers"] = sold_out_df["Order Number"].apply(create_order_links)
    sold_out_df["Style"] = sold_out_df["Style"].apply(
        lambda x: f'<a href="/item_lookup/{quote(str(x).strip())}">{x}</a>' if pd.notna(x) else x
    )
    sold_out_df["SKU"] = sold_out_df["SKU"].apply(
        lambda x: f'<a href="/item_lookup/{quote(str(x).strip())}">{x}</a>' if pd.notna(x) else x
    )
    sold_out_df = sold_out_df.drop("Order Number", axis=1)
    sold_out_df.columns = [
        "Style",
        "SKU",
        "Total Stock",
        "Total Ordered",
        "Total Available",
        "Total Incoming",
        "Total Qty in Orders",
        "Order Numbers",
    ]
    for col in ["Total Stock", "Total Ordered", "Total Available", "Total Incoming", "Total Qty in Orders"]:
        sold_out_df[col] = sold_out_df[col].astype(int)

    sold_out_summary_row = pd.DataFrame(
        [
            {
                "Style": "<strong>TOTALS</strong>",
                "SKU": f"<strong>{len(sold_out_df)} items</strong>",
                "Total Stock": f"<strong>{sold_out_df['Total Stock'].sum():,}</strong>",
                "Total Ordered": f"<strong>{sold_out_df['Total Ordered'].sum():,}</strong>",
                "Total Available": f"<strong>{sold_out_df['Total Available'].sum():,}</strong>",
                "Total Incoming": f"<strong>{sold_out_df['Total Incoming'].sum():,}</strong>",
                "Total Qty in Orders": f"<strong>{sold_out_df['Total Qty in Orders'].sum():,}</strong>",
                "Order Numbers": "",
            }
        ]
    )
    sold_out_df_with_totals = pd.concat([sold_out_df, sold_out_summary_row], ignore_index=True)
    html = sold_out_df_with_totals.to_html(
        index=False,
        classes="table table-bordered table-striped table-sm",
        table_id="sold_out_table",
        escape=False,
    )
    return html, sold_out_df


def _match_excel_inventory(sold_out_df, uploaded_file):
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = secure_filename(uploaded_file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    uploaded_file.save(filepath)
    try:
        excel_df = pd.read_excel(filepath)
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass

    required_columns = ["Stock SKU", "On Hand", "Available", "Reserved"]
    missing_columns = [col for col in required_columns if col not in excel_df.columns]
    if missing_columns:
        return f"<p>Error: Missing required columns in Excel file: {', '.join(missing_columns)}</p>"

    excel_df["Stock SKU"] = excel_df["Stock SKU"].astype(str).str.strip().str.upper()
    for col in ["On Hand", "Available", "Reserved"]:
        excel_df[col] = pd.to_numeric(excel_df[col], errors="coerce").fillna(0)

    sold_out_skus = sold_out_df["SKU"].apply(
        lambda x: x.split(">")[-2].split("<")[0] if "<a href=" in str(x) else str(x)
    ).str.strip().str.upper()

    matched_skus = []
    excel_skus = set(excel_df["Stock SKU"].values)
    for i, sku in enumerate(sold_out_skus):
        if sku not in excel_skus:
            continue
        excel_row = excel_df[excel_df["Stock SKU"] == sku].iloc[0]
        sold_out_row = sold_out_df.iloc[i]
        matched_skus.append(
            {
                "Style": sold_out_row["Style"],
                "SKU": sold_out_row["SKU"],
                "DB Total Stock": sold_out_row["Total Stock"],
                "DB Total Ordered": sold_out_row["Total Ordered"],
                "DB Total Available": sold_out_row["Total Available"],
                "DB Total Incoming": sold_out_row["Total Incoming"],
                "Excel On Hand": int(excel_row["On Hand"]),
                "Excel Available": int(excel_row["Available"]),
                "Excel Reserved": int(excel_row["Reserved"]),
                "Combined Available": sold_out_row["Total Available"] + int(excel_row["Available"]),
                "Total Qty in Orders": sold_out_row["Total Qty in Orders"],
                "Order Numbers": sold_out_row["Order Numbers"],
            }
        )

    if not matched_skus:
        return "<p>No matching SKUs found between sold out items and uploaded Excel file.</p>"

    matched_df = pd.DataFrame(matched_skus)
    matched_summary_row = pd.DataFrame(
        [
            {
                "Style": "<strong>TOTALS</strong>",
                "SKU": f"<strong>{len(matched_df)} matched items</strong>",
                "DB Total Stock": f"<strong>{matched_df['DB Total Stock'].sum():,}</strong>",
                "DB Total Ordered": f"<strong>{matched_df['DB Total Ordered'].sum():,}</strong>",
                "DB Total Available": f"<strong>{matched_df['DB Total Available'].sum():,}</strong>",
                "DB Total Incoming": f"<strong>{matched_df['DB Total Incoming'].sum():,}</strong>",
                "Excel On Hand": f"<strong>{matched_df['Excel On Hand'].sum():,}</strong>",
                "Excel Available": f"<strong>{matched_df['Excel Available'].sum():,}</strong>",
                "Excel Reserved": f"<strong>{matched_df['Excel Reserved'].sum():,}</strong>",
                "Combined Available": f"<strong>{matched_df['Combined Available'].sum():,}</strong>",
                "Total Qty in Orders": f"<strong>{matched_df['Total Qty in Orders'].sum():,}</strong>",
                "Order Numbers": "",
            }
        ]
    )
    matched_df_with_totals = pd.concat([matched_df, matched_summary_row], ignore_index=True)
    return matched_df_with_totals.to_html(
        index=False,
        classes="table table-bordered table-striped table-sm",
        table_id="matched_inventory_table",
        escape=False,
    )


@master_audit_bp.route("/audit", methods=["GET"])
def audit():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    current_user = session["username"]
    status = audit_cache.read_status()
    if audit_cache.cache_is_stale() or not status.get("cache_exists"):
        audit_cache.trigger_refresh(current_app._get_current_object())
        status = audit_cache.read_status()
        status["refreshing"] = True

    return render_template(
        "audit.html",
        current_user=current_user,
        cache_status=status,
    )


@master_audit_bp.route("/audit/api/status", methods=["GET"])
def audit_status():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect
    return jsonify(audit_cache.read_status())


@master_audit_bp.route("/audit/api/data", methods=["GET"])
def audit_data():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect
    payload = audit_cache.read_dashboard()
    if not payload:
        status = audit_cache.read_status()
        if not status.get("refreshing"):
            audit_cache.trigger_refresh(current_app._get_current_object())
        return jsonify({"ok": False, "status": audit_cache.read_status()}), 202
    return jsonify({"ok": True, "status": audit_cache.read_status(), "data": payload})


@master_audit_bp.route("/audit/api/refresh", methods=["POST"])
def audit_refresh():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect
    status = audit_cache.trigger_refresh(current_app._get_current_object(), force=True)
    return jsonify({"ok": True, "status": status})


@master_audit_bp.route("/audit/api/sold-out", methods=["POST"])
def audit_sold_out():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    payload = audit_cache.read_dashboard()
    if not payload:
        return jsonify({"ok": False, "html": "<p>Dashboard cache is not ready yet.</p>"}), 409

    body = request.get_json(silent=True) or {}
    database_filter = body.get("database_filter") or "both"
    start_date = body.get("start_date") or ""
    end_date = body.get("end_date") or ""
    cached_orders = _filter_cached_orders(payload, database_filter, start_date, end_date)
    truncated = len(cached_orders) > 400
    if truncated:
        cached_orders = cached_orders[:400]

    try:
        html, sold_out_df = _build_sold_out_html(
            _orders_data_from_cached(cached_orders), session["username"]
        )
        return jsonify(
            {
                "ok": True,
                "html": html,
                "order_count": len(cached_orders),
                "truncated": truncated,
                "has_sold_out": not sold_out_df.empty,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "html": f"<p>Error loading sold out items: {exc}</p>"}), 500


@master_audit_bp.route("/audit/api/match-inventory", methods=["POST"])
def audit_match_inventory():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect

    uploaded_file = request.files.get("inventory_file")
    if not uploaded_file or not allowed_file(uploaded_file.filename):
        return jsonify({"ok": False, "html": "<p>Please upload a valid .xlsx or .xls file.</p>"}), 400

    payload = audit_cache.read_dashboard()
    if not payload:
        return jsonify({"ok": False, "html": "<p>Dashboard cache is not ready yet.</p>"}), 409

    database_filter = request.form.get("database_filter") or "both"
    start_date = request.form.get("start_date") or ""
    end_date = request.form.get("end_date") or ""
    cached_orders = _filter_cached_orders(payload, database_filter, start_date, end_date)
    if len(cached_orders) > 400:
        cached_orders = cached_orders[:400]

    try:
        _html, sold_out_df = _build_sold_out_html(
            _orders_data_from_cached(cached_orders), session["username"]
        )
        if sold_out_df.empty:
            return jsonify({"ok": False, "html": "<p>No sold out items to match against.</p>"})
        matched_html = _match_excel_inventory(sold_out_df, uploaded_file)
        return jsonify({"ok": True, "html": matched_html})
    except Exception as exc:
        return jsonify({"ok": False, "html": f"<p>Error processing Excel file: {exc}</p>"}), 500


@master_audit_bp.route("/audit/api/pdf", methods=["POST"])
def audit_pdf():
    login_redirect = _require_login()
    if login_redirect:
        return login_redirect
    snapshot = request.get_json(silent=True) or {}
    if not isinstance(snapshot, dict) or not snapshot:
        return jsonify({"ok": False, "error": "Report data is missing."}), 400
    try:
        pdf_bytes = build_audit_pdf(snapshot, prepared_by=session.get("username") or "")
    except Exception as exc:
        current_app.logger.exception("audit pdf failed")
        return jsonify({"ok": False, "error": f"Could not build PDF: {exc}"}), 500

    period = str((snapshot.get("meta") or {}).get("period_label") or "period")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", period).strip("-")[:40] or "period"
    filename = f"FDC_Audit_{slug}_{datetime.now().strftime('%Y%m%d')}.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )
