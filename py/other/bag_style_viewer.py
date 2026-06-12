from flask import Blueprint, render_template, request, jsonify, send_file, current_app, url_for,session,redirect
import os
import io
import re
import datetime
import pandas as pd
import pyodbc
from openpyxl import Workbook
from openpyxl.styles import Alignment

from py.utilities.database_utils import get_mysql_connection
from py.utilities.dam_utils import get_canonical_asset_url, search_assets_by_sku_pattern
from py.utilities.setting import Config

bag_style_viewer_bp = Blueprint('bag_style_viewer', __name__, template_folder='templates')

sql_server_config = Config.SQL_SERVER_CONFIG

# ----- Helpers -----

def get_stock_data_for_styles(styles):
    """Query SQL Server to get stock and ocean data for a list of styles"""
    if not styles:
        return {}
    
    query = """
    SELECT 
        i.ALT_CD AS style,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS total_quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS total_ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD IN ({})
    GROUP BY i.ALT_CD
    """.format(','.join(['?' for _ in styles]))
    
    stock_data = {}
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        
        for conn in [conn_fdc, conn_ef]:
            cursor = conn.cursor()
            cursor.execute(query, styles)
            rows = cursor.fetchall()
            for style, quantity, ocean in rows:
                if style:
                    style_upper = style.strip().upper()
                    if style_upper not in stock_data:
                        stock_data[style_upper] = {'quantity': 0, 'ocean': 0}
                    stock_data[style_upper]['quantity'] += (quantity or 0)
                    stock_data[style_upper]['ocean'] += (ocean or 0)
            cursor.close()
            conn.close()
    except Exception as e:
        current_app.logger.error(f"Error getting stock data: {e}")
    
    return stock_data

def get_detailed_stock_for_style(style):
    """Query SQL Server to get stock and ocean data by SKU (style + color) for a single style"""
    
    query = """
    SELECT 
        i.PROD_CD AS sku,
        i.ALT_CD AS style,
        i.UNIT_COLOR AS color,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD = ?
    GROUP BY i.PROD_CD, i.ALT_CD, i.UNIT_COLOR
    ORDER BY i.PROD_CD
    """
    
    detailed_stock = []
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        
        # Collect data from both databases
        sku_data = {}
        for conn in [conn_fdc, conn_ef]:
            cursor = conn.cursor()
            cursor.execute(query, [style])
            rows = cursor.fetchall()
            for sku, style_code, color, quantity, ocean in rows:
                if sku:
                    sku_key = sku.strip().upper()
                    if sku_key not in sku_data:
                        sku_data[sku_key] = {
                            'sku': sku_key,
                            'color': color.strip() if color else '',
                            'quantity': 0,
                            'ocean': 0
                        }
                    sku_data[sku_key]['quantity'] += (quantity or 0)
                    sku_data[sku_key]['ocean'] += (ocean or 0)
            cursor.close()
            conn.close()
        
        # Convert to list and calculate totals
        for sku_key, data in sku_data.items():
            data['total'] = data['quantity'] + data['ocean']
            detailed_stock.append(data)
        
        # Sort by SKU
        detailed_stock.sort(key=lambda x: x['sku'])
        
    except Exception as e:
        current_app.logger.error(f"Error getting detailed stock data: {e}")
    
    return detailed_stock

def fetch_all_descriptions():
    conn = get_mysql_connection()
    descriptions = []
    if not conn:
        return descriptions
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT description FROM onlinedb.description ORDER BY description")
        rows = cur.fetchall()
        descriptions = [r[0] for r in rows if r[0]]
    finally:
        cur.close()
        conn.close()
    return descriptions

def _style_meets_stock_color_filters(style, stock_filter=None, color_filter=None):
    """Return True if style passes stock/color filters (SKU-level when color is set)."""
    color_q = (color_filter or '').strip().upper()
    if color_q:
        details = get_detailed_stock_for_style(style)
        for item in details:
            color = (item.get('color') or '').strip().upper()
            if color_q not in color:
                continue
            qty = item.get('quantity', 0) or 0
            ocean = item.get('ocean', 0) or 0
            if stock_filter == 'instock':
                if qty <= 0:
                    continue
            elif stock_filter == 'ocean':
                if ocean <= 0:
                    continue
            elif stock_filter == 'both':
                if qty <= 0 and ocean <= 0:
                    continue
            return True
        return False

    if not stock_filter:
        return True

    stock_data = _stock_data_for_styles_batched([style])
    info = stock_data.get(style.upper(), {'quantity': 0, 'ocean': 0})
    qty = info['quantity']
    ocean = info['ocean']
    if stock_filter == 'instock':
        return qty > 0
    if stock_filter == 'ocean':
        return ocean > 0
    if stock_filter == 'both':
        return qty > 0 or ocean > 0
    return True


