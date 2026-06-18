"""Import container PO lines into FDC only (pur_ord, plog, bin_pur)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from py.utilities.database_utils import get_sql_server_connection_fdc
from py.utilities.inventory_adjustment import erp_date, erp_log_time, pad_char
from py.utilities.style_efas_transfer import CONTAINER_CSV_DIR, _decimal

DEFAULT_WHS = "01"
# PUR_CD=1 lines are container PO imports; PUR_CD=2 are OMS bin allocations on receive.
PO_LINE_PUR_CD = 1
VENDOR_FALLBACK = {"F": "50005", "L": "00005"}
STANDARD_VENDOR_IDS = frozenset(VENDOR_FALLBACK.values())


@dataclass
class PoLine:
    prod_cd: str
    po_qty: Decimal
    base_cost: Decimal


@dataclass
class PoTransferResult:
    success: bool
    inserted_lines: list[str] = field(default_factory=list)
    skipped: bool = False
    repaired: bool = False
    repaired_lines: int = 0
    dry_run: bool = False
    target: str = "fdc"
    errors: list[str] = field(default_factory=list)
    pur_amt: Decimal = Decimal("0")
    vendor_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "inserted_lines": self.inserted_lines,
            "skipped": self.skipped,
            "repaired": self.repaired,
            "repaired_lines": self.repaired_lines,
            "dry_run": self.dry_run,
            "target": self.target,
            "errors": self.errors,
            "pur_amt": str(self.pur_amt),
            "vendor_id": self.vendor_id,
            "synced": len(self.inserted_lines),
        }


def _container_po_csv_path(container_number: str) -> str:
    normalized = str(container_number).strip().upper()
    return os.path.normpath(os.path.join(CONTAINER_CSV_DIR, f"{normalized}-PO.csv"))


def _container_prefix(container_number: str) -> str:
    text = str(container_number).strip().upper()
    return "L" if text.startswith("L") else "F"


def parse_container_po_csv(csv_path: str) -> list[PoLine]:
    df = pd.read_csv(csv_path)
    rows: list[PoLine] = []
    for _, record in df.iterrows():
        item = str(record.get("Item#", "")).strip()
        if not item:
            continue
        rows.append(
            PoLine(
                prod_cd=item,
                po_qty=_decimal(record.get("P/O Qty")),
                base_cost=_decimal(record.get("Price")),
            )
        )
    return rows


def fetch_po_lines_from_container_csv(container_number: str) -> list[PoLine]:
    csv_path = _container_po_csv_path(container_number)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"PO CSV not found for {container_number}. Process the container first."
        )
    return parse_container_po_csv(csv_path)


def lookup_container_vendor_id(cursor, container_number: str) -> str:
    prefix = _container_prefix(container_number)
    like = f"{prefix}%"
    cursor.execute(
        """
        SELECT TOP 1 RTRIM(ven_id)
        FROM dbo.pur_ord
        WHERE pur_num LIKE ?
        ORDER BY pur_dt DESC
        """,
        (like,),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return str(row[0]).strip()
    return VENDOR_FALLBACK[prefix]


def resolve_vendor_id(cursor, raw_vendor_id: str) -> tuple[str, str]:
    """Resolve user-entered vendor ID to canonical venfile ID and name."""
    raw = str(raw_vendor_id or "").strip()
    if not raw:
        raise ValueError("Vendor ID is required")

    candidates: list[str] = [raw]
    if raw.isdigit():
        candidates.append(raw.zfill(5))

    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        cursor.execute(
            """
            SELECT TOP 1 RTRIM(VEN_ID), RTRIM(VEN_NM)
            FROM dbo.venfile
            WHERE RTRIM(VEN_ID) = ?
            """,
            (cand,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            return str(row[0]).strip(), str(row[1] or "").strip()

    raise ValueError(f"Vendor ID {raw!r} was not found in OMS")


def lookup_vendor_info(cursor, raw_vendor_id: str) -> dict[str, Any]:
    vendor_id, vendor_name = resolve_vendor_id(cursor, raw_vendor_id)
    return {
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "is_standard": vendor_id in STANDARD_VENDOR_IDS,
    }


def _po_exists(cursor, container_number: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
        """,
        (container_number.strip().upper(),),
    )
    return cursor.fetchone() is not None


