"""Fill missing masterbox dims for available SKUs from all Dropbox container Excels.

Match order:
  1) exact STYLE + COLOR (abbreviated)
  2) STYLE-only fallback (most common packing-list dim for that style)

Writes CASE_LEN/WI/HI to FDC + EFAS, then saves remaining missing SKUs.
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.util
import os
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from py.search.container import (  # noqa: E402
    build_masterbox_dim_catalog,
    excel_recency_key,
    extract_packing_list_case_dimensions,
    lookup_case_dimensions,
)
from py.Toolkits.boat import DROPBOX_CONTAINER_PATH, scan_dropbox_containers  # noqa: E402
from py.utilities.color_abbrev import normalize_color_to_abbrev  # noqa: E402
from py.utilities.database_utils import (  # noqa: E402
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.style_efas_transfer import update_case_dimensions_dual  # noqa: E402


def _iter_dropbox_excel_paths() -> list[str]:
    """All container Excels under container INFO (every year/month).

    Sorted oldest→newest so catalog builders can let newer files win.
    """
    by_id = scan_dropbox_containers(all_years=True)
    paths = set(by_id.values())
    for path in glob.glob(os.path.join(DROPBOX_CONTAINER_PATH, "**", "*.xlsx"), recursive=True):
        name = os.path.basename(path)
        if name.startswith("~$"):
            continue
        if re.match(r"^(FT?\d+|L\d+)", name, re.IGNORECASE):
            paths.add(path)
    return sorted(paths, key=excel_recency_key)


def _fetch_available_missing(connect_fn) -> list[dict]:
    """SKUs with available > 0 and incomplete masterbox dims."""
    sql = """
        SELECT
            RTRIM(i.PROD_CD) AS sku,
            RTRIM(ISNULL(i.ALT_CD, '')) AS style,
            RTRIM(ISNULL(i.UNIT_COLOR, '')) AS color,
            CAST(ISNULL(d.in_stock, 0) - ISNULL(d.order_qty, 0) AS INT) AS available,
            CAST(ISNULL(idt.CASE_LEN, 0) AS FLOAT) AS case_len,
            CAST(ISNULL(idt.CASE_WI, 0) AS FLOAT) AS case_wi,
            CAST(ISNULL(idt.CASE_HI, 0) AS FLOAT) AS case_hi
        FROM dbo.inv AS i
        INNER JOIN dbo.inv_data AS d
            ON i.PROD_CD = d.prod_cd AND RTRIM(d.whs_num) = '01'
        LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
        WHERE CAST(ISNULL(d.in_stock, 0) - ISNULL(d.order_qty, 0) AS INT) > 0
          AND NOT (
                ISNULL(idt.CASE_LEN, 0) > 0
            AND ISNULL(idt.CASE_WI, 0) > 0
            AND ISNULL(idt.CASE_HI, 0) > 0
          )
        ORDER BY i.ALT_CD, i.UNIT_COLOR, i.PROD_CD
    """
    conn = connect_fn()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = []
        for row in cur.fetchall():
            rows.append(
                {
                    "sku": str(row[0] or "").strip(),
                    "style": str(row[1] or "").strip(),
                    "color": str(row[2] or "").strip(),
                    "available": int(row[3] or 0),
                }
            )
        return rows
    finally:
        conn.close()


def _build_dim_catalog(excel_paths: list[str]) -> tuple[dict, dict, dict]:
    """Newest Excel wins for STYLE+COLOR and STYLE masterbox maps."""
    return build_masterbox_dim_catalog(excel_paths)


def _split_sku(sku: str) -> tuple[str, str]:
    parts = sku.rsplit(" ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return sku.strip(), ""


def _resolve_dims(row: dict, exact: dict, style_map: dict) -> tuple[dict | None, str]:
    style = (row.get("style") or "").strip()
    color = normalize_color_to_abbrev((row.get("color") or "").strip())
    sku = (row.get("sku") or "").strip().upper()

    length, width, height = lookup_case_dimensions(exact, style, color)
    if length and width and height:
        return {"length": length, "width": width, "height": height}, "exact"

    sku_style, sku_color = _split_sku(sku)
    length, width, height = lookup_case_dimensions(exact, sku_style, sku_color)
    if length and width and height:
        return {"length": length, "width": width, "height": height}, "sku_key"

    style_key = (style or sku_style).strip().upper()
    hit = style_map.get(style_key)
    if hit:
        return hit, "style"

    return None, "none"


def _write_missing_csv(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fieldnames = ["db", "sku", "style", "color", "available"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--out",
        default=str(ROOT / "output" / "missing_masterbox_skus.csv"),
    )
    args = parser.parse_args()

    app = Flask("fill_missing_masterbox")
    print("Scanning Dropbox container files (all years)...")
    excel_paths = _iter_dropbox_excel_paths()
    print(f"  found {len(excel_paths)} Excel file(s)")

    with app.app_context():
        exact, style_map, cat_stats = _build_dim_catalog(excel_paths)
    print(
        f"  catalog: parsed={cat_stats['parsed']} empty={cat_stats['empty']} "
        f"failed={cat_stats['failed']} exact_keys={cat_stats['exact_keys']} "
        f"styles={cat_stats['styles']}"
    )

    fdc_missing = _fetch_available_missing(get_sql_server_connection_fdc)
    efas_missing = _fetch_available_missing(get_sql_server_connection_oms_ef)
    print(f"\nAvailable missing before: FDC={len(fdc_missing)} EFAS={len(efas_missing)}")

    # Prefer FDC row metadata when building the update map (union of both).
    by_sku: dict[str, dict] = {}
    for row in efas_missing:
        by_sku[row["sku"]] = row
    for row in fdc_missing:
        by_sku[row["sku"]] = row

    updates: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    match_counts = Counter()
    unresolved: list[dict] = []

    for sku, row in sorted(by_sku.items()):
        dims, how = _resolve_dims(row, exact, style_map)
        if not dims:
            match_counts["none"] += 1
            unresolved.append(row)
            continue
        match_counts[how] += 1
        updates[sku] = (
            Decimal(str(dims["length"])),
            Decimal(str(dims["width"])),
            Decimal(str(dims["height"])),
        )

    print(
        f"Unique SKUs to fill: {len(by_sku)} -> matched={len(updates)} "
        f"(exact={match_counts['exact']} sku_key={match_counts['sku_key']} "
        f"style={match_counts['style']}) unmatched={match_counts['none']}"
    )

    if updates:
        result = update_case_dimensions_dual(updates, dry_run=args.dry_run)
        wrote_fdc = len(result.get("updated_fdc") or []) + len(result.get("inserted_fdc") or [])
        wrote_efas = len(result.get("updated_efas") or []) + len(result.get("inserted_efas") or [])
        mode = "DRY-RUN" if args.dry_run else "WROTE"
        print(
            f"{mode} FDC={wrote_fdc} EFAS={wrote_efas} "
            f"not_in_db={len(result.get('missing') or [])} "
            f"errors={len(result.get('errors') or [])}"
        )
        for err in result.get("errors") or []:
            print(f"  error: {err}")
        if result.get("errors"):
            return 1

    # Re-query still-missing after write (or dry-run prediction for unmatched only).
    if args.dry_run:
        still_fdc = [
            {**r, "db": "fdc"}
            for r in fdc_missing
            if r["sku"] not in updates
        ]
        still_efas = [
            {**r, "db": "efas"}
            for r in efas_missing
            if r["sku"] not in updates
        ]
    else:
        still_fdc = [{**r, "db": "fdc"} for r in _fetch_available_missing(get_sql_server_connection_fdc)]
        still_efas = [{**r, "db": "efas"} for r in _fetch_available_missing(get_sql_server_connection_oms_ef)]

    still = still_fdc + still_efas
    _write_missing_csv(args.out, still)

    print("\n=== AFTER ===")
    print(f"FDC still missing: {len(still_fdc)}")
    print(f"EFAS still missing: {len(still_efas)}")
    print(f"List saved: {args.out}")

    print(f"\nFDC still-missing SKUs ({len(still_fdc)}):")
    for row in still_fdc:
        print(f"  {row['sku']}\tavail={row['available']}\tstyle={row['style']}\tcolor={row['color']}")

    if still_efas:
        print(f"\nEFAS still-missing SKUs ({len(still_efas)}):")
        for row in still_efas[:50]:
            print(f"  {row['sku']}\tavail={row['available']}\tstyle={row['style']}\tcolor={row['color']}")
        if len(still_efas) > 50:
            print(f"  ... +{len(still_efas) - 50} more (see CSV)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