def _chunk_list(items, size=200):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _stock_data_for_styles_batched(styles):
    """Load stock/ocean for many styles without oversized SQL IN clauses."""
    stock_data = {}
    unique = list({str(s).strip() for s in (styles or []) if s and str(s).strip()})
    for chunk in _chunk_list(unique, 200):
        part = get_stock_data_for_styles(chunk)
        for key, vals in part.items():
            if key not in stock_data:
                stock_data[key] = {'quantity': 0, 'ocean': 0}
            stock_data[key]['quantity'] += vals.get('quantity', 0) or 0
            stock_data[key]['ocean'] += vals.get('ocean', 0) or 0
    return stock_data


def _normalize_description_list(descriptions):
    if descriptions is None:
        return []
    if isinstance(descriptions, str):
        return [d.strip() for d in descriptions.split(',') if d and d.strip()]
    return [str(d).strip() for d in descriptions if d and str(d).strip()]


def _description_is_missing(desc):
    norm = (desc or '').strip().upper()
    return norm in ('', 'N/A', 'NA')


def _style_passes_desc_filter(description_value, desc_filter):
    if not desc_filter or desc_filter == 'all':
        return True
    missing = _description_is_missing(description_value)
    if desc_filter == 'missing':
        return missing
    if desc_filter == 'has':
        return not missing
    return True


def _filter_styles_by_desc_filter(styles, desc_filter):
    if not desc_filter or desc_filter == 'all':
        return styles
    return [s for s in styles if _style_passes_desc_filter(s.get('description'), desc_filter)]


def fetch_descriptions_bulk(styles):
    """Map style (upper) -> description from MySQL."""
    out = {}
    conn = get_mysql_connection()
    if not conn:
        return out
    unique = list({str(s).strip().upper() for s in (styles or []) if s and str(s).strip()})
    if not unique:
        return out
    try:
        cur = conn.cursor()
        for chunk in _chunk_list(unique, 200):
            placeholders = ','.join(['%s'] * len(chunk))
            cur.execute(
                f"SELECT style, description FROM onlinedb.description WHERE style IN ({placeholders})",
                chunk,
            )
            for style, desc in cur.fetchall():
                if style:
                    out[str(style).strip().upper()] = desc
    finally:
        cur.close()
        conn.close()
    return out


def _fetch_inventory_stock_totals():
    """All ALT_CD stock/ocean totals merged across FDC + EF."""
    query = """
    SELECT
        RTRIM(LTRIM(i.ALT_CD)) AS style,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS total_quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS total_ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD IS NOT NULL AND LTRIM(RTRIM(i.ALT_CD)) <> ''
    GROUP BY RTRIM(LTRIM(i.ALT_CD))
    """
    stock_data = {}
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        for conn in [conn_fdc, conn_ef]:
            cursor = conn.cursor()
            cursor.execute(query)
            for style, quantity, ocean in cursor.fetchall():
                if not style:
                    continue
                style_upper = str(style).strip().upper()
                if style_upper not in stock_data:
                    stock_data[style_upper] = {'quantity': 0, 'ocean': 0}
                stock_data[style_upper]['quantity'] += (quantity or 0)
                stock_data[style_upper]['ocean'] += (ocean or 0)
            cursor.close()
            conn.close()
    except Exception as e:
        current_app.logger.error(f"Error fetching inventory stock totals: {e}")
    return stock_data


def fetch_inventory_styles_by_filters(stock_filter=None, color_filter=None, desc_filter=None):
    """Inventory styles filtered by stock/color and has/missing description."""
    if desc_filter not in ('has', 'missing'):
        return []

    totals = _fetch_inventory_stock_totals()
    candidates = []
    for style_key in sorted(totals.keys()):
        if not _style_meets_stock_color_filters(style_key, stock_filter, color_filter):
            continue
        candidates.append(style_key)
    if not candidates:
        return []

    desc_map = fetch_descriptions_bulk(candidates)
    results = []
    for code in candidates:
        desc = desc_map.get(code)
        if desc_filter == 'missing':
            if not _description_is_missing(desc):
                continue
            display_desc = ''
        else:
            if _description_is_missing(desc):
                continue
            display_desc = (desc or '').strip()
        results.append({
            'style': code,
            'description': display_desc,
            'image': _style_image_url(code),
        })
    return results