def _bin_pur_needs_repair(cursor, container_number: str) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
          AND (
            rcvd_qty IS NULL OR can_qty IS NULL OR rtn_qty IS NULL
            OR comm_ln IS NULL OR price IS NULL OR cost IS NULL
          )
        """,
        (container_number.strip().upper(),),
    )
    return cursor.fetchone() is not None


def _repair_bin_pur_nulls(cursor, container_number: str) -> int:
    cursor.execute(
        """
        UPDATE dbo.bin_pur
        SET rcvd_qty = ISNULL(rcvd_qty, 0),
            can_qty = ISNULL(can_qty, 0),
            rtn_qty = ISNULL(rtn_qty, 0),
            comm_ln = ISNULL(comm_ln, 0),
            price = ISNULL(price, 0),
            cost = ISNULL(cost, 0)
        WHERE RTRIM(pur_num) = ?
          AND (
            rcvd_qty IS NULL OR can_qty IS NULL OR rtn_qty IS NULL
            OR comm_ln IS NULL OR price IS NULL OR cost IS NULL
          )
        """,
        (container_number.strip().upper(),),
    )
    return int(cursor.rowcount or 0)


def _skus_for_container(cursor, container_number: str) -> list[str]:
    cursor.execute(
        """
        SELECT DISTINCT RTRIM(prod_cd)
        FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
        """,
        (container_number.strip().upper(),),
    )
    return [str(row[0]).strip() for row in cursor.fetchall() if row[0]]


def _recalc_on_order_qty_for_skus(cursor, skus: list[str]) -> int:
    """Set inv_data.on_order_qty = open ocean PO qty (sum open bin_pur, skip POs with receives)."""
    normalized = sorted({str(s).strip() for s in skus if str(s).strip()})
    if not normalized:
        return 0
    placeholders = ",".join("?" * len(normalized))
    cursor.execute(
        f"""
        UPDATE id
        SET on_order_qty = ISNULL(open_po.total_open, 0)
        FROM dbo.inv_data id
        INNER JOIN dbo.inv i ON i.prod_cd = id.prod_cd
        LEFT JOIN (
            SELECT RTRIM(bp.prod_cd) AS prod_cd,
                   SUM(ISNULL(bp.po_qty, 0)) AS total_open
            FROM dbo.bin_pur bp
            WHERE bp.pur_cd = {PO_LINE_PUR_CD}
              AND ISNULL(bp.rcvd_qty, 0) = 0
              AND ISNULL(bp.po_qty, 0) > 0
              AND RTRIM(bp.prod_cd) IN ({placeholders})
              AND RTRIM(bp.pur_num) NOT IN (
                  SELECT DISTINCT RTRIM(b2.pur_num)
                  FROM dbo.bin_pur b2
                  WHERE b2.pur_cd = {PO_LINE_PUR_CD}
                    AND RTRIM(b2.prod_cd) = RTRIM(bp.prod_cd)
                    AND ISNULL(b2.rcvd_qty, 0) > 0
              )
            GROUP BY RTRIM(bp.prod_cd)
        ) open_po ON RTRIM(i.prod_cd) = open_po.prod_cd
        WHERE RTRIM(i.prod_cd) IN ({placeholders})
          AND RTRIM(id.whs_num) = ?
          AND ISNULL(id.on_order_qty, 0) <> ISNULL(open_po.total_open, 0)
        """,
        (*normalized, *normalized, DEFAULT_WHS),
    )
    return int(cursor.rowcount or 0)


def _on_order_qty_out_of_sync(cursor, container_number: str) -> bool:
    skus = _skus_for_container(cursor, container_number)
    if not skus:
        return False
    placeholders = ",".join("?" * len(skus))
    cursor.execute(
        f"""
        SELECT 1
        FROM (
            SELECT RTRIM(i.prod_cd) AS prod_cd,
                   ISNULL(id.on_order_qty, 0) AS on_order,
                   ISNULL(open_po.total_open, 0) AS expected
            FROM dbo.inv i
            INNER JOIN dbo.inv_data id ON i.prod_cd = id.prod_cd AND RTRIM(id.whs_num) = ?
            LEFT JOIN (
                SELECT RTRIM(bp.prod_cd) AS prod_cd,
                       SUM(ISNULL(bp.po_qty, 0)) AS total_open
                FROM dbo.bin_pur bp
                WHERE bp.pur_cd = {PO_LINE_PUR_CD}
                  AND ISNULL(bp.rcvd_qty, 0) = 0
                  AND ISNULL(bp.po_qty, 0) > 0
                  AND RTRIM(bp.prod_cd) IN ({placeholders})
                  AND RTRIM(bp.pur_num) NOT IN (
                      SELECT DISTINCT RTRIM(b2.pur_num)
                      FROM dbo.bin_pur b2
                      WHERE b2.pur_cd = {PO_LINE_PUR_CD}
                        AND RTRIM(b2.prod_cd) = RTRIM(bp.prod_cd)
                        AND ISNULL(b2.rcvd_qty, 0) > 0
                  )
                GROUP BY RTRIM(bp.prod_cd)
            ) open_po ON RTRIM(i.prod_cd) = open_po.prod_cd
            WHERE RTRIM(i.prod_cd) IN ({placeholders})
        ) x
        WHERE x.on_order <> x.expected
        """,
        (DEFAULT_WHS, *skus, *skus),
    )
    return cursor.fetchone() is not None


def _sync_on_order_qty_from_po(cursor, container_number: str) -> int:
    """Recalc inv_data.on_order_qty from all open bin_pur lines (not just this container)."""
    skus = _skus_for_container(cursor, container_number)
    return _recalc_on_order_qty_for_skus(cursor, skus)


def _fetch_pur_ord_template(cursor, prefix: str) -> dict | None:
    like = f"{prefix}%"
    cursor.execute(
        """
        SELECT TOP 1 *
        FROM dbo.pur_ord
        WHERE pur_num LIKE ?
        ORDER BY pur_dt DESC
        """,
        (like,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'pur_ord'
        ORDER BY ORDINAL_POSITION
        """
    )
    cols = [col[0] for col in cursor.fetchall()]
    return dict(zip(cols, row))


