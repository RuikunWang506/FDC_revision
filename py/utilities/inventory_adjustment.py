"""ERP manual inventory adjustments (wm_log + inv_data)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from product_detail_module.product_detail import get_db_connections

BLANK10 = " " * 10
BLANK20 = " " * 20
BLANK30 = " " * 30
BLANK8 = " " * 8
BLANK60 = " " * 60

DEFAULT_ADJUSTED_BY = "fdcef"


def pad_char(value: str, width: int) -> str:
    text = (value or "").strip()
    if len(text) > width:
        return text[:width]
    return text + (" " * (width - len(text)))


def normalize_adjusted_by(value: str | None) -> str:
    text = (value or DEFAULT_ADJUSTED_BY).strip() or DEFAULT_ADJUSTED_BY
    return pad_char(text, 8)


def erp_date(cursor, when: datetime | None = None) -> int:
    when = when or datetime.now()
    cursor.execute(
        "SELECT DATEDIFF(DAY, '20230101', ?) + 81088",
        (when.date(),),
    )
    return int(cursor.fetchone()[0])


def erp_log_time(cursor) -> int:
    cursor.execute(
        """
        SELECT (DATEPART(HOUR, GETDATE()) * 3600
              + DATEPART(MINUTE, GETDATE()) * 60
              + DATEPART(SECOND, GETDATE())) * 100
             + DATEPART(MILLISECOND, GETDATE()) / 10
        """
    )
    return int(cursor.fetchone()[0])


def next_log_num(cursor) -> str:
    cursor.execute(
        """
        SELECT ISNULL(MAX(CAST(RTRIM(log_num) AS BIGINT)), 900000000) + 1
        FROM wm_log
        WHERE RTRIM(type_cd) = 'ADJ' AND RTRIM(log_num) LIKE '900%'
        """
    )
    return str(int(cursor.fetchone()[0]))


def lookup_sku_context(cursor, sku: str, whs_num: str = "01"):
    prod_cd = pad_char(sku, 20)
    cursor.execute(
        """
        SELECT CAST(ISNULL(id.in_stock, 0) AS DECIMAL(21, 6)),
               CAST(ISNULL(id.order_qty, 0) AS DECIMAL(21, 6)),
               RTRIM(ISNULL(id.updt_by, ''))
        FROM inv_data id
        WHERE id.prod_cd = ? AND RTRIM(id.whs_num) = ?
        """,
        (prod_cd, whs_num),
    )
    inv_row = cursor.fetchone()
    if not inv_row:
        raise ValueError(f"SKU {sku.strip()} not found in inv_data (warehouse {whs_num})")

    cursor.execute(
        """
        SELECT TOP 1 RTRIM(bin_cd), sku_num, RTRIM(sku_cd)
        FROM wm_binfile
        WHERE prod_cd = ? AND RTRIM(whs_num) = ?
        ORDER BY in_stock DESC, bin_cd
        """,
        (prod_cd, whs_num),
    )
    bin_row = cursor.fetchone()
    if bin_row:
        bin_cd, sku_num, sku_cd = bin_row[0], int(bin_row[1]), bin_row[2]
    else:
        cursor.execute(
            """
            SELECT TOP 1 sku_num, RTRIM(sku_cd)
            FROM wm_skufile
            WHERE prod_cd = ? AND RTRIM(whs_num) = ?
            """,
            (prod_cd, whs_num),
        )
        sku_row = cursor.fetchone()
        if not sku_row:
            raise ValueError(
                f"No wm_binfile or wm_skufile row for {sku.strip()} in warehouse {whs_num}"
            )
        sku_num, sku_cd = int(sku_row[0]), sku_row[1]
        bin_cd = "V"

    return {
        "prod_cd": prod_cd,
        "whs_num": pad_char(whs_num, 8),
        "bin_cd": pad_char(bin_cd, 20),
        "sku_num": sku_num,
        "sku_cd": pad_char(sku_cd or sku.strip(), 100),
        "in_stock_before": Decimal(str(inv_row[0])),
        "order_qty": Decimal(str(inv_row[1])),
        "updt_by_prev": inv_row[2],
    }


def fetch_stock_snapshot(cursor, prod_cd: str):
    cursor.execute(
        """
        SELECT CAST(ISNULL(id.in_stock, 0) AS INT),
               CAST(ISNULL(id.order_qty, 0) AS INT),
               CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT),
               CAST(ISNULL((SELECT SUM(b.in_stock) FROM wm_binfile b WHERE b.prod_cd = ?), 0) AS INT)
        FROM inv_data id
        WHERE id.prod_cd = ?
        """,
        (prod_cd, prod_cd),
    )
    row = cursor.fetchone()
    return {
        "in_stock": int(row[0]),
        "order_qty": int(row[1]),
        "available": int(row[2]),
        "bin_sum": int(row[3]),
    }


def build_wm_log_row(
    *,
    log_dt: int,
    log_time: int,
    log_by: str,
    log_num: str,
    log_qty: Decimal,
    ctx: dict,
    nt_num: int,
) -> dict:
    log_by = normalize_adjusted_by(log_by)
    return {
        "LOG_DT": log_dt,
        "LOG_TIME": log_time,
        "LOG_BY": log_by,
        "TYPE_CD": pad_char("ADJ", 10),
        "LOG_NUM": pad_char(log_num, 30),
        "LOG_CD": 0,
        "BIN_NT_NUM": 0,
        "TRANS_TYPE": pad_char("ADJ", 10),
        "LOG_QTY": log_qty,
        "BIN_QTY": Decimal("0"),
        "ORD_QTY": Decimal("0"),
        "VEN_ID": BLANK10,
        "CUS_ID": BLANK10,
        "PROD_CD": ctx["prod_cd"],
        "WHS_NUM": ctx["whs_num"],
        "LOT_NUM": BLANK20,
        "PROD_CLR": BLANK20,
        "RUN_CD": BLANK20,
        "BIN_CD": ctx["bin_cd"],
        "SKU_CD": ctx["sku_cd"],
        "SKU_NUM": ctx["sku_num"],
        "UT_DESC": BLANK60,
        "IN_STOCK": ctx["in_stock_before"],
        "PRICE_BASE": Decimal("0"),
        "FRT_CUS": Decimal("0"),
        "HNDL_FEE": Decimal("0"),
        "PROD_DUTY": Decimal("0"),
        "AVG_COST": Decimal("0"),
        "SALES_COST": Decimal("0"),
        "LOG_AREA": BLANK20,
        "PLT_CD": BLANK20,
        "UPDT_DT": log_dt,
        "UPDT_TM": -1,
        "UPDT_BY": log_by,
        "NT_NUM": nt_num,
        "BIN_CD2": BLANK20,
        "BIN_CD3": BLANK20,
        "BIN_CD4": BLANK20,
        "BARCODE": BLANK30,
        "CLOSE_CD": 1,
        "WMS_NUM": BLANK30,
        "BAT_NUM": 0,
        "CASE_NUM": BLANK30,
        "CASE_NUM2": BLANK30,
        "RSV_1": BLANK30,
        "RSV_2": BLANK30,
        "RSV_3": BLANK30,
        "RSV_4": BLANK30,
        "RSV_5": BLANK30,
        "POST_NUM": BLANK20,
        "POST_QTY": Decimal("0"),
        "WHS_NUM2": BLANK8,
        "WHS_NUM3": BLANK8,
        "WHS_NUM4": BLANK8,
    }


def insert_wm_log(cursor, row: dict) -> int:
    columns = list(row.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO wm_log ({', '.join(columns)}) VALUES ({placeholders})"
    cursor.execute(sql, [row[c] for c in columns])
    cursor.execute("SELECT CAST(@@IDENTITY AS INT)")
    rec_num = cursor.fetchone()[0]
    if rec_num is None:
        cursor.execute(
            """
            SELECT TOP 1 rec_num FROM wm_log
            WHERE RTRIM(log_num) = ? AND prod_cd = ?
            ORDER BY rec_num DESC
            """,
            (row["LOG_NUM"].strip(), row["PROD_CD"]),
        )
        fetched = cursor.fetchone()
        if not fetched:
            raise RuntimeError("wm_log insert succeeded but rec_num could not be resolved")
        rec_num = int(fetched[0])
    return int(rec_num)


def apply_adjustment(
    conn,
    *,
    sku: str,
    qty: int,
    adjusted_by: str,
    whs_num: str = "01",
    log_num: str | None = None,
    nt_num: int = 1,
    dry_run: bool = True,
    auto_commit: bool = True,
    database_label: str = "FDC",
):
    if qty == 0:
        raise ValueError("Adjustment quantity cannot be 0")

    adjusted_by = normalize_adjusted_by(adjusted_by)
    cursor = conn.cursor()
    try:
        ctx = lookup_sku_context(cursor, sku, whs_num=whs_num)
        before = fetch_stock_snapshot(cursor, ctx["prod_cd"])

        log_dt = erp_date(cursor)
        log_time = erp_log_time(cursor)
        if not log_num:
            log_num = next_log_num(cursor)

        row = build_wm_log_row(
            log_dt=log_dt,
            log_time=log_time,
            log_by=adjusted_by,
            log_num=log_num,
            log_qty=Decimal(qty),
            ctx=ctx,
            nt_num=nt_num,
        )

        after_in_stock = int(ctx["in_stock_before"] + Decimal(qty))
        after_available = after_in_stock - int(ctx["order_qty"])

        plan = {
            "sku": sku.strip(),
            "database": database_label,
            "log_num": log_num.strip(),
            "nt_num": nt_num,
            "qty": qty,
            "adjusted_by": adjusted_by.strip(),
            "warehouse": whs_num,
            "bin": ctx["bin_cd"].strip(),
            "sku_num": ctx["sku_num"],
            "before": before,
            "after_in_stock": after_in_stock,
            "after_available": after_available,
            "rec_num": None,
        }

        if dry_run:
            return plan

        rec_num = insert_wm_log(cursor, row)
        cursor.execute(
            """
            UPDATE inv_data
            SET in_stock = in_stock + ?,
                updt_dt = ?,
                updt_by = ?
            WHERE prod_cd = ? AND RTRIM(whs_num) = ?
            """,
            (
                Decimal(qty),
                log_dt,
                adjusted_by,
                ctx["prod_cd"],
                whs_num,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("inv_data update affected unexpected row count")

        if auto_commit:
            conn.commit()

        plan["rec_num"] = rec_num
        plan["after"] = fetch_stock_snapshot(cursor, ctx["prod_cd"])
        return plan
    except Exception:
        if auto_commit:
            conn.rollback()
        raise
    finally:
        cursor.close()


def execute_inventory_transfer(
    transfers: list[dict[str, Any]],
    direction: str,
    adjusted_by: str | None = None,
) -> dict[str, Any]:
    """
    Transfer inventory between FDC and E-Fashion.

    transfers: [{"sku": "M1141 BK", "qty": 5}, ...]
    direction: "fdc_to_efas" | "efas_to_fdc"
    """
    if direction not in ("fdc_to_efas", "efas_to_fdc"):
        raise ValueError("direction must be fdc_to_efas or efas_to_fdc")

    cleaned: list[tuple[str, int]] = []
    for item in transfers:
        sku = str(item.get("sku", "")).strip()
        try:
            qty = int(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if not sku or qty <= 0:
            continue
        cleaned.append((sku, qty))

    if not cleaned:
        raise ValueError("No transfer quantities entered")

    adjusted_by = normalize_adjusted_by(adjusted_by)
    conn_fdc, conn_ef = get_db_connections()
    if not conn_fdc or not conn_ef:
        raise RuntimeError("Database connection failed")

    if direction == "fdc_to_efas":
        from_conn, to_conn = conn_fdc, conn_ef
        from_label, to_label = "FDC", "E_FAS"
    else:
        from_conn, to_conn = conn_ef, conn_fdc
        from_label, to_label = "E_FAS", "FDC"

    cur_from = from_conn.cursor()
    cur_to = to_conn.cursor()
    try:
        for sku, qty in cleaned:
            ctx = lookup_sku_context(cur_from, sku)
            in_stock = int(ctx["in_stock_before"])
            if in_stock < qty:
                raise ValueError(
                    f"{sku}: insufficient stock in {from_label} "
                    f"(in_stock={in_stock}, requested={qty})"
                )
            lookup_sku_context(cur_to, sku)

        log_num_from = next_log_num(cur_from)
        log_num_to = next_log_num(cur_to)
        log_dt = erp_date(cur_from)
        log_time = erp_log_time(cur_from)

        results = []
        nt_num = 1
        for sku, qty in cleaned:
            minus_plan = apply_adjustment(
                from_conn,
                sku=sku,
                qty=-qty,
                adjusted_by=adjusted_by,
                log_num=log_num_from,
                nt_num=nt_num,
                dry_run=False,
                auto_commit=False,
                database_label=from_label,
            )
            plus_plan = apply_adjustment(
                to_conn,
                sku=sku,
                qty=qty,
                adjusted_by=adjusted_by,
                log_num=log_num_to,
                nt_num=nt_num,
                dry_run=False,
                auto_commit=False,
                database_label=to_label,
            )
            results.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "from": minus_plan,
                    "to": plus_plan,
                }
            )
            nt_num += 1

        from_conn.commit()
        to_conn.commit()

        return {
            "direction": direction,
            "from_database": from_label,
            "to_database": to_label,
            "log_num_from": log_num_from,
            "log_num_to": log_num_to,
            "adjusted_by": adjusted_by.strip(),
            "lines": results,
        }
    except Exception:
        from_conn.rollback()
        to_conn.rollback()
        raise
    finally:
        cur_from.close()
        cur_to.close()
        conn_fdc.close()
        conn_ef.close()
