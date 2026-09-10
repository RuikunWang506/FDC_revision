from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash, current_app
from py.utilities.setting import login_required
from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from datetime import datetime, timedelta
import json
import requests
import hashlib
import hmac
import time

internal_stock_check_bp = Blueprint('internal_stock_check', __name__)

sql_server_config = Config.SQL_SERVER_CONFIG


def get_user_manual_styles_path(username=None):
    """Get the user-specific manual styles JSON file path."""
    if username is None:
        username = session.get('username', 'default')
    import os
    return os.path.join(current_app.root_path, 'data', f'internal_manual_styles_{username}.json')


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


def init_internal_check_database():
    """Initialize the internalcheck database if it doesn't exist."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return False
        
        cursor = connection.cursor()
        
        # Create database if it doesn't exist
        cursor.execute("CREATE DATABASE IF NOT EXISTS internalcheck")
        connection.database = 'internalcheck'
        
        # Create table if it doesn't exist
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS internal_inventory (
            id INT AUTO_INCREMENT PRIMARY KEY,
            date DATETIME DEFAULT CURRENT_TIMESTAMP,
            sku VARCHAR(100) NOT NULL,
            product_name VARCHAR(255),
            quantity INT DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY unique_sku (sku),
            INDEX idx_sku (sku),
            INDEX idx_date (date)
        )
        """
        cursor.execute(create_table_sql)
        connection.commit()
        cursor.close()
        connection.close()
        return True
    except Exception as e:
        current_app.logger.error(f"Database initialization error: {e}")
        return False


def fetch_available_quantity(sku, is_manual=False):
    """Fetch available quantity from both FDC and E-Fashion databases.
    Uses the inv and inv_data tables like product_detail.py does.
    """
    conn_fdc, conn_oms_ef = get_db_connections()
    
    if conn_fdc is None or conn_oms_ef is None:
        return {'fdc': None, 'efashion': None, 'total': None}
    
    try:
        # Fetch from FDC database using inv and inv_data tables
        fdc_qty = None
        try:
            cursor_fdc = conn_fdc.cursor()
            cursor_fdc.execute("""
                SELECT CAST(ISNULL(id.in_stock, 0) AS INT) as available
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                WHERE i.PROD_CD = ? OR i.ALT_CD = ?
            """, (sku, sku))
            row = cursor_fdc.fetchone()
            fdc_qty = row[0] if row else 0
            cursor_fdc.close()
        except Exception as e:
            current_app.logger.debug(f"FDC query for SKU {sku}: {e}")
            fdc_qty = 0
        
        # Fetch from E-Fashion database using inv and inv_data tables
        efashion_qty = None
        try:
            cursor_oms_ef = conn_oms_ef.cursor()
            cursor_oms_ef.execute("""
                SELECT CAST(ISNULL(id.in_stock, 0) AS INT) as available
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                WHERE i.PROD_CD = ? OR i.ALT_CD = ?
            """, (sku, sku))
            row = cursor_oms_ef.fetchone()
            efashion_qty = row[0] if row else 0
            cursor_oms_ef.close()
        except Exception as e:
            current_app.logger.debug(f"E-Fashion query for SKU {sku}: {e}")
            efashion_qty = 0
        
        # Calculate total
        total = fdc_qty + efashion_qty if (fdc_qty is not None and efashion_qty is not None) else None
        
        return {
            'fdc': fdc_qty,
            'efashion': efashion_qty,
            'total': total
        }
    finally:
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()


def fetch_available_quantities(skus, manual_skus=None):
    """Fetch available quantities for multiple SKUs.
    Uses individual queries per SKU for reliability.
    Returns dict with SKU as key and availability data as value.
    """
    if manual_skus is None:
        manual_skus = set()
    
    conn_fdc, conn_oms_ef = get_db_connections()
    
    if conn_fdc is None or conn_oms_ef is None or not skus:
        return {sku: {'fdc': 0, 'efashion': 0, 'total': 0} for sku in skus}
    
    result = {}
    
    # Initialize all SKUs with 0
    for sku in skus:
        result[sku] = {'fdc': 0, 'efashion': 0, 'total': 0}
    
    try:
        cursor_fdc = conn_fdc.cursor()
        
        # Query all SKUs (exact match)
        if skus:
            union_query_parts = []
            for sku in skus:
                union_query_parts.append(f"""
                    SELECT '{sku}' as sku_lookup, CAST(ISNULL(SUM(id.in_stock), 0) AS INT) as total_stock
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE RTRIM(i.PROD_CD) = '{sku}' OR RTRIM(i.ALT_CD) = '{sku}'
                """)
            
            union_query = " UNION ALL ".join(union_query_parts)
            cursor_fdc.execute(union_query)
            rows = cursor_fdc.fetchall()
            
            for row in rows:
                sku = row[0]
                total_stock = row[1]
                if sku in result:
                    result[sku]['fdc'] = total_stock
        
        cursor_fdc.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
    
    try:
        cursor_oms_ef = conn_oms_ef.cursor()
        
        # Query all SKUs (exact match)
        if skus:
            union_query_parts = []
            for sku in skus:
                union_query_parts.append(f"""
                    SELECT '{sku}' as sku_lookup, CAST(ISNULL(SUM(id.in_stock), 0) AS INT) as total_stock
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE RTRIM(i.PROD_CD) = '{sku}' OR RTRIM(i.ALT_CD) = '{sku}'
                """)
            
            union_query = " UNION ALL ".join(union_query_parts)
            cursor_oms_ef.execute(union_query)
            rows = cursor_oms_ef.fetchall()
            
            for row in rows:
                sku = row[0]
                total_stock = row[1]
                if sku in result:
                    result[sku]['efashion'] = total_stock
        
        cursor_oms_ef.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
    
    # Calculate totals
    for sku in result:
        result[sku]['total'] = result[sku]['fdc'] + result[sku]['efashion']
    
    return result


