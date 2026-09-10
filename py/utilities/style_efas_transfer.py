"""Transfer product master rows into FDC / EFAS OMS databases."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

import pandas as pd

from py.utilities.database_utils import (
    get_sql_server_connection_fdc,
    get_sql_server_connection_oms_ef,
)
from py.utilities.inventory_adjustment import erp_date, normalize_adjusted_by, pad_char

DEFAULT_WHS = "01"
BLANK20 = " " * 20
CONTAINER_CSV_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "output", "container_csvs"
)


@dataclass
class StyleRow:
    prod_cd: str
    descrip: str
    retail_prs: Decimal
    class_cd: str
    pc_case: Decimal
    box_case: Decimal
    sales_cost: Decimal
    case_wt: Decimal
    ut_wt: Decimal
    alt_cd: str
    unit_color: str
    ut_len: Decimal | None = None
    ut_wi: Decimal | None = None
    ut_hi: Decimal | None = None
    case_len: Decimal | None = None
    case_wi: Decimal | None = None
    case_hi: Decimal | None = None


@dataclass
class TransferResult:
    success: bool
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "dry_run": self.dry_run,
            "target": self.target,
            "synced": len(self.inserted) + len(self.updated),
        }


@dataclass
class DualTransferResult:
    success: bool
    fdc: TransferResult
    efas: TransferResult
    dry_run: bool = False
    total_skus: int = 0
    missing_from_fdc: list[str] = field(default_factory=list)
    missing_from_efas: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "dry_run": self.dry_run,
            "total_skus": self.total_skus,
            "missing_from_fdc": self.missing_from_fdc,
            "missing_from_efas": self.missing_from_efas,
            "fdc": self.fdc.to_dict(),
            "efas": self.efas.to_dict(),
        }


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    return Decimal(str(value))


def _build_alt_cd_param(style_codes: list[str]) -> str:
    normalized = ",".join(code.strip().upper() for code in style_codes if code.strip())
    return f"',{normalized},'"


def _container_styles_csv_path(container_number: str) -> str:
    normalized = str(container_number).strip().upper()
    return os.path.normpath(os.path.join(CONTAINER_CSV_DIR, f"{normalized}-styles.csv"))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return Decimal(str(value))


def parse_container_styles_csv(csv_path: str) -> list[StyleRow]:
    df = pd.read_csv(csv_path)
    rows: list[StyleRow] = []
    for _, record in df.iterrows():
        item = str(record.get("Item#", "")).strip()
        if not item:
            continue
        rows.append(
            StyleRow(
                prod_cd=item,
                descrip=str(record.get("Descrip", "")).strip(),
                retail_prs=_decimal(record.get("Price1")),
                class_cd=str(record.get("Class#", "L1")).strip() or "L1",
                pc_case=_decimal(record.get("Unit/Case")),
                box_case=_decimal(record.get("Box/Case"), Decimal("1")),
                sales_cost=_decimal(record.get("Sales Cost")),
                case_wt=_decimal(record.get("Case Wt")),
                ut_wt=_decimal(record.get("Unit Wt")),
                alt_cd=str(record.get("Alt.Item#", "")).strip(),
                unit_color=str(record.get("Color", "")).strip(),
                ut_len=_optional_decimal(record.get("Unit Length")),
                ut_wi=_optional_decimal(record.get("Unit Width")),
                ut_hi=_optional_decimal(record.get("Unit Height")),
                case_len=_optional_decimal(record.get("Case Length")),
                case_wi=_optional_decimal(record.get("Case Width")),
                case_hi=_optional_decimal(record.get("Case Height")),
            )
        )
    return rows


def fetch_style_rows_from_container_csv(container_number: str) -> list[StyleRow]:
    csv_path = _container_styles_csv_path(container_number)
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Styles CSV not found for {container_number}. Process the container first."
        )
    return parse_container_styles_csv(csv_path)


def fetch_style_rows_from_fdc(style_codes: list[str]) -> list[StyleRow]:
    if not style_codes:
        return []

    conn = get_sql_server_connection_fdc()
    try:
        cursor = conn.cursor()
        query = """
            DECLARE @AltCDList NVARCHAR(MAX) = ?;
            SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';

            WITH SplitAltCD AS (
                SELECT value
                FROM dbo.SplitString(@AltCDList, ',')
            )
            SELECT
                RTRIM(i.PROD_CD),
                RTRIM(i.DESCRIP),
                i.RETAIL_PRS,
                RTRIM(i.Class_CD),
                i.PC_CASE,
                i.BOX_Case,
                i.Sales_Cost,
                i.CASE_WT,
                i.UT_WT,
                RTRIM(i.ALT_CD),
                RTRIM(i.UNIT_COLOR),
                idt.ut_len,
                idt.ut_wi,
                idt.UT_hi,
                idt.CASE_LEN,
                idt.CASE_WI,
                idt.CASE_HI
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            WHERE EXISTS (
                SELECT 1 FROM SplitAltCD WHERE i.alt_CD LIKE value AND LEN(RTRIM(value)) > 0
            )
            ORDER BY i.PROD_CD
        """
        cursor.execute(query, (_build_alt_cd_param(style_codes),))
        rows: list[StyleRow] = []
        for row in cursor.fetchall():
            rows.append(
                StyleRow(
                    prod_cd=str(row[0]).strip(),
                    descrip=str(row[1] or "").strip(),
                    retail_prs=_decimal(row[2]),
                    class_cd=str(row[3] or "").strip(),
                    pc_case=_decimal(row[4]),
                    box_case=_decimal(row[5]),
                    sales_cost=_decimal(row[6]),
                    case_wt=_decimal(row[7]),
                    ut_wt=_decimal(row[8]),
                    alt_cd=str(row[9] or "").strip(),
                    unit_color=str(row[10] or "").strip(),
                    ut_len=_decimal(row[11]) if row[11] is not None else None,
                    ut_wi=_decimal(row[12]) if row[12] is not None else None,
                    ut_hi=_decimal(row[13]) if row[13] is not None else None,
                    case_len=_decimal(row[14]) if row[14] is not None else None,
                    case_wi=_decimal(row[15]) if row[15] is not None else None,
                    case_hi=_decimal(row[16]) if row[16] is not None else None,
                )
            )
        return rows
    finally:
        conn.close()


def fetch_style_rows_from_efas(style_codes: list[str]) -> list[StyleRow]:
    if not style_codes:
        return []

    conn = get_sql_server_connection_oms_ef()
    try:
        cursor = conn.cursor()
        query = """
            DECLARE @AltCDList NVARCHAR(MAX) = ?;
            SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';

            WITH SplitAltCD AS (
                SELECT value
                FROM dbo.SplitString(@AltCDList, ',')
            )
            SELECT
                RTRIM(i.PROD_CD),
                RTRIM(i.DESCRIP),
                i.RETAIL_PRS,
                RTRIM(i.Class_CD),
                i.PC_CASE,
                i.BOX_Case,
                i.Sales_Cost,
                i.CASE_WT,
                i.UT_WT,
                RTRIM(i.ALT_CD),
                RTRIM(i.UNIT_COLOR),
                idt.ut_len,
                idt.ut_wi,
                idt.UT_hi,
                idt.CASE_LEN,
                idt.CASE_WI,
                idt.CASE_HI
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            WHERE EXISTS (
                SELECT 1 FROM SplitAltCD WHERE i.alt_CD LIKE value AND LEN(RTRIM(value)) > 0
            )
            ORDER BY i.PROD_CD
        """
        cursor.execute(query, (_build_alt_cd_param(style_codes),))
        return _style_rows_from_cursor(cursor)
    finally:
        conn.close()


def fetch_style_rows_by_skus_from_efas(skus: list[str]) -> list[StyleRow]:
    skus = [str(sku).strip() for sku in skus if str(sku).strip()]
    if not skus:
        return []

    conn = get_sql_server_connection_oms_ef()
    try:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in skus)
        query = f"""
            SELECT
                RTRIM(i.PROD_CD),
                RTRIM(i.DESCRIP),
                i.RETAIL_PRS,
                RTRIM(i.Class_CD),
                i.PC_CASE,
                i.BOX_Case,
                i.Sales_Cost,
                i.CASE_WT,
                i.UT_WT,
                RTRIM(i.ALT_CD),
                RTRIM(i.UNIT_COLOR),
                idt.ut_len,
                idt.ut_wi,
                idt.UT_hi,
                idt.CASE_LEN,
                idt.CASE_WI,
                idt.CASE_HI
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            WHERE RTRIM(i.PROD_CD) IN ({placeholders})
            ORDER BY i.PROD_CD
        """
        cursor.execute(query, skus)
        return _style_rows_from_cursor(cursor)
    finally:
        conn.close()


def _style_rows_from_cursor(cursor) -> list[StyleRow]:
    rows: list[StyleRow] = []
    for row in cursor.fetchall():
        rows.append(
            StyleRow(
                prod_cd=str(row[0]).strip(),
                descrip=str(row[1] or "").strip(),
                retail_prs=_decimal(row[2]),
                class_cd=str(row[3] or "").strip(),
                pc_case=_decimal(row[4]),
                box_case=_decimal(row[5]),
                sales_cost=_decimal(row[6]),
                case_wt=_decimal(row[7]),
                ut_wt=_decimal(row[8]),
                alt_cd=str(row[9] or "").strip(),
                unit_color=str(row[10] or "").strip(),
                ut_len=_decimal(row[11]) if row[11] is not None else None,
                ut_wi=_decimal(row[12]) if row[12] is not None else None,
                ut_hi=_decimal(row[13]) if row[13] is not None else None,
                case_len=_decimal(row[14]) if row[14] is not None else None,
                case_wi=_decimal(row[15]) if row[15] is not None else None,
                case_hi=_decimal(row[16]) if row[16] is not None else None,
            )
        )
    return rows


def copy_skus_from_efas_to_fdc(
    skus: list[str],
    *,
    dry_run: bool = False,
    adjusted_by: str = "fdcef",
    update_existing: bool = False,
) -> TransferResult:
    """Copy product master from EFAS to FDC; inventory quantities start at zero."""
    rows = fetch_style_rows_by_skus_from_efas(skus)
    if not rows:
        return TransferResult(
            success=False,
            errors=[f"No EFAS products found for SKUs: {', '.join(skus)}"],
            dry_run=dry_run,
            target="fdc",
        )
    return transfer_style_rows_to_fdc(
        rows,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        update_existing=update_existing,
    )


def _existing_skus(cursor, skus: list[str]) -> set[str]:
    if not skus:
        return set()
    placeholders = ",".join("?" for _ in skus)
    cursor.execute(
        f"""
        SELECT RTRIM(PROD_CD)
        FROM dbo.inv
        WHERE RTRIM(PROD_CD) IN ({placeholders})
        """,
        skus,
    )
    return {str(row[0]).strip() for row in cursor.fetchall()}


def _insert_inv(cursor, row: StyleRow, *, is_efas: bool) -> None:
    if is_efas:
        cursor.execute(
            """
            INSERT INTO dbo.inv (
                PROD_CD, DESCRIP, RETAIL_PRS, CLASS_CD, PC_CASE, BOX_CASE,
                SALES_COST, CASE_WT, UT_WT, ALT_CD, UNIT_COLOR, YEAR_FM, YEAR_TO,
                MAKER, RELT_ITEM
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pad_char(row.prod_cd, 21),
                pad_char(row.descrip, 61),
                row.retail_prs,
                pad_char(row.class_cd, 30),
                row.pc_case,
                row.box_case,
                row.sales_cost,
                row.case_wt,
                row.ut_wt,
                pad_char(row.alt_cd, 20),
                pad_char(row.unit_color, 10),
                Decimal("0"),
                Decimal("0"),
                pad_char("", 40),
                pad_char("", 20),
            ),
        )
        return

    cursor.execute(
        """
        INSERT INTO dbo.inv (
            PROD_CD, DESCRIP, RETAIL_PRS, CLASS_CD, PC_CASE, BOX_CASE,
            SALES_COST, CASE_WT, UT_WT, ALT_CD, UNIT_COLOR, YEAR_FM, YEAR_TO
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pad_char(row.prod_cd, 21),
            pad_char(row.descrip, 61),
            row.retail_prs,
            pad_char(row.class_cd, 30),
            row.pc_case,
            row.box_case,
            row.sales_cost,
            row.case_wt,
            row.ut_wt,
            pad_char(row.alt_cd, 20),
            pad_char(row.unit_color, 10),
            Decimal("0"),
            Decimal("0"),
        ),
    )


def _insert_inv_data(cursor, row: StyleRow, *, updt_by: str, erp_today: int) -> None:
    cursor.execute(
        """
        INSERT INTO dbo.inv_data (
            PROD_CD, WHS_NUM, IN_STOCK, LASTRCV_QTY, LASTRCV_DT, PRICE_BASE,
            FRT_CUS, PROD_DUTY, HANDL_FEE, MISC_FEE, AVG_COST, LT_SL_DT, VENDOR,
            LST_ORDER, ORD_DT, STK_IND, BACK_QTY, ORDER_QTY, ON_ORDER_QTY, WIP_QTY,
            RMA_QTY, WATER_QTY, ORDERSIZE, MINSTOCK, INV_LOC, UNIT_COLOR, CLASS_CD,
            DESCRIP, DEF_UNIT, UPDT_DT, PHYC_DT, IMAGE_NM, OEM_CD, ALT_CD, UPDT_BY,
            CURRENCY_COST, COST_FACTOR, MAXSTOCK, QTYH, QTYW, QTYL
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            pad_char(row.prod_cd, 21),
            pad_char(DEFAULT_WHS, 8),
            Decimal("0"),
            Decimal("0"),
            0,
            row.sales_cost,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            erp_today,
            pad_char("", 10),
            Decimal("0"),
            0,
            " ",
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            BLANK20,
            pad_char(row.unit_color, 11),
            pad_char(row.class_cd, 20),
            pad_char(row.descrip, 61),
            "  ",
            erp_today,
            0,
            pad_char("", 80),
            pad_char("", 20),
            pad_char(row.alt_cd, 20),
            updt_by,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        ),
    )


