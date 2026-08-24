from flask import Blueprint, request, render_template, session, current_app, jsonify
from py.utilities.setting import login_required
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from py.utilities.logging_setup import log_event, log_user_action
from py.utilities.database_utils import get_mysql_connection
from datetime import datetime, timedelta
import py.utilities.models
from py.utilities.dam_utils import search_assets_by_sku_pattern, get_asset_url
from py.utilities.ocean_qty import fetch_ocean_pos_by_style_prefix, fetch_ocean_qty_by_style_prefix
import os
import re
from urllib.parse import quote, unquote
from markupsafe import Markup, escape

# Get the directory of this file
module_dir = os.path.dirname(os.path.abspath(__file__))

product_detail_bp = Blueprint(
    'product_detail', 
    __name__,
    template_folder=module_dir  # Templates are in the same folder as this file
)

sql_server_config = Config.SQL_SERVER_CONFIG

_CONTAINER_TOKEN_RE = re.compile(r"^[A-Za-z]{1,3}\d{3,}$")
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def fdcef_container_search_url(container_id: str) -> str:
    """Container search URL, including the two trailing spaces the live site expects."""
    c = (container_id or "").strip()
    if not c:
        return "/container_search"
    return "/container_search?container_id=" + quote(c + "  ", safe="")


def extract_container_id_from_description(text) -> str | None:
    """Return a container PO when description is a container id or container_search URL."""
    if text is None:
        return None
    t = str(text).strip()
    if not t or t.upper() in {"N/A", "NONE", "NAN", "-"}:
        return None
    low = t.lower()
    if "container_id=" in low or "container_search" in low:
        match = re.search(r"[?&]container_id=([^&#\s]+)", t, re.IGNORECASE)
        if match:
            cid = unquote(match.group(1)).strip().upper()
            return cid or None
    if _HTTP_URL_RE.match(t):
        return None
    if _CONTAINER_TOKEN_RE.match(t):
        return t.upper()
    return None


def description_is_container_link(text) -> bool:
    if extract_container_id_from_description(text):
        return True
    t = str(text or "").strip()
    return bool(t and _HTTP_URL_RE.match(t))


@product_detail_bp.app_template_filter("is_container_desc")
def is_container_desc_filter(value) -> bool:
    return description_is_container_link(value)


@product_detail_bp.app_template_filter("linkify_container_desc")
def linkify_container_desc_filter(value) -> Markup:
    """Turn container IDs / container_search URLs in OMS descriptions into links."""
    if value is None:
        return Markup("")
    text = str(value).strip()
    if not text:
        return Markup("")
    cid = extract_container_id_from_description(text)
    if cid:
        url = fdcef_container_search_url(cid)
        return Markup(
            f'<a class="container-desc-link" href="{escape(url)}" target="_blank" rel="noopener">{escape(cid)}</a>'
        )
    if _HTTP_URL_RE.match(text):
        return Markup(
            f'<a class="container-desc-link" href="{escape(text)}" target="_blank" rel="noopener">{escape(text)}</a>'
        )
    return Markup(str(escape(text)))


def get_db_connections():
    """Establishes connections to both FDC and E_FAS databases."""
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        conn_oms_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        return conn_fdc, conn_oms_ef
    except Exception as e:
        current_app.logger.error(f"Database connection error: {e}")
        return None, None


