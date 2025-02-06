from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file, jsonify
import mysql.connector
from mysql.connector import Error
import pandas as pd
import os
from datetime import datetime
from io import BytesIO
import json
import pyodbc
from flask import current_app

mobile_lite_bp = Blueprint('mobile_lite', __name__, template_folder='templates')

# SQL Server Configuration
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Fdc2022!!'
driver = 'ODBC Driver 17 for SQL Server'

# MySQL Configuration
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'  # Replace with your actual database name
}

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        current_app.logger.error(f"Error connecting to MySQL: {e}")
    return None

def fetch_locations(skus):
    """Fetches location data for a list of SKUs."""
    location_dict = {}
    
    if not skus:
        current_app.logger.info("No SKUs provided to fetch_locations")
        return location_dict
    
    # Convert to set to remove duplicates and back to list
    unique_skus = list(set(sku.strip().lower() for sku in skus))
    
    try:
        connection = get_db_connection()
        if connection is None:
            current_app.logger.error("Failed to connect to the database for fetching locations.")
            return location_dict
        
        cursor = connection.cursor()
        placeholders = ', '.join(['%s'] * len(unique_skus))
        query = f"SELECT SKU, Location, Update_Date FROM products WHERE SKU IN ({placeholders})"
        
        cursor.execute(query, unique_skus)
        results = cursor.fetchall()
        
        for sku, location, update_date in results:
            sku_clean = sku.strip().lower() if sku else ''
            location_dict[sku_clean] = {
                'Location': location,
                'Update_Date': update_date
            }
        
        # Handle SKUs not found by trying stripped SKU
        missing_skus = [sku for sku in unique_skus if sku not in location_dict or not location_dict[sku]['Location']]
        for sku in missing_skus:
            stripped_sku = sku.split(' ')[0].strip()
            if stripped_sku.lower() != sku.lower():
                cursor.execute(
                    "SELECT SKU, Location, Update_Date FROM products WHERE SKU = %s",
                    (stripped_sku,)
                )
                result = cursor.fetchone()
                if result:
                    location_dict[sku] = {
                        'Location': result[1],
                        'Update_Date': result[2]
                    }

    except Error as e:
        current_app.logger.error(f"Error fetching locations: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
    
    return location_dict

def get_color_from_abbr(abbr):
    with open('static/color_abbre.txt', 'r') as f:
        for line in f:
            parts = line.strip().split()
            if parts[0].lower() == abbr.lower():
                return parts[1]
    return '#CCCCCC'

def is_light_color(abbr):
    color = get_color_from_abbr(abbr)
    if color.startswith('#'):
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        brightness = ((r * 299) + (g * 587) + (b * 114)) / 1000
        return brightness > 155
    return True

def format_measurements(products):
    pc_weights = sorted(set(f"{p['package']['pc_weight']:.1f}" for p in products), reverse=True)
    pc_weights_str = ", ".join(f"{w} lbs" for w in pc_weights)
    
    unit_dimensions = []
    for p in products:
        dims = sorted([p['package']['length'], p['package']['width'], p['package']['height']], reverse=True)
        unit_dimensions.append(dims)
    
    unique_unit_dims = sorted(set(tuple(d) for d in unit_dimensions), reverse=True)
    dims_str = "<br>".join(f"{d[0]}\" × {d[1]}\" × {d[2]}\"" for d in unique_unit_dims)
    
    pkg_dimensions = []
    for p in products:
        pkg_dims = sorted([p['package']['box_length'], p['package']['box_width'], p['package']['box_height']], reverse=True)
        pkg_dimensions.append(pkg_dims)
    
    unique_pkg_dims = sorted(set(tuple(d) for d in pkg_dimensions), reverse=True)
    pkg_dims_str = "<br>".join(f"{d[0]}\" × {d[1]}\" × {d[2]}\"" for d in unique_pkg_dims)
    
    pkg_weights = sorted(set(f"{p['package']['box_weight']:.1f}" for p in products), reverse=True)
    pkg_weights_str = ", ".join(f"{w} lbs" for w in pkg_weights)
    
    return {
        'pc_weights': pc_weights_str,
        'dimensions': dims_str,
        'pkg_dimensions': pkg_dims_str,
        'pkg_weights': pkg_weights_str
    }

def get_product_data(user_input):
    try:
        # Normalize and split user input
        user_input = ' '.join(user_input.lower().strip().split())
        input_parts = [part.strip() for part in user_input.split(",")]

        # Build dynamic conditions
        prod_cd_conditions = []
        alt_cd_conditions = []
        descrip_conditions = []
        query_params = []

        for part in input_parts:
            if "%" in part:
                prod_cd_conditions.append("i.PROD_CD LIKE ?")
                alt_cd_conditions.append("i.ALT_CD LIKE ?")
                descrip_conditions.append("i.DESCRIP LIKE ?")
                query_params.extend([part, part, part])
            elif " " in part:
                prod_cd_conditions.append("i.PROD_CD = ?")
                query_params.append(part)
            else:
                prod_cd_conditions.append("i.PROD_CD LIKE ?")
                query_params.append(f"{part}%")

        combined_condition = " OR ".join(prod_cd_conditions + alt_cd_conditions + descrip_conditions)

        # Connect to both databases
        connections = [
            pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'),
            pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}')
        ]

        results = []
        all_skus = set()
        for conn, db_name in zip(connections, ['FDC', 'oms_EF']):
            cursor = conn.cursor()
            query = """
                SELECT 
                    i.PROD_CD AS SKU,
                    i.ALT_CD AS Style,
                    i.DESCRIP AS Container,
                    id.in_stock AS Stock,
                    id.ORDER_QTY AS Sold,
                    (id.in_stock - id.order_qty) AS Available,
                    id.on_order_qty AS Incoming,
                    i.SALES_COST AS Cost,
                    i.RETAIL_PRS AS Price,
                    i.PC_CASE AS CaseQty,
                    i.CASE_WT AS BoxWeight,
                    i.UT_WT AS PCWeight,
                    idt.UT_LEN AS Length,
                    idt.UT_WI AS Width,
                    idt.UT_HI AS Height,
                    idt.BOX_LEN AS BoxLength,
                    idt.BOX_WI AS BoxWidth,
                    idt.BOX_HI AS BoxHeight,
                    idt.BOX_WT AS BoxWeight,
                    CASE 
                        WHEN LT_SL_DT = 0 THEN 'NEVER'
                        ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
                    END AS UpdateDate
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
                WHERE """ + combined_condition
            cursor.execute(query, query_params)
            rows = cursor.fetchall()

            for row in rows:
                # Strip SKU and Style to remove leading/trailing spaces
                sku = row.SKU.strip().upper() if row.SKU else ''
                style = row.Style.strip().upper() if row.Style else ''

                # Collect SKUs for bulk location fetching
                all_skus.add(sku)

                product_data = {
                    'db': db_name,
                    'sku': sku.strip().upper(),  # Ensure consistent case and no whitespace
                    'style': style,
                    'info': {
                        'cost': float(row.Cost) if row.Cost else 0,
                        'price': float(row.Price) if row.Price else 0,
                        'msrp': float(row.Price * 2) if row.Price else 0,
                        'description': row.Container
                    },
                    'inventory': {
                        'stock': int(row.Stock) if row.Stock else 0,
                        'sold': int(row.Sold) if row.Sold else 0,
                        'available': int(row.Available) if row.Available else 0,
                        'incoming': int(row.Incoming) if row.Incoming else 0,
                        'case_qty': int(row.CaseQty) if row.CaseQty else 0,
                        'box_weight': float(row.BoxWeight) if row.BoxWeight else 0,
                        'update_date': row.UpdateDate
                    },
                    'package': {
                        'pc_weight': float(row.PCWeight) if row.PCWeight else 0,
                        'length': float(row.Length) if row.Length else 0,
                        'width': float(row.Width) if row.Width else 0,
                        'height': float(row.Height) if row.Height else 0,
                        'box_length': float(row.BoxLength) if row.BoxLength else 0,
                        'box_width': float(row.BoxWidth) if row.BoxWidth else 0,
                        'box_height': float(row.BoxHeight) if row.BoxHeight else 0,
                        'box_weight': float(row.BoxWeight) if row.BoxWeight else 0
                    }
                }

                # Add image URL
                style_number = style.split()[0].strip() if style else ''
                image_path = os.path.join(current_app.config['CATALOG_ORIGINAL'], f"{style_number}.jpg")
                product_data['image_url'] = f"/image/{style_number}.jpg" if os.path.exists(image_path) else "/static/no_image_available.jpg"

                results.append(product_data)
        
        # Fetch all locations in bulk
        location_dict = fetch_locations(list(all_skus))

        # Assign location data to each product
        for product in results:
            sku_lower = product['sku'].lower()
            location_info = location_dict.get(sku_lower)
            if location_info:
                product['location'] = {
                    'current_location': location_info['Location'] if location_info else '',
                    'update_date': location_info['Update_Date'].strftime('%Y-%m-%d') if location_info and location_info['Update_Date'] else ''
                }
            else:
                product['location'] = {
                    'current_location': '',
                    'update_date': ''
                }

        # Group results by style
        grouped_results = {}
        for product in results:
            style_base = product['style'].split()[0].strip() if product['style'] else ''
            if style_base not in grouped_results:
                grouped_results[style_base] = {
                    'style': style_base,
                    'image_url': product['image_url'],
                    'products': []
                }
            grouped_results[style_base]['products'].append(product)

        return grouped_results

    except Exception as e:
        current_app.logger.error(f"Error in get_product_data: {str(e)}")
        return {}
    finally:
        for conn in connections:
            if conn:
                conn.close()

