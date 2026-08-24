"""
OTB confirmed workbook → Loris Item Add Template (Setup Sheet).

Uses the static template file layout; rows 1–10 unchanged, data from row 11.
"""

from __future__ import annotations

import io
import math
import os
import shutil
import tempfile
from copy import copy
from datetime import datetime

from openpyxl import load_workbook
from openpyxl.styles import PatternFill

from py.loris.loris_500_upc import (
    UPC_WORK_START_ROW,
    load_500_upc_rows,
    normalize_sku,
    normalize_upc,
    save_500_upc_skus,
)
from py.loris.loris_sku_meta import fetch_loris_sku_meta, normalize_storage_sku, upsert_loris_sku_meta
from py.loris.loris_style_meta import (
    extract_style_code,
    fetch_loris_style_meta,
    upsert_loris_style_meta,
)
from py.utilities.database_utils import get_mysql_connection

TEMPLATE_FILENAME = "Loris Gifts Item Add Template Aug 2026 (1).xlsx"
TEMPLATE_DATA_START_ROW = 11
BRAND_NAME = "La Terre"
COUNTRY_OF_ORIGIN = "China"
UOM = "EA"
COST_NUMBER_FORMAT = "0.00"

YELLOW_FILL = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")

COLOR_DICT = {
    "TL": "Teal", "ABL": "ASHY BLUE", "FLOWER": "FLOWER", "BG": "BEIGE", "BK": "BLACK",
    "BL": "BLUE", "BP": "BLUSH PINK", "BR": "BROWN", "CF": "COFFEE", "CG": "COGNAC",
    "CO": "CORAL", "DM": "DENIM BLUE", "DPL": "DARK PLUM", "FU": "FUSCHIA", "GN": "GREEN",
    "GY": "GREY", "KH": "KHAKI", "LBL": "LIGHT BLUE", "LV": "LAVENDER", "MD": "MUSTARD",
    "MV": "MAUVE", "NV": "NAVY", "OL": "OLIVE", "OR": "ORANGE", "OW": "OFF WHITE",
    "PK": "PINK", "PL": "PLUM", "PP": "PURPLE", "RD": "RED", "TN": "TAN", "TP": "TAUPE",
    "TQ": "TEQUISE", "WN": "WINE", "WT": "WHITE", "YL": "YELLOW", "ST": "STONE",
    "L.ST": "L.Stone", "L": "LARGE", "SG": "SAGE", "LEO": "LEOPARD",
}

# Column indexes on Template sheet (1-based)
COL = {
    "vendor_item": 2,
    "upc": 3,
    "english_desc": 5,
    "brand": 6,
    "first_cost": 7,
    "retail": 8,
    "uom": 11,
    "order_multiple": 12,
    "final_cost": 14,
    "color": 17,
    "country": 20,
    "keywords": 45,
    "online_sku": 63,
}

STATIC_COL_VALUES = {
    30: "Yes", 31: "Yes", 32: "No", 33: "No", 34: "No", 35: "No",
    36: "No", 37: "No", 38: "No", 39: "No", 40: "No", 41: "No",
    60: "No", 63: "Yes",
}

# Confirmed OTB sheet columns (1-based)
COL_STYLE = 1          # A - UPC or Item Code / style
COL_DESC = 4           # D - Description
COL_COST = 5           # E - Cost
COL_PROJ_STORE = 12    # L - Proj Store Count → template Order Multiple
COL_COLOR = 13         # M - Color (yellow highlight = selected for export)
COL_MONTH_QTY = 21     # U - month buy qty (e.g. SEPT) when Color (M) isn't highlighted

SKU_COLOR_ALIASES = {
    "LEOPARD": "LEO",
}


def resolve_setup_template_path() -> str | None:
    base = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "static"))
    path = os.path.join(base, TEMPLATE_FILENAME)
    if os.path.isfile(path):
        return path
    return None


def calc_retail_price(cost) -> float:
    """Retail = 2.5x–2.7x of cost, ending in .99."""
    cost = float(cost)
    lo = cost * 2.5
    hi = cost * 2.7
    retail = math.floor(hi) + 0.99
    if retail > hi:
        retail = math.floor(hi - 1) + 0.99
    if retail < lo:
        retail = math.floor(lo) + 0.99
        if retail < lo:
            retail += 1.0
    return round(retail, 2)