def _insert_inv_detl(cursor, row: StyleRow, *, swap_hw: bool) -> None:
    ut_len, ut_hi, ut_wi = _detl_dimensions(row, swap_hw=swap_hw)
    case_len = row.case_len if row.case_len is not None else Decimal("0")
    case_wi = row.case_wi if row.case_wi is not None else Decimal("0")
    case_hi = row.case_hi if row.case_hi is not None else Decimal("0")
    cursor.execute(
        """
        INSERT INTO dbo.inv_detl (PROD_CD, UT_LEN, UT_HI, UT_WI, CASE_LEN, CASE_WI, CASE_HI)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pad_char(row.prod_cd, 21),
            ut_len,
            ut_hi,
            ut_wi,
            case_len,
            case_wi,
            case_hi,
        ),
    )


def _detl_dimensions(row: StyleRow, *, swap_hw: bool) -> tuple[Decimal, Decimal, Decimal]:
    ut_len = row.ut_len if row.ut_len is not None else Decimal("0")
    if swap_hw:
        ut_hi = row.ut_wi if row.ut_wi is not None else Decimal("0")
        ut_wi = row.ut_hi if row.ut_hi is not None else Decimal("0")
    else:
        ut_hi = row.ut_hi if row.ut_hi is not None else Decimal("0")
        ut_wi = row.ut_wi if row.ut_wi is not None else Decimal("0")
    return ut_len, ut_hi, ut_wi


def _case_dimensions(row: StyleRow) -> tuple[Decimal, Decimal, Decimal]:
    return (
        row.case_len if row.case_len is not None else Decimal("0"),
        row.case_wi if row.case_wi is not None else Decimal("0"),
        row.case_hi if row.case_hi is not None else Decimal("0"),
    )


def _update_inv(cursor, row: StyleRow, *, is_efas: bool) -> None:
    cursor.execute(
        """
        UPDATE dbo.inv
        SET DESCRIP = ?, RETAIL_PRS = ?, CLASS_CD = ?, PC_CASE = ?, BOX_CASE = ?,
            SALES_COST = ?, CASE_WT = ?, UT_WT = ?, ALT_CD = ?, UNIT_COLOR = ?
        WHERE RTRIM(PROD_CD) = ?
        """,
        (
            pad_char(row.descrip, 61),
            row.retail_prs,
            pad_char(row.class_cd, 30),
            row.pc_case,
            row.box_case,
            row.sales_cost,
            row.case_wt,
            row.ut_wt,
            pad_char(row.alt_cd, 20),
            pad_char(row.unit_color, 10),
            row.prod_cd.strip(),
        ),
    )


def _update_inv_data(cursor, row: StyleRow, *, updt_by: str, erp_today: int) -> None:
    cursor.execute(
        """
        UPDATE dbo.inv_data
        SET PRICE_BASE = ?, DESCRIP = ?, CLASS_CD = ?, ALT_CD = ?, UNIT_COLOR = ?,
            UPDT_DT = ?, UPDT_BY = ?
        WHERE RTRIM(prod_cd) = ? AND RTRIM(whs_num) = ?
        """,
        (
            row.sales_cost,
            pad_char(row.descrip, 61),
            pad_char(row.class_cd, 20),
            pad_char(row.alt_cd, 20),
            pad_char(row.unit_color, 11),
            erp_today,
            updt_by,
            row.prod_cd.strip(),
            DEFAULT_WHS,
        ),
    )


def _has_inv_data(cursor, prod_cd: str) -> bool:
    cursor.execute(
        """
        SELECT 1 FROM dbo.inv_data
        WHERE RTRIM(prod_cd) = ? AND RTRIM(whs_num) = ?
        """,
        (prod_cd.strip(), DEFAULT_WHS),
    )
    return cursor.fetchone() is not None


def _has_inv_detl(cursor, prod_cd: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM dbo.inv_detl WHERE RTRIM(prod_cd) = ?",
        (prod_cd.strip(),),
    )
    return cursor.fetchone() is not None


def _upsert_inv_detl(cursor, row: StyleRow, *, swap_hw: bool) -> None:
    if _has_inv_detl(cursor, row.prod_cd):
        ut_len, ut_hi, ut_wi = _detl_dimensions(row, swap_hw=swap_hw)
        has_case = (
            row.case_len is not None or row.case_wi is not None or row.case_hi is not None
        )
        if has_case:
            case_len, case_wi, case_hi = _case_dimensions(row)
            cursor.execute(
                """
                UPDATE dbo.inv_detl
                SET UT_LEN = ?, UT_HI = ?, UT_WI = ?, CASE_LEN = ?, CASE_WI = ?, CASE_HI = ?
                WHERE RTRIM(prod_cd) = ?
                """,
                (ut_len, ut_hi, ut_wi, case_len, case_wi, case_hi, row.prod_cd.strip()),
            )
        else:
            cursor.execute(
                """
                UPDATE dbo.inv_detl
                SET UT_LEN = ?, UT_HI = ?, UT_WI = ?
                WHERE RTRIM(prod_cd) = ?
                """,
                (ut_len, ut_hi, ut_wi, row.prod_cd.strip()),
            )
        return
    _insert_inv_detl(cursor, row, swap_hw=swap_hw)


def _write_style_row(
    cursor,
    row: StyleRow,
    *,
    is_efas: bool,
    swap_hw: bool,
    updt_by: str,
    erp_today: int,
    update_existing: bool,
) -> str:
    existing = row.prod_cd in _existing_skus(cursor, [row.prod_cd])
    if existing and update_existing:
        _update_inv(cursor, row, is_efas=is_efas)
        if _has_inv_data(cursor, row.prod_cd):
            _update_inv_data(cursor, row, updt_by=updt_by, erp_today=erp_today)
        else:
            _insert_inv_data(cursor, row, updt_by=updt_by, erp_today=erp_today)
        _upsert_inv_detl(cursor, row, swap_hw=swap_hw)
        if is_efas:
            _ensure_efas_wm_skufile(cursor, row.prod_cd, create_dt=erp_today)
        return "updated"

    if existing:
        return "skipped"

    _insert_inv(cursor, row, is_efas=is_efas)
    _insert_inv_data(cursor, row, updt_by=updt_by, erp_today=erp_today)
    _insert_inv_detl(cursor, row, swap_hw=swap_hw)
    if is_efas:
        _ensure_efas_wm_skufile(cursor, row.prod_cd, create_dt=erp_today)
    return "inserted"


def _ensure_efas_wm_skufile(cursor, prod_cd: str, *, create_dt: int) -> None:
    cursor.execute(
        "SELECT 1 FROM dbo.wm_skufile WHERE RTRIM(PROD_CD) = ?",
        (prod_cd.strip(),),
    )
    if cursor.fetchone():
        return

    cursor.execute(
        """
        INSERT INTO dbo.wm_skufile (
            TYPE_CD, SKU_CD, VEN_ID, CUS_ID, PROD_CD, WHS_NUM,
            LOT_NUM, PROD_CLR, RUN_CD, CREATE_DT, KEY_FLAG
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            0,
            pad_char(prod_cd.strip(), 100),
            pad_char("", 10),
            pad_char("", 10),
            pad_char(prod_cd, 20),
            pad_char("", 8),
            pad_char("", 20),
            pad_char("", 20),
            pad_char("", 20),
            create_dt,
            0,
        ),
    )


