from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, current_app
from py.utilities.database_utils import get_mysql_connection, get_sql_server_connection_fdc
from py.utilities.setting import login_required
import pyodbc

outofstock_check_bp = Blueprint('outofstock_check', __name__)

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def get_fdc_connection():
    """Establishes a connection to the SQL Server FDC database."""
    return get_sql_server_connection_fdc()

@outofstock_check_bp.route('/oosc', methods=['GET'])
@login_required
def outofstock_check():
    """Display styles with locations and their stock levels"""
    current_app.logger.info(f"***{session.get('username', 'Unknown')}***___@@@Accessed@@@__Out of Stock Check")
    
    current_user = session.get('username', 'Unknown')
    
    try:
        # Get all styles with locations from MySQL
        mysql_conn = get_db_connection()
        if mysql_conn is None:
            return render_template('outofstock_check.html', 
                                 error_message="Failed to connect to MySQL database",
                                 current_user=current_user)
        
        mysql_cursor = mysql_conn.cursor(dictionary=True)
        
        # Get unique styles that have locations
        query = """
            SELECT DISTINCT Style 
            FROM products 
            WHERE Location IS NOT NULL AND Location != ''
            ORDER BY Style
        """
        mysql_cursor.execute(query)
        styles_with_locations = mysql_cursor.fetchall()
        
        mysql_cursor.close()
        mysql_conn.close()
        
        if not styles_with_locations:
            return render_template('outofstock_check.html',
                                 styles_data=[],
                                 current_user=current_user)
        
        # Get stock information from SQL Server for these styles
        fdc_conn = get_fdc_connection()
        if fdc_conn is None:
            return render_template('outofstock_check.html',
                                 error_message="Failed to connect to FDC database",
                                 current_user=current_user)
        
        fdc_cursor = fdc_conn.cursor()
        
        # Build list of styles
        style_list = [row['Style'] for row in styles_with_locations]
        
        # Query SQL Server for stock data
        placeholders = ','.join(['?' for _ in style_list])
        stock_query = f"""
            SELECT 
                i.ALT_CD as Style,
                SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) as Stock,
                SUM(CAST(ISNULL(id.order_qty, 0) AS INT)) as Sold,
                SUM(CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)) as Available,
                SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) as Ocean
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            WHERE i.ALT_CD IN ({placeholders})
            GROUP BY i.ALT_CD
        """
        
        fdc_cursor.execute(stock_query, style_list)
        stock_results = fdc_cursor.fetchall()
        
        fdc_cursor.close()
        fdc_conn.close()
        
        # Combine data
        styles_data = []
        for stock_row in stock_results:
            style = stock_row[0] if stock_row[0] else ''
            stock = int(stock_row[1]) if stock_row[1] else 0
            sold = int(stock_row[2]) if stock_row[2] else 0
            available = int(stock_row[3]) if stock_row[3] else 0
            ocean = int(stock_row[4]) if stock_row[4] else 0
            
            styles_data.append({
                'Style': style,
                'Stock': stock,
                'Sold': sold,
                'Available': available,
                'Ocean': ocean,
                'has_negative': available < 0
            })
        
        # Sort by available (negative values first)
        styles_data.sort(key=lambda x: x['Available'])
        
        return render_template('outofstock_check.html',
                             styles_data=styles_data,
                             current_user=current_user)
        
    except Exception as e:
        current_app.logger.error(f"Error in outofstock_check: {str(e)}")
        return render_template('outofstock_check.html',
                             error_message=f"An error occurred: {str(e)}",
                             current_user=current_user)

@outofstock_check_bp.route('/wipe_location', methods=['POST'])
@login_required
def wipe_location():
    """Wipe location for a given style"""
    try:
        data = request.get_json()
        style = data.get('style', '').strip()
        
        if not style:
            return jsonify({'success': False, 'message': 'Style is required'}), 400
        
        mysql_conn = get_db_connection()
        if mysql_conn is None:
            return jsonify({'success': False, 'message': 'Failed to connect to database'}), 500
        
        cursor = mysql_conn.cursor()
        
        # Update all products with this style to have empty location
        update_query = """
            UPDATE products 
            SET Location = '', Update_Date = NOW()
            WHERE Style = %s
        """
        cursor.execute(update_query, (style,))
        mysql_conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        mysql_conn.close()
        
        if rows_affected > 0:
            current_app.logger.info(f"***{session.get('username', 'Unknown')}***___@@@Wiped Location@@@__ Style: {style}, Rows: {rows_affected}")
            return jsonify({
                'success': True, 
                'message': f'Location wiped for style {style} ({rows_affected} records updated)'
            })
        else:
            return jsonify({
                'success': False, 
                'message': f'No records found for style {style}'
            }), 404
        
    except Exception as e:
        current_app.logger.error(f"Error wiping location: {str(e)}")
        return jsonify({'success': False, 'message': f'An error occurred: {str(e)}'}), 500


