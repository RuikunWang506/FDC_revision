from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session
location_massive_audit_bp = Blueprint('location_massive_audit', __name__, template_folder='../../templates')
import pandas as pd
from datetime import datetime
from flask import send_file, jsonify
# --- Sync Over to OMS Route ---
@location_massive_audit_bp.route('/sync-to-oms', methods=['POST'])
def sync_to_oms():
    from py.utilities.database_utils import get_mysql_connection, get_sql_server_connection_fdc, get_sql_server_connection_oms_ef
    db_config = Config.DB_CONFIG
    sql_server_config = Config.SQL_SERVER_CONFIG
    try:
        mysql_connection = get_mysql_connection()
        mysql_cursor = mysql_connection.cursor(dictionary=True)
        mysql_cursor.execute("""
            SELECT sku, GROUP_CONCAT(location ORDER BY location SEPARATOR ', ') as location
            FROM products
            GROUP BY sku
        """)
        products = mysql_cursor.fetchall()
        if not products:
            return jsonify({'message': 'No products to sync.'})
        fdc_conn = get_sql_server_connection_fdc()
        fdc_cursor = fdc_conn.cursor()
        oms_ef_conn = get_sql_server_connection_oms_ef()
        oms_ef_cursor = oms_ef_conn.cursor()
        update_data = []
        MAX_LOCATION_LENGTH = 20
        for product in products:
            sku = (product['sku'] if product['sku'] else '').strip()
            location = (product['location'] if product['location'] else '').strip()
            location = location.replace(' MID', ' M').replace(' TOP', ' T').replace(' BOT', ' B')
            if location:
                location = location.replace('L', '').replace('-', '').replace(', ', ',')
            if len(location) > MAX_LOCATION_LENGTH:
                location = location[:MAX_LOCATION_LENGTH]
            update_data.append((location, sku))
        batch_size = 500
        # FDC update
        fdc_updated = 0
        for i in range(0, len(update_data), batch_size):
            batch = update_data[i:i+batch_size]
            try:
                fdc_cursor.executemany("UPDATE inv_data SET inv_loc = ? WHERE prod_cd = ?", batch)
                fdc_updated += len(batch)
            except Exception as e:
                current_app.logger.error(f"FDC batch {i//batch_size + 1} FAILED: {str(e)}")
                continue
        # OMS_EF update
        oms_updated = 0
        for i in range(0, len(update_data), batch_size):
            batch = update_data[i:i+batch_size]
            try:
                oms_ef_cursor.executemany("UPDATE inv_data SET inv_loc = ? WHERE prod_cd = ?", batch)
                oms_updated += len(batch)
            except Exception as e:
                current_app.logger.error(f"OMS_EF batch {i//batch_size + 1} FAILED: {str(e)}")
                continue
        fdc_conn.commit()
        oms_ef_conn.commit()
        mysql_cursor.close()
        mysql_connection.close()
        fdc_cursor.close()
        fdc_conn.close()
        oms_ef_cursor.close()
        oms_ef_conn.close()
        updated_count = fdc_updated + oms_updated
        return jsonify({'message': f'Sync completed. Updated {updated_count} records.'})
    except Exception as e:
        current_app.logger.error(f"An error occurred during sync: {str(e)}")
        return jsonify({'message': f'An error occurred: {str(e)}'}), 500