def _transfer_rows_to_database(
    rows: list[StyleRow],
    *,
    connect_fn: Callable[[], Any],
    target: str,
    is_efas: bool,
    swap_hw: bool,
    dry_run: bool,
    adjusted_by: str,
    update_existing: bool = False,
) -> TransferResult:
    result = TransferResult(success=True, dry_run=dry_run, target=target)
    if not rows:
        result.success = False
        result.errors.append("No style rows to transfer")
        return result

    updt_by = normalize_adjusted_by(adjusted_by)
    conn = connect_fn()
    try:
        cursor = conn.cursor()
        existing = _existing_skus(cursor, [row.prod_cd for row in rows])

        if update_existing:
            to_process = rows
            if dry_run:
                for row in rows:
                    if row.prod_cd in existing:
                        result.updated.append(row.prod_cd)
                    else:
                        result.inserted.append(row.prod_cd)
                return result
        else:
            to_process = [row for row in rows if row.prod_cd not in existing]
            result.skipped = sorted(existing)
            if not to_process:
                return result
            if dry_run:
                result.inserted = [row.prod_cd for row in to_process]
                return result

        erp_today = erp_date(cursor)

        for row in to_process:
            try:
                action = _write_style_row(
                    cursor,
                    row,
                    is_efas=is_efas,
                    swap_hw=swap_hw,
                    updt_by=updt_by,
                    erp_today=erp_today,
                    update_existing=update_existing,
                )
                if action == "inserted":
                    result.inserted.append(row.prod_cd)
                elif action == "updated":
                    result.updated.append(row.prod_cd)
                else:
                    result.skipped.append(row.prod_cd)
            except Exception as exc:
                conn.rollback()
                result.success = False
                result.errors.append(f"{row.prod_cd}: {exc}")
                result.inserted.clear()
                result.updated.clear()
                return result

        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        return TransferResult(
            success=False,
            errors=[str(exc)],
            dry_run=dry_run,
            target=target,
        )
    finally:
        conn.close()