def fetch_description_options():
    """Lightweight description list for V7 quick filters (no stock counts)."""
    return [{'description': d} for d in fetch_all_descriptions()]


def fetch_description_counts(stock_filter=None, color_filter=None):
    """Return [{description, count}, ...] with counts reflecting stock/color filters."""
    conn = get_mysql_connection()
    by_desc = {}
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT description, style FROM onlinedb.description "
            "WHERE description IS NOT NULL AND TRIM(description) != '' "
            "ORDER BY description, style"
        )
        for desc, style in cur.fetchall():
            if not desc or not style:
                continue
            by_desc.setdefault(str(desc).strip(), []).append(str(style).strip())
    finally:
        cur.close()
        conn.close()

    color_q = (color_filter or '').strip()
    stock_f = stock_filter or None
    if not stock_f and not color_q:
        return [{'description': d, 'count': len(styles)} for d, styles in sorted(by_desc.items())]

    all_styles = list({s for styles in by_desc.values() for s in styles})
    stock_data = _stock_data_for_styles_batched(all_styles) if stock_f and not color_q else {}

    out = []
    for desc, styles in sorted(by_desc.items()):
        n = 0
        for style in styles:
            if color_q:
                if _style_meets_stock_color_filters(style, stock_f, color_q):
                    n += 1
            elif stock_f:
                info = stock_data.get(style.upper(), {'quantity': 0, 'ocean': 0})
                qty, ocean = info['quantity'], info['ocean']
                if stock_f == 'instock' and qty > 0:
                    n += 1
                elif stock_f == 'ocean' and ocean > 0:
                    n += 1
                elif stock_f == 'both' and (qty > 0 or ocean > 0):
                    n += 1
            else:
                n += 1
        out.append({'description': desc, 'count': n})
    return out


def fetch_style_codes_for_filters(descriptions, stock_filter=None, color_filter=None):
    """Return style codes for one or more descriptions with optional stock/color filters."""
    desc_list = _normalize_description_list(descriptions)
    if not desc_list:
        return []
    conn = get_mysql_connection()
    codes = []
    seen = set()
    if not conn:
        return codes
    try:
        cur = conn.cursor()
        placeholders = ','.join(['%s'] * len(desc_list))
        cur.execute(
            f"SELECT style FROM onlinedb.description WHERE description IN ({placeholders}) "
            "ORDER BY description, style",
            desc_list,
        )
        for (style,) in cur.fetchall():
            if not style:
                continue
            code = style.strip().upper()
            if code in seen:
                continue
            if _style_meets_stock_color_filters(style, stock_filter, color_filter):
                seen.add(code)
                codes.append(code)
    finally:
        cur.close()
        conn.close()
    return codes


def get_detailed_stock_for_styles_batch(styles):
    """Batch SKU-level stock for many styles (FDC + EF combined)."""
    if not styles:
        return {}
    unique = list({str(s).strip().upper() for s in styles if s and str(s).strip()})
    if not unique:
        return {}

    query_tpl = """
    SELECT
        i.PROD_CD AS sku,
        i.ALT_CD AS style,
        i.UNIT_COLOR AS color,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD IN ({})
    GROUP BY i.PROD_CD, i.ALT_CD, i.UNIT_COLOR
    ORDER BY i.ALT_CD, i.PROD_CD
    """

    sku_by_style = {}
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        for chunk in _chunk_list(unique, 200):
            chunk_query = query_tpl.format(','.join(['?' for _ in chunk]))
            for conn in [conn_fdc, conn_ef]:
                cursor = conn.cursor()
                cursor.execute(chunk_query, chunk)
                for sku, style_code, color, quantity, ocean in cursor.fetchall():
                    if not sku or not style_code:
                        continue
                    style_key = style_code.strip().upper()
                    sku_key = sku.strip().upper()
                    per_style = sku_by_style.setdefault(style_key, {})
                    if sku_key not in per_style:
                        per_style[sku_key] = {
                            'sku': sku_key,
                            'color': color.strip() if color else '',
                            'quantity': 0,
                            'ocean': 0,
                        }
                    per_style[sku_key]['quantity'] += (quantity or 0)
                    per_style[sku_key]['ocean'] += (ocean or 0)
                cursor.close()
        conn_fdc.close()
        conn_ef.close()
    except Exception as e:
        current_app.logger.error(f"Error in get_detailed_stock_for_styles_batch: {e}")

    out = {}
    for style_key, sku_map in sku_by_style.items():
        rows = []
        for data in sku_map.values():
            data['total'] = data['quantity'] + data['ocean']
            rows.append(data)
        rows.sort(key=lambda x: x['sku'])
        out[style_key] = rows
    return out