@outofstock_check_bp.route('/get_style_skus', methods=['GET'])
@login_required
def get_style_skus():
    """Return SKU and Location list for a given style (lazy-loaded)."""
    try:
        style = request.args.get('style', '').strip()
        if not style:
            return jsonify({'error': 'Style parameter is required'}), 400

        mysql_conn = get_db_connection()
        if mysql_conn is None:
            return jsonify({'error': 'Database connection failed'}), 500

        cursor = mysql_conn.cursor(dictionary=True)
        query = "SELECT SKU, Location FROM products WHERE Style = %s ORDER BY SKU"
        cursor.execute(query, (style,))
        rows = cursor.fetchall()

        # If no SKUs, return empty
        sku_list = [r['SKU'] for r in rows] if rows else []

        # Fetch stock info from FDC SQL Server for these SKUs
        sku_data_map = {}
        if sku_list:
            try:
                fdc_conn = get_fdc_connection()
                if fdc_conn:
                    fdc_cursor = fdc_conn.cursor()
                    # Query PROD_CD rows that begin with the style code (covers variations like spaces/dashes)
                    like_pattern = style + '%'
                    sku_stock_query = """
                        SELECT i.PROD_CD as PROD_CD,
                            SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) as Stock,
                            SUM(CAST(ISNULL(id.order_qty, 0) AS INT)) as Sold,
                            SUM(CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT)) as Available,
                            SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) as Ocean
                        FROM dbo.inv AS i
                        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                        WHERE i.PROD_CD LIKE ?
                        GROUP BY i.PROD_CD
                    """
                    fdc_cursor.execute(sku_stock_query, (like_pattern,))
                    stock_rows = fdc_cursor.fetchall()
                    # Build map keyed by normalized PROD_CD (upper, no spaces/dashes)
                    def norm(s):
                        return (s or '').upper().replace(' ', '').replace('-', '')

                    for r in stock_rows:
                        prod = r[0]
                        key = norm(prod)
                        sku_data_map[key] = {
                            'Stock': int(r[1]) if r[1] is not None else 0,
                            'Sold': int(r[2]) if r[2] is not None else 0,
                            'Available': int(r[3]) if r[3] is not None else 0,
                            'Ocean': int(r[4]) if r[4] is not None else 0,
                            'PROD_CD': prod
                        }
                    fdc_cursor.close()
                    fdc_conn.close()
            except Exception as e:
                current_app.logger.error(f"Error fetching SKU stock data from FDC: {e}")

        # Merge MySQL rows with stock map using normalization (remove spaces/dashes)
        merged = []
        def norm(s):
            return (s or '').upper().replace(' ', '').replace('-', '')

        for r in rows:
            sku = r.get('SKU')
            loc = r.get('Location')
            key = norm(sku)
            stock_info = sku_data_map.get(key)

            # fallback: if exact normalized SKU not found, try using first token (e.g., style only)
            if not stock_info and sku and ' ' in sku:
                first = norm(sku.split(' ')[0])
                stock_info = sku_data_map.get(first)

            if not stock_info:
                stock_info = {'Stock': 0, 'Sold': 0, 'Available': 0, 'Ocean': 0}

            merged.append({
                'SKU': sku,
                'Location': loc,
                'Stock': stock_info['Stock'],
                'Sold': stock_info['Sold'],
                'Available': stock_info['Available'],
                'Ocean': stock_info['Ocean']
            })

        cursor.close()
        mysql_conn.close()

        return jsonify({'skus': merged})
    except Exception as e:
        current_app.logger.error(f"Error getting SKUs for style {style}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