def _missing_skus(connect_fn: Callable[[], Any], skus: list[str]) -> list[str]:
    conn = connect_fn()
    try:
        existing = _existing_skus(conn.cursor(), skus)
    finally:
        conn.close()
    return sorted(set(skus) - existing)


def _finalize_dual_result(
    rows: list[StyleRow],
    fdc_result: TransferResult,
    efas_result: TransferResult,
    *,
    dry_run: bool,
) -> DualTransferResult:
    sku_list = [row.prod_cd for row in rows]
    missing_from_fdc: list[str] = []
    missing_from_efas: list[str] = []
    if not dry_run and fdc_result.success and efas_result.success:
        missing_from_fdc = _missing_skus(get_sql_server_connection_fdc, sku_list)
        missing_from_efas = _missing_skus(get_sql_server_connection_oms_ef, sku_list)

    success = (
        fdc_result.success
        and efas_result.success
        and not missing_from_fdc
        and not missing_from_efas
    )
    if missing_from_fdc:
        fdc_result.errors.append(f"Still missing from FDC: {', '.join(missing_from_fdc)}")
    if missing_from_efas:
        efas_result.errors.append(f"Still missing from EFAS: {', '.join(missing_from_efas)}")

    return DualTransferResult(
        success=success,
        fdc=fdc_result,
        efas=efas_result,
        dry_run=dry_run,
        total_skus=len(rows),
        missing_from_fdc=missing_from_fdc,
        missing_from_efas=missing_from_efas,
    )


