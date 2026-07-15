"""One-time import of 4Seller historical orders (>90 days) into MySQL."""
from __future__ import annotations

import sys

sys.path.insert(0, r"c:\FDC_FLASK_APP")

import importlib.util

spec = importlib.util.spec_from_file_location("sales", r"c:\FDC_FLASK_APP\py\other\4seller_sales.py")
sales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sales)


def _progress(page: int, api_total: int, fetched: int) -> None:
    print(f"page {page}: saved {fetched}/{api_total}", flush=True)


def main() -> int:
    print("Starting historical order import (>90 days)...", flush=True)
    session, _ = sales._session_from_cookie("")
    before = sales._get_db_stats()
    print(f"Database before: {before['total']} orders", flush=True)

    result = sales._sync_historical_orders_from_api(session, progress_callback=_progress)
    after = sales._get_db_stats()

    print(
        f"Done. API total={result['api_total']}, fetched={result['fetched_from_api']}, "
        f"database now={after['total']} (was {before['total']})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
