"""Read/write Loris 500 UPC workbook (Dropbox Elaine folder)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime

from openpyxl import load_workbook

from py.loris.loris_sku_meta import normalize_storage_sku, sku_key_variants

UPC_FILENAME = 'Copy of 500 UPC.xlsx'
UPC_WORK_START_ROW = 1300
ALIAS_SHEET_NAME = 'Alias'
ALIAS_MAX = 5
ALIAS_HEADERS = ['SKU', 'Alias 1', 'Alias 2', 'Alias 3', 'Alias 4', 'Alias 5']


def resolve_500_upc_path():
    """Return path to the 500 UPC workbook, or None if not found."""
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, 'Dropbox', 'Elaine', UPC_FILENAME),
        os.path.join(home, 'Dropbox', 'elaine', UPC_FILENAME),
        os.path.join(home, 'dropbox', 'elaine', UPC_FILENAME),
        r'C:\Users\stream\Dropbox\Elaine\Copy of 500 UPC.xlsx',
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.normpath(path)
    return None


def normalize_upc(value) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    if not text or text.lower() == 'nan':
        return ''
    if 'e+' in text.lower() or 'e-' in text.lower():
        try:
            text = f'{float(text):.0f}'
        except ValueError:
            pass
    elif text.endswith('.0'):
        text = text[:-2]
    return text


def normalize_sku(value) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    return '' if not text or text.lower() == 'nan' else text


def normalize_source(value) -> str:
    if value is None:
        return ''
    text = str(value).strip()
    return '' if not text or text.lower() == 'nan' else text


def parse_aliases(value, sku: str = '', max_n: int = ALIAS_MAX) -> list[str]:
    """Split a list or comma/semicolon string into up to 5 aliases (not equal to SKU)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        text = str(value).replace(';', ',').replace('\n', ',')
        parts = text.split(',')
    sku_key = normalize_sku(sku).upper()
    out = []
    seen = set()
    for part in parts:
        alias = normalize_sku(part)
        if not alias:
            continue
        key = alias.upper()
        if key == sku_key or key in seen:
            continue
        seen.add(key)
        out.append(alias)
        if len(out) >= max_n:
            break
    return out


def _main_sheet(wb):
    if 'Sheet1' in wb.sheetnames:
        return wb['Sheet1']
    return wb[wb.sheetnames[0]]


def _unique_skus_from_sheet1(ws) -> list[str]:
    ordered = []
    seen = set()
    for row in ws.iter_rows(min_row=1, min_col=2, max_col=2, values_only=True):
        sku = normalize_sku(row[0] if row else None)
        if not sku:
            continue
        key = sku.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(sku)
    return ordered


def _alias_row_map(ws) -> dict[str, int]:
    mapping = {}
    for row_num in range(2, (ws.max_row or 1) + 1):
        sku = normalize_sku(ws.cell(row_num, 1).value)
        if sku:
            mapping[sku.upper()] = row_num
    return mapping


def _load_alias_map_from_ws(ws) -> dict[str, list[str]]:
    mapping = {}
    for cells in ws.iter_rows(min_row=2, max_col=1 + ALIAS_MAX, values_only=True):
        sku = normalize_sku(cells[0] if cells else None)
        if not sku:
            continue
        mapping[sku.upper()] = parse_aliases(list(cells[1:1 + ALIAS_MAX]), sku=sku)
    return mapping


def _ensure_alias_sheet(wb, seed_skus: list[str] | None = None):
    """Create Alias sheet if missing; add any SKUs not already in column A."""
    if ALIAS_SHEET_NAME in wb.sheetnames:
        ws = wb[ALIAS_SHEET_NAME]
    else:
        ws = wb.create_sheet(ALIAS_SHEET_NAME)
        for col, header in enumerate(ALIAS_HEADERS, start=1):
            ws.cell(1, col, header)
        ws.freeze_panes = 'A2'
        ws.column_dimensions['A'].width = 18
        for col_letter in ('B', 'C', 'D', 'E', 'F'):
            ws.column_dimensions[col_letter].width = 16

    if ws.cell(1, 1).value in (None, ''):
        for col, header in enumerate(ALIAS_HEADERS, start=1):
            ws.cell(1, col, header)

    existing = _alias_row_map(ws)
    next_row = max((ws.max_row or 1) + 1, 2)
    for sku in seed_skus or []:
        sku = normalize_sku(sku)
        if not sku:
            continue
        key = sku.upper()
        if key in existing:
            continue
        ws.cell(next_row, 1, sku)
        existing[key] = next_row
        next_row += 1
    return ws, existing


def _write_alias_row(ws, row_num: int, sku: str, aliases: list[str]):
    ws.cell(row_num, 1, sku)
    cleaned = parse_aliases(aliases, sku=sku)
    for offset in range(ALIAS_MAX):
        ws.cell(row_num, 2 + offset, cleaned[offset] if offset < len(cleaned) else '')


def ensure_500_upc_alias_sheet() -> str:
    """Create/seed the Alias sheet from unique Sheet1 SKUs. Returns workbook path."""
    path = resolve_500_upc_path()
    if not path:
        raise FileNotFoundError(f'Workbook not found: {UPC_FILENAME}')

    backup_path = path + '.bak'
    shutil.copy2(path, backup_path)

    wb = load_workbook(path)
    try:
        seed = _unique_skus_from_sheet1(_main_sheet(wb))
        _ensure_alias_sheet(wb, seed_skus=seed)
        wb.save(path)
    finally:
        wb.close()
    return path


