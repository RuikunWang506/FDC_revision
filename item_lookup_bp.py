from flask import Blueprint, request, render_template, redirect, url_for, session, jsonify, send_from_directory
import os
import time
import pandas as pd
from io import StringIO
from config import Config
import SQL_connector
from util import resize_image

item_lookup_bp = Blueprint('item_lookup', __name__)

# Duplicate the necessary variables and functions
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'
}

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        import mysql.connector
        from mysql.connector import Error
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def fetch_locations(skus):
    """Helper function to fetch locations for SKUs"""
    from flask import current_app as app
    import mysql.connector
    from mysql.connector import Error
    
    location_dict = {}
    
    if not skus:
        app.logger.info("No SKUs provided to fetch_locations")
        return location_dict
    
    try:
        connection = get_db_connection()
        if connection is None:
            app.logger.error("Failed to connect to the database for fetching locations.")
            return location_dict
        
        cursor = connection.cursor()
        placeholders = ', '.join(['%s'] * len(skus))
        query = f"SELECT SKU, Location FROM products WHERE SKU IN ({placeholders})"
        
        cursor.execute(query, skus)
        results = cursor.fetchall()
        
        for sku, location in results:
            location_dict[sku.lower()] = location
        
        for sku in skus:
            if sku.lower() not in location_dict or not location_dict[sku.lower()]:
                stripped_sku = sku.split(' ')[0]
                if stripped_sku != sku:
                    cursor.execute(
                        "SELECT SKU, Location, Update_Date FROM products WHERE SKU = %s",
                        (stripped_sku,)
                    )
                    result = cursor.fetchone()
                    if result:
                        location_dict[sku.lower()] = result[1]
    
    except Error as e:
        app.logger.error(f"Error fetching locations: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
    
    return location_dict

def sort_filenames(filenames):
    base_files = {}
    version_files = []
    import re
    pattern = re.compile(r"^(.*?)(_v\d+)?\.jpg$")

    for filename in filenames:
        match = pattern.match(filename)
        if match:
            base = match.group(1)
            version = match.group(2) or ""
            if version:
                version_files.append((base, version, filename))
            else:
                base_files[base] = filename

    # Sort versions based on the number after '_v'
    version_files.sort(key=lambda x: (x[0], int(x[1][2:]) if x[1] else 0))

    sorted_filenames = []
    for base, base_file in base_files.items():
        sorted_filenames.append(base_file)
        sorted_filenames.extend([v[2] for v in version_files if v[0] == base])

    return sorted_filenames

@item_lookup_bp.route("/item_lookup", methods=["GET", "POST"])
@item_lookup_bp.route("/item_lookup/<string:user_input>", methods=["GET"])
def item_lookup(user_input=None):
    from flask import current_app as app
    
    if 'username' not in session or not session['username']:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']
    excel_html = None
    history_html = None

    if request.method == "POST":
        user_input = request.form.get("user_input", "").lower().strip()
    elif request.method == "GET" and user_input:
        user_input = user_input.lower().strip()
    else:
        user_input = ""

    if not user_input:
        return render_template("Item_lookup.html", excel_html=None, history_html=None, current_user=current_user, user_input=user_input)

    if user_input.endswith(","):
        user_input = user_input[:-1]

    excel_html, history_html, purchase_html, _, user_input = SQL_connector.generate_excel(user_input)

    if not excel_html and not history_html:
        return render_template("Item_lookup.html", excel_html=None, history_html=None, current_user=current_user, user_input=user_input)

    if excel_html:
        df = pd.read_html(StringIO(excel_html))[0]
        df.insert(0, '#', range(1, len(df) + 1))

        if 'SKU' in df.columns:
            skus = df['SKU'].astype(str).str.lower().tolist()
        else:
            skus = []

        location_dict = fetch_locations(skus)

        df['Location'] = df['SKU'].astype(str).str.lower().map(location_dict).fillna('')
        if 'DB' in df.columns:
            replacement_dict = {
                'oms_ef': 'E_FAS',
                'omsdata_fdc': 'FDC'
            }
            df['DB'] = df['DB'].str.lower().replace(replacement_dict)
        else:
            app.logger.warning("Database column not found in the DataFrame.")
            
        if 'Location' in df.columns:
            df['Location'] = df.apply(
                lambda row: (
                    f'<span class="editable">'
                    f'{row["Location"] if pd.notnull(row["Location"]) and row["Location"] != "" else "*"}</span>'
                ),
                axis=1
            )

        if 'Location' in df.columns:
            cols = list(df.columns)
            loc_index = 3
            cols.insert(loc_index, cols.pop(cols.index('Location')))
            df = df[cols]

        df.columns = df.columns.str.replace('<br>', ' ')
        df.columns = df.columns.str.replace('\n', '<br>')

        excel_html = df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="excel_table",
            escape=False
        )

    search_terms = [term.split()[0].strip() for term in user_input.split(",")]
    file_names = []
    for term in search_terms:
        try:
            for filename in os.listdir(Config.CATALOG_ORIGINAL):
                if filename.lower().startswith(term):
                    file_names.append(filename)
        except FileNotFoundError:
            app.logger.error("Directory not found: %s", Config.CATALOG_ORIGINAL)

    file_names = list(dict.fromkeys(file_names))

    file_info = []
    for filename in file_names:
        original_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
        file_path = original_path 

        if os.path.exists(file_path):
            size = os.path.getsize(file_path)
            modified_time = os.path.getmtime(file_path)
            modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
            file_info.append((filename, size, modified_date))
        else:
            app.logger.warning("File not found: %s", file_path)

    sorted_file_names = sort_filenames(file_names)
    sorted_image_urls = []
    for filename in sorted_file_names:
        original_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
        if os.path.exists(original_path):
            image_url = os.path.join('/image', filename)
        else:
            image_url = '/static/no_image_available.jpg'

        sorted_image_urls.append(image_url)

    # Inventory Manual Adjustment
    manual_adj_html = ""
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            patterns = ["prod_cd LIKE %s" for _ in search_terms]
            sql = (
                "SELECT CONCAT(%s, '.', 'wm_log') AS db_table, "
                "   prod_cd, log_num, log_dt, log_by, log_qty "
                "FROM wm_log "
                "WHERE type_cd = 'adj' AND (" + " OR ".join(patterns) + ")"
            )
            params = [db_config['database']] + [f"%{t}%" for t in search_terms]
            cur.execute(sql, params)
            rows = cur.fetchall()
            if rows:
                df_adj = pd.DataFrame(rows, columns=["DB.Table", "SKU", "Log#", "Date", "By", "Qty"])
                manual_adj_html = df_adj.to_html(
                    index=False,
                    classes="table table-bordered table-striped",
                    table_id="manual_adj_table",
                    escape=False
                )
    except Exception as e:
        app.logger.error("Error fetching manual adjustments: %s", e)
    finally:
        if 'cur' in locals():
            cur.close()
        if conn and conn.is_connected():
            conn.close()

    app.logger.info("***%s***__@@@search SKU@@@__ %s",current_user, user_input)
    return render_template(
        "Item_lookup.html",
        excel_html=excel_html,
        history_html=history_html,
        purchase_html=purchase_html,
        manual_adj_html=manual_adj_html,
        image_urls=sorted_image_urls,
        file_names=sorted_file_names,
        file_info=file_info,
        current_user=current_user,
        user_input=user_input.capitalize()
    )