def _fetch_style_descriptions(styles: list[str]) -> dict[str, str]:
    styles = [str(s).strip() for s in styles if str(s).strip()]
    if not styles:
        return {}

    out = {}
    connection = get_mysql_connection()
    if not connection:
        return out

    try:
        cursor = connection.cursor()
        chunk_size = 400
        for i in range(0, len(styles), chunk_size):
            chunk = styles[i : i + chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"""
                SELECT TRIM(style) AS sty, TRIM(description) AS descr
                FROM onlinedb.description
                WHERE style IN ({placeholders})
                """,
                tuple(chunk),
            )
            for sty, descr in cursor.fetchall():
                if sty and descr and str(descr).strip():
                    text = str(descr).strip()
                    out[str(sty).strip()] = text
                    out[str(sty).strip().upper()] = text
    finally:
        if connection.is_connected():
            connection.close()
    return out


def _is_yellow_highlight(cell) -> bool:
    """True when a confirmed OTB Color cell (col M) is highlighted yellow."""
    fill = cell.fill
    if not fill or not fill.fill_type:
        return False
    try:
        rgb = str(fill.start_color.rgb or "").upper()
        if rgb and "FFFF00" in rgb:
            return True
    except (TypeError, AttributeError):
        pass
    return False


def _has_yellow_color_rows(ws) -> bool:
    """True when any data-row Color cell (col M) is yellow-highlighted."""
    for row_num in range(4, ws.max_row + 1):
        if _is_yellow_highlight(ws.cell(row_num, COL_COLOR)):
            return True
    return False


