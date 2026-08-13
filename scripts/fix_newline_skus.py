"""Fix newline/control chars in OMS PROD_CD / ALT_CD for active SKUs.

Active = non-zero stock/available OR open ocean (plog hdl_chg=0).

Actions:
- style-only corruption: fix ALT_CD on inv + inv_data
- PROD_CD corruption with clean twin: reassign child rows, merge inv_data qty, delete corrupt inv
- PROD_CD corruption without twin: rename PROD_CD/ALT_CD across tables

Usage:
  python scripts/fix_newline_skus.py            # apply
  python scripts/fix_newline_skus.py --dry-run  # preview only
"""
from __future__ import annotations

import argparse
import re
import sys

from py.utilities.database_utils import (
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)

CONTROL_CHARS = ("\n", "\r", "\t")


def clean_text(s: str | None) -> str:
    if s is None:
        return ""
    t = str(s)
    for ch in CONTROL_CHARS:
        t = t.replace(ch, "")
    return " ".join(t.split())


def has_control(s: str | None) -> bool:
    if not s:
        return False
    return any(ch in s for ch in CONTROL_CHARS)


def pad(s: str, width: int) -> str:
    return (s or "")[:width].ljust(width)


def find_clean_twin(cur, sku_clean: str, sku_raw: str):
    cur.execute(
        """
        SELECT i.PROD_CD,
               CAST(ISNULL(id.in_stock, 0) AS INT) AS stock,
               CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS available
        FROM dbo.inv i
        LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE REPLACE(REPLACE(REPLACE(RTRIM(i.PROD_CD), CHAR(10), ''), CHAR(13), ''), CHAR(9), '') = ?
          AND i.PROD_CD <> ?
        ORDER BY i.PROD_CD
        """,
        (sku_clean, sku_raw),
    )
    twins = cur.fetchall()
    if not twins:
        return None
    # Prefer rows without control chars in PROD_CD
    preferred = [t for t in twins if not has_control(t[0])]
    candidates = preferred or list(twins)
    # Prefer exact cleaned RTRIM match after removing controls
    exact = [t for t in candidates if clean_text(t[0]) == sku_clean]
    pool = exact or candidates
    # Prefer the one with largest |available| then stock
    pool.sort(key=lambda t: (abs(t[2] or 0), abs(t[1] or 0)), reverse=True)
    return pool[0][0]


def ocean_open(cur, prod_cd: str) -> tuple[int, float]:
    cur.execute(
        """
        SELECT COUNT(*), ISNULL(SUM(ISNULL(log_qty, 0)), 0)
        FROM dbo.plog
        WHERE prod_cd = ? AND ISNULL(hdl_chg, 0) = 0
        """,
        (prod_cd,),
    )
    cnt, qty = cur.fetchone()
    return int(cnt or 0), float(qty or 0)


def update_prod_cd(cur, table: str, col: str, old: str, new: str, width: int | None = None) -> int:
    """Update prod_cd references. If width set, also try padded/truncated variants of old."""
    targets = {old}
    if width:
        cleaned = clean_text(old)
        # common OMS paddings for the corrupt key as stored in narrower columns
        targets.add(pad(old.rstrip(" ") if False else old[:width], width))
        # exact: take first `width` chars of the inv prod_cd (char truncation)
        targets.add(old[:width] if len(old) >= width else pad(old, width))
        targets.add(pad(cleaned, width))

    total = 0
    for target in targets:
        cur.execute(
            f"UPDATE dbo.{table} SET {col} = ? WHERE {col} = ?",
            (pad(clean_text(new), width) if width else new, target),
        )
        total += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return total