def transfer_style_rows_to_fdc(
    rows: list[StyleRow],
    *,
    dry_run: bool = False,
    adjusted_by: str = "fdcef",
    update_existing: bool = False,
) -> TransferResult:
    return _transfer_rows_to_database(
        rows,
        connect_fn=get_sql_server_connection_fdc,
        target="fdc",
        is_efas=False,
        swap_hw=False,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        update_existing=update_existing,
    )


def transfer_style_rows_to_efas(
    rows: list[StyleRow],
    *,
    dry_run: bool = False,
    adjusted_by: str = "fdcef",
    swap_hw: bool = False,
    update_existing: bool = False,
) -> TransferResult:
    return _transfer_rows_to_database(
        rows,
        connect_fn=get_sql_server_connection_oms_ef,
        target="efas",
        is_efas=True,
        swap_hw=swap_hw,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        update_existing=update_existing,
    )


def transfer_container_styles_to_oms(
    container_number: str,
    *,
    dry_run: bool = False,
    adjusted_by: str = "fdcef",
) -> DualTransferResult:
    rows = fetch_style_rows_from_container_csv(container_number)
    fdc_result = transfer_style_rows_to_fdc(
        rows,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        update_existing=True,
    )
    efas_result = transfer_style_rows_to_efas(
        rows,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        update_existing=True,
    )
    return _finalize_dual_result(rows, fdc_result, efas_result, dry_run=dry_run)