def _pc_case_lookup(cursor, skus: list[str]) -> dict[str, Decimal]:
    if not skus:
        return {}
    placeholders = ",".join("?" for _ in skus)
    cursor.execute(
        f"""
        SELECT RTRIM(prod_cd), pc_case
        FROM dbo.inv
        WHERE RTRIM(prod_cd) IN ({placeholders})
        """,
        skus,
    )
    return {
        str(row[0]).strip(): _decimal(row[1], Decimal("1"))
        for row in cursor.fetchall()
    }


def _repair_plog_nulls(cursor, container_number: str) -> int:
    cursor.execute(
        """
        UPDATE dbo.plog
        SET hdl_chg = ISNULL(hdl_chg, 0)
        WHERE RTRIM(pur_num) = ?
          AND hdl_chg IS NULL
        """,
        (container_number.strip().upper(),),
    )
    return int(cursor.rowcount or 0)


def _insert_pur_ord_header(
    cursor,
    *,
    container_number: str,
    vendor_id: str,
    pur_amt: Decimal,
    erp_today: int,
) -> None:
    prefix = _container_prefix(container_number)
    template = _fetch_pur_ord_template(cursor, prefix)
    if template is None:
        template = {
            "SP_CD": None,
            "TERM": None,
            "SALES_NUM": pad_char("", 10),
            "DISCOUNT": Decimal("0"),
            "PUR_TAX": Decimal("0"),
            "PAID_AMT": Decimal("0"),
            "NT_SEL": "Y",
            "PUR_ADR1": pad_char("Fashion Distribution Center(FDC)", 60),
            "PUR_ADR2": pad_char("827 S LAWSON ST", 60),
            "PUR_ADR22": pad_char("", 60),
            "PUR_ADR3": pad_char("CITY OF INDUSTRY, CA 91748", 60),
            "PUR_COUNTRY": pad_char("", 50),
            "SHIP_DESC": pad_char("", 25),
            "WHS_NUM": pad_char(DEFAULT_WHS, 6),
            "TERM_DESC": pad_char("", 15),
            "FOB_DESC": pad_char("", 25),
            "PTERM_DESC": pad_char("", 20),
            "ATTN": pad_char("", 20),
            "CUS_ID": pad_char("", 11),
            "CURRENCY": pad_char("", 20),
            "EXCHANGE_RT": Decimal("1"),
            "ORD_NUM": pad_char("", 20),
            "REF_NUM": pad_char("", 30),
            "CONT_NUM": pad_char("", 30),
            "TYPE_CD": pad_char("", 6),
            "RESV_1": pad_char("", 20),
            "MFG_CFM_PO": pad_char("", 20),
        }

    template["PUR_DT"] = erp_today
    template["PUR_NUM"] = pad_char(container_number, 8)
    template["VEN_ID"] = pad_char(vendor_id, 11)
    template["PUR_AMT"] = pur_amt
    template["EST_DT"] = erp_today
    template["ONBD_DT"] = erp_today
    template["WHS_NUM"] = pad_char(DEFAULT_WHS, 6)

    columns = list(template.keys())
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(columns)
    cursor.execute(
        f"INSERT INTO dbo.pur_ord ({col_sql}) VALUES ({placeholders})",
        [template[col] for col in columns],
    )