def merge_inv_data(cur, corrupt_cd: str, clean_cd: str) -> str:
    cur.execute(
        """
        SELECT in_stock, order_qty, on_order_qty, ALT_CD
        FROM dbo.inv_data WHERE Prod_cd = ?
        """,
        (corrupt_cd,),
    )
    bad = cur.fetchone()
    if not bad:
        return "no_corrupt_inv_data"

    cur.execute(
        """
        SELECT in_stock, order_qty, on_order_qty
        FROM dbo.inv_data WHERE Prod_cd = ?
        """,
        (clean_cd,),
    )
    good = cur.fetchone()
    if good:
        cur.execute(
            """
            UPDATE dbo.inv_data
            SET in_stock = ISNULL(in_stock, 0) + ?,
                order_qty = ISNULL(order_qty, 0) + ?,
                on_order_qty = ISNULL(on_order_qty, 0) + ?,
                ALT_CD = ?
            WHERE Prod_cd = ?
            """,
            (
                float(bad[0] or 0),
                float(bad[1] or 0),
                float(bad[2] or 0),
                pad(clean_text(bad[3]), 20),
                clean_cd,
            ),
        )
        cur.execute("DELETE FROM dbo.inv_data WHERE Prod_cd = ?", (corrupt_cd,))
        return "merged_inv_data"
    # move row to clean sku
    cur.execute(
        """
        UPDATE dbo.inv_data
        SET Prod_cd = ?, ALT_CD = ?
        WHERE Prod_cd = ?
        """,
        (clean_cd, pad(clean_text(bad[3]), 20), corrupt_cd),
    )
    return "moved_inv_data"


def delete_or_move(cur, table: str, col: str, corrupt: str, clean: str, width: int | None = None) -> str:
    """If clean already has rows, delete corrupt; else update corrupt -> clean."""
    clean_key = pad(clean_text(clean), width) if width else clean
    corrupt_keys = [corrupt]
    if width:
        corrupt_keys.append(corrupt[:width] if len(corrupt) >= width else pad(corrupt, width))

    cur.execute(f"SELECT COUNT(*) FROM dbo.{table} WHERE {col} = ?", (clean_key,))
    clean_count = cur.fetchone()[0]

    touched = 0
    if clean_count:
        for key in corrupt_keys:
            cur.execute(f"DELETE FROM dbo.{table} WHERE {col} = ?", (key,))
            touched += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return f"deleted_{table}:{touched}"

    for key in corrupt_keys:
        cur.execute(
            f"UPDATE dbo.{table} SET {col} = ? WHERE {col} = ?",
            (clean_key, key),
        )
        touched += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return f"moved_{table}:{touched}"


def reassign_history(cur, corrupt: str, clean: str) -> list[str]:
    actions = []
    # plog / ord_log / wm_log use char(21) like inv
    for table in ("plog", "ord_log", "wm_log"):
        try:
            cur.execute(
                f"UPDATE dbo.{table} SET prod_cd = ? WHERE prod_cd = ?",
                (clean, corrupt),
            )
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            actions.append(f"{table}:{n}")
        except Exception as e:
            actions.append(f"{table}:ERR:{e}")

    # bin_pur is char(20); reassign rows (not unique on prod_cd alone)
    clean20 = pad(clean_text(clean), 20)
    corrupt20 = corrupt[:20] if len(corrupt) >= 20 else pad(corrupt, 20)
    try:
        cur.execute(
            "UPDATE dbo.bin_pur SET prod_cd = ? WHERE prod_cd = ? OR prod_cd = ?",
            (clean20, corrupt, corrupt20),
        )
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        actions.append(f"bin_pur:{n}")
    except Exception as e:
        actions.append(f"bin_pur:ERR:{e}")

    # wm_skufile is often unique on prod_cd+whs — drop corrupt if clean exists
    actions.append(delete_or_move(cur, "wm_skufile", "prod_cd", corrupt, clean, width=20))
    return actions


def fix_style_only(cur, prod_cd: str, style_clean: str) -> list[str]:
    alt = pad(style_clean, 20)
    actions = []
    cur.execute("UPDATE dbo.inv SET ALT_CD = ? WHERE PROD_CD = ?", (alt, prod_cd))
    actions.append(f"inv_alt:{cur.rowcount}")
    cur.execute("UPDATE dbo.inv_data SET ALT_CD = ? WHERE Prod_cd = ?", (alt, prod_cd))
    actions.append(f"inv_data_alt:{cur.rowcount}")
    return actions


def rename_sku(cur, corrupt: str, sku_clean: str, style_clean: str) -> list[str]:
    new_cd = pad(sku_clean, 21)
    new_alt = pad(style_clean, 20)
    actions = []

    # child tables first
    actions.extend(reassign_history(cur, corrupt, new_cd))
    actions.append(delete_or_move(cur, "inv_detl", "prod_cd", corrupt, new_cd))
    # inv_data
    cur.execute(
        "UPDATE dbo.inv_data SET Prod_cd = ?, ALT_CD = ? WHERE Prod_cd = ?",
        (new_cd, new_alt, corrupt),
    )
    actions.append(f"inv_data_rename:{cur.rowcount}")
    cur.execute(
        "UPDATE dbo.inv SET PROD_CD = ?, ALT_CD = ? WHERE PROD_CD = ?",
        (new_cd, new_alt, corrupt),
    )
    actions.append(f"inv_rename:{cur.rowcount}")
    return actions


