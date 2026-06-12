"""One-time backfill of lori_orders tracking/status and loris_pkg_info from FedEx Shipment Report."""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.loris.lori_automation import _normalize_lori_po_number, get_db_connection

DEFAULT_REPORT = ROOT / "Shipment_Report_2026-04-11_to_2026-06-11.xlsx"


def _safe_float(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _build_shipment_groups(df):
    """Group shipment report rows by normalized PO number."""
    groups = {}
    for _, row in df.iterrows():
        po_number = _normalize_lori_po_number(row.get("reference"))
        if not po_number:
            continue

        entry = groups.setdefault(
            po_number,
            {"tracking": None, "service_type": None, "boxes": []},
        )

        tracking = row.get("masterTrackingNumber")
        if pd.notna(tracking) and str(tracking).strip():
            tracking_text = str(tracking).strip()
            if not entry["tracking"]:
                entry["tracking"] = tracking_text

        service_type = row.get("serviceType")
        if pd.notna(service_type) and str(service_type).strip():
            entry["service_type"] = str(service_type).strip()

        length = _safe_float(row.get("length"))
        width = _safe_float(row.get("width"))
        height = _safe_float(row.get("height"))
        weight = _safe_float(row.get("packageWeight"))
        if length and width and height and weight:
            box = {
                "length": round(length, 1),
                "width": round(width, 1),
                "height": round(height, 1),
                "weight": round(weight, 1),
            }
            if box not in entry["boxes"]:
                entry["boxes"].append(box)

    return groups


def backfill_shipment_report(report_path, dry_run=True):
    df = pd.read_excel(report_path, sheet_name="Overview")
    groups = _build_shipment_groups(df)

    connection = get_db_connection()
    if not connection:
        raise RuntimeError("Database connection failed")

    cursor = connection.cursor(dictionary=True)

    stats = {
        "report_pos": len(groups),
        "tracking_updated": 0,
        "status_updated": 0,
        "package_inserted": 0,
        "skipped_not_in_db": 0,
        "skipped_has_tracking": 0,
        "skipped_has_package": 0,
        "skipped_no_boxes": 0,
    }

    try:
        for po_number, shipment in groups.items():
            cursor.execute(
                "SELECT po_number, status, tracking FROM lori_orders WHERE po_number = %s",
                (po_number,),
            )
            order = cursor.fetchone()
            if not order:
                stats["skipped_not_in_db"] += 1
                continue

            tracking = shipment["tracking"]
            current_tracking = (order.get("tracking") or "").strip()
            if tracking and not current_tracking:
                if not dry_run:
                    cursor.execute(
                        """
                        UPDATE lori_orders
                        SET tracking = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE po_number = %s
                        """,
                        (tracking, po_number),
                    )
                stats["tracking_updated"] += 1
            elif tracking and current_tracking:
                stats["skipped_has_tracking"] += 1

            if (order.get("status") or "").lower() != "shipped":
                if not dry_run:
                    cursor.execute(
                        """
                        UPDATE lori_orders
                        SET status = 'shipped', updated_at = CURRENT_TIMESTAMP
                        WHERE po_number = %s
                        """,
                        (po_number,),
                    )
                stats["status_updated"] += 1

            cursor.execute(
                "SELECT po_number FROM loris_pkg_info WHERE po_number = %s",
                (po_number,),
            )
            if cursor.fetchone():
                stats["skipped_has_package"] += 1
                continue

            boxes = shipment["boxes"]
            if not boxes:
                stats["skipped_no_boxes"] += 1
                continue

            total_weight = round(sum(box["weight"] for box in boxes), 1)
            service_type = shipment.get("service_type") or "FEDEX_GROUND"

            if not dry_run:
                cursor.execute(
                    """
                    INSERT INTO loris_pkg_info
                    (po_number, box_count, box_dimensions, total_weight,
                     fedex_service_type, fedex_packaging_type, special_instructions, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        po_number,
                        len(boxes),
                        json.dumps(boxes),
                        total_weight,
                        service_type,
                        "YOUR_PACKAGING",
                        "Backfilled from FedEx Shipment Report",
                        "backfill_script",
                    ),
                )
            stats["package_inserted"] += 1

        if not dry_run:
            connection.commit()
    finally:
        cursor.close()
        connection.close()

    return stats


def verify_backfill(report_path):
    df = pd.read_excel(report_path, sheet_name="Overview")
    groups = _build_shipment_groups(df)
    report_pos = list(groups.keys())

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    placeholders = ",".join(["%s"] * len(report_pos))

    cursor.execute(
        f"SELECT status, COUNT(*) AS c FROM lori_orders WHERE po_number IN ({placeholders}) GROUP BY status",
        report_pos,
    )
    print("Status counts:", cursor.fetchall())

    cursor.execute(
        f"""
        SELECT COUNT(*) AS c FROM lori_orders
        WHERE po_number IN ({placeholders})
          AND (tracking IS NULL OR tracking = '')
        """,
        report_pos,
    )
    print("Missing tracking:", cursor.fetchone()["c"])

    cursor.execute(
        f"SELECT COUNT(*) AS c FROM loris_pkg_info WHERE po_number IN ({placeholders})",
        report_pos,
    )
    print("Orders with package info:", cursor.fetchone()["c"])

    cursor.close()
    connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Path to FedEx Shipment Report .xlsx",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to the database (default is dry-run preview only)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Print post-backfill verification stats only",
    )
    args = parser.parse_args()

    if not args.report.exists():
        raise SystemExit(f"Report not found: {args.report}")

    if args.verify:
        verify_backfill(args.report)
        return

    dry_run = not args.apply
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"[{mode}] Backfilling from {args.report}")

    stats = backfill_shipment_report(args.report, dry_run=dry_run)
    for key, value in stats.items():
        print(f"  {key}: {value}")

    if dry_run:
        print("\nNo changes written. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