def _insert_plog_line(
    cursor,
    *,
    container_number: str,
    vendor_id: str,
    line: PoLine,
    line_no: int,
    pc_unit: Decimal,
    erp_today: int,
    log_time: int,
) -> None:
    cursor.execute(
        """
        INSERT INTO dbo.plog (
            LOG_DATE, LOG_TIME, VEN_ID, INVS_NUM, PUR_NUM, PUR_CD, PROD_CD, LOG_QTY,
            BASE_COST, HDL_CHG, EST_DT, PUR_LNT, NT_NUM, COMM_LN, UT_DESC, WHS_NUM, PC_UNIT,
            DEF_UT, EXC_RATE, CURRENCY_AMT, BATCH_NUM
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            erp_today,
            log_time,
            pad_char(vendor_id, 11),
            pad_char("0", 10),
            pad_char(container_number, 8),
            1,
            pad_char(line.prod_cd, 21),
            line.po_qty,
            line.base_cost,
            Decimal("0"),
            erp_today,
            "Y",
            line_no,
            line_no,
            pad_char(container_number, 60),
            pad_char(DEFAULT_WHS, 6),
            pc_unit,
            "PC",
            Decimal("1"),
            line.base_cost,
            0,
        ),
    )


def _insert_bin_pur_line(
    cursor,
    *,
    container_number: str,
    line: PoLine,
    line_no: int,
) -> None:
    cursor.execute(
        """
        INSERT INTO dbo.bin_pur (
            PUR_NUM, PUR_CD, BATCH_NUM, NT_NUM, PROD_CD, WHS_NUM, BIN_CD,
            PO_QTY, RCVD_QTY, CAN_QTY, RTN_QTY, COMM_LN, PRICE, COST
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pad_char(container_number, 8),
            1,
            0,
            line_no,
            pad_char(line.prod_cd, 20),
            pad_char(DEFAULT_WHS, 8),
            pad_char("V", 16),
            line.po_qty,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            0,
            Decimal("0"),
            Decimal("0"),
        ),
    )