def _filter_sku_rows(skus, stock_filter=None, color_filter=None):
    color_q = (color_filter or '').strip().upper()
    rows = []
    for item in skus or []:
        color = (item.get('color') or '').strip().upper()
        qty = item.get('quantity', 0) or 0
        ocean = item.get('ocean', 0) or 0
        if color_q and color_q not in color:
            continue
        if stock_filter == 'instock' and qty <= 0:
            continue
        if stock_filter == 'ocean' and ocean <= 0:
            continue
        if stock_filter == 'both' and qty <= 0 and ocean <= 0:
            continue
        rows.append(item)
    return rows


def _container_numeric_sort_key(pur_num):
    """Sort key for container IDs by numeric suffix (F261102 > F26199)."""
    s = (pur_num or '').strip().upper()
    m = re.search(r'(\d+)$', s)
    if m:
        try:
            return (int(m.group(1)), s)
        except ValueError:
            pass
    return (-1, s)


def _extract_container_pur_num(token):
    """Container PO from URL or plain token (e.g. F261106)."""
    t = (token or '').strip()
    if not t:
        return None
    low = t.lower()
    if 'container_id=' in low or 'container_search' in low:
        m = re.search(r'[?&]container_id=([^&#\s]+)', t, re.IGNORECASE)
        if m:
            return m.group(1).strip().upper()
    if re.match(r'^https?://', low, re.IGNORECASE):
        return None
    s = t.upper()
    if re.match(r'^[A-Za-z]{1,2}\d{4}([-]?[smlSML]*)?$', s):
        return None
    if re.match(r'^[A-Z][A-Z0-9]{3,}$', s):
        return s
    return None


def _styles_from_container_pur_nums(pur_nums):
    """Resolve container PO numbers (plog.pur_num) to unique style codes."""
    unique = []
    seen = set()
    for p in pur_nums or []:
        u = (p or '').strip().upper()
        if u and u not in seen:
            seen.add(u)
            unique.append(u)
    if not unique:
        return {}, None

    by_pur = {p: [] for p in unique}
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(unique))
        query = f"""
            SELECT DISTINCT pl.pur_num,
                LEFT(pl.prod_cd, CHARINDEX(' ', pl.prod_cd + ' ') - 1) AS style
            FROM plog pl
            WHERE pl.pur_num IN ({placeholders})
            AND (pl.hdl_chg > 0 OR pl.log_qty > 0)
            AND pl.prod_cd IS NOT NULL AND LTRIM(RTRIM(pl.prod_cd)) <> ''
        """
        cursor.execute(query, unique)
        for row in cursor.fetchall():
            pur = (row[0] or '').strip().upper()
            style = (row[1] or '').strip().upper()
            if pur and style and style not in by_pur.setdefault(pur, []):
                by_pur[pur].append(style)
        cursor.close()
        conn.close()
    except Exception as e:
        current_app.logger.error('bag_style_viewer container lookup failed: %s', e, exc_info=True)
        return None, 'Could not load styles from container database.'

    return by_pur, None


