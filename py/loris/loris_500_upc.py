"""Read/write Loris 500 UPC workbook (Dropbox Elaine folder)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime

from openpyxl import load_workbook

from py.loris.loris_sku_meta import normalize_storage_sku, sku_key_variants

UPC_FILENAME = 'Copy of 500 UPC.xlsx'
UPC_WORK_START_ROW = 1300


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


def load_500_upc_rows():
    """
    Load UPC/SKU rows from the workbook.

    Returns (rows, file_path, stats dict).
    """
    path = resolve_500_upc_path()
    if not path:
        return [], None, {'found': False, 'total': 0, 'matched': 0, 'unmatched': 0}

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    matched = 0
    for idx, cells in enumerate(
        ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=3, values_only=True),
        start=1,
    ):
        upc = normalize_upc(cells[0])
        sku = normalize_sku(cells[1] if len(cells) > 1 else None)
        source = normalize_source(cells[2] if len(cells) > 2 else None)
        if sku:
            matched += 1
        rows.append({'row': idx, 'upc': upc, 'sku': sku, 'source': source})
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


def save_500_upc_skus(updates):
    """
    Persist SKU values back to column B and optional source to column C.

    updates: list of {'row': int, 'sku': str, 'auto': bool (optional)}
    """
    path = resolve_500_upc_path()
    if not path:
        raise FileNotFoundError(f'Workbook not found: {UPC_FILENAME}')

    backup_path = path + '.bak'
    shutil.copy2(path, backup_path)

    wb = load_workbook(path)
    ws = wb.active
    for item in updates:
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
    wb.save(path)
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