# --- Download Products as Excel Route ---
@location_massive_audit_bp.route('/download_products_excel', methods=['POST'])
def download_products_excel():
    try:
        connection = get_mysql_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM products")
        products = cursor.fetchall()
        cursor.close()
        connection.close()
        if not products:
            return "No products available to export.", 404
        df = pd.DataFrame(products)
        if 'Update_Date' in df.columns:
            df['Update_Date'] = df['Update_Date'].apply(lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if x else '')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'products_backup_{timestamp}.xlsx'
        from io import BytesIO
        bio = BytesIO()
        df.to_excel(bio, index=False)
        bio.seek(0)
        return send_file(
            bio,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        return f'An error occurred during export: {e}', 500
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session
from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config
import pyodbc
import json

location_massive_audit_bp = Blueprint('location_massive_audit', __name__, template_folder='../../templates')

# List of locations (from user)
LOCATIONS = [
    "823", "L1", "L1-A", "L1-B", "L1-C", "L1-D", "L1-E", "L1-F", "L1-G", "L1-H", "L1-I", "L1-J", "L1-K", "L1-L", "L1-M", "L1-Wall",  "L2", "L2-A", "L2-B", "L2-C", "L2-D", "L2-E", "L2-F", "L2-G", "L2-H", "L2-I", "L2-Wall", "L3", "L3-A", "L3-B", "L3-C", "L3-D", "L3-E", "L3-F", "L3-G", "L3-H", "L3-I", "L3-Wall", "L4", "L4-A", "L4-B", "L4-C", "L4-D", "L4-E", "L4-F", "L4-G", "L4-H", "L4-I", "L4-J", "L4-K", "L4-L", "L4-Wall","L5", "L5-A", "L5-B", "L5-C", "L5-D", "L5-E", "L5-F", "L5-G", "L5-H", "L5-I", "L5-J", "L5-K", "L5-L", "L5-Wall", "L6", "L6-A", "L6-B", "L6-C", "L6-D", "L6-E", "L6-F", "L6-G", "L6-H", "L6-I", "L6-J", "L6-K", "L6-L", "L6-Wall", "L20-D", "L20-C", "L20-B", "L20-A", "L20", "L7", "L7-A", "L7-B", "L7-C", "L7-D", "L7-E", "L7-F", "L7-G", "L7-H", "L7-I", "L7-J", "L7-K", "L7-L", "L7-Wall",  "L19-D", "L19-C", "L19-B", "L19-A", "L19", "L18-D", "L18-C", "L18-B", "L18-A", "L18", "L8", "L8-A", "L8-B", "L8-C", "L8-D", "L8-E", "L8-F", "L8-G", "L8-H", "L8-I", "L8-J", "L8-K", "L8-L", "L8-Wall", "L9", "L9-A", "L9-B", "L9-C", "L9-D", "L9-E", "L9-F", "L9-G", "L9-H", "L9-I", "L9-J", "L9-K", "L9-L", "L9-Wall", "L17-D", "L17-C", "L17-B", "L17-A", "L17", "L16", "L16-A","L16-B","L16-C","L16-D", "L10", "L10-A", "L10-B", "L10-C", "L10-D", "L10-E", "L10-F", "L10-G", "L10-H", "L10-I", "L10-J", "L10-K", "L10-L", "L11", "L11-A", "L11-B", "L11-C", "L11-D", "L11-E", "L11-F", "L11-G", "L11-H", "L11-I", "L11-J", "L11-K", "L11-L",  "L15", "L12", "L12-A", "L12-B", "L12-C", "L12-D", "L12-E", "L12-F", "L12-G", "L12-H", "L12-I", "L12-J", "L12-K", "L12-L", "L13", "L13-A", "L13-B", "L13-C", "L13-D", "L13-E", "L13-F", "L13-G", "L14", "L14-A", "L14-B", "L14-C", "L14-D", "L14-E", "L14-F", "L14-G", "L15-A", "L15-B", "L15-C", "L15-D","L22"]

SUFFIXES = ["BOT", "MID", "TOP"]


def get_available_styles():
    # Get styles from FDC_OMS SQL Server database
    sql_config = Config.SQL_SERVER_CONFIG
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_config["driver"]};'
            f'SERVER={sql_config["server"]};'
            f'DATABASE={sql_config["database_fdc"]};'
            f'UID={sql_config["username"]};'
            f'PWD={sql_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT ALT_CD FROM inv WHERE ALT_CD IS NOT NULL ORDER BY ALT_CD")
        styles = [row[0].strip() for row in cursor.fetchall() if row[0] and row[0].strip()]
        cursor.close()
        conn.close()
        return styles
    except Exception as e:
        current_app.logger.error(f"Error getting styles from FDC database: {e}")
        return []