def update_retail_prices_in_database(
    cursor,
    prod_cd: str,
    retail_prs: Decimal,
) -> bool:
    cursor.execute(
        """
        UPDATE dbo.inv
        SET RETAIL_PRS = ?
        WHERE RTRIM(PROD_CD) = ?
        """,
        (retail_prs, prod_cd.strip()),
    )
    return cursor.rowcount > 0


def update_retail_prices_dual(
    price_updates: dict[str, Decimal],
    *,
    adjusted_by: str = "fdcef",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Update RETAIL_PRS only in FDC and EFAS for the given SKU map."""
    if not price_updates:
        return {
            "success": False,
            "error": "No retail prices to update",
            "updated_fdc": [],
            "updated_efas": [],
            "skipped": [],
        }

    updt_by = normalize_adjusted_by(adjusted_by)
    updated_fdc: list[str] = []
    updated_efas: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for target, connect_fn, bucket in (
        ("fdc", get_sql_server_connection_fdc, updated_fdc),
        ("efas", get_sql_server_connection_oms_ef, updated_efas),
    ):
        conn = connect_fn()
        try:
            cursor = conn.cursor()
            for prod_cd, retail_prs in price_updates.items():
                sku = prod_cd.strip()
                if dry_run:
                    bucket.append(sku)
                    continue
                if update_retail_prices_in_database(cursor, sku, retail_prs):
                    bucket.append(sku)
                else:
                    skipped.append(f"{sku} ({target})")
            if not dry_run:
                conn.commit()
        except Exception as exc:
            conn.rollback()
            errors.append(f"{target}: {exc}")
        finally:
            conn.close()

    success = not errors
    return {
        "success": success,
        "updated_fdc": sorted(set(updated_fdc)),
        "updated_efas": sorted(set(updated_efas)),
        "skipped": sorted(set(skipped)),
        "errors": errors,
        "adjusted_by": updt_by,
        "dry_run": dry_run,
    }


def delete_sku_from_database(cursor, prod_cd: str) -> None:
    sku = prod_cd.strip()
    cursor.execute("DELETE FROM dbo.wm_skufile WHERE RTRIM(prod_cd) = ?", (sku,))
    cursor.execute("DELETE FROM dbo.inv_data WHERE RTRIM(prod_cd) = ?", (sku,))
    cursor.execute("DELETE FROM dbo.inv_detl WHERE RTRIM(prod_cd) = ?", (sku,))
    cursor.execute("DELETE FROM dbo.inv WHERE RTRIM(PROD_CD) = ?", (sku,))


def delete_skus_for_container(
    container_number: str,
    prod_cds: list[str],
    *,
    connect_fn: Callable[[], Any],
    target: str,
) -> list[str]:
    """Delete SKUs only when inv.DESCRIP matches the container number."""
    removed: list[str] = []
    normalized_container = container_number.strip().upper()
    conn = connect_fn()
    try:
        cursor = conn.cursor()
        for prod_cd in prod_cds:
            sku = prod_cd.strip()
            cursor.execute(
                """
                SELECT RTRIM(DESCRIP)
                FROM dbo.inv
                WHERE RTRIM(PROD_CD) = ?
                """,
                (sku,),
            )
            row = cursor.fetchone()
            if not row:
                continue
            if str(row[0]).strip().upper() != normalized_container:
                continue
            delete_sku_from_database(cursor, sku)
            removed.append(sku)
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_case_dimensions_in_database(
    cursor,
    prod_cd: str,
    case_len: Decimal,
    case_wi: Decimal,
    case_hi: Decimal,
) -> str:
    """Update or insert CASE_LEN/WI/HI on inv_detl. Returns updated|inserted|missing."""
    sku = prod_cd.strip()
    cursor.execute(
        "SELECT 1 FROM dbo.inv WHERE RTRIM(PROD_CD) = ?",
        (sku,),
    )
    if not cursor.fetchone():
        return "missing"

    if _has_inv_detl(cursor, sku):
        cursor.execute(
            """
            UPDATE dbo.inv_detl
            SET CASE_LEN = ?, CASE_WI = ?, CASE_HI = ?
            WHERE RTRIM(prod_cd) = ?
            """,
            (case_len, case_wi, case_hi, sku),
        )
        return "updated"

    cursor.execute(
        """
        INSERT INTO dbo.inv_detl (PROD_CD, UT_LEN, UT_HI, UT_WI, CASE_LEN, CASE_WI, CASE_HI)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pad_char(sku, 21),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            case_len,
            case_wi,
            case_hi,
        ),
    )
    return "inserted"