def merge_into_twin(cur, corrupt: str, clean: str, style_clean: str) -> list[str]:
    actions = []
    actions.extend(reassign_history(cur, corrupt, clean))
    actions.append(merge_inv_data(cur, corrupt, clean))
    actions.append(delete_or_move(cur, "inv_detl", "prod_cd", corrupt, clean))
    # ensure clean style is fixed
    actions.extend(fix_style_only(cur, clean, style_clean))
    cur.execute("DELETE FROM dbo.inv WHERE PROD_CD = ?", (corrupt,))
    actions.append(f"inv_delete:{cur.rowcount}")
    return actions


def process_db(name: str, conn, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            i.PROD_CD, i.ALT_CD,
            CAST(ISNULL(id.in_stock, 0) AS INT),
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)
        FROM dbo.inv i
        LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD LIKE '%' + CHAR(10) + '%'
           OR i.PROD_CD LIKE '%' + CHAR(13) + '%'
           OR i.PROD_CD LIKE '%' + CHAR(9) + '%'
           OR i.ALT_CD LIKE '%' + CHAR(10) + '%'
           OR i.ALT_CD LIKE '%' + CHAR(13) + '%'
           OR i.ALT_CD LIKE '%' + CHAR(9) + '%'
        ORDER BY i.PROD_CD
        """
    )
    rows = cur.fetchall()
    fixed = 0
    skipped = 0

    print(f"\n==== {name}: {len(rows)} corrupt rows ====")
    for prod_cd, alt_cd, stock, avail in rows:
        sku_clean = clean_text(prod_cd)
        style_clean = clean_text(alt_cd)
        oc, oq = ocean_open(cur, prod_cd)
        active = bool((stock and stock != 0) or (avail and avail != 0) or oc > 0)
        sku_bad = has_control(prod_cd)
        alt_bad = has_control(alt_cd)

        if not active:
            print(f"SKIP inactive {prod_cd!r} -> {sku_clean!r}")
            skipped += 1
            continue

        if not sku_clean or not re.match(r"^[A-Za-z0-9]", sku_clean):
            print(f"SKIP garbage {prod_cd!r}")
            skipped += 1
            continue

        twin = find_clean_twin(cur, sku_clean, prod_cd) if sku_bad else None

        if sku_bad and twin:
            action = "MERGE"
            detail = f"twin={twin!r}"
        elif sku_bad and not twin:
            action = "RENAME"
            detail = f"new={pad(sku_clean, 21)!r}"
        elif alt_bad:
            action = "STYLE"
            detail = f"alt={pad(style_clean, 20)!r}"
        else:
            print(f"SKIP nothing-to-do {prod_cd!r}")
            skipped += 1
            continue

        print(
            f"{action} {prod_cd!r} style={alt_cd!r} stock={stock} avail={avail} "
            f"ocean={oc}/{oq} {detail}"
        )

        if dry_run:
            fixed += 1
            continue

        try:
            if action == "MERGE":
                acts = merge_into_twin(cur, prod_cd, twin, style_clean)
            elif action == "RENAME":
                acts = rename_sku(cur, prod_cd, sku_clean, style_clean)
            else:
                acts = fix_style_only(cur, prod_cd, style_clean)
            print("  ->", "; ".join(str(a) for a in acts))
            fixed += 1
        except Exception as e:
            print(f"  !! ERROR {e}")
            conn.rollback()
            raise

    if dry_run:
        conn.rollback()
        print(f"{name} dry-run: would fix {fixed}, skipped {skipped}")
    else:
        conn.commit()
        print(f"{name} committed: fixed {fixed}, skipped {skipped}")
    cur.close()
    return fixed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    total = 0
    for name, getter in [
        ("FDC", get_sql_server_connection_fdc),
        ("E_FAS", get_sql_server_connection_oms_ef),
    ]:
        conn = getter()
        try:
            total += process_db(name, conn, args.dry_run)
        finally:
            conn.close()
    print(f"\nDone. total fixed/would-fix={total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
