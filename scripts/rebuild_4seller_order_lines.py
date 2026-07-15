"""Rebuild 4seller_orders with one row per SKU line and shipping fields."""
from __future__ import annotations

import sys

sys.path.insert(0, r"c:\FDC_FLASK_APP")

import importlib.util

spec = importlib.util.spec_from_file_location("sales", r"c:\FDC_FLASK_APP\py\other\4seller_sales.py")
sales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sales)


def _progress(label: str):
    def callback(page: int, api_total: int, fetched: int) -> None:
        print(f"{label} page {page}: saved {fetched}/{api_total}", flush=True)

    return callback


def main() -> int:
    print("Rebuilding 4seller_orders (SKU per row + shipping)...", flush=True)
    sales._ensure_4seller_orders_table()
    session, _ = sales._session_from_cookie("")

    print("Importing recent orders (90 days)...", flush=True)
    recent = sales._sync_recent_orders_from_api(session)
    print(f"Recent lines saved: {recent['fetched_from_api']}", flush=True)

    print("Importing historical orders (>90 days)...", flush=True)
    historical = sales._sync_historical_orders_from_api(session, progress_callback=_progress("historical"))
    print(f"Historical lines saved: {historical['fetched_from_api']}", flush=True)

    stats = sales._get_db_stats()
    print(f"Done. Database rows: {stats['total']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
