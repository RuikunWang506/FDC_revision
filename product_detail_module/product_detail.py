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
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()



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
                    'last_activity': row[16],
                    'create_dt': row[17]
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
                    'msrp': item['msrp'],
                    'cost': item['cost'],
                    'dimensions': f"{item['len']}x{item['wid']}x{item['hei']}"
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
    Returns list of dicts with: database, product_code, order_number, order_date, customer_id, order_quantity, unit_price
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
            
            # Query to get order history - uses LIKE for wildcard search
            query = """
                SELECT TOP 100
                    ol.prod_cd,
                    ol.ord_num,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS order_dt,
                    c.cus_nm AS cus_id,
                    ol.order_qty,
                    ISNULL(ol.unit_prs, 0.00) as unit_price
                FROM ord_log ol
                LEFT JOIN customer c ON ol.cus_id = c.cus_id
                WHERE ol.prod_cd LIKE ?
                ORDER BY ol.olg_dt DESC
            """
            
            try:
                cursor.execute(query, (search_param,))
                rows = cursor.fetchall()
                current_app.logger.info(f"Item history query for {style_or_sku} in {db_name}: {len(rows)} rows")
                
                for row in rows:
                    history_data.append({
                        'database': db_name,
                        'product_code': row[0],
                        'order_number': row[1],
                        'order_date': str(row[2]) if row[2] else '',
                        'customer_id': row[3],
                        'order_quantity': int(row[4]),
                        'unit_price': float(row[5])
                    })
            except Exception as e:
                current_app.logger.error(f"Error fetching item history from {db_name}: {e}")
            finally:
                cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by order date descending
        if history_data:
            history_data.sort(key=lambda x: x['order_date'], reverse=True)
        current_app.logger.info(f"Total item history records found: {len(history_data)}")
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
        current_app.logger.info(f"Adjustment search - Conditions: {adjustment_conditions}, Params: {adjustment_params}")
        
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
                current_app.logger.info(f"Adjustment history query for {style_or_sku} in {db_name}: {len(rows)} rows")
                
                for row in rows:
                    adjustment_data.append({
                        'database': db_name,
                        'sku': row[0],
                        'log_number': row[1],
                        'date': str(row[2]) if row[2] else '',
                        'adjusted_by': row[3],
                        'quantity': int(row[4])
                    })
                cursor.close()
            except Exception as e:
                current_app.logger.error(f"Error fetching adjustment history from {db_name}: {e}")
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by date descending
        if adjustment_data:
            adjustment_data.sort(key=lambda x: x['date'], reverse=True)
        current_app.logger.info(f"Total adjustment history records found: {len(adjustment_data)}")
        return adjustment_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching adjustment history for {style_or_sku}: {e}")
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()
        return []


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
                current_app.logger.info(f"Purchase history query for {style_or_sku} in {db_name}: {len(rows)} rows")
                
                for row in rows:
                    purchase_data.append({
                        'database': db_name,
                        'sku': row[0],
                        'po_date': str(row[1]) if row[1] else '',
                        'po_number': row[2],
                        'purchase_quantity': int(row[3])
                    })
            except Exception as e:
                current_app.logger.error(f"Error fetching purchase history from {db_name}: {e}")
            finally:
                cursor.close()
        
        conn_fdc.close()
        conn_oms_ef.close()
        
        # Sort by PO date descending
        if purchase_data:
            purchase_data.sort(key=lambda x: x['po_date'], reverse=True)
        current_app.logger.info(f"Total purchase history records found: {len(purchase_data)}")
        return purchase_data
        
    except Exception as e:
        current_app.logger.error(f"Error fetching purchase history for {style_or_sku}: {e}")
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
        
        for color in style_group['colors']:
            sku = color['sku']
            
            # Fetch history from both databases
            history_fdc = fetch_sku_history(sku, conn_fdc) if conn_fdc else {'history_po': 0, 'history_sold': 0, 'history_fix': 0}
            history_efas = fetch_sku_history(sku, conn_oms_ef) if conn_oms_ef else {'history_po': 0, 'history_sold': 0, 'history_fix': 0}
            
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
        product_data = search_product_data_with_history(search_input)
        if not product_data:
            error_message = "No products found matching your search."
        else:
            # Fetch images for each style using first SKU
            for style_group in product_data:
                first_sku = style_group['colors'][0]['sku'] if style_group['colors'] else None
                style_group['images'] = fetch_style_images(style_group['style'], first_sku)
            
            # Collect all SKUs from the search results
            all_skus = []
            for style_group in product_data:
                for color in style_group['colors']:
                    all_skus.append(color['sku'])
            
            # Fetch history data for each SKU found
            if all_skus:
                for sku in all_skus:
                    history_data.extend(fetch_item_history(sku))
                    adjustment_data.extend(fetch_adjustment_history(sku))
                    purchase_data.extend(fetch_purchase_history(sku))
                
                # Remove duplicates and sort
                history_data = list({(h['product_code'], h['order_number'], h['order_date']): h for h in history_data}.values())
                adjustment_data = list({(a['sku'], a['log_number'], a['date']): a for a in adjustment_data}.values())
                purchase_data = list({(p['sku'], p['po_number'], p['po_date']): p for p in purchase_data}.values())
                
                history_data.sort(key=lambda x: x['order_date'], reverse=True)
                adjustment_data.sort(key=lambda x: x['date'], reverse=True)
                purchase_data.sort(key=lambda x: x['po_date'], reverse=True)
    
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


@product_detail_bp.route("/api/product/search", methods=["POST"])
@login_required
def api_product_search():
    """API endpoint for AJAX searches"""
    try:
        search_input = request.json.get("search_input", "").strip()
        
        if not search_input:
            return jsonify({"success": False, "error": "Search input required"})
        
        product_data = search_product_data(search_input)
        
        if product_data:
            return jsonify({"success": True, "data": product_data})
        else:
            return jsonify({"success": False, "error": "No products found"})
            
    except Exception as e:
        current_app.logger.error(f"API search error: {e}")
        return jsonify({"success": False, "error": str(e)})