def update_case_dimensions_dual(
    case_updates: dict[str, tuple[Decimal, Decimal, Decimal]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write masterbox L/W/H into FDC + EFAS inv_detl for the given SKU map."""
    if not case_updates:
        return {
            "success": False,
            "error": "No case dimensions to update",
            "updated_fdc": [],
            "updated_efas": [],
            "inserted_fdc": [],
            "inserted_efas": [],
            "missing": [],
            "errors": [],
            "dry_run": dry_run,
        }

    updated_fdc: list[str] = []
    updated_efas: list[str] = []
    inserted_fdc: list[str] = []
    inserted_efas: list[str] = []
    missing: list[str] = []
    errors: list[str] = []

    for target, connect_fn, updated_bucket, inserted_bucket in (
        ("fdc", get_sql_server_connection_fdc, updated_fdc, inserted_fdc),
        ("efas", get_sql_server_connection_oms_ef, updated_efas, inserted_efas),
    ):
        conn = connect_fn()
        try:
            cursor = conn.cursor()
            for prod_cd, dims in case_updates.items():
                sku = prod_cd.strip()
                case_len, case_wi, case_hi = dims
                if dry_run:
                    cursor.execute(
                        "SELECT 1 FROM dbo.inv WHERE RTRIM(PROD_CD) = ?",
                        (sku,),
                    )
                    if cursor.fetchone():
                        updated_bucket.append(sku)
                    else:
                        missing.append(f"{sku} ({target})")
                    continue
                status = update_case_dimensions_in_database(
                    cursor, sku, case_len, case_wi, case_hi
                )
                if status == "updated":
                    updated_bucket.append(sku)
                elif status == "inserted":
                    inserted_bucket.append(sku)
                else:
                    missing.append(f"{sku} ({target})")
            if not dry_run:
                conn.commit()
        except Exception as exc:
            conn.rollback()
            errors.append(f"{target}: {exc}")
        finally:
            conn.close()

    return {
        "success": not errors,
        "updated_fdc": sorted(set(updated_fdc)),
        "updated_efas": sorted(set(updated_efas)),
        "inserted_fdc": sorted(set(inserted_fdc)),
        "inserted_efas": sorted(set(inserted_efas)),
        "missing": sorted(set(missing)),
        "errors": errors,
        "dry_run": dry_run,
        "sku_count": len(case_updates),
    }


def case_dimension_updates_from_styles_csv(
    container_number: str,
) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """Build SKU → (CASE_LEN, CASE_WI, CASE_HI) from a container styles CSV."""
    rows = fetch_style_rows_from_container_csv(container_number)
    updates: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for row in rows:
        if row.case_len is None and row.case_wi is None and row.case_hi is None:
            continue
        if not (
            (row.case_len or 0) > 0 or (row.case_wi or 0) > 0 or (row.case_hi or 0) > 0
        ):
            continue
        updates[row.prod_cd.strip()] = (
            row.case_len if row.case_len is not None else Decimal("0"),
            row.case_wi if row.case_wi is not None else Decimal("0"),
            row.case_hi if row.case_hi is not None else Decimal("0"),
        )
    return updates


def backfill_container_styles(
    container_number: str,
    *,
    remove_skus: list[str] | None = None,
    adjusted_by: str = "fdcef",
) -> DualTransferResult:
    """Remove bad SKUs for a container, then re-import from styles CSV."""
    remove_skus = remove_skus or []
    for connect_fn, target in (
        (get_sql_server_connection_fdc, "fdc"),
        (get_sql_server_connection_oms_ef, "efas"),
    ):
        delete_skus_for_container(
            container_number,
            remove_skus,
            connect_fn=connect_fn,
            target=target,
        )
    return transfer_container_styles_to_oms(container_number, adjusted_by=adjusted_by)


def transfer_styles_to_efas(
    style_codes: list[str],
    *,
    dry_run: bool = False,
    adjusted_by: str = "fdcef",
) -> TransferResult:
    rows = fetch_style_rows_from_fdc(style_codes)
    if not rows:
        return TransferResult(
            success=False,
            errors=[f"No FDC products found for style codes: {', '.join(style_codes)}"],
            dry_run=dry_run,
            target="efas",
        )
    return transfer_style_rows_to_efas(
        rows,
        dry_run=dry_run,
        adjusted_by=adjusted_by,
        swap_hw=True,
    )