def get_existing_styles_for_locations(locations_with_suffix):
    connection = get_mysql_connection()
    cursor = connection.cursor()
    location_styles = {}
    
    # Much simpler with one row per location!
    cursor.execute("SELECT DISTINCT Style, Location FROM products WHERE Location IS NOT NULL AND Location != '' AND Style IS NOT NULL")
    results = cursor.fetchall()
    
    for loc in locations_with_suffix:
        styles = []
        for style, location in results:
            if location == loc and style and style.strip():
                style_clean = style.strip()
                if style_clean not in styles:
                    styles.append(style_clean)
        location_styles[loc] = styles
    
    cursor.close()
    connection.close()
    return location_styles

@location_massive_audit_bp.route('/add_style_to_location', methods=['POST'])
def add_style_to_location():
    data = request.get_json()
    loc = data['location']
    style = data['style'].strip().upper()
    
    # Get all SKUs for this style from FDC database
    sql_config = Config.SQL_SERVER_CONFIG
    try:
        fdc_conn = pyodbc.connect(
            f'DRIVER={sql_config["driver"]};'
            f'SERVER={sql_config["server"]};'
            f'DATABASE={sql_config["database_fdc"]};'
            f'UID={sql_config["username"]};'
            f'PWD={sql_config["password"]}'
        )
        fdc_cursor = fdc_conn.cursor()
        fdc_cursor.execute("SELECT PROD_CD FROM inv WHERE ALT_CD = ?", (style,))
        skus = [row[0] for row in fdc_cursor.fetchall()]
        fdc_cursor.close()
        fdc_conn.close()
        
        if not skus:
            return {'success': False, 'message': 'Style not found in FDC database'}
        
        # Update MySQL for each SKU
        connection = get_mysql_connection()
        cursor = connection.cursor()
        updated_count = 0
        
        for sku in skus:
            # Trim SKU to remove trailing spaces from FDC database
            sku = sku.strip()
            
            # Check if this SKU-Location combination already exists
            cursor.execute("SELECT SKU FROM products WHERE SKU = %s AND Location = %s", (sku, loc))
            result = cursor.fetchone()
            cursor.fetchall()
            
            if not result:
                # Insert new record only if this SKU-Location combination doesn't exist
                cursor.execute("INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())", (sku, style, loc))
                updated_count += cursor.rowcount
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return {'success': updated_count > 0, 'message': f'Updated {updated_count} products' if updated_count > 0 else 'No products updated'}
    
    except Exception as e:
        current_app.logger.error(f"Error adding style to location: {e}")
        return {'success': False, 'message': f'Error: {str(e)}'}

@location_massive_audit_bp.route('/remove_style_from_location', methods=['POST'])
def remove_style_from_location():
    data = request.get_json()
    loc = data['location']
    style = data['style'].strip().upper()
    
    # Get all SKUs for this style from FDC database
    sql_config = Config.SQL_SERVER_CONFIG
    try:
        fdc_conn = pyodbc.connect(
            f'DRIVER={sql_config["driver"]};'
            f'SERVER={sql_config["server"]};'
            f'DATABASE={sql_config["database_fdc"]};'
            f'UID={sql_config["username"]};'
            f'PWD={sql_config["password"]}'
        )
        fdc_cursor = fdc_conn.cursor()
        fdc_cursor.execute("SELECT PROD_CD FROM inv WHERE ALT_CD = ?", (style,))
        skus = [row[0].strip() for row in fdc_cursor.fetchall()]
        fdc_cursor.close()
        fdc_conn.close()
        
        if not skus:
            return {'success': False, 'message': 'Style not found in FDC database'}
        
        # Remove location from each SKU
        connection = get_mysql_connection()
        cursor = connection.cursor()
        removed_count = 0
        
        for sku in skus:
            # Simply delete the SKU-Location combination
            cursor.execute("DELETE FROM products WHERE SKU = %s AND Location = %s", (sku, loc))
            removed_count += cursor.rowcount
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return {'success': removed_count > 0, 'message': f'Removed from {removed_count} SKUs' if removed_count > 0 else 'Location not found for any SKU'}
    
    except Exception as e:
        current_app.logger.error(f"Error removing style from location: {e}")
        return {'success': False, 'message': f'Error: {str(e)}'}