def fetch_latest_containers(limit=10):
    """Most recent container PO numbers from plog."""
    rows = []
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP {int(limit)} pur_num
            FROM (
                SELECT pur_num, MAX(log_date) AS max_log_date
                FROM plog
                WHERE pur_num IS NOT NULL AND LTRIM(RTRIM(pur_num)) <> ''
                AND pur_num NOT LIKE 'L%%'
                GROUP BY pur_num
            ) recent
            ORDER BY max_log_date DESC
            """
        )
        rows = [(row[0] or '').strip().upper() for row in cursor.fetchall() if row and row[0]]
        cursor.close()
        conn.close()
    except Exception as e:
        current_app.logger.error('fetch_latest_containers failed: %s', e)
    rows.sort(key=_container_numeric_sort_key, reverse=True)
    return rows


def _style_image_url(style):
    img_url = get_canonical_asset_url(style, width=800, source_filter='catalog_original')
    if not img_url:
        assets = search_assets_by_sku_pattern(
            style, source_filter=['catalog_original', 'dropbox', 'big_picture'],
        )
        if assets:
            img_url = f"/image/{assets[0].id}/800"
    return img_url or '/static/no_image_available.jpg'


def fetch_styles_by_descriptions(descriptions, stock_filter=None, color_filter=None):
    results = []
    seen = set()
    for desc in _normalize_description_list(descriptions):
        for item in fetch_styles_by_description(desc, stock_filter, color_filter):
            key = (item.get('style') or '').strip().upper()
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(item)
    results.sort(key=lambda x: (x.get('style') or '').upper())
    return results


def fetch_styles_from_containers(container_ids, stock_filter=None, color_filter=None):
    by_pur, err = _styles_from_container_pur_nums(container_ids)
    if err:
        return [], err
    if by_pur is None:
        return [], 'Could not load styles from container database.'

    codes = []
    seen = set()
    for cid in container_ids:
        for style in by_pur.get((cid or '').strip().upper(), []):
            u = style.strip().upper()
            if u and u not in seen:
                seen.add(u)
                codes.append(u)

    results = []
    for code in sorted(codes):
        if not _style_meets_stock_color_filters(code, stock_filter, color_filter):
            continue
        results.append({
            'style': code,
            'description': fetch_description_for_style(code) or '',
            'image': _style_image_url(code),
        })
    return results, None


def build_style_viewer_results_for_styles(styles, stock_filter=None, color_filter=None, hide_zero_skus=False):
    """Attach SKU rows to resolved style records."""
    if not styles:
        return []
    stock_map = get_detailed_stock_for_styles_batch([s['style'] for s in styles])
    results = []
    for item in styles:
        style_key = item['style'].strip().upper()
        skus = _filter_sku_rows(stock_map.get(style_key, []), stock_filter, color_filter)
        if hide_zero_skus:
            skus = [r for r in skus if (r.get('quantity', 0) or 0) > 0 or (r.get('ocean', 0) or 0) > 0]
        results.append({
            **item,
            'skus': skus,
            'total_quantity': sum(r.get('quantity', 0) or 0 for r in skus),
            'total_ocean': sum(r.get('ocean', 0) or 0 for r in skus),
            'total': sum(r.get('total', 0) or 0 for r in skus),
        })
    return results


def resolve_bag_style_viewer_results(
    selected_descriptions,
    selected_containers,
    stock_filter=None,
    color_filter=None,
    hide_zero_skus=False,
    desc_filter=None,
):
    """Merge styles from description(s) and/or container PO(s)."""
    desc_filter = (desc_filter or '').strip().lower() or None
    if desc_filter == 'all':
        desc_filter = None

    merged = []
    seen = set()

    if selected_descriptions or selected_containers:
        for item in fetch_styles_by_descriptions(selected_descriptions, stock_filter, color_filter):
            key = (item.get('style') or '').strip().upper()
            if key and key not in seen:
                seen.add(key)
                merged.append(item)

        if selected_containers:
            cont_styles, err = fetch_styles_from_containers(selected_containers, stock_filter, color_filter)
            if err:
                return [], err
            for item in cont_styles:
                key = (item.get('style') or '').strip().upper()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(item)

        merged = _filter_styles_by_desc_filter(merged, desc_filter)
    elif desc_filter in ('has', 'missing'):
        merged = fetch_inventory_styles_by_filters(stock_filter, color_filter, desc_filter)
    else:
        return [], None

    merged.sort(key=lambda x: (x.get('style') or '').upper())
    return build_style_viewer_results_for_styles(
        merged, stock_filter, color_filter, hide_zero_skus=hide_zero_skus,
    ), None


def build_style_viewer_results(description, stock_filter=None, color_filter=None, hide_zero_skus=False):
    """Styles with image/description plus SKU rows for list view (single description)."""
    return resolve_bag_style_viewer_results(
        [description] if description else [],
        [],
        stock_filter=stock_filter,
        color_filter=color_filter,
        hide_zero_skus=hide_zero_skus,
    )[0]


def fetch_styles_by_description(description, stock_filter=None, color_filter=None):
    conn = get_mysql_connection()
    results = []
    if not conn:
        return results
    try:
        cur = conn.cursor()
        cur.execute("SELECT style, description FROM onlinedb.description WHERE description = %s ORDER BY style", (description,))
        rows = cur.fetchall()
        
        for style, desc in rows:
            if not _style_meets_stock_color_filters(style, stock_filter, color_filter):
                continue
            
            # Try to get image via DAM (catalog_original preferred)
            img_url = get_canonical_asset_url(style, width=800, source_filter='catalog_original')
            if not img_url:
                # Fallback to any source
                assets = search_assets_by_sku_pattern(style, source_filter=['catalog_original', 'dropbox', 'big_picture'])
                if assets:
                    # Use first match
                    img_url = f"/image/{assets[0].id}/800"
            if not img_url:
                img_url = "/static/no_image_available.jpg"
            results.append({
                'style': style,
                'description': desc,
                'image': img_url
            })
    finally:
        cur.close()
        conn.close()
    return results

def fetch_description_for_style(style):
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT description FROM onlinedb.description WHERE style = %s", (style,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()

def upsert_description(style, description):
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # Try update first
        cur.execute("UPDATE onlinedb.description SET description=%s WHERE style=%s", (description, style))
        if cur.rowcount == 0:
            # Insert
            cur.execute("INSERT INTO onlinedb.description(style, description) VALUES(%s, %s)", (style, description))
        conn.commit()
        return True
    except Exception as e:
        current_app.logger.error(f"upsert_description error: {e}")
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

def delete_description(style):
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM onlinedb.description WHERE style=%s", (style,))
        conn.commit()
        return True
    except Exception as e:
        current_app.logger.error(f"delete_description error: {e}")
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

# ----- Routes -----

@bag_style_viewer_bp.route('/bag_style_viewer', methods=['GET'])
def index():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']
    descriptions = fetch_all_descriptions()
    latest_containers = fetch_latest_containers(10)
    selected_descriptions = request.args.getlist('description')
    if not selected_descriptions:
        legacy = (request.args.get('description') or '').strip()
        if legacy:
            selected_descriptions = [legacy]

    selected_containers = []
    for c in request.args.getlist('container'):
        u = (c or '').strip().upper()
        if u and u not in selected_containers:
            selected_containers.append(u)
    container_q = (request.args.get('container_q') or '').strip()
    parsed_container = _extract_container_pur_num(container_q)
    if parsed_container and parsed_container not in selected_containers:
        selected_containers.append(parsed_container)
    selected_containers.sort(key=_container_numeric_sort_key, reverse=True)

    stock_filter = request.args.get('stock_filter') or None
    color_filter = (request.args.get('color_filter') or '').strip() or None
    style_q = (request.args.get('style_q') or request.args.get('sku') or '').strip().upper()
    view_mode = (request.args.get('view') or 'grid').strip().lower()
    if view_mode not in ('grid', 'list'):
        view_mode = 'grid'
    hide_zero_skus = request.args.get('hide_zero') == '1'
    desc_filter = (request.args.get('desc_filter') or '').strip().lower() or ''
    if desc_filter == 'all':
        desc_filter = ''
    filter_error = None

    styles = []
    styles_data = []
    search_result = None
    has_bulk_filters = (
        selected_descriptions or selected_containers or desc_filter in ('has', 'missing')
    )

    if has_bulk_filters:
        styles_data, filter_error = resolve_bag_style_viewer_results(
            selected_descriptions,
            selected_containers,
            stock_filter,
            color_filter,
            hide_zero_skus=hide_zero_skus,
            desc_filter=desc_filter or None,
        )
        if style_q:
            styles_data = [
                s for s in styles_data
                if style_q in (s.get('style') or '').upper()
                or any(
                    style_q in (sku_row.get('sku') or '').upper()
                    for sku_row in (s.get('skus') or [])
                )
            ]
        styles = [
            {'style': s['style'], 'description': s['description'], 'image': s['image']}
            for s in styles_data
        ]
    elif style_q:
        desc = fetch_description_for_style(style_q)
        img_url = get_canonical_asset_url(style_q, width=800, source_filter='catalog_original') or \
                  "/static/no_image_available.jpg"
        search_result = {'style': style_q, 'description': desc or 'Not found', 'image': img_url}

    return render_template(
        'bag_style_viewer.html',
        descriptions=descriptions,
        latest_containers=latest_containers,
        selected_descriptions=selected_descriptions,
        selected_containers=selected_containers,
        container_q=container_q,
        styles=styles,
        styles_data=styles_data,
        search_result=search_result,
        filter_error=filter_error,
        stock_filter=stock_filter or '',
        color_filter=color_filter or '',
        desc_filter=desc_filter,
        style_q=style_q,
        view_mode=view_mode,
        hide_zero_skus=hide_zero_skus,
        current_user=current_user,
    )


@bag_style_viewer_bp.route('/bag_style_viewer/styles_for_filters', methods=['GET'])
def styles_for_filters():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Login required'}), 401
    description = request.args.get('description', '').strip()
    stock_filter = request.args.get('stock_filter') or None
    color_filter = request.args.get('color_filter') or None
    if not description:
        return jsonify({'success': False, 'message': 'Description required'}), 400
    styles = fetch_style_codes_for_filters(description, stock_filter, color_filter)
    return jsonify({'success': True, 'styles': styles, 'count': len(styles)})

@bag_style_viewer_bp.route('/bag_style_viewer/update', methods=['POST'])
def update_description():
    data = request.get_json(force=True)
    style = data.get('style', '').strip().upper()
    description = data.get('description', '').strip()
    if not style or not description:
        return jsonify({'success': False, 'message': 'Style and description required'}), 400
    ok = upsert_description(style, description)
    return jsonify({'success': ok})

@bag_style_viewer_bp.route('/bag_style_viewer/stock/<style>', methods=['GET'])
def get_stock_info(style):
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    """Get detailed stock information for a specific style"""
    current_user = session['username']
    
    # Get detailed stock data by SKU (style + color)
    detailed_stock = get_detailed_stock_for_style(style.upper())
    
    # Calculate totals
    total_quantity = sum(item['quantity'] for item in detailed_stock)
    total_ocean = sum(item['ocean'] for item in detailed_stock)
    
    return jsonify({
        'style': style.upper(),
        'total_quantity': total_quantity,
        'total_ocean': total_ocean,
        'total': total_quantity + total_ocean,
        'details': detailed_stock,
        'current_user': current_user
    })

@bag_style_viewer_bp.route('/bag_style_viewer/export', methods=['GET'])
def export_template():
    # Export all description rows with an Action column and instruction row
    conn = get_mysql_connection()
    rows = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT style, description FROM onlinedb.description ORDER BY style")
            rows = cur.fetchall()
        finally:
            cur.close()
            conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Descriptions'

    # Row 1 instruction
    ws.append(["Action (DELETE|MODIFY|CREATE)", "Style", "Description"])  # Template header/instruction
    # Freeze top row
    ws.freeze_panes = 'A2'

    # Add data starting row 2 with empty Action
    for style, desc in rows:
        ws.append(["", style, desc])

    # Center headers
    for cell in ws[1]:
        cell.alignment = Alignment(horizontal='center')

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    filename = f"bag_descriptions_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(out, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@bag_style_viewer_bp.route('/bag_style_viewer/upload', methods=['POST'])
def upload_template():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'success': False, 'message': 'Invalid file type'}), 400

    # Read with pandas
    try:
        df = pd.read_excel(f, engine='openpyxl')
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to read Excel: {e}'}), 400

    # Expect columns: Action, Style, Description (case-insensitive)
    cols = {c.lower(): c for c in df.columns}
    if not {'action', 'style', 'description'}.issubset(set(cols.keys())):
        return jsonify({'success': False, 'message': 'Missing required columns: Action, Style, Description'}), 400

    actions_applied = []
    for _, row in df.iterrows():
        action = str(row[cols['action']]).strip().lower() if pd.notna(row[cols['action']]) else ''
        style = str(row[cols['style']]).strip().upper() if pd.notna(row[cols['style']]) else ''
        description = str(row[cols['description']]).strip() if pd.notna(row[cols['description']]) else ''
        if not style:
            continue
        if action == 'delete':
            ok = delete_description(style)
        elif action == 'modify':
            ok = upsert_description(style, description)
        elif action == 'create':
            ok = upsert_description(style, description)
        else:
            # Skip rows without action
            continue
        actions_applied.append({'style': style, 'action': action, 'ok': ok})

    return jsonify({'success': True, 'applied': actions_applied})
