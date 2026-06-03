from flask import Blueprint, request, render_template, session, current_app, jsonify
from py.utilities.setting import login_required
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from py.utilities.logging_setup import log_event
from py.utilities.database_utils import get_mysql_connection
from datetime import datetime, timedelta
import py.utilities.models
from py.utilities.dam_utils import search_assets_by_sku_pattern, get_asset_url
import os

# Get the directory of this file
module_dir = os.path.dirname(os.path.abspath(__file__))

product_detail_bp = Blueprint(
    'product_detail', 
    __name__,
    template_folder=module_dir  # Templates are in the same folder as this file
)

sql_server_config = Config.SQL_SERVER_CONFIG


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
            
            # Add database identifier to each row
            for row in rows:
                all_results.append({
                    'db': db_name,
                    'sku': row[0],
                    'style': row[1],
                    'container': row[2],
                    'stock': row[3],
                    'sold': row[4],
                    'available': row[5],
                    'incoming': row[6],
                    'cost': row[7],
                    'msrp': row[8],
                    'case_qty': row[9],
                    'box_wt': row[10],
                    'pc_wt': row[11],
                    'color': row[12],
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
        
        return group_by_style(all_results) if all_results else None
        
    except Exception as e:
        current_app.logger.error(f"Error searching product data: {e}")
        return None


def group_by_style(results):
    """
    Groups results by style and organizes colors.
    Returns structure: {style: {colors: [...], parent_info: {...}}}
    """
    grouped = {}
    
    for item in results:
        style = item['style']
        
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
        color_key = item['color'] if item['color'] else 'NO_COLOR'
        
        if color_key not in grouped[style]['color_variants']:
            grouped[style]['color_variants'][color_key] = {
                'color': item['color'],
                'sku': item['sku'],
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
        result.append(style_entry)
    
    return result


def fetch_sku_history(sku, db_conn):
    """
    Fetch order history and manual adjustments for a specific SKU from plog, ord_log, and wm_log tables
    
    Returns:
    - history_po: Sum of quantities from plog for exact SKU (total PO received)
    - history_sold: Sum of order quantities from ord_log for exact SKU (total quantity sold to customers)
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


def fetch_item_history(style_or_sku):
    """
    Fetches order history for a style or SKU from both databases.
    Returns list of dicts with: database, product_code, order_number, order_date, customer_id, order_quantity, unit_price, color
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
                    ISNULL(i.UNIT_COLOR, '') as color
                FROM ord_log ol
                LEFT JOIN customer c ON ol.cus_id = c.cus_id
                LEFT JOIN dbo.inv i ON ol.prod_cd = i.PROD_CD
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
                    
                    history_data.append({
                        'database': db_name,
                        'product_code': row[0],
                        'order_number': row[1],
                        'order_date': str(row[2]) if row[2] else '',
                        'customer_id': row[3],
                        'order_quantity': int(row[4]),
                        'unit_price': float(row[5]),
                        'color': color
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
            RTRIM(ISNULL(trans_type, '')) AS trans_type
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
    # First get the base product data
    product_data = search_product_data(search_input)
    
    if not product_data:
        return None
    
    # Now fetch history data for each color
    conn_fdc, conn_oms_ef = get_db_connections()
    
    for style_group in product_data:
        # Fetch location for first SKU of this style group
        first_sku = style_group['colors'][0]['sku'] if style_group['colors'] else None
        location_data = fetch_locations([first_sku]) if first_sku else {}
        
        # Get location using lowercase SKU
        try:
            location_key = first_sku.replace('\u00A0', ' ').strip().lower() if first_sku else ''
        except Exception:
            location_key = str(first_sku).strip().lower() if first_sku else ''
        
        style_group['location'] = location_data.get(location_key, 'N/A')
        
        # Fetch description for style
        style = style_group['parent_info']['style']
        style_group['description'] = fetch_description(style) or 'N/A'
        style_group['oms_description'] = fetch_oms_description(style, conn_fdc, conn_oms_ef) or 'N/A'
        
        for color in style_group['colors']:
            sku = color['sku']
            
            # Fetch history from both databases
            history_fdc = fetch_sku_history(sku, conn_fdc) if conn_fdc else {'history_po': 0, 'history_sold': 0, 'history_fix': 0}
            history_efas = fetch_sku_history(sku, conn_oms_ef) if conn_oms_ef else {'history_po': 0, 'history_sold': 0, 'history_fix': 0}
            
            # Fetch SKU-specific OMS description
            color['oms_description'] = fetch_oms_description_by_sku(sku, conn_fdc, conn_oms_ef) or 'N/A'
            
            # Store history data
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



@product_detail_bp.route("/productdetail", methods=["GET", "POST"])
@product_detail_bp.route("/productdetail/<string:search_input>", methods=["GET"])
@product_detail_bp.route("/product_detail", methods=["GET", "POST"])
@login_required
def product_detail(search_input=None):
    """Main route for product detail page"""
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
        current_app.logger.info("***%s***__@@@search SKU@@@__ %s [newpage]", current_user, search_input.lower())
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
    try:
        search_input = request.json.get("search_input", "").strip()
        current_user = session.get('username', 'User')
        
        if not search_input:
            return jsonify({"success": False, "error": "Search input required"})

        current_app.logger.info("***%s***__@@@search SKU@@@__ %s", current_user, search_input.lower())
        
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
    """Fetch BOL number from Google Sheets for a container"""
    try:
        import pandas as pd
        
        # Google Sheets export URL - same as used in boat service
        GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv"
        
        # Fetch data from Google Sheets
        df = pd.read_csv(GOOGLE_SHEETS_URL)
        
        # Find the container in the sheet
        container_id = container_id.strip().upper()
        match = df[df['FDC container'].astype(str).str.upper() == container_id]
        
        if not match.empty:
            bol_num = match.iloc[0].get('BOL NUM', '')
            return jsonify({'success': True, 'bol_num': str(bol_num) if bol_num and pd.notna(bol_num) else ''})
        else:
            return jsonify({'success': True, 'bol_num': ''})
    
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
    is_arrived = False

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
                    CASE WHEN pl.hdl_chg > 0 THEN pl.hdl_chg ELSE pl.log_qty END as container_qty,
                    ISNULL(i.RETAIL_PRS, 0) as price,
                    pl.hdl_chg
                FROM plog pl
                LEFT JOIN dbo.inv i ON pl.prod_cd = i.PROD_CD
                WHERE pl.pur_num = ?
                AND (pl.hdl_chg > 0 OR pl.log_qty > 0)
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

                if row_dict.get('hdl_chg', 0) > 0:
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
        is_arrived=is_arrived,
        current_user=current_user
    )
