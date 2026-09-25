"""Import container PO lines into FDC only (pur_ord, plog, bin_pur)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from py.utilities.database_utils import get_sql_server_connection_fdc
from py.utilities.inventory_adjustment import erp_date, erp_log_time, pad_char
from py.utilities.style_efas_transfer import CONTAINER_CSV_DIR, _decimal

DEFAULT_WHS = "01"
# PUR_CD=1 lines are container PO imports.
# PUR_CD=2 are OMS receive allocations: same PO_QTY as CD=1, RCVD_QTY=0, plus a
# second plog batch (LOG_QTY=po, HDL_CHG=0) dated at receive time.
PO_LINE_PUR_CD = 1
RECEIVE_ALLOC_PUR_CD = 2
_RECEIVE_TIMES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "output",
    "boat_cache",
    "receive_times.json",
)
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


def _table_data_columns(cursor, table_name: str) -> list[str]:
    cursor.execute(
        """
        SELECT c.COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS c
        WHERE c.TABLE_SCHEMA = 'dbo'
          AND c.TABLE_NAME = ?
          AND ISNULL(
              COLUMNPROPERTY(
                  OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME),
                  c.COLUMN_NAME,
                  'IsIdentity'
              ),
              0
          ) = 0
          AND ISNULL(
              COLUMNPROPERTY(
                  OBJECT_ID(c.TABLE_SCHEMA + '.' + c.TABLE_NAME),
                  c.COLUMN_NAME,
                  'IsComputed'
              ),
              0
          ) = 0
        ORDER BY c.ORDINAL_POSITION
        """,
        (table_name,),
    )
    return [str(row[0]) for row in cursor.fetchall()]


def _existing_receive_batch_num(cursor, container_number: str) -> int | None:
    normalized = str(container_number).strip().upper()
    cursor.execute(
        """
        SELECT MAX(batch_num)
        FROM (
            SELECT batch_num
            FROM dbo.bin_pur
            WHERE RTRIM(pur_num) = ?
              AND pur_cd = ?
              AND ISNULL(batch_num, 0) > 0
            UNION ALL
            SELECT batch_num
            FROM dbo.plog
            WHERE RTRIM(pur_num) = ?
              AND pur_cd = ?
              AND ISNULL(batch_num, 0) > 0
        ) t
        """,
        (normalized, RECEIVE_ALLOC_PUR_CD, normalized, RECEIVE_ALLOC_PUR_CD),
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def _next_oms_receive_batch_num(cursor) -> int:
    cursor.execute(
        f"""
        SELECT ISNULL(MAX(batch_num), 0)
        FROM (
            SELECT batch_num
            FROM dbo.bin_pur
            WHERE pur_cd = {RECEIVE_ALLOC_PUR_CD}
              AND ISNULL(batch_num, 0) > 0
            UNION ALL
            SELECT batch_num
            FROM dbo.plog
            WHERE pur_cd = {RECEIVE_ALLOC_PUR_CD}
              AND ISNULL(batch_num, 0) > 0
        ) t
        """
    )
    return int(cursor.fetchone()[0] or 0) + 1


def _cd1_fully_received(cursor, container_number: str) -> bool:
    cursor.execute(
        """
        SELECT SUM(ISNULL(po_qty, 0)), SUM(ISNULL(rcvd_qty, 0))
        FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
          AND pur_cd = ?
        """,
        (str(container_number).strip().upper(), PO_LINE_PUR_CD),
    )
    row = cursor.fetchone()
    if not row:
        return False
    po_qty = _decimal(row[0])
    rcvd_qty = _decimal(row[1])
    return po_qty > 0 and rcvd_qty >= po_qty


def _copy_bin_pur_receive_rows(cursor, container_number: str, batch_num: int) -> int:
    normalized = str(container_number).strip().upper()
    cols = _table_data_columns(cursor, "bin_pur")
    select_parts: list[str] = []
    params: list[Any] = []
    for col in cols:
        key = col.upper()
        if key == "PUR_CD":
            select_parts.append(str(RECEIVE_ALLOC_PUR_CD))
        elif key == "BATCH_NUM":
            select_parts.append("?")
            params.append(batch_num)
        elif key == "RCVD_QTY":
            select_parts.append("0")
        else:
            select_parts.append(f"src.{col}")
    params.append(normalized)
    cursor.execute(
        f"""
        INSERT INTO dbo.bin_pur ({", ".join(cols)})
        SELECT {", ".join(select_parts)}
        FROM dbo.bin_pur src
        WHERE RTRIM(src.pur_num) = ?
          AND src.pur_cd = {PO_LINE_PUR_CD}
          AND NOT EXISTS (
              SELECT 1
              FROM dbo.bin_pur dst
              WHERE RTRIM(dst.pur_num) = RTRIM(src.pur_num)
                AND dst.pur_cd = {RECEIVE_ALLOC_PUR_CD}
                AND RTRIM(dst.prod_cd) = RTRIM(src.prod_cd)
                AND ISNULL(dst.nt_num, 0) = ISNULL(src.nt_num, 0)
          )
        """,
        params,
    )
    return int(cursor.rowcount or 0)


def _copy_plog_receive_rows(
    cursor,
    container_number: str,
    *,
    batch_num: int,
    erp_today: int,
    log_time: int,
) -> int:
    normalized = str(container_number).strip().upper()
    cols = _table_data_columns(cursor, "plog")
    select_parts: list[str] = []
    params: list[Any] = []
    for col in cols:
        key = col.upper()
        if key == "LOG_DATE":
            select_parts.append("?")
            params.append(erp_today)
        elif key == "LOG_TIME":
            select_parts.append("?")
            params.append(log_time)
        elif key == "INVS_NUM":
            select_parts.append("?")
            params.append(pad_char(normalized, 10))
        elif key == "PUR_CD":
            select_parts.append(str(RECEIVE_ALLOC_PUR_CD))
        elif key == "HDL_CHG":
            select_parts.append("0")
        elif key == "EST_DT":
            select_parts.append("?")
            params.append(batch_num)
        elif key == "PUR_LNT":
            select_parts.append("' '")
        elif key == "BATCH_NUM":
            select_parts.append("?")
            params.append(batch_num)
        elif key == "CAL_SEL":
            select_parts.append("3")
        elif key == "AVG_COST":
            select_parts.append("ISNULL(src.BASE_COST, 0)")
        else:
            select_parts.append(f"src.{col}")
    params.append(normalized)
    cursor.execute(
        f"""
        INSERT INTO dbo.plog ({", ".join(cols)})
        SELECT {", ".join(select_parts)}
        FROM dbo.plog src
        WHERE RTRIM(src.pur_num) = ?
          AND src.pur_cd = {PO_LINE_PUR_CD}
          AND NOT EXISTS (
              SELECT 1
              FROM dbo.plog dst
              WHERE RTRIM(dst.pur_num) = RTRIM(src.pur_num)
                AND dst.pur_cd = {RECEIVE_ALLOC_PUR_CD}
                AND RTRIM(dst.prod_cd) = RTRIM(src.prod_cd)
                AND ISNULL(dst.nt_num, 0) = ISNULL(src.nt_num, 0)
          )
        """,
        params,
    )
    return int(cursor.rowcount or 0)


def _delete_oms_receive_rows(cursor, container_number: str) -> tuple[int, int]:
    normalized = str(container_number).strip().upper()
    cursor.execute(
        """
        DELETE FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
          AND pur_cd = ?
        """,
        (normalized, RECEIVE_ALLOC_PUR_CD),
    )
    bin_deleted = int(cursor.rowcount or 0)
    cursor.execute(
        """
        DELETE FROM dbo.plog
        WHERE RTRIM(pur_num) = ?
          AND pur_cd = ?
        """,
        (normalized, RECEIVE_ALLOC_PUR_CD),
    )
    plog_deleted = int(cursor.rowcount or 0)
    return bin_deleted, plog_deleted


def _erp_datetime_from_serial(erp_dt: int) -> datetime:
    return datetime(2023, 1, 1) + timedelta(days=int(erp_dt) - 81088)


def _lookup_container_receive_at(cursor, container_number: str) -> datetime:
    """Best clock for a historical receive: packing-slip cache, else LASTRCV_DT, else now."""
    normalized = str(container_number).strip().upper()
    try:
        if os.path.isfile(_RECEIVE_TIMES_PATH):
            with open(_RECEIVE_TIMES_PATH, encoding="utf-8") as handle:
                store = json.load(handle)
            info = ((store or {}).get("containers") or {}).get(normalized) or {}
            iso = str(info.get("sort") or "").strip()
            if iso:
                return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass

    cursor.execute(
        """
        SELECT MIN(id.lastrcv_dt)
        FROM dbo.bin_pur bp
        INNER JOIN dbo.inv_data id
            ON RTRIM(id.prod_cd) = RTRIM(bp.prod_cd)
           AND RTRIM(id.whs_num) = ?
        WHERE RTRIM(bp.pur_num) = ?
          AND bp.pur_cd = ?
          AND ISNULL(bp.rcvd_qty, 0) > 0
          AND id.lastrcv_dt IS NOT NULL
          AND CAST(id.lastrcv_dt AS INT) > 0
        """,
        (DEFAULT_WHS, normalized, PO_LINE_PUR_CD),
    )
    row = cursor.fetchone()
    if row and row[0]:
        return _erp_datetime_from_serial(int(row[0]))
    return datetime.now()


def _ensure_oms_receive_pattern(
    cursor,
    container_number: str,
    *,
    received_at: datetime | None = None,
    insert_plog: bool | None = None,
) -> dict[str, Any]:
    """Insert missing native OMS receive rows (PUR_CD=2 bin_pur + plog). No stock change."""
    normalized = str(container_number).strip().upper()
    when = received_at or datetime.now()
    batch_num = _existing_receive_batch_num(cursor, normalized)
    allocated_batch = False
    if batch_num is None:
        batch_num = _next_oms_receive_batch_num(cursor)
        allocated_batch = True

    bin_inserted = _copy_bin_pur_receive_rows(cursor, normalized, batch_num)
    should_insert_plog = (
        _cd1_fully_received(cursor, normalized) if insert_plog is None else insert_plog
    )
    plog_inserted = 0
    if should_insert_plog:
        plog_inserted = _copy_plog_receive_rows(
            cursor,
            normalized,
            batch_num=batch_num,
            erp_today=erp_date(cursor, when),
            log_time=erp_log_time(cursor, when),
        )
    if allocated_batch and bin_inserted == 0 and plog_inserted == 0:
        batch_num = 0
    return {
        "batch_num": batch_num,
        "bin_pur_inserted": bin_inserted,
        "plog_inserted": plog_inserted,
    }


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
        "ap_invoice_deleted": False,
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

        ap = _delete_ap_invoice(cursor, normalized)
        result["ap_invoice_deleted"] = bool(ap.get("deleted"))
        if ap.get("error"):
            result["errors"].append(f"A/P invoice: {ap['error']}")
            # Paid A/P blocks PO delete so we do not orphan payments.
            if not ap.get("skipped"):
                conn.rollback()
                return result

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


def _sync_plog_received_for_skus(cursor, container_number: str, skus: list[str]) -> int:
    """Set plog.hdl_chg = log_qty for received SKUs so product-detail Ocean drops them."""
    normalized = str(container_number).strip().upper()
    updated = 0
    for sku in skus:
        sku_key = str(sku).strip()
        if not sku_key:
            continue
        cursor.execute(
            """
            UPDATE dbo.plog
            SET hdl_chg = ISNULL(log_qty, 0)
            WHERE RTRIM(pur_num) = ?
              AND RTRIM(prod_cd) = ?
              AND pur_cd = ?
              AND ISNULL(hdl_chg, 0) = 0
              AND ISNULL(log_qty, 0) > 0
            """,
            (normalized, sku_key, PO_LINE_PUR_CD),
        )
        updated += int(cursor.rowcount or 0)
    return updated


def _sync_plog_ocean_for_skus(cursor, container_number: str, skus: list[str]) -> int:
    """Clear plog.hdl_chg when un-landing so lines return to Ocean."""
    normalized = str(container_number).strip().upper()
    updated = 0
    for sku in skus:
        sku_key = str(sku).strip()
        if not sku_key:
            continue
        cursor.execute(
            """
            UPDATE dbo.plog
            SET hdl_chg = 0
            WHERE RTRIM(pur_num) = ?
              AND RTRIM(prod_cd) = ?
              AND pur_cd = ?
              AND ISNULL(hdl_chg, 0) <> 0
            """,
            (normalized, sku_key, PO_LINE_PUR_CD),
        )
        updated += int(cursor.rowcount or 0)
    return updated


def _container_has_received_plog(cursor, container_number: str) -> bool:
    """True if OMS already wrote receive lines (hdl_chg>0) for this PO."""
    cursor.execute(
        """
        SELECT TOP 1 1
        FROM dbo.plog
        WHERE RTRIM(pur_num) = ?
          AND ISNULL(hdl_chg, 0) > 0
        """,
        (str(container_number).strip().upper(),),
    )
    return cursor.fetchone() is not None


def _sync_plog_for_fully_received_container(cursor, container_number: str) -> int:
    """Backfill plog.hdl_chg for SKUs already fully received in bin_pur but still ocean in plog.

    Skips POs that already have OMS receive plog rows (hdl_chg>0) to avoid
    double-counting Hist-Received.
    """
    normalized = str(container_number).strip().upper()
    if _container_has_received_plog(cursor, normalized):
        return 0
    cursor.execute(
        """
        SELECT RTRIM(prod_cd)
        FROM dbo.bin_pur
        WHERE RTRIM(pur_num) = ?
          AND pur_cd = ?
        GROUP BY RTRIM(prod_cd)
        HAVING SUM(ISNULL(po_qty, 0)) > 0
           AND SUM(ISNULL(rcvd_qty, 0)) >= SUM(ISNULL(po_qty, 0))
        """,
        (normalized, PO_LINE_PUR_CD),
    )
    skus = [str(row[0]).strip() for row in cursor.fetchall() if row[0]]
    return _sync_plog_received_for_skus(cursor, normalized, skus)


# A/P GL accounts used by native OMS container invoices (vendor 50005 / 00005).
_AP_LIABILITY_CHT = 21120
_AP_INVENTORY_CHT = "51200"
_AP_INVENTORY_DESC = "INVENTORY-PURCHASE"
_AP_OPEN_BY_DEFAULT = "DENNIS"
# L-vendor terms observed on recent L2026* acctpbl rows.
_AP_L_VENDOR_DUE_DAYS = 45


def _ap_open_by(opened_by: str | None) -> str:
    text = (opened_by or _AP_OPEN_BY_DEFAULT).strip().upper() or _AP_OPEN_BY_DEFAULT
    return pad_char(text, 8).strip()


def _ap_memo_line(container_number: str, invs_dt: int) -> str:
    when = _erp_datetime_from_serial(invs_dt)
    # Match OMS memo style: "P/O# F261107  6/18/2026"
    return f"P/O# {container_number}  {when.month}/{when.day:02d}/{when.year}"


def _ap_invoice_exists(cursor, container_number: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM dbo.acctpbl WHERE RTRIM(INVS_NUM) = ?",
        (str(container_number).strip().upper(),),
    )
    return cursor.fetchone() is not None


def _next_ap_batch_num(cursor) -> int:
    cursor.execute("SELECT ISNULL(MAX(BAT_NUM), 0) FROM dbo.acctpbl")
    return int(cursor.fetchone()[0] or 0) + 1


def _ap_amount_and_receive_stamp(
    cursor, container_number: str
) -> tuple[Decimal, int, int] | None:
    """Invoice amount + receive date/time from PUR_CD=2 plog (native receive batch)."""
    normalized = str(container_number).strip().upper()
    cursor.execute(
        """
        SELECT SUM(ISNULL(LOG_QTY, 0) * ISNULL(BASE_COST, 0)),
               MAX(LOG_DATE),
               MIN(LOG_TIME),
               COUNT(*)
        FROM dbo.plog
        WHERE RTRIM(PUR_NUM) = ?
          AND PUR_CD = ?
        """,
        (normalized, RECEIVE_ALLOC_PUR_CD),
    )
    row = cursor.fetchone()
    if not row or not row[3]:
        return None
    amt = _decimal(row[0]).quantize(Decimal("0.01"))
    if amt <= 0:
        return None
    return amt, int(row[1]), int(row[2] or 0)


def _ensure_ap_invoice(
    cursor,
    container_number: str,
    *,
    opened_by: str | None = None,
) -> dict[str, Any]:
    """Insert acctpbl + pbl_ins + pbl_dist for a landed container if missing.

    Amount/date come from the OMS receive plog (PUR_CD=2), matching how DENNIS
    enters container A/P invoices in OMS.
    """
    normalized = str(container_number).strip().upper()
    out: dict[str, Any] = {
        "inserted": False,
        "skipped": False,
        "invs_amt": 0.0,
        "bat_num": 0,
        "error": "",
    }
    if _ap_invoice_exists(cursor, normalized):
        out["skipped"] = True
        return out

    stamp = _ap_amount_and_receive_stamp(cursor, normalized)
    if stamp is None:
        out["error"] = "no receive plog amount"
        return out
    amt, invs_dt, invs_tm = stamp

    cursor.execute(
        "SELECT RTRIM(VEN_ID) FROM dbo.pur_ord WHERE RTRIM(PUR_NUM) = ?",
        (normalized,),
    )
    ven_row = cursor.fetchone()
    if not ven_row or not ven_row[0]:
        out["error"] = "no vendor on pur_ord"
        return out
    vend_id = str(ven_row[0]).strip()
    # L/00005 uses ~net-45; F/50005 due date = invoice date.
    due_dt = (
        invs_dt + _AP_L_VENDOR_DUE_DAYS
        if vend_id == VENDOR_FALLBACK["L"]
        else invs_dt
    )

    open_by = _ap_open_by(opened_by)
    bat_num = _next_ap_batch_num(cursor)
    memo = _ap_memo_line(normalized, invs_dt)
    pord = pad_char(normalized, 8).strip()

    cursor.execute(
        """
        INSERT INTO dbo.acctpbl (
            VEND_ID, INVS_NUM, INVS_CD, PORD_NUM, BAT_NUM,
            RCV_DT, INVS_DT, INVS_TM, OPEN_NUM, BUS_CD,
            SHIP_CD, HANDL_CD, MISC_CD, TAX_CD, DISC_CD, DUE_DT,
            INVS_AMT, INVS_TAX, MISC_CHG, HANDL_FEE, SHIP_FEE,
            DISCNT_AMT, DISCNT_RT, REFER_NUM, VEN_INVS_DT, WHS_NUM,
            CALC_SEL, CONT_NUM, CONT_TYPE, CONT_RT, CONT_WT, CONT_VOL,
            LAST_UPDT, LAST_OPEN
        ) VALUES (
            ?, ?, 1, ?, ?,
            ?, ?, ?, ?, 1,
            0, 0, 0, 0, 0, ?,
            ?, 0, 0, 0, 0,
            0, 0, '', ?, '',
            0, '', 0, 0, 0, 0,
            0, ''
        )
        """,
        (
            vend_id,
            normalized,
            pord,
            bat_num,
            invs_dt,
            invs_dt,
            invs_tm,
            open_by,
            due_dt,
            amt,
            invs_dt,
        ),
    )
    cursor.execute(
        """
        INSERT INTO dbo.pbl_ins (
            VEND_ID, INVS_NUM, PO_NUM, INVS_DT, INVS_TM, VEN_INVS_DT,
            CHT_NUM, GL_ACT, DUE_DT, REFER_NUM, NOTE_NUM,
            INVS_AMT, DISCNT_AMT, BAL_AMT, OPEN_NUM, AP_PT, INS_VAL,
            LAST_UPDT, LAST_OPEN, PROJ_NUM
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, '', 0,
            ?, 0, ?, ?, 0, 0,
            0, '', ''
        )
        """,
        (
            vend_id,
            normalized,
            normalized,
            invs_dt,
            invs_tm,
            invs_dt,
            _AP_LIABILITY_CHT,
            str(_AP_LIABILITY_CHT),
            due_dt,
            amt,
            amt,
            open_by,
        ),
    )
    cursor.execute(
        """
        INSERT INTO dbo.pbl_dist (
            VEND_ID, INVS_NUM, INVS_DT, CHT_NUM, GL_ACT, CHT_DESC,
            MEMO_LN, CHT_AMT, INS_VAL, COMM_LN
        ) VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, 0, 1
        )
        """,
        (
            vend_id,
            normalized,
            invs_dt,
            _AP_INVENTORY_CHT,
            _AP_INVENTORY_CHT,
            _AP_INVENTORY_DESC,
            memo[:40],
            amt,
        ),
    )
    out["inserted"] = True
    out["invs_amt"] = float(amt)
    out["bat_num"] = bat_num
    return out


def _delete_ap_invoice(cursor, container_number: str) -> dict[str, Any]:
    """Remove unpaid A/P invoice rows for a container (acctpbl / pbl_ins / pbl_dist)."""
    normalized = str(container_number).strip().upper()
    out: dict[str, Any] = {
        "deleted": False,
        "skipped": False,
        "acctpbl": 0,
        "pbl_ins": 0,
        "pbl_dist": 0,
        "error": "",
    }
    if not _ap_invoice_exists(cursor, normalized):
        out["skipped"] = True
        return out

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM dbo.pbl_trs
        WHERE RTRIM(INVS_NUM) = ?
        """,
        (normalized,),
    )
    if int(cursor.fetchone()[0] or 0) > 0:
        out["error"] = "has payments in pbl_trs"
        return out

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM dbo.pbl_ins
        WHERE RTRIM(INVS_NUM) = ?
          AND ISNULL(BAL_AMT, 0) < ISNULL(INVS_AMT, 0)
        """,
        (normalized,),
    )
    if int(cursor.fetchone()[0] or 0) > 0:
        out["error"] = "balance already reduced"
        return out

    cursor.execute("DELETE FROM dbo.pbl_dist WHERE RTRIM(INVS_NUM) = ?", (normalized,))
    out["pbl_dist"] = int(cursor.rowcount or 0)
    cursor.execute("DELETE FROM dbo.pbl_ins WHERE RTRIM(INVS_NUM) = ?", (normalized,))
    out["pbl_ins"] = int(cursor.rowcount or 0)
    cursor.execute("DELETE FROM dbo.acctpbl WHERE RTRIM(INVS_NUM) = ?", (normalized,))
    out["acctpbl"] = int(cursor.rowcount or 0)
    out["deleted"] = bool(out["acctpbl"] or out["pbl_ins"] or out["pbl_dist"])
    return out


def receive_container_po_at_827(
    container_number: str,
    *,
    opened_by: str | None = None,
) -> dict[str, Any]:
    """Receive open PO lines at 827 the way native OMS does.

    Sets PUR_CD=1 rcvd_qty and inv_data stock, flips original plog.hdl_chg,
    then inserts PUR_CD=2 allocation rows (rcvd=0) and a second plog batch
    dated now with HDL_CHG=0 so Hist-Received is not doubled.
    Also creates the matching A/P invoice (acctpbl / pbl_ins / pbl_dist) when missing.
    """
    normalized = str(container_number).strip().upper()
    received_at = datetime.now()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "lines_received": 0,
        "qty_received": 0.0,
        "plog_synced": 0,
        "bin_pur_alloc_inserted": 0,
        "plog_receive_inserted": 0,
        "receive_batch_num": 0,
        "ap_invoice_inserted": False,
        "ap_invoice_amt": 0.0,
        "received_date": received_at.strftime("%m/%d/%Y"),
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
            # Already landed in bin_pur — repair ocean hdl_chg, receive rows, and A/P.
            plog_synced = _sync_plog_for_fully_received_container(cursor, normalized)
            pattern = _ensure_oms_receive_pattern(
                cursor, normalized, received_at=_lookup_container_receive_at(cursor, normalized)
            )
            ap = _ensure_ap_invoice(cursor, normalized, opened_by=opened_by)
            changed = (
                plog_synced
                or pattern["bin_pur_inserted"]
                or pattern["plog_inserted"]
                or ap.get("inserted")
            )
            if changed:
                conn.commit()
            result["success"] = True
            result["already_landed"] = True
            result["plog_synced"] = plog_synced
            result["bin_pur_alloc_inserted"] = pattern["bin_pur_inserted"]
            result["plog_receive_inserted"] = pattern["plog_inserted"]
            result["receive_batch_num"] = pattern["batch_num"]
            result["ap_invoice_inserted"] = bool(ap.get("inserted"))
            result["ap_invoice_amt"] = float(ap.get("invs_amt") or 0)
            if ap.get("error"):
                result["errors"].append(f"A/P invoice: {ap['error']}")
            return result

        erp_today = erp_date(cursor, received_at)
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
                  AND pur_cd = ?
                  AND RTRIM(prod_cd) = ?
                  AND ISNULL(rcvd_qty, 0) < ISNULL(po_qty, 0)
                """,
                (normalized, PO_LINE_PUR_CD, sku),
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

        plog_synced = _sync_plog_received_for_skus(cursor, normalized, affected_skus)
        pattern = _ensure_oms_receive_pattern(
            cursor, normalized, received_at=received_at, insert_plog=True
        )
        _recalc_on_order_qty_for_skus(cursor, affected_skus)
        ap = _ensure_ap_invoice(cursor, normalized, opened_by=opened_by)

        conn.commit()
        result["success"] = True
        result["lines_received"] = lines_received
        result["qty_received"] = float(qty_received)
        result["plog_synced"] = plog_synced
        result["bin_pur_alloc_inserted"] = pattern["bin_pur_inserted"]
        result["plog_receive_inserted"] = pattern["plog_inserted"]
        result["receive_batch_num"] = pattern["batch_num"]
        result["ap_invoice_inserted"] = bool(ap.get("inserted"))
        result["ap_invoice_amt"] = float(ap.get("invs_amt") or 0)
        if ap.get("error"):
            result["errors"].append(f"A/P invoice: {ap['error']}")
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def revert_container_to_ocean(container_number: str) -> dict[str, Any]:
    """Undo 827 receive: remove OMS receive rows, zero CD1 RCVD_QTY, restore stock."""
    normalized = str(container_number).strip().upper()
    result: dict[str, Any] = {
        "success": False,
        "container": normalized,
        "lines_reverted": 0,
        "qty_reverted": 0.0,
        "plog_synced": 0,
        "bin_pur_alloc_deleted": 0,
        "plog_receive_deleted": 0,
        "ap_invoice_deleted": False,
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
        bin_deleted, plog_deleted = _delete_oms_receive_rows(cursor, normalized)
        result["bin_pur_alloc_deleted"] = bin_deleted
        result["plog_receive_deleted"] = plog_deleted
        ap = _delete_ap_invoice(cursor, normalized)
        result["ap_invoice_deleted"] = bool(ap.get("deleted"))
        if ap.get("error"):
            result["errors"].append(f"A/P invoice: {ap['error']}")
        if not lines:
            if bin_deleted or plog_deleted or ap.get("deleted"):
                conn.commit()
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
                  AND pur_cd = ?
                  AND RTRIM(prod_cd) = ?
                  AND ISNULL(rcvd_qty, 0) > 0
                """,
                (normalized, PO_LINE_PUR_CD, sku),
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

        plog_synced = _sync_plog_ocean_for_skus(cursor, normalized, affected_skus)
        _recalc_on_order_qty_for_skus(cursor, affected_skus)

        conn.commit()
        result["success"] = True
        result["lines_reverted"] = lines_reverted
        result["qty_reverted"] = float(qty_reverted)
        result["plog_synced"] = plog_synced
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def backfill_plog_hdl_chg_for_landed_containers(
    container_numbers: list[str] | None = None,
) -> dict[str, Any]:
    """Set plog.hdl_chg for boat-landed containers still ocean in plog.

    Only touches POs with no existing hdl_chg>0 lines, so native OMS receives
    (which add a second plog row) are not double-counted in Hist-Received.
    """
    result: dict[str, Any] = {
        "success": False,
        "containers_checked": 0,
        "containers_fixed": 0,
        "containers_skipped_oms_receive": 0,
        "plog_lines_updated": 0,
        "fixed": [],
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if container_numbers:
            targets = sorted(
                {str(c).strip().upper() for c in container_numbers if str(c).strip()}
            )
        else:
            # Boat / Dropbox container ids (F###### / L######), fully received in bin_pur,
            # with open plog lines and no OMS receive plog rows yet.
            cursor.execute(
                f"""
                SELECT RTRIM(bp.pur_num)
                FROM dbo.bin_pur bp
                WHERE bp.pur_cd = {PO_LINE_PUR_CD}
                  AND (
                      RTRIM(bp.pur_num) LIKE 'F[0-9]%'
                      OR RTRIM(bp.pur_num) LIKE 'L[0-9]%'
                  )
                GROUP BY RTRIM(bp.pur_num)
                HAVING SUM(ISNULL(bp.po_qty, 0)) > 0
                   AND SUM(ISNULL(bp.rcvd_qty, 0)) >= SUM(ISNULL(bp.po_qty, 0))
                   AND NOT EXISTS (
                       SELECT 1
                       FROM dbo.plog pl
                       WHERE RTRIM(pl.pur_num) = RTRIM(bp.pur_num)
                         AND ISNULL(pl.hdl_chg, 0) > 0
                   )
                   AND EXISTS (
                       SELECT 1
                       FROM dbo.plog pl2
                       WHERE RTRIM(pl2.pur_num) = RTRIM(bp.pur_num)
                         AND ISNULL(pl2.hdl_chg, 0) = 0
                         AND ISNULL(pl2.log_qty, 0) > 0
                   )
                """
            )
            targets = [str(row[0]).strip().upper() for row in cursor.fetchall() if row[0]]

        result["containers_checked"] = len(targets)
        for container in targets:
            try:
                if _container_has_received_plog(cursor, container):
                    result["containers_skipped_oms_receive"] += 1
                    continue
                updated = _sync_plog_for_fully_received_container(cursor, container)
                if updated:
                    result["containers_fixed"] += 1
                    result["plog_lines_updated"] += updated
                    result["fixed"].append(
                        {"container": container, "plog_lines_updated": updated}
                    )
            except Exception as exc:
                result["errors"].append(f"{container}: {exc}")

        conn.commit()
        result["success"] = not result["errors"]
        return result
    except Exception as exc:
        conn.rollback()
        result["errors"].append(str(exc))
        return result
    finally:
        conn.close()


def _landed_containers_missing_oms_receive(cursor) -> list[str]:
    cursor.execute(
        f"""
        SELECT RTRIM(bp.pur_num)
        FROM dbo.bin_pur bp
        WHERE bp.pur_cd = {PO_LINE_PUR_CD}
          AND (
              RTRIM(bp.pur_num) LIKE 'F[0-9]%'
              OR RTRIM(bp.pur_num) LIKE 'L[0-9]%'
          )
        GROUP BY RTRIM(bp.pur_num)
        HAVING SUM(ISNULL(bp.rcvd_qty, 0)) > 0
           AND (
               NOT EXISTS (
                   SELECT 1
                   FROM dbo.bin_pur alloc
                   WHERE RTRIM(alloc.pur_num) = RTRIM(bp.pur_num)
                     AND alloc.pur_cd = {RECEIVE_ALLOC_PUR_CD}
               )
               OR NOT EXISTS (
                   SELECT 1
                   FROM dbo.plog recv
                   WHERE RTRIM(recv.pur_num) = RTRIM(bp.pur_num)
                     AND recv.pur_cd = {RECEIVE_ALLOC_PUR_CD}
               )
           )
        """
    )
    return [str(row[0]).strip().upper() for row in cursor.fetchall() if row[0]]


def backfill_oms_receive_pattern_for_landed_containers(
    container_numbers: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Insert missing native OMS receive rows for boat-landed containers.

    Does not change inv_data stock or LASTRCV_DT. Receive plog dates use packing-slip
    / mark-landed cache when present, otherwise LASTRCV_DT at midnight.
    """
    result: dict[str, Any] = {
        "success": False,
        "dry_run": dry_run,
        "containers_checked": 0,
        "containers_fixed": 0,
        "bin_pur_inserted": 0,
        "plog_inserted": 0,
        "fixed": [],
        "skipped": [],
        "errors": [],
    }

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        if container_numbers:
            targets = sorted(
                {str(c).strip().upper() for c in container_numbers if str(c).strip()}
            )
        else:
            targets = _landed_containers_missing_oms_receive(cursor)

        result["containers_checked"] = len(targets)
        for container in targets:
            try:
                if not _po_exists(cursor, container):
                    result["skipped"].append({"container": container, "reason": "no_po"})
                    continue
                received_at = _lookup_container_receive_at(cursor, container)
                pattern = _ensure_oms_receive_pattern(
                    cursor, container, received_at=received_at
                )
                if pattern["bin_pur_inserted"] or pattern["plog_inserted"]:
                    result["containers_fixed"] += 1
                    result["bin_pur_inserted"] += pattern["bin_pur_inserted"]
                    result["plog_inserted"] += pattern["plog_inserted"]
                    result["fixed"].append(
                        {
                            "container": container,
                            "received_at": received_at.strftime("%Y-%m-%d %H:%M:%S"),
                            **pattern,
                        }
                    )
                    if not dry_run:
                        conn.commit()
                else:
                    result["skipped"].append(
                        {"container": container, "reason": "already_complete"}
                    )
            except Exception as exc:
                conn.rollback()
                result["errors"].append(f"{container}: {exc}")

        if dry_run:
            conn.rollback()
        result["success"] = not result["errors"]
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
    """Remove PUR_CD<>1 bin_pur stubs that have no matching PO line.

    Native OMS receive allocations (PUR_CD=2, rcvd=0, same SKU as PUR_CD=1) are kept.
    """
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
              AND NOT EXISTS (
                  SELECT 1
                  FROM dbo.bin_pur cd1
                  WHERE RTRIM(cd1.pur_num) = RTRIM(bin_pur.pur_num)
                    AND cd1.pur_cd = ?
                    AND RTRIM(cd1.prod_cd) = RTRIM(bin_pur.prod_cd)
                    AND ISNULL(cd1.nt_num, 0) = ISNULL(bin_pur.nt_num, 0)
              )
            """,
            (normalized, PO_LINE_PUR_CD, PO_LINE_PUR_CD),
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