@mobile_lite_bp.route('/mobile', methods=['GET', 'POST'])
def mobile_view():
    if 'username' not in session:
        return redirect(url_for("login"))
    
    current_user = session['username']
    data = None
    user_input = request.form.get('user_input', '').strip()
    
    if request.method == 'POST' and user_input:
        current_app.logger.info(f"***{current_user}***__@@@mobile search@@@__ {user_input}")
        data = get_product_data(user_input)
        for style_data in data.values():
            measurements = format_measurements(style_data['products'])
            style_data['measurements'] = measurements
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(data)
    
    return render_template('mobile_lite.html', 
                         data=data, 
                         user_input=user_input,
                         get_color_from_abbr=get_color_from_abbr,
                         is_light_color=is_light_color,
                         current_user=current_user)

@mobile_lite_bp.route('/update_dimensions', methods=['POST'])
def update_dimensions():
    # Strip leading/trailing spaces from input fields
    style = request.form.get('style', '').strip()
    length = request.form.get('length', '').strip()
    width = request.form.get('width', '').strip()
    height = request.form.get('height', '').strip()
    
    connections = [
        pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'),
        pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}')
    ]
    
    try:
        for conn in connections:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE inv_detl 
                SET ut_len = ?, ut_wi = ?, ut_hi = ?
                WHERE prod_cd IN (SELECT PROD_CD FROM inv WITH (NOLOCK) WHERE ALT_CD LIKE ?)
            """, (length, width, height, f"{style}%"))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error(f"Error updating dimensions: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        for conn in connections:
            conn.close()

@mobile_lite_bp.route('/update_package', methods=['POST'])
def update_package():
    # Strip leading/trailing spaces from input fields
    style = request.form.get('style', '').strip()
    box_len = request.form.get('box_len', '').strip()
    box_wi = request.form.get('box_wi', '').strip()
    box_hi = request.form.get('box_hi', '').strip()
    box_wt = request.form.get('box_wt', '').strip()
    
    connections = [
        pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'),
        pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}')
    ]
    
    try:
        for conn in connections:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE inv_detl 
                SET box_len = ?, box_wi = ?, box_hi = ?, box_wt = ?
                WHERE prod_cd IN (SELECT PROD_CD FROM inv WHERE ALT_CD LIKE ?)
            """, (box_len, box_wi, box_hi, box_wt, f"{style}%"))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        current_app.logger.error(f"Error updating package: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        for conn in connections:
            conn.close()

@mobile_lite_bp.route('/update_mobile_location', methods=['POST'])
def update_mobile_location():
    # Strip leading/trailing spaces from input fields
    sku = request.form.get('sku', '').strip()
    style = request.form.get('style', '').strip()
    new_location = request.form.get('location', '').strip()
    
    try:
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        
        # Check if record exists
        check_query = "SELECT * FROM products WHERE SKU = %s"
        cursor.execute(check_query, (sku,))
        exists = cursor.fetchone()
        
        if exists:
            update_query = """
                UPDATE products 
                SET Location = %s, Update_Date = NOW()
                WHERE SKU = %s
            """
            cursor.execute(update_query, (new_location, sku))
        else:
            insert_query = """
                INSERT INTO products (SKU, Style, Location, Update_Date)
                VALUES (%s, %s, %s, NOW())
            """
            cursor.execute(insert_query, (sku, style, new_location))
            
        connection.commit()
        return jsonify({'success': True})
    except Error as e:
        current_app.logger.error(f"Error updating location: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
