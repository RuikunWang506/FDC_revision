from flask import Blueprint, request, jsonify, redirect, session, render_template, url_for, current_app, send_file
import pyodbc
import pandas as pd
import os
import io
import csv

# Define the blueprint
order_processor_bp = Blueprint('order_processor', __name__)

# Database connection details
server = "192.168.254.204"
username = "tristen"
password = "Fdc2022!!"
database = "oms_EF"
driver = 'ODBC Driver 17 for SQL Server'
table_name = 'dbo.ord_log'

def get_db_connection():
    """Create a database connection"""
    return pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={database};UID={username};PWD={password}'
    )

@order_processor_bp.route('/order_processor', methods=['GET'])
def order_processor():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("login"))
    current_user = session['username']
    return render_template('order_processor.html', current_user=current_user)

@order_processor_bp.route('/download_sample', methods=['GET'])
def download_sample():
    """Generate and download a sample CSV file"""
    sample_data = [
        ['EN1980 BK', '3', '9.5'],
        ['EN1980 BR', '2', '9.5'],
        ['EN1980 NV', '1', '9.5'],
        ['EN1980 OL', '1', '9.5']
    ]
    
    # Create a CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Product Code', 'Quantity', 'Price'])
    writer.writerows(sample_data)
    
    # Create a sample data file
    mem = io.BytesIO()
    mem.write(output.getvalue().encode('utf-8'))
    mem.seek(0)
    output.close()
    
    return send_file(
        mem,
        as_attachment=True,
        download_name='sample_data.csv',
        mimetype='text/csv'
    )

@order_processor_bp.route('/verify_order', methods=['POST'])
def verify_order():
    """Verify order and get current details for items"""
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    
    data = request.get_json()
    order_number = data.get('order_number')
    items = data.get('items', [])
    
    if not order_number:
        return jsonify({"success": False, "message": "Order number is required"}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if order exists
        cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE ord_num = ?", order_number)
        count = cursor.fetchone()[0]
        
        if count == 0:
            return jsonify({"success": False, "message": f"Order {order_number} not found"}), 404
        
        # Get current details for all items in the order
        cursor.execute(f"""
        SELECT PROD_CD, ORDER_QTY, UNIT_PRS
        FROM {table_name}
        WHERE ord_num = ?
        """, order_number)
        
        all_order_items = []
        for row in cursor.fetchall():
            all_order_items.append({
                'product_code': row[0].strip(),
                'quantity': row[1],
                'price': float(row[2])
            })
        
        conn.close()
        
        return jsonify({
            "success": True,
            "data": {
                "order_number": order_number,
                "items": all_order_items
            }
        })
        
    except Exception as e:
        current_app.logger.error(f"Error verifying order: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

@order_processor_bp.route('/update_order', methods=['POST'])
def update_order():
    """Update order with new quantities and prices"""
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    
    data = request.get_json()
    order_number = data.get('order_number')
    items = data.get('items', [])
    current_user = session['username']
    
    if not order_number or not items:
        return jsonify({"success": False, "message": "Order number and items are required"}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Update each item
        updated_items = []
        not_found_items = []
        
        for item in items:
            product_code = item['product_code']
            quantity = item['quantity']
            price = item['price']
            
            # Check if item exists in order
            cursor.execute(f"""
            SELECT COUNT(*) FROM {table_name} 
            WHERE ord_num = ? AND PROD_CD = ?
            """, order_number, product_code)
            
            count = cursor.fetchone()[0]
            
            if count > 0:
                # Update existing item
                query = f"""
                UPDATE {table_name}
                SET ORDER_QTY = ?, UNIT_PRS = ?
                WHERE ord_num = ? AND PROD_CD = ?
                """
                cursor.execute(query, quantity, price, order_number, product_code)
                current_app.logger.info(f"User {current_user} updated item {product_code} in order {order_number}: qty={quantity}, price={price}")
                updated_items.append(product_code)
            else:
                # Log warning for items not in order
                current_app.logger.warning(f"Item {product_code} not found in order {order_number}")
                not_found_items.append(product_code)
        
        conn.commit()
        
        # Fetch updated data for verification
        cursor.execute(f"""
        SELECT PROD_CD, ORDER_QTY, UNIT_PRS 
        FROM {table_name} 
        WHERE ord_num = ? AND PROD_CD IN ({','.join(['?'] * len(updated_items))})
        """, [order_number] + updated_items)
        
        updated_data = []
        for row in cursor.fetchall():
            updated_data.append({
                'product_code': row[0].strip(),
                'quantity': row[1],
                'price': float(row[2])
            })
        
        conn.close()
        
        result_message = f"Successfully updated {len(updated_items)} items in order {order_number}"
        if not_found_items:
            result_message += f". {len(not_found_items)} items were not found in the order: {', '.join(not_found_items)}"
        
        current_app.logger.info(f"User {current_user} {result_message}")
        
        return jsonify({
            "success": True, 
            "message": result_message,
            "updated_items": updated_items,
            "not_found_items": not_found_items,
            "updated_data": updated_data
        })
        
    except Exception as e:
        current_app.logger.error(f"Error updating order: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

@order_processor_bp.route('/generate_excel', methods=['POST'])
def generate_excel():
    """Generate an Excel file from the formatted data"""
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    
    data = request.get_json()
    items = data.get('items', [])
    
    if not items:
        return jsonify({"success": False, "message": "No items provided"}), 400
    
    try:
        # Convert to DataFrame
        df = pd.DataFrame(items)
        df = df.rename(columns={
            'product_code': 'PROD_CD',
            'quantity': 'ORDER_QTY',
            'price': 'UNIT_PRS'
        })
        
        # Create Excel in memory
        output = io.BytesIO()
        writer = pd.ExcelWriter(output, engine='xlsxwriter')
        df.to_excel(writer, sheet_name='Sheet1', index=False)
        writer.close()
        output.seek(0)
        
        # Return as downloadable file
        return send_file(
            output,
            as_attachment=True,
            download_name='order_data.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error generating Excel: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500