def _parse_cost(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return round(float(text), 2)
    except (TypeError, ValueError):
        return None


def _parse_order_multiple(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _build_sku(style: str, color_code: str) -> str:
    alias = SKU_COLOR_ALIASES.get(color_code.upper(), color_code.upper())
    return f"{style}-{alias}"


def parse_confirmed_otb(otb_file) -> list[dict]:
    """
    Parse Lori OTB confirmed sheet.

    Preferred: yellow-highlighted Color (column M); Order Multiple from
    Proj Store Count (column L).

    Fallback: if no Color cells are highlighted, rows with a numeric qty in
    column U (month buy, e.g. SEPT) are exported; Order Multiple from U.
    """
    if hasattr(otb_file, "seek"):
        otb_file.seek(0)

    wb = load_workbook(otb_file, data_only=False)
    if "OTB" not in wb.sheetnames:
        wb.close()
        raise ValueError('Confirmed file must contain an "OTB" sheet.')

    ws = wb["OTB"]
    use_month_qty = not _has_yellow_color_rows(ws)
    qty_col = COL_MONTH_QTY if use_month_qty else COL_PROJ_STORE

    items = []
    current_style = None
    current_desc = None
    current_cost = None

    for row_num in range(4, ws.max_row + 1):
        code = ws.cell(row_num, COL_STYLE).value
        if code and str(code).strip():
            current_style = str(code).strip()
            desc = ws.cell(row_num, COL_DESC).value
            if desc and str(desc).strip():
                current_desc = str(desc).strip()

        row_cost = _parse_cost(ws.cell(row_num, COL_COST).value)
        if row_cost is not None:
            current_cost = row_cost

        color_cell = ws.cell(row_num, COL_COLOR)
        if use_month_qty:
            if _parse_order_multiple(ws.cell(row_num, COL_MONTH_QTY).value) is None:
                continue
        elif not _is_yellow_highlight(color_cell):
            continue

        color = color_cell.value
        if color is None or not str(color).strip():
            continue
        if not current_style:
            continue

        cost = row_cost if row_cost is not None else current_cost
        if cost is None:
            continue

        color_code = str(color).strip().upper()
        items.append({
            "style": current_style,
            "sku": _build_sku(current_style, color_code),
            "color_code": color_code,
            "cost": cost,
            "confirm_desc": current_desc or "",
            "order_multiple": _parse_order_multiple(ws.cell(row_num, qty_col).value),
            "source_row": row_num,
        })

    wb.close()
    return items


def _build_upc_lookup():
    rows, _, stats = load_500_upc_rows()
    if not stats.get("found"):
        return {}, list(rows), []

    by_sku = {}
    for row in rows:
        sku = normalize_sku(row.get("sku"))
        upc = normalize_upc(row.get("upc"))
        if sku and upc:
            by_sku[sku.upper()] = {"upc": upc, "row": row["row"]}

    open_rows = [
        r for r in rows
        if r.get("row", 0) > UPC_WORK_START_ROW and r.get("upc") and not normalize_sku(r.get("sku"))
    ]
    open_rows.sort(key=lambda r: r["row"])
    return by_sku, rows, open_rows


def _resolve_color_name(sku: str, color_code: str, meta: dict) -> str:
    saved = (meta.get("loris_color") or "").strip()
    if saved:
        return saved
    mapped = COLOR_DICT.get(color_code) or COLOR_DICT.get(color_code.upper())
    if mapped:
        return mapped.title() if mapped.isupper() else mapped
    return color_code.title()


def _resolve_english_desc(sku: str, style: str, confirm_desc: str, style_meta: dict, style_descs: dict) -> str:
    saved = (style_meta.get("loris_desc") or "").strip()
    if saved:
        return saved
    sys_desc = style_descs.get(style) or style_descs.get(style.upper())
    if sys_desc:
        return sys_desc
    return confirm_desc or ""


def _copy_row_style(ws, source_row: int, target_row: int, max_col: int = 74):
    """Copy cell styles from one template row to another."""
    for col in range(1, max_col + 1):
        src = ws.cell(source_row, col)
        dst = ws.cell(target_row, col)
        if src.has_style:
            dst.font = copy(src.font)
            dst.border = copy(src.border)
            dst.fill = copy(src.fill)
            dst.number_format = copy(src.number_format)
            dst.protection = copy(src.protection)
            dst.alignment = copy(src.alignment)


def _set_template_formulas(ws, row_num: int):
    r = row_num
    ws.cell(r, 49).value = f"=B{r}"
    ws.cell(r, 50).value = f"=C{r}"
    ws.cell(r, 51).value = f'=PROPER(F{r}&" "&E{r}&" "&Q{r}&" "&R{r})'
    ws.cell(r, 53).value = f"=N{r}"
    ws.cell(r, 54).value = f"=H{r}"
    ws.cell(r, 55).value = f"=SUM(BB{r}-BA{r})/BB{r}"
    ws.cell(r, 57).value = f"=SUM(BD{r}-BA{r})/BD{r}"
    ws.cell(r, 58).value = f"=K{r}"
    ws.cell(r, 59).value = f"=L{r}"
    ws.cell(r, 67).value = f"=Q{r}"
    ws.cell(r, 72).value = f"=R{r}"
    ws.cell(r, 73).value = f"=AA{r}"


def _clear_template_data_rows(ws, from_row: int):
    for r in range(from_row, ws.max_row + 1):
        for c in range(2, 75):
            cell = ws.cell(r, c)
            cell.value = None
            cell.fill = PatternFill()


def _enrich_otb_items(items: list[dict]) -> tuple[list[dict], dict, dict]:
    """Add description, color, retail; return (items, sku_meta_map, style_descs)."""
    skus = [it["sku"] for it in items]
    styles = list({it["style"] for it in items})
    sku_meta_map = fetch_loris_sku_meta(skus)
    style_meta_map = fetch_loris_style_meta(styles)
    style_descs = _fetch_style_descriptions(styles)

    for item in items:
        sku_key = item["sku"].upper()
        sku_meta = sku_meta_map.get(item["sku"]) or sku_meta_map.get(sku_key) or {}
        style_meta = style_meta_map.get(item["style"]) or style_meta_map.get(item["style"].upper()) or {}
        item["english_desc"] = _resolve_english_desc(
            item["sku"], item["style"], item["confirm_desc"], style_meta, style_descs
        )
        item["color_name"] = _resolve_color_name(item["sku"], item["color_code"], sku_meta)
        item["retail"] = calc_retail_price(item["cost"])

    return items, sku_meta_map, style_descs


def _apply_upc_to_items(items: list[dict]) -> list[dict]:
    """Resolve UPC per item and persist new assignments to the 500 UPC workbook."""
    upc_by_sku, _, open_upc_rows = _build_upc_lookup()
    upc_assignments = []
    open_idx = 0

    for item in items:
        sku_key = item["sku"].upper()
        hit = upc_by_sku.get(sku_key)
        if hit:
            item["upc"] = hit["upc"]
            item["new_upc_assigned"] = False
            continue

        if open_idx >= len(open_upc_rows):
            raise ValueError(
                f"No available UPC rows after row {UPC_WORK_START_ROW} for SKU {item['sku']}."
            )
        slot = open_upc_rows[open_idx]
        open_idx += 1
        item["upc"] = slot["upc"]
        item["new_upc_assigned"] = True
        item["upc_row"] = slot["row"]
        upc_assignments.append({"row": slot["row"], "sku": item["sku"], "auto": True})
        upc_by_sku[sku_key] = {"upc": slot["upc"], "row": slot["row"]}

    if upc_assignments:
        save_500_upc_skus(upc_assignments)

    return upc_assignments


def _write_setup_row(ws, row_num: int, item: dict, *, highlight_new_upc: bool = False):
    cost = round(float(item["cost"]), 2)
    retail = item["retail"]
    order_mult = item.get("order_multiple")

    ws.cell(row_num, COL["vendor_item"]).value = item["sku"]
    ws.cell(row_num, COL["upc"]).value = item["upc"]
    ws.cell(row_num, COL["english_desc"]).value = item["english_desc"]
    ws.cell(row_num, COL["brand"]).value = BRAND_NAME
    first_cost_cell = ws.cell(row_num, COL["first_cost"])
    first_cost_cell.value = cost
    first_cost_cell.number_format = COST_NUMBER_FORMAT
    ws.cell(row_num, COL["retail"]).value = retail
    ws.cell(row_num, COL["uom"]).value = UOM
    ws.cell(row_num, COL["order_multiple"]).value = order_mult
    final_cost_cell = ws.cell(row_num, COL["final_cost"])
    final_cost_cell.value = cost
    final_cost_cell.number_format = COST_NUMBER_FORMAT
    ws.cell(row_num, COL["color"]).value = item["color_name"]
    ws.cell(row_num, COL["country"]).value = COUNTRY_OF_ORIGIN
    ws.cell(row_num, COL["keywords"]).value = "Womens Fashion Handbags"
    ws.cell(row_num, COL["online_sku"]).value = "Yes"

    for col, val in STATIC_COL_VALUES.items():
        ws.cell(row_num, col).value = val

    _set_template_formulas(ws, row_num)

    if highlight_new_upc:
        for col in range(COL["vendor_item"], COL["country"] + 1):
            ws.cell(row_num, col).fill = YELLOW_FILL


def _build_setup_workbook(items: list[dict]) -> io.BytesIO:
    template_path = resolve_setup_template_path()
    if not template_path:
        raise FileNotFoundError(f"Template not found: {TEMPLATE_FILENAME}")

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    try:
        shutil.copy2(template_path, tmp.name)
        wb = load_workbook(tmp.name)
        ws = wb["Template"]

        style_source_row = TEMPLATE_DATA_START_ROW
        _clear_template_data_rows(ws, TEMPLATE_DATA_START_ROW)

        for offset, item in enumerate(items):
            row_num = TEMPLATE_DATA_START_ROW + offset
            if row_num != style_source_row:
                _copy_row_style(ws, style_source_row, row_num)
            _write_setup_row(
                ws,
                row_num,
                item,
                highlight_new_upc=item.get("new_upc_assigned", False),
            )

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        wb.close()
        return out
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def convert_otb_to_setup_sheet(otb_file) -> tuple[io.BytesIO, dict]:
    """Build Setup Sheet from confirmed OTB (yellow-highlighted colors only)."""
    items = parse_confirmed_otb(otb_file)
    if not items:
        raise ValueError(
            "No confirmed rows found. Yellow-highlight column M (Color), or fill buy qty in column U."
        )

    _enrich_otb_items(items)
    upc_assignments = _apply_upc_to_items(items)

    output = _build_setup_workbook(items)
    summary = {
        "item_count": len(items),
        "new_upc_count": len(upc_assignments),
        "new_upc_skus": [a["sku"] for a in upc_assignments],
    }
    return output, summary


def ingest_final_setup_sheet(upload_file) -> dict:
    """
    Read user-edited Template sheet and persist loris_desc / loris_color.
    """
    if hasattr(upload_file, "seek"):
        upload_file.seek(0)

    wb = load_workbook(upload_file, read_only=True, data_only=True)
    if "Template" not in wb.sheetnames:
        wb.close()
        raise ValueError("Upload must contain a Template sheet.")

    ws = wb["Template"]
    style_records = []
    sku_records = []
    # iter_rows is required in read_only mode — ws.cell() per row is extremely slow
    for row in ws.iter_rows(
        min_row=TEMPLATE_DATA_START_ROW,
        min_col=COL["vendor_item"],
        max_col=COL["color"],
        values_only=True,
    ):
        sku_val, desc_val, color_val = row[0], row[3], row[15]
        if sku_val is None or not str(sku_val).strip():
            if style_records or sku_records:
                break
            continue

        sku = normalize_storage_sku(str(sku_val).strip())
        style = extract_style_code(sku)
        desc = str(desc_val).strip() if desc_val is not None and str(desc_val).strip() else ""
        color = str(color_val).strip() if color_val is not None and str(color_val).strip() else ""

        if desc and style:
            style_records.append({"style": style, "loris_desc": desc})
        if color:
            sku_records.append({
                "sku": sku,
                "loris_desc": "",
                "loris_color": color,
            })
    wb.close()

    if not style_records and not sku_records:
        raise ValueError("No SKU rows found from row 11 on Template sheet.")

    saved_style = upsert_loris_style_meta(style_records) if style_records else 0
    saved_sku = upsert_loris_sku_meta(sku_records) if sku_records else 0
    return {"saved": saved_style + saved_sku, "row_count": len(style_records) + len(sku_records)}


def default_setup_sheet_filename(source_filename: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if source_filename:
        base = source_filename.rsplit(".", 1)[0]
        return f"{base}_setup_sheet_{stamp}.xlsx"
    return f"setup_sheet_{stamp}.xlsx"
