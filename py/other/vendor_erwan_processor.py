"""
vendor_erwan_processor.py
Processes uploaded 发货/退货 billing Excel files and produces
a Channel_ERWAN output workbook with a new tab per billing period.
"""
import json
import os
import re
import pyodbc
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from io import BytesIO
from py.utilities.setting import Config

sql_server_config = Config.SQL_SERVER_CONFIG

SKU_DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'erwan_sku_database.json')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MONTH_MAP = {
    '1': 'jan', '2': 'feb', '3': 'mar', '4': 'apr',
    '5': 'may', '6': 'jun', '7': 'jul', '8': 'aug',
    '9': 'sep', '10': 'oct', '11': 'nov', '12': 'dec',
}

HEADER_FILL   = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT   = Font(color="FFFFFF", bold=True)
TITLE_FONT    = Font(bold=True, size=13)
RETURN_FILL   = PatternFill("solid", fgColor="C6EFCE")
THIN_BORDER   = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'),  bottom=Side(style='thin')
)


def derive_tab_name(filename: str) -> str:
    """
    Derive a tab name like 'oct-dec-25' from a filename containing
    Chinese month numbers, e.g. '10、11、12 月发货退货账单.xlsx'.
    """
    base = os.path.splitext(filename)[0]
    months = re.findall(r'\d+', base)
    # 4-digit year → last 2 digits
    year_match = re.search(r'20(\d{2})', base)
    year_suffix = year_match.group(1) if year_match else '25'

    month_tokens = [MONTH_MAP[m] for m in months if m in MONTH_MAP]
    if len(month_tokens) >= 2:
        return f"{month_tokens[0]}-{month_tokens[-1]}-{year_suffix}"
    elif len(month_tokens) == 1:
        return f"{month_tokens[0]}-{year_suffix}"
    return f"period-{year_suffix}"


def _connect_fdc():
    return pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};'
        f'SERVER={sql_server_config["server"]};'
        f'DATABASE={sql_server_config["database_fdc"]};'
        f'UID={sql_server_config["username"]};'
        f'PWD={sql_server_config["password"]}'
    )


def _normalize_sku(sku: str) -> str:
    """Normalize SKU for lookup: '/' → space, trim, uppercase (case-insensitive match)."""
    if sku is None:
        return ''
    return str(sku).replace('/', ' ').strip().upper()