def fetch_locations(skus):
    """Fetches locations for SKUs from MySQL database."""
    location_dict = {}
    
    if not skus:
        return location_dict
    
    try:
        connection = get_mysql_connection()
        if connection is None:
            current_app.logger.error("Failed to connect to MySQL database for fetching locations.")
            return location_dict
        
        cursor = connection.cursor()
        
        def _clean_input(s):
            try:
                return s.replace('\u00A0', ' ').strip().lower()
            except Exception:
                return str(s).strip().lower()
        
        lowered = [_clean_input(s) for s in skus]
        placeholders = ', '.join(['%s'] * len(lowered))
        
        query = f"""
            SELECT SKU, GROUP_CONCAT(Location ORDER BY Location SEPARATOR ', ') as locations
            FROM products 
            WHERE LOWER(SKU) IN ({placeholders}) AND Location IS NOT NULL AND Location != ''
            GROUP BY SKU
        """
        
        cursor.execute(query, lowered)
        results = cursor.fetchall()
        
        for sku, locations in results:
            try:
                k = sku.replace('\u00A0', ' ').strip().lower()
            except Exception:
                k = str(sku).strip().lower()
            location_dict[k] = locations
        
        # Fallback for SKUs not found
        for sku in skus:
            try:
                key = sku.replace('\u00A0', ' ').strip().lower()
            except Exception:
                key = str(sku).strip().lower()
            
            if key not in location_dict:
                try:
                    stripped_sku = sku.replace('\u00A0', ' ').replace(' ', '').strip().lower()
                except Exception:
                    stripped_sku = str(sku).replace(' ', '').strip().lower()
                
                if stripped_sku != key:
                    cursor.execute(
                        "SELECT GROUP_CONCAT(Location ORDER BY Location SEPARATOR ', ') FROM products WHERE LOWER(SKU) = %s",
                        (stripped_sku,)
                    )
                    result = cursor.fetchone()
                    if result and result[0]:
                        location_dict[key] = result[0]
    
    except Exception as e:
        current_app.logger.error(f"Error fetching locations: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
    
    return location_dict


def fetch_description(style):
    """Fetches product description for a style from MySQL database."""
    if not style:
        return None
    
    try:
        connection = get_mysql_connection()
        if connection is None:
            current_app.logger.error("Failed to connect to MySQL database for fetching description.")
            return None
        
        cursor = connection.cursor()
        # Convert style to uppercase to match database storage
        style_upper = style.strip().upper() if isinstance(style, str) else str(style).strip().upper()
        cursor.execute("SELECT description FROM onlinedb.description WHERE style = %s", (style_upper,))
        row = cursor.fetchone()
        
        return row[0] if row else None
    
    except Exception as e:
        current_app.logger.error(f"Error fetching description: {e}")
        return None


def fetch_oms_description(style, conn_fdc=None, conn_oms_ef=None):
    """Fetches ALL OMS descriptions for a style from SQL Server databases (FDC and E_FAS).
    Returns a list of unique descriptions."""
    if not style:
        return None
    
    try:
        # Clean the style for comparison
        style_clean = style.strip().upper() if isinstance(style, str) else str(style).strip().upper()
        descriptions = set()
        
        # Try to get from FDC first
        if conn_fdc:
            try:
                cursor = conn_fdc.cursor()
                query = """
                    SELECT DISTINCT i.DESCRIP
                    FROM dbo.inv AS i
                    WHERE UPPER(i.ALT_CD) = ? OR UPPER(i.PROD_CD) LIKE ?
                """
                cursor.execute(query, (style_clean, f"{style_clean}%"))
                rows = cursor.fetchall()
                for row in rows:
                    if row and row[0]:
                        descriptions.add(row[0])
            except Exception as e:
                pass
        
        # Try to get from E_FAS
        if conn_oms_ef:
            try:
                cursor = conn_oms_ef.cursor()
                query = """
                    SELECT DISTINCT i.DESCRIP
                    FROM dbo.inv AS i
                    WHERE UPPER(i.ALT_CD) = ? OR UPPER(i.PROD_CD) LIKE ?
                """
                cursor.execute(query, (style_clean, f"{style_clean}%"))
                rows = cursor.fetchall()
                for row in rows:
                    if row and row[0]:
                        descriptions.add(row[0])
            except Exception as e:
                pass
        
        return list(descriptions) if descriptions else None
    
    except Exception as e:
        current_app.logger.error(f"Error fetching OMS descriptions: {e}")
        return None



def fetch_oms_description_by_sku(sku, conn_fdc=None, conn_oms_ef=None):
    """Fetches OMS description for a specific SKU from SQL Server databases.
    Returns a list of unique descriptions for that SKU."""
    if not sku:
        return None
    
    try:
        # Clean the SKU - handle non-breaking spaces and other whitespace characters
        sku_clean = sku.replace('\u00A0', ' ').strip().upper() if isinstance(sku, str) else str(sku).replace('\u00A0', ' ').strip().upper()
        descriptions = set()
        
        # Try to get from FDC first
        if conn_fdc:
            try:
                cursor = conn_fdc.cursor()
                query = """
                    SELECT DISTINCT i.DESCRIP
                    FROM dbo.inv AS i
                    WHERE i.PROD_CD = ? OR UPPER(LTRIM(RTRIM(i.PROD_CD))) = ?
                """
                cursor.execute(query, (sku_clean, sku_clean))
                rows = cursor.fetchall()
                for row in rows:
                    if row and row[0]:
                        descriptions.add(row[0])
            except Exception as e:
                pass
        
        # Try to get from E_FAS
        if conn_oms_ef:
            try:
                cursor = conn_oms_ef.cursor()
                query = """
                    SELECT DISTINCT i.DESCRIP
                    FROM dbo.inv AS i
                    WHERE i.PROD_CD = ? OR UPPER(LTRIM(RTRIM(i.PROD_CD))) = ?
                """
                cursor.execute(query, (sku_clean, sku_clean))
                rows = cursor.fetchall()
                for row in rows:
                    if row and row[0]:
                        descriptions.add(row[0])
            except Exception as e:
                pass
        
        return list(descriptions) if descriptions else None
    
    except Exception as e:
        current_app.logger.error(f"Error fetching OMS description for SKU: {e}")
        return None


def normalize_oms_text(value):
    """Strip OMS padding and control chars (CR/LF/TAB) from SKU/style/color text."""
    if value is None:
        return ''
    text = str(value).replace('\r', '').replace('\n', '').replace('\t', '')
    return ' '.join(text.split())


def search_product_data(search_input):
    """
    Search for products by SKU or Style.
    Returns style-grouped data with color variants.
    Only processes the first search term (single product).
    """
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return None

    try:
        search_input = search_input.lower().strip()
        # Only use the first search term - single product only
        search_input_parts = [search_input.split(",")[0].strip()]
        
        all_results = []
        ocean_pos_by_style = {}
        
        # Query both databases
        for conn, db_name in [(conn_fdc, 'FDC'), (conn_oms_ef, 'E_FAS')]:
            cursor = conn.cursor()
            
            # Build dynamic conditions
            conditions = []
            params = []
            
            for part in search_input_parts:
                if part:
                    # Search by SKU (exact match) or Style (prefix match)
                    conditions.append(f"(i.PROD_CD LIKE ? OR i.ALT_CD LIKE ? OR i.PROD_CD = ?)")
                    params.append(f"{part}%")  # SKU like
                    params.append(f"{part}%")  # ALT_CD like
                    params.append(part)        # Exact SKU match
            
            combined_condition = " OR ".join(conditions) if conditions else "1=1"
            
            query = f"""
                SELECT 
                    i.PROD_CD AS SKU,
                    i.ALT_CD AS Style,
                    i.DESCRIP AS Container,
                    CAST(ISNULL(id.in_stock, 0) AS INT) AS stock,
                    CAST(ISNULL(id.ORDER_QTY, 0) AS INT) AS sold,
                    CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS available,
                    CAST(ISNULL(id.on_order_qty, 0) AS INT) AS incoming,
                    i.SALES_COST AS cost,
                    i.RETAIL_PRS AS msrp,
                    CAST(ISNULL(i.PC_CASE, 0) AS INT) AS case_qty,
                    i.CASE_WT AS box_wt,
                    i.UT_WT AS pc_wt,
                    i.UNIT_COLOR AS color,
                    idt.ut_len AS len,
                    idt.ut_wi AS wid,
                    idt.UT_hi AS hei,
                    idt.CASE_LEN AS master_len,
                    idt.CASE_WI AS master_wid,
                    idt.CASE_HI AS master_hei,
                    CASE 
                        WHEN LT_SL_DT = 0 THEN 'NEVER'
                        ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
                    END AS last_activity,
                    fcd.create_dt
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
                LEFT JOIN (
                    SELECT 
                        prod_cd,
                        RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
                    FROM wm_skufile WHERE wm_skufile.WHS_NUM = '01'
                ) AS fcd ON i.PROD_CD = fcd.prod_cd
                WHERE {combined_condition}
                ORDER BY i.ALT_CD, i.UNIT_COLOR
            """
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            ocean_lookup = fetch_ocean_qty_by_style_prefix(conn, search_input_parts[0])
            try:
                pos_map = fetch_ocean_pos_by_style_prefix(conn, search_input_parts[0])
                for style_key, pos in pos_map.items():
                    bucket = ocean_pos_by_style.setdefault(style_key, [])
                    for po in pos:
                        if po not in bucket:
                            bucket.append(po)
            except Exception:
                pass
            
            # Add database identifier to each row
            for row in rows:
                sku_norm = normalize_oms_text(row[0])
                style_norm = normalize_oms_text(row[1])
                color_norm = normalize_oms_text(row[12]) or None
                sku_key = sku_norm
                if sku_key in ocean_lookup:
                    incoming = ocean_lookup[sku_key]
                else:
                    incoming = int(row[6] or 0)
                all_results.append({
                    'db': db_name,
                    'sku': sku_norm or row[0],
                    'style': style_norm or row[1],
                    'container': normalize_oms_text(row[2]) or row[2],
                    'stock': row[3],
                    'sold': row[4],
                    'available': row[5],
                    'incoming': incoming,
                    'cost': row[7],
                    'msrp': row[8],
                    'case_qty': row[9],
                    'box_wt': row[10],
                    'pc_wt': row[11],
                    'color': color_norm,
                    'len': row[13],
                    'wid': row[14],
                    'hei': row[15],
                    'master_len': row[16],
                    'master_wid': row[17],
                    'master_hei': row[18],
                    'last_activity': row[19],
                    'create_dt': row[20]
                })
            
            cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        return group_by_style(all_results, ocean_pos_by_style) if all_results else None
        
    except Exception as e:
        current_app.logger.error(f"Error searching product data: {e}")
        return None


def group_by_style(results, ocean_pos_by_style=None):
    """
    Groups results by style and organizes colors.
    Returns structure: {style: {colors: [...], parent_info: {...}}}
    """
    grouped = {}
    
    for item in results:
        style = normalize_oms_text(item.get('style')) or (item.get('style') or '')
        
        if style not in grouped:
            grouped[style] = {
                'colors': [],
                'parent_info': {
                    'style': style,
                    'container': item['container'],
                    'box_wt': item['box_wt'],
                    'pc_wt': item['pc_wt'],
                    'len': item['len'],
                    'wid': item['wid'],
                    'hei': item['hei'],
                    'case_qty': item['case_qty'],
                    'master_len': item['master_len'],
                    'master_wid': item['master_wid'],
                    'master_hei': item['master_hei'],
                    'msrp': item['msrp'],
                    'cost': item['cost'],
                    'dimensions': f"{item['len']}x{item['wid']}x{item['hei']}",
                    'last_activity': item['last_activity'],
                    'create_dt': item['create_dt']
                },
                'color_variants': {}
            }
        
        # Create color key for aggregation
        color_norm = normalize_oms_text(item.get('color')) if item.get('color') else ''
        color_key = color_norm if color_norm else 'NO_COLOR'
        sku_norm = normalize_oms_text(item.get('sku')) or item.get('sku')
        
        if color_key not in grouped[style]['color_variants']:
            grouped[style]['color_variants'][color_key] = {
                'color': color_norm or item['color'],
                'sku': sku_norm,
                'fdc': None,
                'efas': None
            }
        
        # Store data by database
        db_data = {
            'stock': item['stock'],
            'sold': item['sold'],
            'available': item['available'],
            'incoming': item['incoming'],
            'cost': item['cost'],
            'msrp': item['msrp'],
            'last_activity': item['last_activity'],
            'create_dt': item['create_dt']
        }
        
        if item['db'] == 'FDC':
            grouped[style]['color_variants'][color_key]['fdc'] = db_data
        else:
            grouped[style]['color_variants'][color_key]['efas'] = db_data
    
    # Convert to list and calculate totals
    result = []
    for style, data in grouped.items():
        style_entry = {
            'style': style,
            'parent_info': data['parent_info'],
            'colors': []
        }
        
        # Calculate style totals
        style_totals = {
            'fdc': {'stock': 0, 'sold': 0, 'available': 0, 'incoming': 0},
            'efas': {'stock': 0, 'sold': 0, 'available': 0, 'incoming': 0},
            'combined': {'stock': 0, 'sold': 0, 'available': 0, 'incoming': 0}
        }
        
        for color_key, color_data in data['color_variants'].items():
            color_entry = {
                'color': color_data['color'],
                'sku': color_data['sku'],
                'fdc': color_data['fdc'],
                'efas': color_data['efas'],
                'totals': {'fdc': {}, 'efas': {}, 'combined': {}}
            }
            
            # Calculate color totals
            if color_data['fdc']:
                for key in ['stock', 'sold', 'available', 'incoming']:
                    color_entry['totals']['fdc'][key] = color_data['fdc'].get(key, 0)
                    style_totals['fdc'][key] += color_data['fdc'].get(key, 0)
                    style_totals['combined'][key] += color_data['fdc'].get(key, 0)
            
            if color_data['efas']:
                for key in ['stock', 'sold', 'available', 'incoming']:
                    color_entry['totals']['efas'][key] = color_data['efas'].get(key, 0)
                    style_totals['efas'][key] += color_data['efas'].get(key, 0)
                    style_totals['combined'][key] += color_data['efas'].get(key, 0)
            
            # Combined totals for this color
            color_entry['totals']['combined'] = {
                'stock': color_entry['totals']['fdc'].get('stock', 0) + color_entry['totals']['efas'].get('stock', 0),
                'sold': color_entry['totals']['fdc'].get('sold', 0) + color_entry['totals']['efas'].get('sold', 0),
                'available': color_entry['totals']['fdc'].get('available', 0) + color_entry['totals']['efas'].get('available', 0),
                'incoming': color_entry['totals']['fdc'].get('incoming', 0) + color_entry['totals']['efas'].get('incoming', 0)
            }
            
            style_entry['colors'].append(color_entry)
        
        style_entry['totals'] = style_totals
        if style_totals['combined'].get('incoming'):
            style_key = style.strip().upper()
            style_entry['ocean_pos'] = list((ocean_pos_by_style or {}).get(style_key) or [])
        else:
            style_entry['ocean_pos'] = []
        result.append(style_entry)
    
    return result


def fetch_sku_history(sku, db_conn):
    """
    Fetch order history and manual adjustments for a specific SKU from plog, ord_log, and wm_log tables
    
    Returns:
    - history_po: Sum of quantities from plog for exact SKU (total PO received)
    - history_sold: ord_log order qty plus INVSPICK warehouse picks (not on customer orders)
    - history_fix: Sum of adjustment quantities from wm_log where type_cd='adj' for exact SKU (total adjustment qty)
    """
    try:
        cursor = db_conn.cursor()
        
        # Query 1: Get purchase history from plog - EXACT SKU match
        # history_po = SUM(hdl_chg) to get total quantity received from POs
        po_query = """
            SELECT 
                ISNULL(SUM(pl.hdl_chg), 0) as total_qty
            FROM plog pl
            WHERE pl.prod_cd = ? AND pl.hdl_chg > 0
        """
        
        cursor.execute(po_query, (sku,))
        po_result = cursor.fetchone()
        history_po = int(po_result[0]) if po_result and po_result[0] else 0
        
        # Query 2: Get sales/order history from ord_log - EXACT SKU match
        # history_sold = SUM(order_qty) to get total quantity sold to customers
        sold_query = """
            SELECT 
                ISNULL(SUM(ol.order_qty), 0) as total_qty
            FROM ord_log ol
            WHERE ol.prod_cd = ?
        """
        
        cursor.execute(sold_query, (sku,))
        sold_result = cursor.fetchone()
        history_sold = int(sold_result[0]) if sold_result and sold_result[0] else 0

        # Inventory picks (INVSPICK) reduce stock but are not in ord_log.
        inv_pick_query = """
            SELECT ISNULL(SUM(ABS(log_qty)), 0) as total_qty
            FROM wm_log
            WHERE prod_cd = ? AND RTRIM(type_cd) = 'INVSPICK' AND log_qty < 0
        """
        cursor.execute(inv_pick_query, (sku,))
        inv_pick_result = cursor.fetchone()
        history_sold += int(inv_pick_result[0]) if inv_pick_result and inv_pick_result[0] else 0
        
        # Query 3: Get manual adjustments from wm_log - EXACT SKU match
        # history_fix = SUM(log_qty) to get total adjustment quantity
        adjustment_query = """
            SELECT ISNULL(SUM(log_qty), 0) as total_adj_qty
            FROM wm_log
            WHERE prod_cd = ? AND type_cd = 'adj'
        """
        
        cursor.execute(adjustment_query, (sku,))
        adj_result = cursor.fetchone()
        history_fix = int(adj_result[0]) if adj_result and adj_result[0] else 0
        
        return {
            'history_po': history_po,           # Total purchase order quantity received for exact SKU
            'history_sold': history_sold,       # Total quantity sold to customers for exact SKU
            'history_fix': history_fix          # Total manual adjustment quantity for exact SKU
        }
        
    except Exception as e:
        current_app.logger.error(f"Error fetching history for {sku}: {e}")
        return {'history_po': 0, 'history_sold': 0, 'history_fix': 0}
    finally:
        if 'cursor' in locals():
            cursor.close()


_EMPTY_SKU_HISTORY = {'history_po': 0, 'history_sold': 0, 'history_fix': 0}


def _history_sku_key(sku) -> str:
    return normalize_oms_text(sku).upper()


def fetch_sku_history_batch(skus, db_conn, *, chunk_size: int = 400):
    """Batch plog / ord_log / wm_log totals for many SKUs (same math as fetch_sku_history)."""
    unique = []
    seen = set()
    for sku in skus:
        key = _history_sku_key(sku)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(normalize_oms_text(sku))

    result = {key: dict(_EMPTY_SKU_HISTORY) for key in seen}
    if not unique or not db_conn:
        return result

    cursor = db_conn.cursor()
    try:
        for start in range(0, len(unique), chunk_size):
            chunk = unique[start:start + chunk_size]
            placeholders = ",".join("?" * len(chunk))

            cursor.execute(
                f"""
                SELECT RTRIM(pl.prod_cd), ISNULL(SUM(pl.hdl_chg), 0)
                FROM plog pl
                WHERE pl.prod_cd IN ({placeholders}) AND pl.hdl_chg > 0
                GROUP BY RTRIM(pl.prod_cd)
                """,
                chunk,
            )
            for sku, qty in cursor.fetchall():
                key = _history_sku_key(sku)
                if key in result:
                    result[key]['history_po'] = int(qty or 0)

            cursor.execute(
                f"""
                SELECT RTRIM(ol.prod_cd), ISNULL(SUM(ol.order_qty), 0)
                FROM ord_log ol
                WHERE ol.prod_cd IN ({placeholders})
                GROUP BY RTRIM(ol.prod_cd)
                """,
                chunk,
            )
            for sku, qty in cursor.fetchall():
                key = _history_sku_key(sku)
                if key in result:
                    result[key]['history_sold'] = int(qty or 0)

            cursor.execute(
                f"""
                SELECT RTRIM(prod_cd),
                    ISNULL(SUM(CASE WHEN RTRIM(type_cd) = 'INVSPICK' AND log_qty < 0 THEN ABS(log_qty) ELSE 0 END), 0),
                    ISNULL(SUM(CASE WHEN type_cd = 'adj' THEN log_qty ELSE 0 END), 0)
                FROM wm_log
                WHERE prod_cd IN ({placeholders})
                  AND (type_cd = 'adj' OR (RTRIM(type_cd) = 'INVSPICK' AND log_qty < 0))
                GROUP BY RTRIM(prod_cd)
                """,
                chunk,
            )
            for sku, inv_pick, adj_qty in cursor.fetchall():
                key = _history_sku_key(sku)
                if key not in result:
                    continue
                result[key]['history_sold'] += int(inv_pick or 0)
                result[key]['history_fix'] = int(adj_qty or 0)
    except Exception as e:
        current_app.logger.error(f"Error batch-fetching SKU history: {e}")
    finally:
        cursor.close()

    return result


def fetch_oms_descriptions_by_skus(skus, conn_fdc=None, conn_oms_ef=None, *, chunk_size: int = 400):
    """Return {sku_key: [unique descriptions]} for many SKUs in a few queries."""
    unique = []
    seen = set()
    for sku in skus:
        key = _history_sku_key(sku)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(key)

    result = {key: [] for key in seen}
    if not unique:
        return result

    for conn in (conn_fdc, conn_oms_ef):
        if not conn:
            continue
        cursor = conn.cursor()
        try:
            for start in range(0, len(unique), chunk_size):
                chunk = unique[start:start + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(
                    f"""
                    SELECT UPPER(LTRIM(RTRIM(i.PROD_CD))), i.DESCRIP
                    FROM dbo.inv AS i
                    WHERE UPPER(LTRIM(RTRIM(i.PROD_CD))) IN ({placeholders})
                    """,
                    chunk,
                )
                for sku, descrip in cursor.fetchall():
                    key = _history_sku_key(sku)
                    text = normalize_oms_text(descrip) if descrip else ""
                    if key in result and text and text not in result[key]:
                        result[key].append(text)
        except Exception as e:
            current_app.logger.error(f"Error batch-fetching OMS descriptions: {e}")
        finally:
            cursor.close()

    return result


def fetch_item_history(style_or_sku):
    """
    Fetches order history for a style or SKU from both databases.
    Returns list of dicts with: database, product_code, order_number, order_date, customer_id, order_quantity, unit_price, color, invoice_number, invoice_status
    """
    # Clean up the input - remove extra spaces
    style_or_sku = style_or_sku.strip() if style_or_sku else ""
    if not style_or_sku:
        return []
    
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return []
    
    history_data = []
    
    try:
        # Build search parameter with wildcard
        search_param = f"{style_or_sku}%"
        
        for conn, db_name in [(conn_fdc, 'FDC'), (conn_oms_ef, 'E_FAS')]:
            cursor = conn.cursor()
            
            # Query to get order history with color info from inv table
            query = """
                SELECT TOP 100
                    ol.prod_cd,
                    ol.ord_num,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS order_dt,
                    c.cus_nm AS cus_id,
                    ol.order_qty,
                    ISNULL(ol.unit_prs, 0.00) as unit_price,
                    ISNULL(i.UNIT_COLOR, '') as color,
                    CASE
                        WHEN o.INVS_NUM IS NULL OR o.INVS_NUM = 0 THEN 'not invoiced'
                        ELSE CAST(o.INVS_NUM AS VARCHAR)
                    END AS invoice_number
                FROM ord_log ol
                LEFT JOIN customer c ON ol.cus_id = c.cus_id
                LEFT JOIN dbo.inv i ON ol.prod_cd = i.PROD_CD
                LEFT JOIN dbo.orders o ON ol.ord_num = o.ord_num
                WHERE ol.prod_cd LIKE ?
                ORDER BY ol.olg_dt DESC
            """
            
            try:
                cursor.execute(query, (search_param,))
                rows = cursor.fetchall()
                
                for row in rows:
                    color = str(row[6]).strip().lower() if row[6] else ''
                    
                    # Fallback: extract color from product code if not found in inv table
                    if not color or color == 'none':
                        product_code = str(row[0]).strip()
                        # Product code format is like "T3305 BK", extract the part after space
                        parts = product_code.split()
                        if len(parts) > 1:
                            color = parts[-1].lower().strip()  # Get last part (color code) and strip
                    
                    # Final normalization: remove any special characters and extra spaces
                    color = color.strip().lower()
                    
                    raw_invoice = str(row[7]).strip() if row[7] is not None else ''
                    invoice_number = (
                        raw_invoice
                        if raw_invoice and raw_invoice not in ('0', 'not invoiced')
                        else 'not invoiced'
                    )
                    history_data.append({
                        'database': db_name,
                        'product_code': row[0],
                        'order_number': row[1],
                        'order_date': str(row[2]) if row[2] else '',
                        'customer_id': row[3],
                        'order_quantity': int(row[4]),
                        'unit_price': float(row[5]),
                        'color': color,
                        'invoice_number': invoice_number,
                        'invoice_status': 'Invoiced' if invoice_number != 'not invoiced' else 'Not Invoiced',
                    })
            except Exception as e:
                pass
            finally:
                cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by order date descending
        if history_data:
            history_data.sort(key=lambda x: x['order_date'], reverse=True)
        return history_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching item history for {style_or_sku}: {e}")
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return []


def fetch_adjustment_history(style_or_sku):
    """
    Fetches manual adjustment history for a style or SKU from both databases.
    Returns list of dicts with: database, sku, log_number, date, adjusted_by, quantity
    Matches SQL_connector.py generate_inventory_adjustments() pattern.
    """
    # Clean up the input - remove extra spaces
    style_or_sku = style_or_sku.strip() if style_or_sku else ""
    if not style_or_sku:
        return []
    
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return []
    
    adjustment_data = []
    
    try:
        # Build conditions matching SQL_connector pattern
        adjustment_conditions = []
        adjustment_params = []
        
        # Handle single or space-separated SKU
        input_parts = style_or_sku.split()
        
        for part in input_parts:
            if "%" in part:
                adjustment_conditions.append("prod_cd LIKE ?")
                adjustment_params.append(part)
            else:
                adjustment_conditions.append("prod_cd LIKE ?")
                adjustment_params.append(f"{part}%")
        
        combined_condition_adjustment = " OR ".join(adjustment_conditions) if adjustment_conditions else "1=1"
        
        # Query matching SQL_connector exactly
        adjustment_query = f"""
            SELECT 
                prod_cd,
                log_num,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR) AS formatted_log_dt,
                log_by,
                log_qty
            FROM [dbo].[wm_log]
            WHERE ({combined_condition_adjustment})
            AND type_cd = 'adj'
            ORDER BY log_dt DESC
        """
        
        for conn, db_name in [(conn_fdc, 'FDC'), (conn_oms_ef, 'E_FAS')]:
            try:
                cursor = conn.cursor()
                cursor.execute(adjustment_query, adjustment_params)
                rows = cursor.fetchall()
                
                for row in rows:
                    adjustment_data.append({
                        'database': db_name,
                        'sku': str(row[0]).strip() if row[0] else '',
                        'log_number': str(row[1]).strip() if row[1] else '',
                        'date': str(row[2]) if row[2] else '',
                        'adjusted_by': str(row[3]).strip() if row[3] else '',
                        'quantity': int(row[4])
                    })
                cursor.close()
            except Exception as e:
                pass
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by date descending
        if adjustment_data:
            adjustment_data.sort(key=lambda x: x['date'], reverse=True)
        return adjustment_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching adjustment history for {style_or_sku}: {e}")
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return []


def fetch_adjustment_log_detail(log_number, database):
    """
    Fetch all line items for an inventory adjustment log batch from one database.
    Returns dict with log header info and items list, or None if not found.
    """
    log_number = str(log_number).strip() if log_number else ""
    database = str(database).strip().upper() if database else ""
    if not log_number or database not in ("FDC", "E_FAS"):
        return None

    conn_fdc, conn_oms_ef = get_db_connections()
    conn = conn_fdc if database == "FDC" else conn_oms_ef
    if not conn:
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return None

    detail_query = """
        SELECT
            RTRIM(prod_cd) AS prod_cd,
            log_qty,
            RTRIM(log_by) AS log_by,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR) AS formatted_log_dt,
            RTRIM(ISNULL(bin_cd, '')) AS bin_cd,
            RTRIM(ISNULL(whs_num, '')) AS whs_num,
            nt_num,
            RTRIM(ISNULL(trans_type, '')) AS trans_type,
            CAST(ISNULL(in_stock, 0) AS INT) AS in_stock,
            RTRIM(ISNULL(ut_desc, '')) AS ut_desc,
            RTRIM(ISNULL(updt_by, '')) AS updt_by,
            CAST(ISNULL(bin_qty, 0) AS INT) AS bin_qty,
            CAST(ISNULL(ord_qty, 0) AS INT) AS ord_qty
        FROM [dbo].[wm_log]
        WHERE RTRIM(log_num) = ? AND type_cd = 'adj'
        ORDER BY nt_num
    """

    try:
        cursor = conn.cursor()
        cursor.execute(detail_query, (log_number,))
        rows = cursor.fetchall()
        cursor.close()

        if not rows:
            return None

        items = []
        total_qty = 0
        for row in rows:
            qty = int(row[1]) if row[1] is not None else 0
            total_qty += qty
            items.append({
                'sku': row[0] or '',
                'quantity': qty,
                'adjusted_by': row[2] or '',
                'date': str(row[3]) if row[3] else '',
                'bin': row[4] or '',
                'warehouse': row[5] or '',
                'line_num': int(row[6]) if row[6] is not None else 0,
                'trans_type': row[7] or '',
                'in_stock': int(row[8]) if row[8] is not None else 0,
                'ut_desc': row[9] or '',
                'updated_by': row[10] or '',
                'bin_qty': int(row[11]) if row[11] is not None else 0,
                'ord_qty': int(row[12]) if row[12] is not None else 0,
            })

        first = items[0]
        return {
            'log_number': log_number,
            'database': database,
            'date': first['date'],
            'adjusted_by': first['adjusted_by'],
            'trans_type': first['trans_type'],
            'line_count': len(items),
            'total_quantity': total_qty,
            'items': items,
        }
    except Exception as e:
        current_app.logger.error(
            f"Error fetching adjustment log detail {log_number} ({database}): {e}"
        )
        return None
    finally:
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()


def fetch_purchase_history(style_or_sku):
    """
    Fetches purchase order history for a style or SKU from both databases.
    Returns list of dicts with: database, sku, po_number, po_date, purchase_quantity
    """
    # Clean up the input - remove extra spaces
    style_or_sku = style_or_sku.strip() if style_or_sku else ""
    if not style_or_sku:
        return []
    
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return []
    
    purchase_data = []
    
    try:
        # Build search parameter with wildcard
        search_param = f"{style_or_sku}%"
        
        for conn, db_name in [(conn_fdc, 'FDC'), (conn_oms_ef, 'E_FAS')]:
            cursor = conn.cursor()
            
            # Query to get purchase order history - matches SQL_connector pattern
            query = """
                SELECT TOP 100
                    pl.prod_cd,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR) AS formatted_date,
                    pl.pur_num,
                    pl.hdl_chg
                FROM plog pl
                WHERE pl.prod_cd LIKE ?
                AND pl.hdl_chg > 0
                ORDER BY pl.log_date DESC
            """
            
            try:
                cursor.execute(query, (search_param,))
                rows = cursor.fetchall()
                
                for row in rows:
                    purchase_data.append({
                        'database': db_name,
                        'sku': row[0],
                        'po_date': str(row[1]) if row[1] else '',
                        'po_number': row[2],
                        'purchase_quantity': int(row[3])
                    })
            except Exception as e:
                pass
            finally:
                cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by PO date descending
        if purchase_data:
            purchase_data.sort(key=lambda x: x['po_date'], reverse=True)
        return purchase_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching purchase history for {style_or_sku}: {e}")
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return []


def _sku_color_token(sku) -> str:
    parts = str(sku or "").strip().split()
    return parts[-1] if len(parts) > 1 else ""


def _boat_hub_index() -> dict:
    """Container id -> /boat hub row from the cached payload."""
    try:
        from py.Toolkits.boat import load_latest_cached_boat_payload, normalize_container_id
        payload = load_latest_cached_boat_payload() or {}
    except Exception:
        return {}
    index = {}
    for row in payload.get("data") or []:
        cid = normalize_container_id(row.get("container"))
        if cid:
            index[cid] = row
    return index


def _enrich_ocean_with_boat(ocean_data: list) -> None:
    """Attach BOL / vessel / ETD / ETA / status from Container Hub."""
    if not ocean_data:
        return
    try:
        from py.Toolkits.boat import normalize_container_id
    except Exception:
        def normalize_container_id(container_val):
            return str(container_val or "").strip().upper()

    index = _boat_hub_index()
    for item in ocean_data:
        item["color"] = _sku_color_token(item.get("sku"))
        cid = normalize_container_id(item.get("po_number"))
        hub = index.get(cid) or {}
        item["bol"] = str(hub.get("bol") or "").strip()
        item["vessel_no"] = str(hub.get("vessel_no") or "").strip()
        item["etd"] = str(hub.get("etd") or "").strip()
        item["eta"] = str(hub.get("eta") or "").strip()
        item["status"] = str(hub.get("status_label") or "").strip()
        item["status_mode"] = str(hub.get("action_mode") or "").strip()


def fetch_ocean_data(style_or_sku):
    """
    Fetches ocean/in-transit items for a style or SKU from both databases.
    Returns list of dicts with: database, sku, po_number, po_date, quantity
    Ocean items are those where hdl_chg = 0 (not yet received)
    Excludes any PO that has ANY received items (hdl_chg > 0)
    """
    # Clean up the input - remove extra spaces
    style_or_sku = style_or_sku.strip() if style_or_sku else ""
    if not style_or_sku:
        return []
    
    conn_fdc, conn_oms_ef = get_db_connections()
    if not conn_fdc or not conn_oms_ef:
        return []
    
    ocean_data = []
    
    try:
        # Build search parameter with wildcard
        search_param = f"{style_or_sku}%"
        
        for conn, db_name in [(conn_fdc, 'FDC'), (conn_oms_ef, 'E_FAS')]:
            cursor = conn.cursor()
            
            # Query to get ocean data - items not yet received (hdl_chg = 0)
            # BUT only from POs that have NO received items at all
            # This excludes mixed POs that have both received and unrecieved items
            query = """
                SELECT TOP 100
                    pl.prod_cd,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR) AS formatted_date,
                    pl.pur_num,
                    CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END as qty
                FROM plog pl
                WHERE pl.prod_cd LIKE ?
                AND pl.hdl_chg = 0
                AND pl.log_qty > 0
                AND pl.pur_num NOT IN (
                    SELECT DISTINCT pur_num FROM plog WHERE prod_cd LIKE ? AND hdl_chg > 0
                )
                ORDER BY pl.log_date DESC
            """
            
            try:
                cursor.execute(query, (search_param, search_param))
                rows = cursor.fetchall()
                
                for row in rows:
                    ocean_data.append({
                        'database': db_name,
                        'sku': row[0],
                        'po_date': str(row[1]) if row[1] else '',
                        'po_number': row[2],
                        'quantity': int(row[3])
                    })
            except Exception as e:
                pass
            finally:
                cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by PO date descending
        if ocean_data:
            ocean_data.sort(key=lambda x: x['po_date'], reverse=True)
            _enrich_ocean_with_boat(ocean_data)
        return ocean_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching ocean data for {style_or_sku}: {e}")
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return []



def search_product_data_with_history(search_input):
    """
    Search for products by SKU or Style with history data.
    Returns style-grouped data with color variants and history.
    """
    product_data = search_product_data(search_input)
    
    if not product_data:
        return None
    
    conn_fdc, conn_oms_ef = get_db_connections()
    all_skus = [
        color.get('sku')
        for style_group in product_data
        for color in style_group.get('colors') or []
        if color.get('sku')
    ]
    history_fdc_map = fetch_sku_history_batch(all_skus, conn_fdc) if conn_fdc else {}
    history_efas_map = fetch_sku_history_batch(all_skus, conn_oms_ef) if conn_oms_ef else {}
    
    for style_group in product_data:
        first_sku = style_group['colors'][0]['sku'] if style_group['colors'] else None
        location_data = fetch_locations([first_sku]) if first_sku else {}
        
        try:
            location_key = first_sku.replace('\u00A0', ' ').strip().lower() if first_sku else ''
        except Exception:
            location_key = str(first_sku).strip().lower() if first_sku else ''
        
        style_group['location'] = location_data.get(location_key, 'N/A')
        
        style = style_group['parent_info']['style']
        style_group['description'] = fetch_description(style) or 'N/A'
        style_oms = fetch_oms_description(style, conn_fdc, conn_oms_ef) or 'N/A'
        style_group['oms_description'] = style_oms
        need_per_sku_desc = isinstance(style_oms, list) and len(style_oms) > 1
        sku_desc_map = (
            fetch_oms_descriptions_by_skus(
                [c.get('sku') for c in style_group['colors'] if c.get('sku')],
                conn_fdc,
                conn_oms_ef,
            )
            if need_per_sku_desc
            else {}
        )
        
        for color in style_group['colors']:
            sku_key = _history_sku_key(color.get('sku'))
            history_fdc = history_fdc_map.get(sku_key) or dict(_EMPTY_SKU_HISTORY)
            history_efas = history_efas_map.get(sku_key) or dict(_EMPTY_SKU_HISTORY)
            color['oms_description'] = sku_desc_map.get(sku_key) or 'N/A'
            color['history_fdc'] = history_fdc
            color['history_efas'] = history_efas
            color['history_combined'] = {
                'history_po': int(history_fdc['history_po']) + int(history_efas['history_po']),
                'history_sold': int(history_fdc['history_sold']) + int(history_efas['history_sold']),
                'history_fix': int(history_fdc['history_fix']) + int(history_efas['history_fix'])
            }
    
    if conn_fdc:
        conn_fdc.close()
    if conn_oms_ef:
        conn_oms_ef.close()
    
    return product_data


def fetch_style_images(style, first_sku=None):
    """
    Fetch catalog images for a specific style from DAM system.
    The style parameter IS the search term (e.g., "M1141").
    Returns list of (image_url, filename) tuples.
    """
    try:
        # Strip and use style name directly - it's the base product code
        search_term = style.strip() if style else first_sku
        
        # Search for images matching the style using DAM system
        # Only get catalog_original source for product detail
        matching_assets = search_assets_by_sku_pattern(search_term, source_filter='catalog_original')
        
        # Filter to canonical images only
        canonical_assets = [asset for asset in matching_assets if asset.is_canonical]
        
        # If no canonical found, use all
        if not canonical_assets:
            canonical_assets = matching_assets
        
        # Convert to image URLs and filenames
        images = []
        for asset in canonical_assets[:3]:  # Limit to first 3 images
            image_url = get_asset_url(asset.id)
            images.append({
                'url': image_url,
                'filename': asset.filename
            })
        
        return images
        
    except Exception as e:
        current_app.logger.error(f"Error fetching images for {style}: {e}", exc_info=True)
        return []



@product_detail_bp.before_request
def _redirect_client_users_from_internal_product_detail():
    from py.other.clientview import redirect_client_user_to_product_detail

    return redirect_client_user_to_product_detail()


@product_detail_bp.route("/productdetail", methods=["GET", "POST"])
@product_detail_bp.route("/productdetail/<string:search_input>", methods=["GET"])
@product_detail_bp.route("/product_detail", methods=["GET", "POST"])
@login_required
def product_detail(search_input=None):
    """Main route for product detail page"""
    from py.other.clientview import redirect_client_user_to_product_detail

    blocked = redirect_client_user_to_product_detail()
    if blocked:
        return blocked

    current_user = session.get('username', 'User')
    product_data = None
    error_message = None
    history_data = []
    adjustment_data = []
    purchase_data = []
    
    # Get search input from POST, URL parameter, or query string
    if request.method == "POST":
        search_input = request.form.get("search_input", "").strip()
    elif request.method == "GET" and search_input:
        search_input = search_input.strip()
    else:
        # Check for search_id or search query parameter
        search_input = request.args.get("search_id", "").strip()
        if not search_input:
            search_input = request.args.get("search", "").strip()
        if not search_input:
            search_input = ""
    
    # Search if input provided
    if search_input:
        log_user_action(f"***{current_user}***__@@@search SKU@@@__ {search_input.lower()} [newpage]", current_user)
        # Fetch product data WITHOUT history (optimization: lazy load on demand)
        product_data = search_product_data_with_history(search_input)
        if not product_data:
            error_message = "No products found matching your search."
        else:
            # Fetch images for each style using first SKU
            for style_group in product_data:
                first_sku = style_group['colors'][0]['sku'] if style_group['colors'] else None
                style_group['images'] = fetch_style_images(style_group['style'], first_sku)
                
                # Pre-aggregate color count for pagination
                style_group['total_colors'] = len(style_group['colors'])
            
            # DO NOT fetch all history upfront - it will be lazy-loaded via AJAX
            # This reduces initial page load time significantly
    
    return render_template(
        "product_detail.html",
        product_data=product_data,
        search_input=search_input,
        current_user=current_user,
        error_message=error_message,
        history_data=history_data,
        adjustment_data=adjustment_data,
        purchase_data=purchase_data
    )


@product_detail_bp.route("/get_descriptions", methods=["GET"])
def get_descriptions():
    """Get all available descriptions from the database"""
    try:
        connection = get_mysql_connection()
        if not connection:
            return jsonify({"success": False, "error": "Database connection failed"})
        
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT description FROM onlinedb.description ORDER BY description")
        rows = cursor.fetchall()
        descriptions = [row[0] for row in rows]
        cursor.close()
        connection.close()
        
        return jsonify({"success": True, "descriptions": descriptions})
    except Exception as e:
        current_app.logger.error(f"Error fetching descriptions: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/update_description", methods=["POST"])
def update_description():
    """Update description for a style"""
    try:
        data = request.get_json()
        style = data.get('style', '').strip()
        description = data.get('description', '').strip()
        
        if not style or not description:
            return jsonify({"success": False, "error": "Style and description are required"})
        
        connection = get_mysql_connection()
        if not connection:
            return jsonify({"success": False, "error": "Database connection failed"})
        
        cursor = connection.cursor()
        
        # Check if description exists for this style
        cursor.execute("SELECT COUNT(*) FROM onlinedb.description WHERE style = %s", (style.upper(),))
        count = cursor.fetchone()[0]
        
        if count > 0:
            # Update existing
            cursor.execute(
                "UPDATE onlinedb.description SET description = %s WHERE style = %s",
                (description, style.upper())
            )
        else:
            # Insert new
            cursor.execute(
                "INSERT INTO onlinedb.description (style, description) VALUES (%s, %s)",
                (style.upper(), description)
            )
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({"success": True, "message": "Description updated successfully"})
    except Exception as e:
        current_app.logger.error(f"Error updating description: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/product/search", methods=["POST"])
@login_required
def api_product_search():
    """API endpoint for AJAX searches"""
    from py.other.clientview import redirect_client_user_to_product_detail

    blocked = redirect_client_user_to_product_detail()
    if blocked:
        return blocked

    try:
        search_input = request.json.get("search_input", "").strip()
        current_user = session.get('username', 'User')
        
        if not search_input:
            return jsonify({"success": False, "error": "Search input required"})

        log_user_action(f"***{current_user}***__@@@search SKU@@@__ {search_input.lower()}", current_user)
        
        product_data = search_product_data(search_input)
        
        if product_data:
            return jsonify({"success": True, "data": product_data})
        else:
            return jsonify({"success": False, "error": "No products found"})
            
    except Exception as e:
        current_app.logger.error(f"API search error: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/history/<string:style>", methods=["GET"])
def get_history_data(style):
    """Lazy-load history data for a style"""
    from py.utilities.client_product_detail_permissions import enforce_client_pd_section_api

    blocked = enforce_client_pd_section_api("item_history")
    if blocked:
        return blocked
    try:
        history_data = fetch_item_history(style)
        return jsonify({"success": True, "data": history_data})
    except Exception as e:
        current_app.logger.error(f"Error fetching history for {style}: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/adjustments/<string:style>", methods=["GET"])
def get_adjustment_data(style):
    """Lazy-load adjustment data for a style"""
    from py.utilities.client_product_detail_permissions import enforce_client_pd_section_api

    blocked = enforce_client_pd_section_api("inventory_adjustment")
    if blocked:
        return blocked
    try:
        adjustment_data = fetch_adjustment_history(style)
        return jsonify({"success": True, "data": adjustment_data})
    except Exception as e:
        current_app.logger.error(f"Error fetching adjustments for {style}: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/adjustments/log/<string:database>/<string:log_number>", methods=["GET"])
def get_adjustment_log_detail(database, log_number):
    """Fetch all line items for a single inventory adjustment log batch."""
    from py.utilities.client_product_detail_permissions import enforce_client_pd_section_api

    blocked = enforce_client_pd_section_api("inventory_adjustment")
    if blocked:
        return blocked
    try:
        detail = fetch_adjustment_log_detail(log_number, database)
        if not detail:
            return jsonify({"success": False, "error": "Adjustment log not found."}), 404
        return jsonify({"success": True, "data": detail})
    except Exception as e:
        current_app.logger.error(
            f"Error fetching adjustment log {log_number} ({database}): {e}"
        )
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/purchases/<string:style>", methods=["GET"])
def get_purchase_data(style):
    """Lazy-load purchase data for a style"""
    from py.utilities.client_product_detail_permissions import enforce_client_pd_section_api

    blocked = enforce_client_pd_section_api("purchase_history")
    if blocked:
        return blocked
    try:
        purchase_data = fetch_purchase_history(style)
        return jsonify({"success": True, "data": purchase_data})
    except Exception as e:
        current_app.logger.error(f"Error fetching purchases for {style}: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route("/api/ocean/<string:style>", methods=["GET"])
def get_ocean_data(style):
    """Lazy-load ocean/in-transit data for a style"""
    from py.utilities.client_product_detail_permissions import enforce_client_pd_section_api

    blocked = enforce_client_pd_section_api("ocean")
    if blocked:
        return blocked
    try:
        ocean_data = fetch_ocean_data(style)
        return jsonify({"success": True, "data": ocean_data})
    except Exception as e:
        current_app.logger.error(f"Error fetching ocean data for {style}: {e}")
        return jsonify({"success": False, "error": str(e)})


def _normalize_sku_key(sku):
    try:
        return sku.replace('\u00A0', ' ').strip().lower()
    except Exception:
        return str(sku).strip().lower()


def _fetch_skus_for_style(style, conn_fdc, conn_oms_ef):
    """Collect distinct SKUs for a style from FDC and E_FAS (matches product search logic)."""
    style_clean = style.strip().upper() if isinstance(style, str) else str(style).strip().upper()
    if not style_clean:
        return []

    skus = set()
    query = """
        SELECT DISTINCT i.PROD_CD
        FROM dbo.inv AS i
        WHERE UPPER(LTRIM(RTRIM(i.ALT_CD))) = ?
           OR UPPER(LTRIM(RTRIM(i.PROD_CD))) LIKE ?
    """
    for conn in (conn_fdc, conn_oms_ef):
        if not conn:
            continue
        cursor = conn.cursor()
        cursor.execute(query, (style_clean, f"{style_clean}%"))
        for row in cursor.fetchall():
            if row and row[0]:
                skus.add(row[0])
        cursor.close()

    return sorted(skus, key=lambda s: str(s).upper())


@product_detail_bp.route('/api/locations/<string:style>', methods=['GET'])
@login_required
def get_locations_for_style(style):
    """Get all locations for all SKUs of a given style"""
    try:
        conn_fdc, conn_oms_ef = get_db_connections()
        if not conn_fdc and not conn_oms_ef:
            return jsonify({"success": False, "error": "Database connection failed"})

        skus = _fetch_skus_for_style(style, conn_fdc, conn_oms_ef)

        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()

        if not skus:
            return jsonify({"success": True, "data": {}})

        # Fetch locations from MySQL; include every SKU (empty string if none set)
        location_dict = fetch_locations(skus)
        result = {}
        for sku in skus:
            key = _normalize_sku_key(sku)
            result[key] = location_dict.get(key, '')

        return jsonify({"success": True, "data": result})
    except Exception as e:
        current_app.logger.error(f"Error fetching locations for style {style}: {e}")
        return jsonify({"success": False, "error": str(e)})


@product_detail_bp.route('/api/container/bol/<container_id>', methods=['GET'])
@login_required
def get_container_bol(container_id):
    """Fetch container hub row from /boat data (BOL, vessel, ETD/ETA, status, etc.)."""
    try:
        from py.Toolkits.boat import lookup_boat_hub_row

        row = lookup_boat_hub_row(container_id)
        if row:
            return jsonify({
                'success': True,
                'bol_num': row.get('bol') or '',
                'row': row,
            })

        import pandas as pd
        from py.utilities.boat_sheet_cache import GOOGLE_SHEETS_URL, load_sheet_dataframe

        try:
            df, _sheet_info = load_sheet_dataframe()
        except Exception:
            df = pd.read_csv(GOOGLE_SHEETS_URL)

        container_id = (container_id or "").strip().upper()
        if "FDC container" not in df.columns:
            return jsonify({'success': True, 'bol_num': '', 'row': None})

        match = df[df['FDC container'].astype(str).str.strip().str.upper() == container_id]
        if match.empty:
            return jsonify({'success': True, 'bol_num': '', 'row': None})

        sheet_row = match.iloc[0]
        bol_num = sheet_row.get('BOL NUM', '')
        bol_num = str(bol_num).strip() if bol_num is not None and pd.notna(bol_num) else ''
        return jsonify({'success': True, 'bol_num': bol_num, 'row': None})

    except Exception as e:
        current_app.logger.error(f"Error fetching BOL for {container_id}: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


def fetch_combined_inventory(skus, conn_fdc, conn_oms_ef):
    """Combined FDC + E_FAS inventory per SKU (matches product_detail combined view)."""
    result = {
        sku: {'stock': 0, 'available': 0, 'sold': 0, 'catalog': None, 'color': None}
        for sku in skus
    }
    if not skus:
        return result

    inv_query = """
        SELECT
            i.PROD_CD,
            CAST(ISNULL(id.in_stock, 0) AS INT),
            CAST(ISNULL(id.ORDER_QTY, 0) AS INT),
            CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT),
            i.DESCRIP,
            i.UNIT_COLOR
        FROM dbo.inv i
        LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.Prod_cd
        WHERE i.PROD_CD = ?
    """

    for conn in (conn_fdc, conn_oms_ef):
        if not conn:
            continue
        cursor = conn.cursor()
        for sku in skus:
            cursor.execute(inv_query, [sku])
            row = cursor.fetchone()
            if not row:
                continue
            entry = result[sku]
            entry['stock'] += int(row[1] or 0)
            entry['sold'] += int(row[2] or 0)
            entry['available'] += int(row[3] or 0)
            if row[4] and not entry['catalog']:
                entry['catalog'] = row[4]
            if row[5] and not entry['color']:
                entry['color'] = (row[5] or '').strip()
        cursor.close()

    return result


def group_container_by_style(items):
    """Group container line items by style with per-style totals and catalog image."""
    from collections import OrderedDict

    groups_dict = OrderedDict()
    for item in sorted(items, key=lambda x: ((x.get('style') or ''), x.get('sku') or '')):
        style = (item.get('style') or '').strip().upper()
        if style not in groups_dict:
            groups_dict[style] = {
                'style': style,
                'rows': [],
                'image_url': None,
                'totals': {
                    'container_qty': 0,
                    'available': 0,
                    'in_warehouse': 0,
                    'sold_reserved': 0,
                },
            }
        group = groups_dict[style]
        group['rows'].append(item)
        group['totals']['container_qty'] += int(item.get('container_qty', 0) or 0)
        group['totals']['available'] += int(item.get('available', 0) or 0)
        group['totals']['in_warehouse'] += int(item.get('in_warehouse', 0) or 0)
        group['totals']['sold_reserved'] += int(item.get('sold_reserved', 0) or 0)

    style_groups = []
    grand_totals = {
        'container_qty': 0,
        'available': 0,
        'in_warehouse': 0,
        'sold_reserved': 0,
    }
    for group in groups_dict.values():
        images = fetch_style_images(group['style'], group['rows'][0]['sku'] if group['rows'] else None)
        group['image_url'] = images[0]['url'] if images else None
        group['row_span'] = len(group['rows']) + 1
        style_groups.append(group)
        for key in grand_totals:
            grand_totals[key] += group['totals'][key]

    return style_groups, grand_totals


@product_detail_bp.route('/container_search', methods=['GET', 'POST'])
@login_required
def container_search():
    """Search products by container PO number - simplified using plog table"""
    current_user = session.get('username', 'Unknown')
    container_id = request.args.get('container_id', '') or request.form.get('container_id', '')
    container_id = container_id.strip().upper()

    container_data = []
    style_groups = []
    grand_totals = None
    po_date = None
    total_msrp = 0
    latest_containers = []
    ocean_containers = []
    arrived_containers = []
    is_arrived = False
    boat_row = None

    try:
        conn_fdc, conn_oms_ef = get_db_connections()

        if conn_fdc:
            cursor = conn_fdc.cursor()
            cursor.execute("""
                SELECT DISTINCT TOP 5 PUR_NUM, LOG_DATE
                FROM plog
                WHERE PUR_NUM IS NOT NULL AND PUR_NUM != ''
                AND PUR_NUM NOT LIKE 'L%'
                ORDER BY LOG_DATE DESC
            """)
            latest_containers = [row[0] for row in cursor.fetchall()]
            cursor.close()

        try:
            from py.Toolkits.boat import sidebar_container_lists
            ocean_containers, arrived_containers = sidebar_container_lists()
        except Exception as list_err:
            current_app.logger.warning(f"Could not load sidebar container lists: {list_err}")
        if not arrived_containers:
            arrived_containers = [
                {"id": str(c or "").strip(), "eta": ""}
                for c in latest_containers
                if str(c or "").strip()
            ]

        if container_id and conn_fdc:
            cursor = conn_fdc.cursor()
            query = """
                SELECT
                    pl.prod_cd as sku,
                    LEFT(pl.prod_cd, CHARINDEX(' ', pl.prod_cd + ' ') - 1) as style,
                    pl.pur_num as po_num,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR) AS po_date,
                    CASE WHEN ISNULL(pl.hdl_chg, 0) > 0 THEN pl.hdl_chg ELSE pl.log_qty END as container_qty,
                    ISNULL(i.RETAIL_PRS, 0) as price,
                    ISNULL(pl.hdl_chg, 0) as hdl_chg
                FROM plog pl
                LEFT JOIN dbo.inv i ON pl.prod_cd = i.PROD_CD
                WHERE RTRIM(pl.pur_num) = ?
                AND (ISNULL(pl.hdl_chg, 0) > 0 OR ISNULL(pl.log_qty, 0) > 0)
                ORDER BY style, pl.prod_cd
            """
            cursor.execute(query, [container_id])
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            cursor.close()

            seen_skus = set()
            for row in rows:
                row_dict = dict(zip(columns, row))
                sku = (row_dict.get('sku') or '').strip()
                if not sku or sku in seen_skus:
                    continue
                seen_skus.add(sku)
                container_data.append(row_dict)

                if float(row_dict.get('hdl_chg') or 0) > 0:
                    is_arrived = True

                qty = float(row_dict.get('container_qty', 0) or 0)
                price = float(row_dict.get('price', 0) or 0)
                total_msrp += qty * price

                if row_dict.get('po_date') and not po_date:
                    po_date = row_dict['po_date']

            inventory = fetch_combined_inventory(list(seen_skus), conn_fdc, conn_oms_ef)
            for item in container_data:
                sku = (item.get('sku') or '').strip()
                inv = inventory.get(sku, {})
                item['catalog'] = inv.get('catalog') or 'N/A'
                item['color'] = inv.get('color') or '-'
                item['available'] = inv.get('stock', 0)
                item['in_warehouse'] = inv.get('available', 0)
                item['sold_reserved'] = inv.get('sold', 0)

            style_groups, grand_totals = group_container_by_style(container_data)

        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()

        if container_id:
            try:
                from py.Toolkits.boat import lookup_boat_hub_row
                boat_row = lookup_boat_hub_row(container_id)
            except Exception as boat_err:
                current_app.logger.warning(f"Could not load boat hub row for {container_id}: {boat_err}")

        current_app.logger.info(
            f"Container search for {container_id} by {current_user}: "
            f"Found {len(container_data)} items in {len(style_groups)} styles, "
            f"Status: {'arrived' if is_arrived else 'ocean'}, Total MSRP: ${total_msrp:.2f}"
        )

    except Exception as e:
        current_app.logger.error(f"Error searching container {container_id}: {str(e)}", exc_info=True)
        container_data = []
        style_groups = []
        grand_totals = None

    return render_template(
        "container_detail.html",
        container_id=container_id,
        container_data=container_data,
        style_groups=style_groups,
        grand_totals=grand_totals,
        po_date=po_date,
        total_msrp=total_msrp,
        latest_containers=latest_containers,
        ocean_containers=ocean_containers,
        arrived_containers=arrived_containers,
        is_arrived=is_arrived,
        boat_row=boat_row or {},
        current_user=current_user
    )
