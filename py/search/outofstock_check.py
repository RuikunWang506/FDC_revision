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