def load_500_upc_rows():
    """
    Load UPC/SKU rows from the workbook.

    Returns (rows, file_path, stats dict).
    """
    path = resolve_500_upc_path()
    if not path:
        return [], None, {'found': False, 'total': 0, 'matched': 0, 'unmatched': 0}

    peek = load_workbook(path, read_only=True)
    try:
        has_alias_sheet = ALIAS_SHEET_NAME in peek.sheetnames
    finally:
        peek.close()
    if not has_alias_sheet:
        ensure_500_upc_alias_sheet()

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = _main_sheet(wb)
    alias_map = {}
    if ALIAS_SHEET_NAME in wb.sheetnames:
        alias_map = _load_alias_map_from_ws(wb[ALIAS_SHEET_NAME])
    rows = []
    matched = 0
    for idx, cells in enumerate(
        ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=3, values_only=True),
        start=1,
    ):
        upc = normalize_upc(cells[0])
        sku = normalize_sku(cells[1] if len(cells) > 1 else None)
        source = normalize_source(cells[2] if len(cells) > 2 else None)
        aliases = alias_map.get(sku.upper(), []) if sku else []
        if sku:
            matched += 1
        rows.append({
            'row': idx,
            'upc': upc,
            'sku': sku,
            'aliases': aliases,
            'source': source,
        })
    wb.close()

    total = len(rows)
    return rows, path, {
        'found': True,
        'path': path,
        'total': total,
        'matched': matched,
        'unmatched': total - matched,
        'modified': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M:%S'),
    }


def find_first_unmatched_row(rows, after_row=0):
    """First Excel row (1-based) with UPC but empty SKU, optionally after a row cutoff."""
    for item in rows:
        row_num = item.get('row', 0)
        if after_row and row_num <= after_row:
            continue
        if item.get('upc') and not normalize_sku(item.get('sku')):
            return item['row']
    return None


def save_500_upc_skus(updates, alias_updates=None):
    """
    Persist SKU values back to column B and optional source to column C.
    Optional alias_updates: list of {'sku': str, 'aliases': list[str]} written to Alias sheet.

    updates: list of {'row': int, 'sku': str, 'auto': bool (optional)}
    """
    path = resolve_500_upc_path()
    if not path:
        raise FileNotFoundError(f'Workbook not found: {UPC_FILENAME}')

    backup_path = path + '.bak'
    shutil.copy2(path, backup_path)

    wb = load_workbook(path)
    try:
        ws = _main_sheet(wb)
        for item in updates or []:
            row_num = int(item['row'])
            sku = normalize_sku(item.get('sku'))
            cell_b = ws.cell(row=row_num, column=2)
            cell_c = ws.cell(row=row_num, column=3)
            if sku:
                cell_b.value = sku
                if item.get('auto'):
                    cell_c.value = 'auto'
            else:
                # openpyxl keeps the old value when assigned None; clear explicitly
                cell_b.value = ''
                cell_c.value = ''

        seed = _unique_skus_from_sheet1(ws)
        alias_ws, existing = _ensure_alias_sheet(wb, seed_skus=seed)
        for item in alias_updates or []:
            sku = normalize_sku(item.get('sku'))
            if not sku:
                continue
            aliases = parse_aliases(item.get('aliases'), sku=sku)
            row_num = existing.get(sku.upper())
            if not row_num:
                row_num = max((alias_ws.max_row or 1) + 1, 2)
                existing[sku.upper()] = row_num
            _write_alias_row(alias_ws, row_num, sku, aliases)

        wb.save(path)
    finally:
        wb.close()

    return path, backup_path


def find_sku_rows(rows=None) -> dict[str, int]:
    """Map normalized SKU -> Excel row number (1-based)."""
    if rows is None:
        rows, _, stats = load_500_upc_rows()
        if not stats.get('found'):
            return {}
    out = {}
    for item in rows:
        sku = normalize_storage_sku(normalize_sku(item.get('sku')))
        if sku:
            for key in sku_key_variants(sku):
                out[key.upper()] = item['row']
    return out


def update_upcs_by_sku(updates):
    """
    Update column A (UPC) for rows matched by SKU in column B.

    updates: list of {'sku': str, 'upc': str}
    """
    path = resolve_500_upc_path()
    if not path:
        raise FileNotFoundError(f'Workbook not found: {UPC_FILENAME}')

    rows, _, stats = load_500_upc_rows()
    if not stats.get('found'):
        raise FileNotFoundError(f'Workbook not found: {UPC_FILENAME}')

    sku_rows = find_sku_rows(rows)
    backup_path = path + '.bak'
    shutil.copy2(path, backup_path)

    wb = load_workbook(path)
    ws = wb.active
    updated = 0
    for item in updates:
        sku = normalize_storage_sku(normalize_sku(item.get('sku')))
        upc = normalize_upc(item.get('upc'))
        if not sku or not upc:
            continue
        row_num = None
        for key in sku_key_variants(sku):
            row_num = sku_rows.get(key.upper())
            if row_num:
                break
        if not row_num:
            continue
        ws.cell(row=row_num, column=1, value=upc)
        updated += 1
    wb.save(path)
    wb.close()
    return path, backup_path, updated