@location_massive_audit_bp.route('/get_occupied_locations', methods=['GET'])
def get_occupied_locations():
    """Get all occupied locations with their sublocation status (BOT/MID/TOP)"""
    try:
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        # Query to get all distinct locations that have products
        cursor.execute("""
            SELECT DISTINCT Location 
            FROM products 
            WHERE Location IS NOT NULL 
            AND Location != '' 
            AND Style IS NOT NULL
        """)
        results = cursor.fetchall()
        cursor.close()
        connection.close()
        
        # Parse locations to determine which have BOT/MID/TOP occupied
        occupied = {}
        for (location,) in results:
            if not location:
                continue
            
            location = location.strip()
            
            # Check if it's a sublocation (ends with BOT/MID/TOP)
            if location.endswith(' BOT') or location.endswith(' MID') or location.endswith(' TOP'):
                # Extract base location and sublocation
                parts = location.rsplit(' ', 1)
                base = parts[0]
                subloc = parts[1]  # BOT, MID, or TOP
                
                if base not in occupied:
                    occupied[base] = {'bot': False, 'mid': False, 'top': False}
                
                if subloc == 'BOT':
                    occupied[base]['bot'] = True
                elif subloc == 'MID':
                    occupied[base]['mid'] = True
                elif subloc == 'TOP':
                    occupied[base]['top'] = True
            else:
                # Regular location without sublocation
                if location not in occupied:
                    occupied[location] = {'occupied': True}
        
        return {'success': True, 'occupied': occupied}
    
    except Exception as e:
        current_app.logger.error(f"Error getting occupied locations: {e}")
        return {'success': False, 'message': f'Error: {str(e)}'}

@location_massive_audit_bp.route('/location_massive_update', methods=['GET', 'POST'])
def location_massive_update():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    styles = get_available_styles()
    # Remove duplicates from LOCATIONS
    unique_locations = list(dict.fromkeys(LOCATIONS))
    # Group locations by base
    groups = {}
    l_bases = set()
    for loc in unique_locations:
        if loc.startswith('L') and any(c.isdigit() for c in loc.split('-')[0]):
            base = loc.split('-')[0]
            l_bases.add(base)
        else:
            if 'Other' not in groups:
                groups['Other'] = []
            groups['Other'].append(loc)
    for base in sorted(l_bases, key=lambda x: int(x[1:])):
        groups[base] = [loc for loc in unique_locations if loc == base or loc.startswith(base + '-')]
        groups[base].sort(key=lambda x: (x != base, x))
    if 'Other' in groups:
        groups['Other'].sort()
    # Separate locations
    dashed_non_wall = [loc for loc in unique_locations if '-' in loc and 'wall' not in loc.lower()]
    nodash = [loc for loc in unique_locations if '-' not in loc]
    wall = [loc for loc in unique_locations if 'wall' in loc.lower()]
    locations_with_suffix = [f"{loc} {suffix}" for loc in dashed_non_wall for suffix in SUFFIXES] + nodash + wall
    location_styles = get_existing_styles_for_locations(locations_with_suffix)
    if request.method == 'POST':
        updates = []
        for loc in locations_with_suffix:
            styles_json = request.form.get(f'skus_{loc}', '[]')
            styles = json.loads(styles_json)
            for style in styles:
                style = style.strip()
                if style:
                    updates.append((loc, style))
        if not updates:
            flash('No updates submitted.', 'error')
            return render_template('location_massive_update.html', groups=groups, styles=styles, location_styles=location_styles,current_user=current_user)
        connection = get_mysql_connection()
        cursor = connection.cursor()
        updated_count = 0
        for loc, style in updates:
            update_query = """
            UPDATE products SET Location = %s, Update_Date = NOW()
            WHERE Style = %s
        """
            cursor.execute(update_query, (loc, style))
            updated_count += cursor.rowcount
        connection.commit()
        cursor.close()
        connection.close()
        flash(f'Updated {updated_count} products successfully.', 'message')
        return redirect(url_for('location_massive_audit.location_massive_update'))
    return render_template('location_massive_update.html', groups=groups, styles=styles, location_styles=location_styles,current_user=current_user)