def _load_sku_db() -> dict:
    """
    Load erwan_sku_database.json and return a dict keyed by normalized SKU.
    {normalized_sku: {'cost': float, 'msrp': float, 'pkg_fee': float}}
    """
    try:
        with open(SKU_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return {_normalize_sku(entry['sku']): {
            'cost':    float(entry.get('cost', 0)),
            'msrp':    float(entry.get('msrp', 0)),
            'pkg_fee': float(entry.get('pkg_fee', 3)),
        } for entry in data if entry.get('sku')}
    except Exception:
        return {}


def _save_sku_db_entries(new_entries: dict) -> None:
    """
    Merge new_entries {normalized_sku: {'cost', 'msrp', 'pkg_fee'}} into the JSON file.
    Existing SKUs are NOT overwritten; only truly new SKUs are appended.
    """
    try:
        with open(SKU_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = []

    existing_keys = {_normalize_sku(e['sku']) for e in data if e.get('sku')}
    for sku_norm, info in new_entries.items():
        if sku_norm not in existing_keys:
            data.append({'sku': sku_norm, 'cost': info['cost'], 'msrp': info.get('msrp', 0), 'pkg_fee': info['pkg_fee']})
            existing_keys.add(sku_norm)

    data.sort(key=lambda x: x['sku'])
    with open(SKU_DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_costs_from_db(sku_list: list) -> dict:
    """
    For SKUs not found in the JSON file, fall back to dbo.inv (SALES_COST, RETAIL_PRS).
    Returns {normalized_sku: {'cost': float, 'msrp': float, 'pkg_fee': float}}.
    New SKUs found in DB are persisted back to the JSON file.
    """
    result = {}
    if not sku_list:
        return result

    missing = list(sku_list)

    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        placeholders = ','.join(['?' for _ in missing])
        cursor.execute(
            f'SELECT PROD_CD, SALES_COST, RETAIL_PRS FROM dbo.inv WHERE PROD_CD IN ({placeholders})',
            missing
        )
        db_new = {}
        for row in cursor.fetchall():
            sku = str(row[0]).strip()
            db_new[sku] = {
                'cost':    round(float(row[1] or 0), 2),
                'msrp':    round(float(row[2] or 0), 2),
                'pkg_fee': 3.0,
            }
        conn.close()

        # Persist newly found SKUs to JSON
        if db_new:
            _save_sku_db_entries(db_new)
        result.update(db_new)
    except Exception:
        pass

    # Anything still missing gets zero defaults
    for sku in missing:
        if sku not in result:
            result[sku] = {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0}

    return result


# ---------------------------------------------------------------------------
# Excel sheet reading helpers
# ---------------------------------------------------------------------------

def _find_sheet(wb, keywords: list):
    """Return the first sheet whose name contains any of the keywords (case-insensitive)."""
    for name in wb.sheetnames:
        for kw in keywords:
            if kw in name or kw.lower() in name.lower():
                return wb[name]
    return None


def _sheet_preview(sheet, max_rows=10) -> list:
    """Return first max_rows rows as list-of-lists for debugging."""
    preview = []
    for row in sheet.iter_rows(min_row=1, max_row=max_rows, values_only=True):
        preview.append(list(row))
    return preview


def _read_sku_qty(sheet) -> tuple:
    """
    Read (sku, qty) pairs from a sheet.
    Returns (data_list, debug_info_dict).
    Requires BOTH sku AND qty header keywords found in the SAME row,
    and the two columns must be different.
    """
    sku_col = qty_col = None
    header_row_idx = None
    debug = {'sheet': sheet.title, 'preview': _sheet_preview(sheet), 'sku_col': None, 'qty_col': None, 'header_row': None}

    # Specific keywords only — avoid single-character or overly generic tokens
    SKU_KEYWORDS = ['sku', '款号', '货号', '品号', '产品编号', '型号', '货品编号', '颜色编号']
    QTY_KEYWORDS = ['数量', 'qty', '件数', '出货数量', '发货数量', '出货件数', '发货件数']

    # Scan row by row — require BOTH columns found in the SAME row and different columns
    for row in sheet.iter_rows(max_row=15):
        row_sku = row_qty = None
        for cell in row:
            val = str(cell.value or '').strip().lower()
            if not val:
                continue
            if row_sku is None and any(k in val for k in SKU_KEYWORDS):
                row_sku = cell.column
            if row_qty is None and any(k in val for k in QTY_KEYWORDS):
                row_qty = cell.column
        if row_sku and row_qty and row_sku != row_qty:
            sku_col = row_sku
            qty_col = row_qty
            header_row_idx = row[0].row
            break

    # Fallback: find first data row that looks like (non-empty-string, positive-number)
    if not (sku_col and qty_col):
        for ri, row in enumerate(sheet.iter_rows(min_row=1, max_row=sheet.max_row, values_only=True), start=1):
            if len(row) < 2:
                continue
            for c_offset in range(len(row) - 1):
                a = row[c_offset]
                b = row[c_offset + 1]
                if a and isinstance(a, str) and len(a.strip()) > 1:
                    try:
                        v = float(b)
                        if v > 0:
                            sku_col = c_offset + 1
                            qty_col = c_offset + 2
                            header_row_idx = ri - 1  # data starts at this row (it IS data, not header)
                            break
                    except (ValueError, TypeError):
                        pass
            if sku_col:
                break

    if not (sku_col and qty_col):
        sku_col, qty_col = 1, 2
        header_row_idx = 0

    debug['sku_col'] = sku_col
    debug['qty_col'] = qty_col
    debug['header_row'] = header_row_idx

    SKIP_VALUES = {'sku', '款号', '货号', '品号', '型号', '合计', 'total', '总计', '小计', ''}

    data = []
    start = (header_row_idx or 0) + 1
    for row in sheet.iter_rows(min_row=start, values_only=True):
        if len(row) < max(sku_col, qty_col):
            continue
        sku_val = row[sku_col - 1]
        qty_val = row[qty_col - 1]
        if sku_val and qty_val is not None:
            sku_str = str(sku_val).strip()
            if sku_str.lower() in SKIP_VALUES:
                continue
            try:
                qty = float(qty_val)
                if qty > 0:
                    data.append((sku_str, qty))
            except (ValueError, TypeError):
                pass
    return data, debug


def _read_return_rows(sheet) -> list:
    """Read all non-empty rows from the 退货 sheet as raw lists."""
    rows = []
    for row in sheet.iter_rows(values_only=True):
        if any(v is not None for v in row):
            rows.append(list(row))
    return rows


def _parse_return_rows(raw_rows: list) -> list:
    """Parse raw return rows into structured dicts.

    Locates the header row by scanning for the '退款' keyword, then extracts
    sku (款号/颜色), qty (数量), refund (退款), and source_sku (SKU) per data row.

    Returns a list of dicts: {sku, qty, refund, source_sku}
    """
    if not raw_rows:
        return []

    # Find header row index and column positions
    sku_col = qty_col = refund_col = src_sku_col = None
    header_idx = None
    tracking_col = date_col = None
    for i, row in enumerate(raw_rows):
        row_str = [str(v).strip() if v is not None else '' for v in row]
        if '退款' in row_str or any('退款' in s for s in row_str):
            # locate columns by keyword
            for ci, val in enumerate(row_str):
                v = val.lower()
                if ('款号' in v or '颜色' in v) and sku_col is None:
                    sku_col = ci
                elif '数量' in v and qty_col is None:
                    qty_col = ci
                elif '快递' in v and tracking_col is None:
                    tracking_col = ci
                elif ('签收' in v or '日期' in v) and date_col is None:
                    date_col = ci
                elif '退款' in v and refund_col is None:
                    refund_col = ci
                elif v == 'sku' and src_sku_col is None:
                    src_sku_col = ci
            header_idx = i
            break

    # Fallback column positions based on known structure if detection failed
    # 款号/颜色(0) 数量(1) 快递单号(2) 签收日期(3) 退款(4) SKU(5)
    if header_idx is None:
        sku_col, qty_col, tracking_col, date_col, refund_col, src_sku_col = 0, 1, 2, 3, 4, 5
    else:
        # Apply per-column fallbacks for any that weren't individually detected
        if sku_col      is None: sku_col      = 0
        if qty_col      is None: qty_col      = 1
        if tracking_col is None: tracking_col = 2
        if date_col     is None: date_col     = 3
        if refund_col   is None: refund_col   = 4
        if src_sku_col  is None: src_sku_col  = 5

    result = []
    start = (header_idx + 1) if header_idx is not None else 0
    for row in raw_rows[start:]:
        if not any(v is not None for v in row):
            continue
        def _get(col, default=None):
            if col is None or col >= len(row):
                return default
            return row[col]

        sku_val = _get(sku_col, '')
        if not sku_val:
            continue
        sku_str = str(sku_val).strip()
        if not sku_str or sku_str in ('款号/颜色', '款号', 'SKU'):
            continue   # skip stray header rows
        # Skip title/section rows — product SKUs must contain a letter+digit pattern
        import re as _re
        if not _re.search(r'[A-Za-z]\d|\d[A-Za-z]', sku_str):
            continue

        try:
            qty_val = float(_get(qty_col, 0) or 0)
        except (TypeError, ValueError):
            qty_val = 0

        refund_raw = _get(refund_col)
        try:
            refund_val = float(refund_raw) if refund_raw is not None else 0.0
        except (TypeError, ValueError):
            refund_val = 0.0

        tracking_raw = _get(tracking_col, '')
        tracking_str = str(tracking_raw).strip() if tracking_raw is not None else ''

        date_raw = _get(date_col, '')
        date_str = str(date_raw).strip() if date_raw is not None else ''

        src_sku_raw = _get(src_sku_col, '')
        src_sku_str = str(src_sku_raw).strip() if src_sku_raw is not None else ''

        result.append({
            'sku':        sku_str,
            'qty':        qty_val,
            'tracking':   tracking_str,
            'date':       date_str,
            'refund':     refund_val,
            'source_sku': src_sku_str,
        })

    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _style_header_row(sheet, row, ncols):
    for col in range(1, ncols + 1):
        cell = sheet.cell(row=row, column=col)
        cell.fill  = HEADER_FILL
        cell.font  = HEADER_FONT
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = THIN_BORDER


def _auto_col_widths(sheet):
    for col in sheet.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        sheet.column_dimensions[col_letter].width = max(10, min(max_len + 4, 40))


# ---------------------------------------------------------------------------
# Core processor
# ---------------------------------------------------------------------------

def process_erwan_file(file_bytes: bytes, filename: str,
                       template_bytes: bytes = None) -> tuple:
    """
    Parameters
    ----------
    file_bytes      : raw bytes of the uploaded billing file
    filename        : original filename (used for tab-name derivation)
    template_bytes  : optional raw bytes of an existing Channel_ERWAN workbook
                      to append into; a fresh workbook is created if None

    Returns
    -------
    (excel_bytes, error_message)  –  error_message is None on success
    """
    # --- Load uploaded source file ---
    try:
        wb_in = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return None, f"Cannot open uploaded file: {e}"

    # --- Find 发货总计 sheet ---
    fa_sheet = _find_sheet(wb_in, ['发货总计', '发货', '总计'])
    if fa_sheet is None:
        sheets_found = ', '.join(wb_in.sheetnames)
        return None, f"Sheet '发货总计' not found. Found sheets: {sheets_found}"

    shipped_data, fa_debug = _read_sku_qty(fa_sheet)
    if not shipped_data:
        return None, (
            f"No SKU/qty data found in '{fa_sheet.title}'. "
            f"Debug: sku_col={fa_debug['sku_col']}, qty_col={fa_debug['qty_col']}, "
            f"header_row={fa_debug['header_row']}, "
            f"preview={fa_debug['preview'][:3]}"
        )

    # --- Find 退货 sheet ---
    return_sheet = _find_sheet(wb_in, ['退货'])
    return_rows  = _read_return_rows(return_sheet) if return_sheet else []

    # --- Derive tab name ---
    tab_name = derive_tab_name(filename)

    # --- Load or create output workbook ---
    if template_bytes:
        try:
            wb_out = openpyxl.load_workbook(BytesIO(template_bytes))
        except Exception:
            wb_out = openpyxl.Workbook()
            if 'Sheet' in wb_out.sheetnames:
                del wb_out['Sheet']
    else:
        wb_out = openpyxl.Workbook()
        if 'Sheet' in wb_out.sheetnames:
            del wb_out['Sheet']

    # --- Load costs from JSON SKU database ---
    all_skus      = [s for s, _ in shipped_data]
    all_skus_norm = [_normalize_sku(s) for s in all_skus]
    sku_db        = _load_sku_db()

    # For any SKU not in JSON, fall back to dbo.inv and persist the result
    missing_skus = [s for s in all_skus_norm if s not in sku_db]
    if missing_skus:
        fetched = fetch_costs_from_db(missing_skus)  # also saves to JSON
        sku_db.update(fetched)

    db_entries = {}
    for sku_norm in all_skus_norm:
        info = sku_db.get(sku_norm, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
        db_entries[sku_norm] = info

    MARKUP = 0.20   # 20% markup over cost

    # --- Create / replace the billing tab ---
    if tab_name in wb_out.sheetnames:
        del wb_out[tab_name]
    ws = wb_out.create_sheet(tab_name)

    # Row 1 – title
    ws['A1'] = f'Channel ERWAN  —  {tab_name}'
    ws['A1'].font = TITLE_FONT
    ws.merge_cells('A1:J1')

    # Row 2 – column headers
    # A: SKU(原)  B: 数量  C: SKU(DB)  D: 成本  E: MSRP  F: mark up  G: 你的价  H: 人工包装  I: 单价  J: 小计
    col_headers = [
        'SKU',          # A – original (with /)
        '数量',          # B – qty
        'SKU (DB)',      # C – normalized
        '成本 Cost',     # D
        'MSRP',          # E
        'mark up',       # F – 20%
        '你的价',         # G = D*(1+F)
        '人工包装',       # H – pkg_fee
        '单价',           # I = G+H
        '小计',           # J = I*B
    ]
    for col_i, header in enumerate(col_headers, start=1):
        ws.cell(2, col_i, header)
    _style_header_row(ws, 2, len(col_headers))

    # Rows 3+ – shipped data
    DATA_START = 3
    for i, (sku, qty) in enumerate(shipped_data):
        r = DATA_START + i
        norm = _normalize_sku(sku)
        info = db_entries.get(norm, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
        ws.cell(r, 1, sku)                                                    # A – SKU original
        ws.cell(r, 2, qty)                                                    # B – qty
        ws.cell(r, 3, norm)                                                   # C – SKU normalized
        ws.cell(r, 4, info['cost'])                                           # D – cost
        ws.cell(r, 5, info.get('msrp', 0))                                   # E – MSRP
        ws.cell(r, 6, MARKUP)                                                 # F – markup
        ws.cell(r, 6).number_format = '0%'
        ws.cell(r, 7).value = f'=ROUND(D{r}*(1+F{r}),2)'                     # G – 你的价
        ws.cell(r, 8, info['pkg_fee'])                                        # H – pkg_fee
        ws.cell(r, 9).value = f'=G{r}+H{r}'                                  # I – 单价
        ws.cell(r, 10).value = f'=ROUND(I{r}*B{r},2)'                        # J – 小计

        for col_i in range(1, 11):
            ws.cell(r, col_i).border = THIN_BORDER

    last_sku_row = DATA_START + len(shipped_data) - 1

    # Totals row
    tot_r = last_sku_row + 1
    ws.cell(tot_r, 1, '合计 TOTAL').font = Font(bold=True)
    for col_i in [2, 10]:
        col_letter = get_column_letter(col_i)
        ws.cell(tot_r, col_i).value = (
            f'=SUM({col_letter}{DATA_START}:{col_letter}{last_sku_row})'
        )
        ws.cell(tot_r, col_i).font = Font(bold=True)

    # --- Returns section (6 raw columns from source) ---
    parsed_returns = _parse_return_rows(return_rows) if return_rows else []
    if parsed_returns:
        ret_title_row = last_sku_row + 10
        ws.cell(ret_title_row, 1, '退货数据').font = Font(bold=True, color="C00000")
        ws.merge_cells(f'A{ret_title_row}:F{ret_title_row}')

        ret_header_row = ret_title_row + 1
        ret_headers = ['款号/颜色', '数量', '快递单号', '签收日期', '退款', 'SKU']
        for col_i, h in enumerate(ret_headers, start=1):
            ws.cell(ret_header_row, col_i, h)
        _style_header_row(ws, ret_header_row, len(ret_headers))
        ws[f'A{ret_header_row}'].fill = PatternFill("solid", fgColor="C00000")

        for j, rd in enumerate(parsed_returns, start=1):
            r = ret_header_row + j
            norm_ret = _normalize_sku(rd['sku'])
            info = sku_db.get(norm_ret, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
            refund_val = -round(info['cost'] * (1 + MARKUP), 2)  # 退款 = -(你的价) = -(cost * 1.2)
            ws.cell(r, 1, rd['sku'])
            ws.cell(r, 2, rd['qty'])
            ws.cell(r, 3, rd.get('tracking', ''))
            ws.cell(r, 4, rd.get('date', ''))
            ws.cell(r, 5, refund_val)
            ws.cell(r, 6, rd['source_sku'])
            for col_i in range(1, 7):
                ws.cell(r, col_i).border = THIN_BORDER

        # Totals row for returns
        ret_last_row = ret_header_row + len(parsed_returns)
        ret_tot_r = ret_last_row + 1
        ws.cell(ret_tot_r, 1, '退货合计').font = Font(bold=True, color="C00000")
        ws.cell(ret_tot_r, 2).value = f'=SUM(B{ret_header_row+1}:B{ret_last_row})'
        ws.cell(ret_tot_r, 5).value = f'=SUM(E{ret_header_row+1}:E{ret_last_row})'
        for col_i in [1, 2, 5]:
            ws.cell(ret_tot_r, col_i).font = Font(bold=True, color="C00000")

    # --- P-R column list (starting at DATA_START) ---
    # Column P uses SKU DB format (space-separated), same as col C — not vendor slash format.
    ws.cell(2, 16, 'SKU (DB)').font = Font(bold=True)
    ws.cell(2, 17, '数量 Qty').font   = Font(bold=True)
    ws.cell(2, 18, '单价 Price').font = Font(bold=True)
    for col_i in (16, 17, 18):
        ws.cell(2, col_i).fill   = HEADER_FILL
        ws.cell(2, col_i).font   = Font(color="FFFFFF", bold=True)
        ws.cell(2, col_i).border = THIN_BORDER

    for i, (sku, qty) in enumerate(shipped_data):
        r = DATA_START + i
        norm = _normalize_sku(sku)
        ws.cell(r, 16, f'{norm} ')         # P – SKU DB format + trailing space for import quirks
        ws.cell(r, 17, qty)                 # Q – quantity
        ws.cell(r, 18).value = f'=I{r}'    # R – 单价 (links to col I)
        for col_i in (16, 17, 18):
            ws.cell(r, col_i).border = THIN_BORDER

    # --- Final formatting ---
    _auto_col_widths(ws)
    ws.freeze_panes = 'A3'

    # --- Serialize to bytes ---
    output = BytesIO()
    wb_out.save(output)
    output.seek(0)
    return output.getvalue(), None


# ---------------------------------------------------------------------------
# Preview — returns structured JSON data for rendering on the page
# ---------------------------------------------------------------------------

def process_erwan_preview(file_bytes: bytes, filename: str) -> tuple:
    """
    Parses the billing file and returns:
      (preview_dict, error_message)

    preview_dict keys:
      tab_name      : derived tab name (e.g. 'oct-dec-25')
      shipped       : list of {sku, qty, cost, pkg_fee, unit_total, total_cost,
                               unit_price, total_revenue, profit, margin_pct}
      returns       : list of raw row lists from 退货 sheet
      db_error      : str or None
      erwan_prices  : {sku: price}  (for display)
    """
    try:
        wb_in = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return None, f"Cannot open uploaded file: {e}"

    fa_sheet = _find_sheet(wb_in, ['发货总计', '发货', '总计'])
    if fa_sheet is None:
        return None, f"Sheet '发货总计' not found. Found: {', '.join(wb_in.sheetnames)}"

    shipped_data, fa_debug = _read_sku_qty(fa_sheet)
    if not shipped_data:
        return None, (
            f"No SKU/qty data found in '{fa_sheet.title}'. "
            f"Debug: sku_col={fa_debug['sku_col']}, qty_col={fa_debug['qty_col']}, "
            f"preview={fa_debug['preview'][:3]}"
        )

    return_sheet = _find_sheet(wb_in, ['退货'])
    return_rows  = _read_return_rows(return_sheet) if return_sheet else []

    tab_name = derive_tab_name(filename)
    all_skus      = [s for s, _ in shipped_data]
    all_skus_norm = [_normalize_sku(s) for s in all_skus]

    # Load JSON SKU database; fall back to dbo.inv for missing SKUs
    sku_db = _load_sku_db()
    missing_skus = [s for s in all_skus_norm if s not in sku_db]
    db_fetch_error = None
    if missing_skus:
        fetched = fetch_costs_from_db(missing_skus)  # also persists to JSON
        sku_db.update(fetched)

    MARKUP = 0.20

    # Build db_entries keyed by normalized SKU
    db_entries = {}
    for sku_norm in all_skus_norm:
        info = sku_db.get(sku_norm, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
        db_entries[sku_norm] = {
            'cost':    info['cost'],
            'msrp':    info.get('msrp', 0.0),
            'pkg_fee': info['pkg_fee'],
        }

    # Build shipped rows
    shipped_rows = []
    for sku, qty in shipped_data:
        norm    = _normalize_sku(sku)
        info    = db_entries.get(norm, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
        cost    = info['cost']
        msrp    = info['msrp']
        pkg_fee = info['pkg_fee']
        nide_jia  = round(cost * (1 + MARKUP), 2)   # 你的价 = cost * 1.2
        dan_jia   = round(nide_jia + pkg_fee, 2)      # 单价
        xiao_ji   = round(dan_jia * qty, 2)           # 小计

        shipped_rows.append({
            'sku':      sku,
            'sku_norm': norm,
            'qty':      qty,
            'cost':     cost,
            'msrp':     msrp,
            'markup':   MARKUP,
            'nide_jia': nide_jia,
            'pkg_fee':  pkg_fee,
            'dan_jia':  dan_jia,
            'xiao_ji':  xiao_ji,
        })

    # Build return rows (6 raw columns); refund = cost * 1.2 from SKU DB
    parsed_returns = _parse_return_rows(return_rows)
    return_out = []
    for rd in parsed_returns:
        norm_ret = _normalize_sku(rd['sku'])
        info     = sku_db.get(norm_ret, {'cost': 0.0, 'msrp': 0.0, 'pkg_fee': 3.0})
        refund_val = -round(info['cost'] * (1 + MARKUP), 2)  # 退款 = -(你的价) = -(cost * 1.2)
        return_out.append({
            'sku':        rd['sku'],
            'qty':        rd['qty'],
            'tracking':   rd.get('tracking', ''),
            'date':       rd.get('date', ''),
            'refund':     refund_val,
            'source_sku': rd['source_sku'],
        })

    return {
        'tab_name': tab_name,
        'shipped':  shipped_rows,
        'returns':  return_out,
        'db_error': db_fetch_error,
    }, None

