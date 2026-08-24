"""Background cache for /audit so page views never hit SQL Server."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from py.utilities import SQL_connector

CACHE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "output", "audit_cache")
)
DATA_PATH = os.path.join(CACHE_DIR, "dashboard.json")
HISTORY_PATH = os.path.join(CACHE_DIR, "history.json")
STATUS_PATH = os.path.join(CACHE_DIR, "status.json")

MONTHS_BACK = 24
HISTORY_START = "2024-01-01"
HISTORY_END = "2025-12-31"
RECENT_START = "2026-01-01"
REFRESH_INTERVAL_SEC = 7 * 24 * 60 * 60
PREFIX_COLUMNS = ["S(ES)", "N(EN)", "BN", "BT", "GN", "M(EM)", "L", "GT", "T(ET)", "TJ", "Other"]
_COLOR_TOKEN = re.compile(r"^[A-Z]{1,6}(?:[-/][A-Z]{1,6})?\d{0,2}$")

_refresh_lock = threading.Lock()
_refreshing = False
_worker_started = False


def _ensure_dir() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_write_json(path: str, payload: Any) -> None:
    _ensure_dir()
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix="audit_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_status() -> dict[str, Any]:
    status = _read_json(STATUS_PATH) or {}
    status.setdefault("state", "empty")
    status["refreshing"] = bool(_refreshing)
    status["cache_exists"] = os.path.isfile(DATA_PATH)
    if os.path.isfile(DATA_PATH):
        try:
            status["cache_bytes"] = os.path.getsize(DATA_PATH)
            status["cache_mtime"] = datetime.fromtimestamp(os.path.getmtime(DATA_PATH)).isoformat(
                timespec="seconds"
            )
        except OSError:
            pass
    return status


def read_dashboard() -> dict[str, Any] | None:
    return _read_json(DATA_PATH)


def write_status(**kwargs: Any) -> None:
    current = _read_json(STATUS_PATH) or {}
    current.update(kwargs)
    current["updated_at"] = _now_iso()
    _atomic_write_json(STATUS_PATH, current)


def normalize_customer_name(name: Any) -> str:
    customer_name = str(name).strip() if pd.notna(name) else ""
    lowered = customer_name.lower()
    if "sam moon" in lowered:
        return "Sam Moon (All Locations)"
    if "fashion q" in lowered:
        return "Fashion Q (All Locations)"
    return customer_name


def get_prefix_bucket(sku: Any) -> str:
    s = str(sku).strip().upper()
    if not s or s in {"NONE", "NAN"}:
        return "Other"
    if s.startswith("ES") or s.startswith("S"):
        return "S(ES)"
    if s.startswith("EN") or s.startswith("N"):
        return "N(EN)"
    if s.startswith("BN"):
        return "BN"
    if s.startswith("BT"):
        return "BT"
    if s.startswith("GT"):
        return "GT"
    if s.startswith("GN"):
        return "GN"
    if s.startswith("EM") or s.startswith("M"):
        return "M(EM)"
    if s.startswith("TJ"):
        return "TJ"
    if s.startswith("ET") or s.startswith("T"):
        return "T(ET)"
    if s.startswith("L"):
        return "L"
    return "Other"


def get_color_code(sku: Any) -> str:
    s = str(sku).strip().upper()
    if not s or s in {"NONE", "NAN"}:
        return "Other"
    if " " in s:
        token = s.rsplit(None, 1)[-1]
        if _COLOR_TOKEN.fullmatch(token):
            return token.replace("/", "-")
    match = re.search(r"\d([A-Z]{2,6})$", s)
    if match:
        return match.group(1)
    return "Other"


def _empty_prefix_map() -> dict[str, dict[str, float]]:
    return {col: {"qty": 0, "revenue": 0.0} for col in PREFIX_COLUMNS}


def _num(value: Any, digits: int = 2) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(number):
        return 0.0
    return round(number, digits)


def _int(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if pd.isna(number):
        return 0
    return int(number)


def _prefix_from_frame(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    result = _empty_prefix_map()
    if frame.empty:
        return result
    frame = frame.dropna(subset=["sku"])
    if frame.empty:
        return result
    grouped = frame.groupby("prefix", dropna=False).agg(qty=("qty", "sum"), revenue=("line_revenue", "sum"))
    for prefix, row in grouped.iterrows():
        key = str(prefix) if str(prefix) in result else "Other"
        result[key]["qty"] += _int(row["qty"])
        result[key]["revenue"] = _num(result[key]["revenue"] + _num(row["revenue"]))
    return result


def _colors_from_frame(frame: pd.DataFrame) -> dict[str, int]:
    result: dict[str, int] = {}
    if frame.empty:
        return result
    work = frame.dropna(subset=["sku"])
    if work.empty:
        return result
    grouped = work.groupby("color", dropna=False)["qty"].sum()
    for color, qty in grouped.items():
        key = str(color).strip() or "Other"
        n = _int(qty)
        if n:
            result[key] = result.get(key, 0) + n
    return result


def _aggregate_slice(orders: pd.DataFrame, lines: pd.DataFrame) -> dict[str, Any]:
    monthly: list[dict[str, Any]] = []
    if not orders.empty:
        month_order = (
            orders.groupby("month", dropna=False)
            .agg(
                revenue=("order_amount", "sum"),
                order_total=("order_total", "sum"),
                orders=("order_number", "nunique"),
                items=("items", "sum"),
                qty=("qty", "sum"),
                fdc_revenue=("fdc_amount", "sum"),
                efas_revenue=("efas_amount", "sum"),
            )
            .reset_index()
            .sort_values("month")
        )
        month_styles = {
            str(k): _int(v)
            for k, v in (
                lines.dropna(subset=["sku"]).groupby("month")["sku"].nunique().items()
                if not lines.empty
                else []
            )
        }
        month_prefixes = (
            {str(month): _prefix_from_frame(group) for month, group in lines.groupby("month")}
            if not lines.empty
            else {}
        )
        month_colors = (
            {str(month): _colors_from_frame(group) for month, group in lines.groupby("month")}
            if not lines.empty
            else {}
        )
        for _, row in month_order.iterrows():
            month = str(row["month"])
            monthly.append(
                {
                    "month": month,
                    "revenue": _num(row["revenue"]),
                    "order_total": _num(row["order_total"]),
                    "orders": _int(row["orders"]),
                    "items": _int(row["items"]),
                    "qty": _int(row["qty"]),
                    "unique_styles": month_styles.get(month, 0),
                    "fdc_revenue": _num(row["fdc_revenue"]),
                    "efas_revenue": _num(row["efas_revenue"]),
                    "prefixes": month_prefixes.get(month, _empty_prefix_map()),
                    "colors": month_colors.get(month, {}),
                }
            )

    customers: list[dict[str, Any]] = []
    if not orders.empty:
        customer_totals = (
            orders.groupby("customer", dropna=False)
            .agg(
                revenue=("order_amount", "sum"),
                orders=("order_number", "nunique"),
                items=("items", "sum"),
                qty=("qty", "sum"),
            )
            .reset_index()
        )
        style_totals = {
            str(k): _int(v)
            for k, v in (
                lines.dropna(subset=["sku"]).groupby("customer")["sku"].nunique().items()
                if not lines.empty
                else []
            )
        }
        prefix_totals = (
            {name: _prefix_from_frame(group) for name, group in lines.groupby("customer")}
            if not lines.empty
            else {}
        )
        month_customer_orders = (
            orders.groupby(["customer", "month"], dropna=False)
            .agg(
                revenue=("order_amount", "sum"),
                orders=("order_number", "nunique"),
                items=("items", "sum"),
                qty=("qty", "sum"),
            )
            .reset_index()
        )
        month_customer_styles = {
            (str(cust), str(month)): _int(val)
            for (cust, month), val in (
                lines.dropna(subset=["sku"]).groupby(["customer", "month"])["sku"].nunique().items()
                if not lines.empty
                else []
            )
        }
        month_customer_prefixes: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
        if not lines.empty:
            for (cust, month), group in lines.groupby(["customer", "month"]):
                month_customer_prefixes[(str(cust), str(month))] = _prefix_from_frame(group)

        for _, row in customer_totals.sort_values("revenue", ascending=False).iterrows():
            customer = str(row["customer"] or "")
            months_map: dict[str, Any] = {}
            customer_month_slice = month_customer_orders[month_customer_orders["customer"] == customer]
            for _, mrow in customer_month_slice.iterrows():
                month_key = str(mrow["month"])
                months_map[month_key] = {
                    "revenue": _num(mrow["revenue"]),
                    "orders": _int(mrow["orders"]),
                    "items": _int(mrow["items"]),
                    "qty": _int(mrow["qty"]),
                    "unique_styles": month_customer_styles.get((customer, month_key), 0),
                    "prefixes": month_customer_prefixes.get((customer, month_key), _empty_prefix_map()),
                }
            customers.append(
                {
                    "customer": customer,
                    "revenue": _num(row["revenue"]),
                    "orders": _int(row["orders"]),
                    "items": _int(row["items"]),
                    "qty": _int(row["qty"]),
                    "unique_styles": style_totals.get(customer, 0),
                    "prefixes": prefix_totals.get(customer, _empty_prefix_map()),
                    "months": months_map,
                }
            )

    unique_styles = 0
    if not lines.empty:
        unique_styles = _int(lines.dropna(subset=["sku"])["sku"].nunique())

    daily_prefixes: dict[str, dict[str, dict[str, float]]] = {}
    daily_colors: dict[str, dict[str, int]] = {}
    if not lines.empty:
        day_key = lines["iso_date"].astype(str).str.slice(0, 10)
        for day, group in lines.groupby(day_key, dropna=False):
            key = str(day)[:10]
            if not key:
                continue
            daily_prefixes[key] = _prefix_from_frame(group)
            daily_colors[key] = _colors_from_frame(group)

    return {
        "monthly": monthly,
        "customers": customers,
        "unique_styles": unique_styles,
        "colors": _colors_from_frame(lines) if not lines.empty else {},
        "daily_prefixes": daily_prefixes,
        "daily_colors": daily_colors,
    }


def build_dashboard_payload(line_rows: list[tuple]) -> dict[str, Any]:
    columns = [
        "database",
        "order_number",
        "iso_date",
        "customer_raw",
        "order_amount",
        "handling_fee",
        "misc_charge",
        "sku",
        "qty",
        "unit_price",
    ]
    lines = pd.DataFrame.from_records(line_rows, columns=columns)
    if lines.empty:
        empty = {
            "monthly": [],
            "customers": [],
            "unique_styles": 0,
            "colors": {},
            "daily_prefixes": {},
            "daily_colors": {},
        }
        return {
            "generated_at": _now_iso(),
            "months_back": MONTHS_BACK,
            "months": [],
            "prefix_columns": PREFIX_COLUMNS,
            "databases": {"both": empty, "fdc": empty, "efas": empty},
            "orders": [],
            "line_count": 0,
            "order_count": 0,
        }

    lines["order_number"] = lines["order_number"].astype(str)
    lines["iso_date"] = lines["iso_date"].astype(str)
    lines["customer_raw"] = lines["customer_raw"].fillna("").astype(str)
    lines["customer"] = lines["customer_raw"].apply(normalize_customer_name)
    for col in ("order_amount", "handling_fee", "misc_charge", "qty", "unit_price"):
        lines[col] = pd.to_numeric(lines[col], errors="coerce").fillna(0)
    lines["sku"] = lines["sku"].where(lines["sku"].notna(), None)
    lines["sku"] = lines["sku"].apply(lambda x: str(x).strip() if x is not None and str(x).strip() not in {"", "None"} else None)
    lines["line_revenue"] = lines["qty"] * lines["unit_price"]
    lines["prefix"] = lines["sku"].apply(lambda x: get_prefix_bucket(x) if x else "Other")
    lines["color"] = lines["sku"].apply(lambda x: get_color_code(x) if x else "Other")
    lines["month"] = lines["iso_date"].str.slice(0, 7)
    lines["order_total"] = lines["order_amount"] + lines["handling_fee"] + lines["misc_charge"]

    order_groups = lines.groupby(["database", "order_number"], dropna=False)
    orders = order_groups.agg(
        iso_date=("iso_date", "first"),
        customer_raw=("customer_raw", "first"),
        customer=("customer", "first"),
        order_amount=("order_amount", "first"),
        handling_fee=("handling_fee", "first"),
        misc_charge=("misc_charge", "first"),
        order_total=("order_total", "first"),
        items=("sku", "count"),
        qty=("qty", "sum"),
    ).reset_index()
    orders["month"] = orders["iso_date"].astype(str).str.slice(0, 7)
    orders["fdc_amount"] = orders["order_amount"].where(orders["database"] == "FDC", 0.0)
    orders["efas_amount"] = orders["order_amount"].where(orders["database"] == "E_FAS", 0.0)
    orders = orders.sort_values(["iso_date", "order_number"], ascending=[False, False])

    fdc_lines = lines[lines["database"] == "FDC"]
    efas_lines = lines[lines["database"] == "E_FAS"]
    fdc_orders = orders[orders["database"] == "FDC"]
    efas_orders = orders[orders["database"] == "E_FAS"]

    databases = {
        "both": _aggregate_slice(orders, lines),
        "fdc": _aggregate_slice(fdc_orders, fdc_lines),
        "efas": _aggregate_slice(efas_orders, efas_lines),
    }

    months = sorted({str(m) for m in orders["month"].dropna().unique() if str(m)})
    order_records = []
    for _, row in orders.iterrows():
        iso_date = str(row["iso_date"] or "")
        display = iso_date
        if len(iso_date) == 10:
            display = f"{iso_date[5:7]}/{iso_date[8:10]}/{iso_date[0:4]}"
        order_records.append(
            {
                "database": str(row["database"]),
                "order_number": str(row["order_number"]),
                "order_date": iso_date,
                "order_date_display": display,
                "customer": str(row["customer"]),
                "customer_raw": str(row["customer_raw"]),
                "order_amount": _num(row["order_amount"]),
                "handling_fee": _num(row["handling_fee"]),
                "misc_charge": _num(row["misc_charge"]),
                "order_total": _num(row["order_total"]),
                "items": _int(row["items"]),
                "qty": _int(row["qty"]),
            }
        )

    return {
        "generated_at": _now_iso(),
        "months_back": MONTHS_BACK,
        "months": months,
        "prefix_columns": PREFIX_COLUMNS,
        "databases": databases,
        "orders": order_records,
        "line_count": int(len(lines)),
        "order_count": int(len(orders)),
    }


def _add_prefix_maps(left: dict | None, right: dict | None) -> dict[str, dict[str, float]]:
    out = _empty_prefix_map()
    for col in PREFIX_COLUMNS:
        a = (left or {}).get(col) or {}
        b = (right or {}).get(col) or {}
        out[col] = {
            "qty": _int(a.get("qty")) + _int(b.get("qty")),
            "revenue": _num(_num(a.get("revenue")) + _num(b.get("revenue"))),
        }
    return out


def _add_color_maps(left: dict | None, right: dict | None) -> dict[str, int]:
    out = dict(left or {})
    for key, qty in (right or {}).items():
        out[str(key)] = _int(out.get(key, 0)) + _int(qty)
    return {k: v for k, v in out.items() if _int(v)}


def _merge_month_row(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "month": left.get("month") or right.get("month"),
        "revenue": _num(_num(left.get("revenue")) + _num(right.get("revenue"))),
        "order_total": _num(_num(left.get("order_total")) + _num(right.get("order_total"))),
        "orders": _int(left.get("orders")) + _int(right.get("orders")),
        "items": _int(left.get("items")) + _int(right.get("items")),
        "qty": _int(left.get("qty")) + _int(right.get("qty")),
        "unique_styles": _int(left.get("unique_styles")) + _int(right.get("unique_styles")),
        "fdc_revenue": _num(_num(left.get("fdc_revenue")) + _num(right.get("fdc_revenue"))),
        "efas_revenue": _num(_num(left.get("efas_revenue")) + _num(right.get("efas_revenue"))),
        "prefixes": _add_prefix_maps(left.get("prefixes"), right.get("prefixes")),
        "colors": _add_color_maps(left.get("colors"), right.get("colors")),
    }


def _merge_customer(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    months: dict[str, Any] = dict(left.get("months") or {})
    for month, row in (right.get("months") or {}).items():
        if month in months:
            months[month] = {
                "revenue": _num(_num(months[month].get("revenue")) + _num(row.get("revenue"))),
                "orders": _int(months[month].get("orders")) + _int(row.get("orders")),
                "items": _int(months[month].get("items")) + _int(row.get("items")),
                "qty": _int(months[month].get("qty")) + _int(row.get("qty")),
                "unique_styles": _int(months[month].get("unique_styles")) + _int(row.get("unique_styles")),
                "prefixes": _add_prefix_maps(months[month].get("prefixes"), row.get("prefixes")),
            }
        else:
            months[month] = row
    return {
        "customer": left.get("customer") or right.get("customer"),
        "revenue": _num(_num(left.get("revenue")) + _num(right.get("revenue"))),
        "orders": _int(left.get("orders")) + _int(right.get("orders")),
        "items": _int(left.get("items")) + _int(right.get("items")),
        "qty": _int(left.get("qty")) + _int(right.get("qty")),
        "unique_styles": max(_int(left.get("unique_styles")), _int(right.get("unique_styles"))),
        "prefixes": _add_prefix_maps(left.get("prefixes"), right.get("prefixes")),
        "months": months,
    }


def _merge_slice(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    left = left or {}
    right = right or {}
    monthly_map: dict[str, dict[str, Any]] = {}
    for row in (left.get("monthly") or []) + (right.get("monthly") or []):
        month = str(row.get("month") or "")
        if not month:
            continue
        monthly_map[month] = _merge_month_row(monthly_map[month], row) if month in monthly_map else row
    customers_map: dict[str, dict[str, Any]] = {}
    for row in (left.get("customers") or []) + (right.get("customers") or []):
        name = str(row.get("customer") or "")
        customers_map[name] = _merge_customer(customers_map[name], row) if name in customers_map else row
    daily_prefixes = dict(left.get("daily_prefixes") or {})
    daily_prefixes.update(right.get("daily_prefixes") or {})
    daily_colors = dict(left.get("daily_colors") or {})
    daily_colors.update(right.get("daily_colors") or {})
    return {
        "monthly": [monthly_map[k] for k in sorted(monthly_map)],
        "customers": sorted(customers_map.values(), key=lambda c: float(c.get("revenue") or 0), reverse=True),
        "unique_styles": _int(left.get("unique_styles")) + _int(right.get("unique_styles")),
        "colors": _add_color_maps(left.get("colors"), right.get("colors")),
        "daily_prefixes": daily_prefixes,
        "daily_colors": daily_colors,
    }


def merge_payloads(history: dict[str, Any] | None, recent: dict[str, Any] | None) -> dict[str, Any]:
    history = history or {}
    recent = recent or {}
    hist_db = history.get("databases") or {}
    rec_db = recent.get("databases") or {}
    months = sorted(set(history.get("months") or []) | set(recent.get("months") or []))
    orders = list(history.get("orders") or []) + list(recent.get("orders") or [])
    orders.sort(key=lambda o: (str(o.get("order_date") or ""), str(o.get("order_number") or "")), reverse=True)
    return {
        "generated_at": recent.get("generated_at") or history.get("generated_at") or _now_iso(),
        "months_back": MONTHS_BACK,
        "history_start": HISTORY_START,
        "history_end": HISTORY_END,
        "recent_start": RECENT_START,
        "months": months,
        "prefix_columns": PREFIX_COLUMNS,
        "databases": {
            "both": _merge_slice(hist_db.get("both"), rec_db.get("both")),
            "fdc": _merge_slice(hist_db.get("fdc"), rec_db.get("fdc")),
            "efas": _merge_slice(hist_db.get("efas"), rec_db.get("efas")),
        },
        "orders": orders,
        "line_count": _int(history.get("line_count")) + _int(recent.get("line_count")),
        "order_count": _int(history.get("order_count")) + _int(recent.get("order_count")),
    }


def _history_is_complete(history: dict[str, Any] | None) -> bool:
    if not history:
        return False
    return (
        str(history.get("range_start") or "") == HISTORY_START
        and str(history.get("range_end") or "") == HISTORY_END
        and bool(history.get("months"))
    )


def refresh_audit_cache(*, force: bool = False, force_history: bool = False) -> dict[str, Any]:
    global _refreshing
    if not _refresh_lock.acquire(blocking=False):
        return read_status()
    try:
        if _refreshing and not force:
            return read_status()
        _refreshing = True
        started = time.perf_counter()
        write_status(state="refreshing", started_at=_now_iso(), error=None)

        history = _read_json(HISTORY_PATH)
        if force_history or not _history_is_complete(history):
            write_status(
                state="refreshing",
                started_at=_now_iso(),
                error=None,
                phase="history",
            )
            hist_rows = SQL_connector.get_audit_order_lines_range(HISTORY_START, HISTORY_END)
            history = build_dashboard_payload(hist_rows)
            history["cache_kind"] = "history"
            history["range_start"] = HISTORY_START
            history["range_end"] = HISTORY_END
            _atomic_write_json(HISTORY_PATH, history)

        write_status(
            state="refreshing",
            started_at=_now_iso(),
            error=None,
            phase="recent",
        )
        today = date.today().isoformat()
        recent_rows = SQL_connector.get_audit_order_lines_range(RECENT_START, today)
        recent = build_dashboard_payload(recent_rows)
        recent["cache_kind"] = "recent"
        recent["range_start"] = RECENT_START
        recent["range_end"] = today
        payload = merge_payloads(history, recent)
        _atomic_write_json(DATA_PATH, payload)
        duration = round(time.perf_counter() - started, 1)
        write_status(
            state="ready",
            generated_at=payload.get("generated_at"),
            error=None,
            duration_sec=duration,
            order_count=payload.get("order_count", 0),
            line_count=payload.get("line_count", 0),
            months_back=MONTHS_BACK,
            history_start=HISTORY_START,
            history_end=HISTORY_END,
            history_orders=(history or {}).get("order_count", 0),
            phase="ready",
        )
        return read_status()
    except Exception as exc:
        write_status(
            state="error" if not os.path.isfile(DATA_PATH) else "ready",
            error=str(exc)[:500],
        )
        raise
    finally:
        _refreshing = False
        _refresh_lock.release()


def trigger_refresh(app, *, force: bool = False) -> dict[str, Any]:
    status = read_status()
    if _refreshing:
        status["refreshing"] = True
        return status

    def _run():
        with app.app_context():
            try:
                refresh_audit_cache(force=force)
            except Exception:
                app.logger.exception("audit cache refresh failed")

    thread = threading.Thread(target=_run, name="audit-cache-refresh", daemon=True)
    thread.start()
    status = read_status()
    status["refreshing"] = True
    status["state"] = "refreshing"
    return status


def cache_is_stale(max_age_sec: int = REFRESH_INTERVAL_SEC) -> bool:
    if not os.path.isfile(DATA_PATH):
        return True
    if not _history_is_complete(_read_json(HISTORY_PATH)):
        return True
    try:
        age = time.time() - os.path.getmtime(DATA_PATH)
    except OSError:
        return True
    return age >= max_age_sec


def start_audit_cache_worker(app) -> None:
    global _worker_started
    if _worker_started:
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _worker_started = True

    def _loop():
        with app.app_context():
            while True:
                try:
                    if cache_is_stale() or not os.path.isfile(DATA_PATH):
                        refresh_audit_cache()
                except Exception:
                    app.logger.exception("audit cache background refresh failed")
                time.sleep(60)

    threading.Thread(target=_loop, name="audit-cache-worker", daemon=True).start()