# Main page - Login required for internal users
@internal_stock_check_bp.route('/internal_stock_check', methods=['GET'])
@login_required
def internal_stock_check_page():
    """Internal stock check page - requires login."""
    # Initialize database
    init_internal_check_database()
    current_user = session['username']
    return render_template('internal_stock_check.html', show_password=False,current_user=current_user)


# API: Get current inventory data
@internal_stock_check_bp.route('/api/internal_stock_check/get_inventory', methods=['GET'])
@login_required
def get_inventory():
    """Get current inventory from database with stock levels."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        connection.database = 'internalcheck'
        cursor = connection.cursor(dictionary=True)
        
        # Get all inventory items
        cursor.execute("""
            SELECT sku, product_name, quantity, date
            FROM internal_inventory
            ORDER BY date DESC, sku
        """)
        
        items = cursor.fetchall()
        
        # Extract all SKUs and fetch availability in bulk
        skus = [item['sku'] for item in items]
        availability_map = fetch_available_quantities(skus) if skus else {}
        
        # Build results with availability data
        results = []
        for item in items:
            avail_data = availability_map.get(item['sku'], {'fdc': 0, 'efashion': 0, 'total': 0})
            results.append({
                'sku': item['sku'],
                'product_name': item['product_name'],
                'internal_quantity': item['quantity'],
                'date': item['date'].isoformat() if item['date'] else None,
                'fdc_available': avail_data['fdc'],
                'efashion_available': avail_data['efashion'],
                'total_available': avail_data['total']
            })
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'data': results
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching inventory: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get basic inventory data (fast - MySQL only, no availability lookup)
@internal_stock_check_bp.route('/api/internal_stock_check/get_inventory_basic', methods=['GET'])
@login_required
def get_inventory_basic():
    """Get basic inventory data from MySQL plus manual styles (fast load, no availability lookup)."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        connection.database = 'internalcheck'
        cursor = connection.cursor(dictionary=True)
        
        # Get all inventory items from internal inventory
        cursor.execute("""
            SELECT sku, product_name, quantity, date
            FROM internal_inventory
            ORDER BY date DESC, sku
        """)
        
        items = cursor.fetchall()
        
        # Build results with basic data only
        results = []
        internal_skus = set()
        internal_base_styles = set()  # Track base styles from internal inventory
        
        for item in items:
            internal_skus.add(item['sku'])
            # Extract base style (first word, before space) for duplicate detection
            base_style = item['product_name'].split()[0] if ' ' in item['product_name'] else item['product_name']
            internal_base_styles.add(base_style)
            results.append({
                'sku': item['sku'],
                'product_name': item['product_name'],
                'internal_quantity': item['quantity'],
                'date': item['date'].isoformat() if item['date'] else None,
                'is_manual': False
            })
        
        # Load manual styles from internal JSON file (user-specific)
        import os
        manual_styles_path = get_user_manual_styles_path()
        
        if os.path.exists(manual_styles_path):
            with open(manual_styles_path, 'r', encoding='utf-8') as f:
                manual_data = json.load(f)
            
            # Add manual styles that are NOT in internal inventory
            manual_styles = manual_data.get('styles', [])
            for base_sku in manual_styles:
                # Skip if this base style already exists in internal inventory
                if base_sku not in internal_base_styles:
                    # Query database for all SKUs matching this pattern
                    conn_fdc, conn_oms_ef = get_db_connections()
                    if conn_fdc and conn_oms_ef:
                        try:
                            cursor_fdc = conn_fdc.cursor()
                            like_sku = f"{base_sku}%"
                            cursor_fdc.execute(
                                """
                                SELECT DISTINCT RTRIM(i.PROD_CD) as prod_cd
                                FROM dbo.inv AS i
                                WHERE i.PROD_CD LIKE ? OR i.ALT_CD LIKE ?
                                ORDER BY RTRIM(i.PROD_CD)
                                """,
                                (like_sku, like_sku),
                            )
                            matched_skus = {row[0] for row in cursor_fdc.fetchall()}
                            cursor_fdc.close()
                            
                            if conn_fdc:
                                conn_fdc.close()
                            if conn_oms_ef:
                                conn_oms_ef.close()
                        except Exception as e:
                            current_app.logger.debug(f"Error fetching matched SKUs for {base_sku}: {e}")
                            matched_skus = set()
                    else:
                        matched_skus = set()
                    
                    # If we found matched SKUs, add them; otherwise add the base style
                    if matched_skus:
                        for sku in sorted(matched_skus):
                            # Extract base style (everything before first space)
                            base_style = sku.split()[0] if ' ' in sku else sku
                            results.append({
                                'sku': sku,
                                'product_name': base_style,  # Style column shows base style only
                                'internal_quantity': None,
                                'date': datetime.now().isoformat(),
                                'is_manual': True
                            })
                    else:
                        # Fallback: add base style if no matches found
                        results.append({
                            'sku': base_sku,
                            'product_name': base_sku,
                            'internal_quantity': None,
                            'date': datetime.now().isoformat(),
                            'is_manual': True
                        })
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'data': results
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching basic inventory: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get availability for multiple SKUs
@internal_stock_check_bp.route('/api/internal_stock_check/get_availability', methods=['POST'])
@login_required
def get_availability():
    """Get availability data for multiple SKUs from SQL Server databases."""
    try:
        data = request.get_json()
        skus = data.get('skus', [])
        
        if not skus:
            return jsonify({'success': True, 'data': {}})
        
        availability_map = fetch_available_quantities(skus)
        
        return jsonify({
            'success': True,
            'data': availability_map
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching availability: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get single SKU stock levels
@internal_stock_check_bp.route('/api/internal_stock_check/stock/<sku>', methods=['GET'])
@login_required
def get_sku_stock(sku):
    """Get stock levels for a specific SKU."""
    try:
        stock_data = fetch_available_quantity(sku)
        return jsonify({
            'success': True,
            'sku': sku,
            'stock': stock_data
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching stock for SKU {sku}: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get SKU availability from SQL Server
@internal_stock_check_bp.route('/api/internal_stock_check/sku_availability/<sku>', methods=['GET'])
@login_required
def get_sku_availability(sku):
    """Get availability for a specific SKU from SQL Server databases."""
    try:
        stock_data = fetch_available_quantity(sku)
        
        return jsonify({
            'sku': sku,
            'fdc': stock_data['fdc'],
            'efashion': stock_data['efashion'],
            'total': stock_data['total']
        })
    except Exception as e:
        current_app.logger.error(f"Error getting availability for {sku}: {e}")
        return jsonify({'error': str(e)}), 500


# API: Export complete CSV with all inventory and availability data
@internal_stock_check_bp.route('/api/internal_stock_check/export_csv', methods=['POST'])
@login_required
def export_csv_complete():
    """
    Export all inventory data (internal + manual) with availability as CSV.
    Fetches fresh data from both MySQL and SQL Server databases.
    """
    from flask import send_file
    import io
    import os
    
    try:
        # Get all inventory from MySQL
        mysql_conn = get_mysql_connection()
        if mysql_conn is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        mysql_conn.database = 'internalcheck'
        cursor = mysql_conn.cursor(dictionary=True)
        cursor.execute("SELECT sku, product_name, quantity as internal_qty, last_updated FROM internal_inventory ORDER BY sku")
        inventory_data = cursor.fetchall()
        cursor.close()
        
        # Load manual styles from internal JSON file (user-specific)
        manual_styles_data = []
        manual_styles_path = get_user_manual_styles_path()
        
        if os.path.exists(manual_styles_path):
            with open(manual_styles_path, 'r', encoding='utf-8') as f:
                manual_data = json.load(f)
            
            manual_styles = manual_data.get('styles', [])
            for base_sku in manual_styles:
                # Query database for all SKUs matching this pattern
                conn_fdc, conn_oms_ef = get_db_connections()
                if conn_fdc and conn_oms_ef:
                    try:
                        cursor_fdc = conn_fdc.cursor()
                        like_sku = f"{base_sku}%"
                        cursor_fdc.execute(
                            """
                            SELECT DISTINCT RTRIM(i.PROD_CD) as prod_cd
                            FROM dbo.inv AS i
                            WHERE i.PROD_CD LIKE ? OR i.ALT_CD LIKE ?
                            ORDER BY RTRIM(i.PROD_CD)
                            """,
                            (like_sku, like_sku),
                        )
                        matched_skus = {row[0] for row in cursor_fdc.fetchall()}
                        cursor_fdc.close()
                        
                        if conn_fdc:
                            conn_fdc.close()
                        if conn_oms_ef:
                            conn_oms_ef.close()
                    except Exception as e:
                        current_app.logger.debug(f"Error fetching matched SKUs for {base_sku}: {e}")
                        matched_skus = set()
                else:
                    matched_skus = set()
                
                # Add matched SKUs to manual styles data
                if matched_skus:
                    for sku in sorted(matched_skus):
                        base_style = sku.split()[0] if ' ' in sku else sku
                        manual_styles_data.append({
                            'sku': sku,
                            'product_name': base_style,
                            'internal_qty': None
                        })
        
        mysql_conn.close()
        
        if not inventory_data and not manual_styles_data:
            return jsonify({'error': 'No inventory data found'}), 404
        
        # Extract all SKUs (both internal and manual)
        all_skus = [item['sku'] for item in inventory_data] + [item['sku'] for item in manual_styles_data]
        
        # Fetch availability for all SKUs
        availability_data = fetch_available_quantities(all_skus)
        
        # Build CSV data
        csv_rows = [['Style', 'SKU', 'Source', 'Internal Qty', 'Available']]
        
        # Add internal inventory rows
        for item in inventory_data:
            sku = item['sku']
            style = item['product_name'].split()[0] if item['product_name'] else ''
            internal_qty = item['internal_qty'] or 0
            available = availability_data.get(sku, {}).get('total', 0)
            source = 'Internal'
            
            csv_rows.append([style, sku, source, internal_qty, available])
        
        # Add manual styles rows
        for item in manual_styles_data:
            sku = item['sku']
            style = item['product_name']
            available = availability_data.get(sku, {}).get('total', 0)
            source = 'Manual'
            
            csv_rows.append([style, sku, source, 'N/A', available])
        
        # Write to StringIO
        output = io.StringIO()
        for row in csv_rows:
            output.write(','.join(str(cell) for cell in row) + '\n')
        
        # Create BytesIO for download
        output.seek(0)
        mem = io.BytesIO(output.getvalue().encode('utf-8'))
        mem.seek(0)
        
        return send_file(
            mem,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'internal_inventory_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
    
    except Exception as e:
        current_app.logger.error(f"CSV export error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# API: Get manual styles from JSON file
@internal_stock_check_bp.route('/api/internal_stock_check/get_manual_styles', methods=['GET'])
@login_required
def get_manual_styles():
    """Get manual styles from JSON file."""
    try:
        # Load manual styles from internal JSON file (user-specific)
        import os
        manual_styles_path = get_user_manual_styles_path()
        
        if not os.path.exists(manual_styles_path):
            return jsonify({'success': True, 'styles': []})
        
        with open(manual_styles_path, 'r', encoding='utf-8') as f:
            manual_data = json.load(f)
        
        styles_list = manual_data.get('styles', [])
        
        return jsonify({
            'success': True,
            'styles': styles_list
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching manual styles: {e}")
        return jsonify({'error': str(e)}), 500


# API: Save manual styles to JSON file
@internal_stock_check_bp.route('/api/internal_stock_check/save_manual_styles', methods=['POST'])
@login_required
def save_manual_styles():
    """Save edited manual styles to JSON file."""
    try:
        data = request.json
        styles = data.get('styles', [])
        
        # Load current data to preserve any other fields (user-specific)
        import os
        manual_styles_path = get_user_manual_styles_path()
        
        # Get all SKUs from internal inventory to filter
        mysql_conn = get_mysql_connection()
        if mysql_conn:
            mysql_conn.database = 'internalcheck'
            cursor = mysql_conn.cursor()
            cursor.execute("SELECT sku FROM internal_inventory")
            internal_skus = {row[0] for row in cursor.fetchall()}
            cursor.close()
            mysql_conn.close()
        else:
            internal_skus = set()
        
        # Filter out styles that are in internal inventory
        filtered_styles = [sku for sku in styles if sku not in internal_skus]
        
        # Save to file
        output_data = {
            'styles': filtered_styles
        }
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(manual_styles_path), exist_ok=True)
        
        with open(manual_styles_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        return jsonify({
            'success': True,
            'message': f'Saved {len(filtered_styles)} styles (excluded {len(styles) - len(filtered_styles)} that are in internal inventory)'
        })
    except Exception as e:
        current_app.logger.error(f"Error saving manual styles: {e}")
        return jsonify({'error': str(e)}), 500