def delete_container_po_from_fdc(container_number: str) -> dict[str, Any]:
    """Remove container PO header and lines from FDC (pur_ord, plog, bin_pur)."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "bin_pur_deleted": 0,
        "plog_deleted": 0,
        "pur_ord_deleted": 0,
        "on_order_cleared": 0,
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if not _po_exists(cursor, normalized):
            result["success"] = True
            result["skipped"] = True
            return result

        affected_skus = _skus_for_container(cursor, normalized)

        cursor.execute(
            "DELETE FROM dbo.bin_pur WHERE RTRIM(pur_num) = ?",
            (normalized,),
        )
        result["bin_pur_deleted"] = int(cursor.rowcount or 0)

        cursor.execute(
            "DELETE FROM dbo.plog WHERE RTRIM(pur_num) = ?",
            (normalized,),
        )
        result["plog_deleted"] = int(cursor.rowcount or 0)

        cursor.execute(
            "DELETE FROM dbo.pur_ord WHERE RTRIM(pur_num) = ?",
            (normalized,),
        )
        result["pur_ord_deleted"] = int(cursor.rowcount or 0)

        result["on_order_cleared"] = _recalc_on_order_qty_for_skus(cursor, affected_skus)

        conn.commit()
        result["success"] = True
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def transfer_container_po_to_fdc(
    container_number: str,
    *,
    dry_run: bool = False,
    lines: list[PoLine] | None = None,
    vendor_id: str | None = None,
) -> PoTransferResult:
    normalized = str(container_number).strip().upper()
    result = PoTransferResult(success=True, dry_run=dry_run)

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if _po_exists(cursor, normalized):
            result.vendor_id = lookup_container_vendor_id(cursor, normalized)
            if _bin_pur_needs_repair(cursor, normalized):
                if dry_run:
                    result.repaired = True
                    result.repaired_lines = 1
                    return result
                repaired = _repair_bin_pur_nulls(cursor, normalized)
                synced = _sync_on_order_qty_from_po(cursor, normalized)
                plog_fixed = _repair_plog_nulls(cursor, normalized)
                conn.commit()
                result.repaired = True
                result.repaired_lines = repaired + synced + plog_fixed
                return result
            if _on_order_qty_out_of_sync(cursor, normalized):
                if dry_run:
                    result.repaired = True
                    result.repaired_lines = 1
                    return result
                synced = _sync_on_order_qty_from_po(cursor, normalized)
                plog_fixed = _repair_plog_nulls(cursor, normalized)
                conn.commit()
                result.repaired = True
                result.repaired_lines = synced + plog_fixed
                return result
            plog_fixed = _repair_plog_nulls(cursor, normalized)
            if plog_fixed:
                conn.commit()
                result.repaired = True
                result.repaired_lines = plog_fixed
                return result
            synced = _sync_on_order_qty_from_po(cursor, normalized)
            if synced:
                conn.commit()
                result.repaired = True
                result.repaired_lines = synced
            result.skipped = True
            return result

        po_lines = lines or fetch_po_lines_from_container_csv(normalized)
        if not po_lines:
            result.success = False
            result.errors.append("No PO lines to import")
            return result

        if vendor_id:
            resolved_id, _ = resolve_vendor_id(cursor, vendor_id)
            vendor_id = resolved_id
        else:
            vendor_id = lookup_container_vendor_id(cursor, normalized)
        result.vendor_id = vendor_id
        pur_amt = sum((line.po_qty * line.base_cost for line in po_lines), Decimal("0"))
        result.pur_amt = pur_amt
        pc_cases = _pc_case_lookup(cursor, [line.prod_cd for line in po_lines])

        if dry_run:
            result.inserted_lines = [line.prod_cd for line in po_lines]
            return result

        erp_today = erp_date(cursor)
        log_time = erp_log_time(cursor)
        _insert_pur_ord_header(
            cursor,
            container_number=normalized,
            vendor_id=vendor_id,
            pur_amt=pur_amt,
            erp_today=erp_today,
        )

        for idx, line in enumerate(po_lines, start=1):
            pc_unit = pc_cases.get(line.prod_cd.strip(), Decimal("1"))
            _insert_plog_line(
                cursor,
                container_number=normalized,
                vendor_id=vendor_id,
                line=line,
                line_no=idx,
                pc_unit=pc_unit,
                erp_today=erp_today,
                log_time=log_time,
            )
            _insert_bin_pur_line(
                cursor,
                container_number=normalized,
                line=line,
                line_no=idx,
            )
            result.inserted_lines.append(line.prod_cd)

        synced = _sync_on_order_qty_from_po(cursor, normalized)
        if synced:
            result.repaired_lines = synced

        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        result.success = False
        result.errors.append(str(exc))
        result.inserted_lines.clear()
        return result
    finally:
        conn.close()


def receive_container_po_at_827(container_number: str) -> dict[str, Any]:
    """Receive open PO lines at 827: bin_pur RCVD_QTY → PO_QTY and update inv_data stock."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "lines_received": 0,
        "qty_received": 0.0,
        "received_date": datetime.now().strftime("%m/%d/%Y"),
        "already_landed": False,
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if not _po_exists(cursor, normalized):
            result["errors"].append(f"No PO in OMS for {normalized}")
            return result

        cursor.execute(
            """
            SELECT RTRIM(prod_cd),
                   SUM(ISNULL(po_qty, 0)) AS po_qty,
                   SUM(ISNULL(rcvd_qty, 0)) AS rcvd_qty
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd = ?
            GROUP BY RTRIM(prod_cd)
            HAVING SUM(ISNULL(po_qty, 0)) > SUM(ISNULL(rcvd_qty, 0))
            """,
            (normalized, PO_LINE_PUR_CD),
        )
        lines = cursor.fetchall()
        if not lines:
            result["success"] = True
            result["already_landed"] = True
            return result

        erp_today = erp_date(cursor)
        lines_received = 0
        qty_received = Decimal("0")
        affected_skus: list[str] = []

        for prod_cd, po_qty, rcvd_qty in lines:
            po_qty = _decimal(po_qty)
            rcvd_qty = _decimal(rcvd_qty)
            receive_delta = po_qty - rcvd_qty
            if receive_delta <= 0:
                continue

            sku = str(prod_cd).strip()
            affected_skus.append(sku)

            cursor.execute(
                """
                UPDATE dbo.bin_pur
                SET rcvd_qty = po_qty
                WHERE RTRIM(pur_num) = ?
                  AND RTRIM(prod_cd) = ?
                  AND ISNULL(rcvd_qty, 0) < ISNULL(po_qty, 0)
                """,
                (normalized, sku),
            )

            cursor.execute(
                """
                UPDATE id
                SET in_stock = ISNULL(id.in_stock, 0) + ?,
                    lastrcv_qty = ?,
                    lastrcv_dt = ?
                FROM dbo.inv_data id
                INNER JOIN dbo.inv i ON i.prod_cd = id.prod_cd
                WHERE RTRIM(i.prod_cd) = ?
                  AND RTRIM(id.whs_num) = ?
                """,
                (
                    receive_delta,
                    receive_delta,
                    erp_today,
                    sku,
                    DEFAULT_WHS,
                ),
            )
            lines_received += 1
            qty_received += receive_delta

        _recalc_on_order_qty_for_skus(cursor, affected_skus)

        conn.commit()
        result["success"] = True
        result["lines_received"] = lines_received
        result["qty_received"] = float(qty_received)
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def revert_container_to_ocean(container_number: str) -> dict[str, Any]:
    """Undo 827 receive: zero bin_pur RCVD_QTY and restore inv_data on-order stock."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "lines_reverted": 0,
        "qty_reverted": 0.0,
        "already_ocean": False,
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if not _po_exists(cursor, normalized):
            result["errors"].append(f"No PO in OMS for {normalized}")
            return result

        cursor.execute(
            """
            SELECT RTRIM(prod_cd), SUM(ISNULL(rcvd_qty, 0)) AS rcvd_qty
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd = ?
            GROUP BY RTRIM(prod_cd)
            HAVING SUM(ISNULL(rcvd_qty, 0)) > 0
            """,
            (normalized, PO_LINE_PUR_CD),
        )
        lines = cursor.fetchall()
        if not lines:
            result["success"] = True
            result["already_ocean"] = True
            return result

        lines_reverted = 0
        qty_reverted = Decimal("0")
        affected_skus: list[str] = []

        for prod_cd, rcvd_qty in lines:
            rcvd_qty = _decimal(rcvd_qty)
            if rcvd_qty <= 0:
                continue
            sku = str(prod_cd).strip()
            affected_skus.append(sku)

            cursor.execute(
                """
                UPDATE dbo.bin_pur
                SET rcvd_qty = 0
                WHERE RTRIM(pur_num) = ?
                  AND RTRIM(prod_cd) = ?
                  AND ISNULL(rcvd_qty, 0) > 0
                """,
                (normalized, sku),
            )

            cursor.execute(
                """
                UPDATE id
                SET in_stock = CASE
                        WHEN ISNULL(id.in_stock, 0) >= ? THEN ISNULL(id.in_stock, 0) - ?
                        ELSE 0
                    END
                FROM dbo.inv_data id
                INNER JOIN dbo.inv i ON i.prod_cd = id.prod_cd
                WHERE RTRIM(i.prod_cd) = ?
                  AND RTRIM(id.whs_num) = ?
                """,
                (
                    rcvd_qty,
                    rcvd_qty,
                    sku,
                    DEFAULT_WHS,
                ),
            )

            lines_reverted += 1
            qty_reverted += rcvd_qty

        _recalc_on_order_qty_for_skus(cursor, affected_skus)

        conn.commit()
        result["success"] = True
        result["lines_reverted"] = lines_reverted
        result["qty_reverted"] = float(qty_reverted)
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def repair_doubled_receive_stock(container_number: str) -> dict[str, Any]:
    """Undo duplicate stock from PUR_CD=2 bin_pur rows created alongside PO import lines."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "skus_repaired": 0,
        "qty_corrected": 0.0,
        "bin_pur_deleted": 0,
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT RTRIM(prod_cd), SUM(ISNULL(rcvd_qty, 0)) AS excess_qty
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd <> ?
              AND ISNULL(rcvd_qty, 0) > 0
            GROUP BY RTRIM(prod_cd)
            """,
            (normalized, PO_LINE_PUR_CD),
        )
        excess_rows = cursor.fetchall()
        if not excess_rows:
            result["success"] = True
            return result

        qty_corrected = Decimal("0")
        for prod_cd, excess_qty in excess_rows:
            excess_qty = _decimal(excess_qty)
            if excess_qty <= 0:
                continue
            sku = str(prod_cd).strip()

            cursor.execute(
                """
                UPDATE id
                SET in_stock = CASE
                        WHEN ISNULL(id.in_stock, 0) >= ? THEN ISNULL(id.in_stock, 0) - ?
                        ELSE 0
                    END
                FROM dbo.inv_data id
                INNER JOIN dbo.inv i ON i.prod_cd = id.prod_cd
                WHERE RTRIM(i.prod_cd) = ?
                  AND RTRIM(id.whs_num) = ?
                """,
                (excess_qty, excess_qty, sku, DEFAULT_WHS),
            )
            qty_corrected += excess_qty
            result["skus_repaired"] += 1

        cursor.execute(
            """
            DELETE FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd <> ?
              AND ISNULL(rcvd_qty, 0) > 0
            """,
            (normalized, PO_LINE_PUR_CD),
        )
        result["bin_pur_deleted"] = int(cursor.rowcount or 0)

        conn.commit()
        result["success"] = True
        result["qty_corrected"] = float(qty_corrected)
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def cleanup_orphan_bin_pur_rows(container_number: str) -> dict[str, Any]:
    """Remove PUR_CD<>1 bin_pur allocation stubs (rcvd=0) left by OMS native receive."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "bin_pur_deleted": 0,
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd <> ?
              AND ISNULL(rcvd_qty, 0) = 0
            """,
            (normalized, PO_LINE_PUR_CD),
        )
        result["bin_pur_deleted"] = int(cursor.rowcount or 0)
        conn.commit()
        result["success"] = True
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()