@item_lookup_bp.route('/image/<path:filename>')
def serve_image(filename):
    from flask import current_app as app
    
    if 'username' not in session:
        return jsonify({"error": "Unauthorized"}), 401
            
    image_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
    
    if not os.path.exists(image_path):
        return send_from_directory('static', 'no_image_available.jpg')
    
    size = request.args.get('size', 'full')
    
    if size == 'full':
        return send_from_directory(Config.CATALOG_ORIGINAL, filename)
    
    try:
        buffer = resize_image(image_path, int(size) if size.isdigit() else 300)
        return send_from_directory(buffer, mimetype='image/jpeg')
    except Exception as e:
        app.logger.error(f"Error serving image {filename}: {e}")
        return send_from_directory('static', 'no_image_available.jpg')

@item_lookup_bp.route('/update_location', methods=['POST'])
def update_location():
    from flask import current_app as app
    import mysql.connector
    from mysql.connector import Error
    
    data = request.get_json()
    sku = data.get('sku')
    style = data.get('style')
    location = data.get('location')

    if not sku or not style:
        return jsonify({'success': False, 'message': 'SKU or Style is missing.'}), 400

    connection = get_db_connection()
    if connection is None:
        return jsonify({'success': False, 'message': 'Failed to connect to the database.'}), 500

    try:
        cursor = connection.cursor(dictionary=True)

        # Check if the product exists
        select_query = "SELECT * FROM products WHERE SKU = %s AND Style = %s"
        cursor.execute(select_query, (sku, style))
        product = cursor.fetchone()

        if product:
            # Update existing product
            update_query = "UPDATE products SET Location = %s, Update_Date = NOW() WHERE SKU = %s AND Style = %s"
            cursor.execute(update_query, (location, sku, style))
        else:
            # Insert new product
            insert_query = "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())"
            cursor.execute(insert_query, (sku, style, location))
            
        connection.commit()
        return jsonify({'success': True, 'message': 'Location updated successfully.'})

    except Error as e:
        app.logger.error(f"Database error during location update: {e}")
        return jsonify({'success': False, 'message': 'An error occurred while updating the location.'}), 500

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

@item_lookup_bp.route('/download_images', methods=['POST'])
def download_images():
    from flask import send_file
    import io
    import zipfile
    from PIL import Image
    
    user_input = request.form.get('user_input', '')
    image_type = request.form.get('image_type', 'resized')
    selected_images = request.form.get('selected_images', '')
    image_size = request.form.get('image_size')

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        if selected_images:
            selected_files = selected_images.split(',')
            for filename in selected_files:
                if filename:
                    file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                    if os.path.exists(file_path):
                        if image_size and image_size != 'full':
                            try:
                                with open(file_path, 'rb') as img_file:
                                    resized_img = resize_image(img_file, int(image_size))
                                    zf.writestr(filename, resized_img.getvalue())
                            except (ValueError, TypeError):
                                zf.write(file_path, arcname=filename)
                        else:
                            zf.write(file_path, arcname=filename)
        else:
            search_terms = [term.split()[0].strip() for term in user_input.split(",")]
            for term in search_terms:
                try:
                    for filename in os.listdir(Config.CATALOG_ORIGINAL):
                        if filename.lower().startswith(term.lower()):
                            file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                            if os.path.exists(file_path):
                                if image_type == 'resized':
                                    with Image.open(file_path) as img:
                                        buffer = io.BytesIO()
                                        if img.mode != 'RGB':
                                            img = img.convert('RGB')
                                        img.save(buffer, format='JPEG', quality=20)
                                        buffer.seek(0)
                                        zf.writestr(filename, buffer.getvalue())
                                else:
                                    zf.write(file_path, arcname=filename)
                except FileNotFoundError:
                    from flask import current_app as app
                    app.logger.error("Directory not found: %s", Config.CATALOG_ORIGINAL)

    memory_file.seek(0)
    zip_filename = "selected_images.zip" if selected_images else "images.zip"
    return send_file(
        memory_file,
        download_name=zip_filename,
        as_attachment=True
    )