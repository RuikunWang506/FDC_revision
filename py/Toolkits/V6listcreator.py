from flask import Blueprint, render_template, request, jsonify, current_app, send_file,send_from_directory,redirect, url_for, flash, session
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, PatternFill, Border, Side
from openpyxl.drawing.image import Image as OpenPyXLImage
from openpyxl.utils import get_column_letter

from io import BytesIO
import os
import re
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from py.utilities.setting import resize_image
from py.utilities.dam_utils import get_canonical_assets_for_skus, get_asset_url
import py.utilities.models
import datetime
import mysql.connector
from mysql.connector import Error
import zipfile
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

v6_listcreator_bp = Blueprint('v6_listcreator', __name__, template_folder='templates')

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG
db_config = Config.DB_CONFIG

 

selected_images = []  # This should be populated based on your application logic
formatted_date = datetime.datetime.now().strftime("%Y%m%d")  # Example date formatting


def list_from_dropbox(list_input, current_user):
    """Processes a dropbox path and returns list of styles and image mappings."""
    match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        current_app.logger.info(f"***{current_user}*** processing regular input: @@@__{relative_path}")
        
        if not os.path.exists(dropbox_path):
            return {"error": "Directory not found. Please check your URL."}
        
        file_list = set()
        sku_to_image = {}
        
        for filename in os.listdir(dropbox_path):
            if filename.endswith('.jpg'):
                # Extract the base SKU first (1-2 letters followed by 4 digits)
                base_match = re.match(r'([A-Za-z]{1,2}\d{4})', filename)
                if base_match:
                    base_sku = base_match.group(1).upper()
                    # Check if it's a size suffix pattern
                    size_suffix = re.search(r'[-]?[smlSML](?:\.|$)', filename[len(base_sku):])
                    if size_suffix:
                        # Handle size suffix cases (both with and without dash)
                        dash_style = f"{base_sku}-{size_suffix.group().strip('.-')}"
                        no_dash_style = f"{base_sku}{size_suffix.group().strip('.-')}"
                        file_list.add(dash_style)
                        file_list.add(no_dash_style)
                        sku_to_image[dash_style] = os.path.join(dropbox_path, filename)
                        sku_to_image[no_dash_style] = os.path.join(dropbox_path, filename)
                    else:
                        # For any other variations, just use the base SKU
                        file_list.add(base_sku)
                        sku_to_image[base_sku] = os.path.join(dropbox_path, filename)
        
        if not file_list:
            return {"error": "No images found in the specified directory. Please check your URL."}
        
        file_list = sorted(list(file_list))
        clean_list_for_memo = str(file_list).replace("'","")
        current_app.logger.info(f"list_from_dropbox called with input: {clean_list_for_memo}")
        
        return file_list, sku_to_image

    return list_input, {}


def list_from_origin(list_input, current_user):
    # Convert input string to list of SKUs
    current_app.logger.info(f"***{current_user}*** processing regular input:@@@__ {list_input}")

    # Split by commas and clean up each SKU
    raw_styles = [sku.strip().upper() for sku in list_input.split(',') if sku.strip()]
    style_list = []
    
    for style in raw_styles:
        # Check if the style has a suffix (like M, L, etc.)
        base_match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
        
        if base_match and base_match.group(3):  # If it has a suffix
            base_style = base_match.group(1)    # The base part (e.g., "L1343")
            has_dash = base_match.group(2) == '-'  # Whether it has a dash
            suffix = base_match.group(3)        # The suffix part (e.g., "M")
            
            # For styles with suffix (L1343M or L1343-M), add both formats
            if has_dash:
                # If input had dash (L1343-M), use this as canonical form
                style_list.append(style)
                # Also add no-dash version for search
                no_dash_style = base_style + suffix
                style_list.append(no_dash_style)
            else:
                # If input had no dash (L1343M), use this as canonical form
                style_list.append(style)
                # Also add dash version for search
                dash_style = base_style + '-' + suffix
                style_list.append(dash_style)
        else:
            # For base styles without suffix (L1343), only add the exact style
            style_list.append(style)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_style_list = [x for x in style_list if not (x in seen or seen.add(x))]
    return unique_style_list

def normalize_style(style):
    """Remove dash and convert to uppercase for comparison"""
    return re.sub(r'-', '', style).upper()

@v6_listcreator_bp.route('/v6_listcreator', methods=['GET', 'POST'])
@v6_listcreator_bp.route("/v6_listcreator/<string:user_input>", methods=["GET"])
def index(user_input=None):
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']
    error_message = None
    dropbox_images = {}
    missing_descriptions = []
    image_urls = {}
    missing_styles = []
    duplicate_styles = []
    duplicate_items = []
    clean_list_for_memo = None
    
    # Handle GET request with query parameter
    if request.method == 'GET' and request.args.get('list_input'):
        list_input = request.args.get('list_input')
        session['original_input'] = list_input
        # Process the input as if it was a POST request
        return process_list_input(list_input, current_user)
    
    # Handle POST request
    if request.method == 'POST':
        list_input = request.form.get('list_input')
        session['original_input'] = list_input
        return process_list_input(list_input, current_user)
        
    return render_template('V6_Listcreator.html', current_user=current_user, error_message=error_message)

def process_list_input(list_input, current_user):
    """Process the list input and return the appropriate response."""
    error_message = None
    dropbox_images = {}
    missing_descriptions = []
    image_urls = {}
    missing_styles = []
    duplicate_styles = []
    duplicate_items = []
    clean_list_for_memo = None
    
    original_input_styles = [s.strip().upper() for s in list_input.split(',') if s.strip()]

    if 'dropbox' in list_input.lower():
        match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
        if match:
            relative_path = match.group(1)
            full_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
            if os.path.exists(full_path):
                base_sku_to_files = {}
                for filename in os.listdir(full_path):
                    if filename.endswith('.jpg'):
                        base_match = re.match(r'([A-Za-z]{1,2}\d{4})', filename)
                        if base_match:
                            base_sku = base_match.group(1).upper()
                            base_sku_to_files.setdefault(base_sku, []).append(filename)
                for base_sku, files in base_sku_to_files.items():
                    if len(files) > 1:
                        duplicate_items.append({'base_sku': base_sku, 'files': files})

        result = list_from_dropbox(list_input, current_user)
        if isinstance(result, dict) and 'error' in result:
            error_message = result['error']
            return render_template('V6_Listcreator.html', current_user=current_user, error_message=error_message)
        style_list, dropbox_images = result
        current_app.logger.info(f"***{current_user}*** processing dropbox input: {list_input}")
        clean_list_for_memo = ','.join(style_list).replace("'", "")
    else:
        style_list = list_from_origin(list_input, current_user)
        normalized_style_list = [re.sub(r'-', '', s).upper() for s in style_list]
        for orig_style in original_input_styles:
            normalized_orig = re.sub(r'-', '', orig_style).upper()
            if normalized_orig not in normalized_style_list:
                missing_styles.append(orig_style)

    if style_list:
        image_urls = get_images_for_selection(style_list)
        descriptions = {}
        data = get_sql_data(style_list)
        db_styles = set()
        
        if data:
            for row in data:
                exact_style = row[2].upper() if row[2] else ''
                if exact_style:
                    db_styles.add(exact_style)
                    session[f'verified_{exact_style}'] = True
                    sku_match = re.match(r'([A-Za-z]{1,2}\d{4})', row[2])
                    if sku_match:
                        base_sku = sku_match.group(1).upper()
                        db_styles.add(base_sku)
                        session[f'verified_{base_sku}'] = True
        normalized_db_styles = {normalize_style(s) for s in db_styles}
        for style in style_list:
            if normalize_style(style) not in normalized_db_styles:
                if style not in missing_styles:
                    missing_styles.append(style)
                placeholder_data = [('N/A', f'{style}BK', style, 0, 0, 0, 0, 999.0, 0, 0, 'BK', 0, 0, 0, 'N/A', 'N/A', 'Not in OMS')]
                if data:
                    data.extend(placeholder_data)
                else:
                    data = placeholder_data
        for style in style_list:
            description = get_description_from_mysql(style)
            descriptions[style] = description
            if description == "No description available":
                missing_descriptions.append(style)
                current_app.logger.info(f"Added {style} to missing_descriptions list")
        
        style_count = {}
        for style in style_list:
            style_upper = style.upper()
            style_count[style_upper] = style_count.get(style_upper, 0) + 1
        duplicate_styles = [style for style, count in style_count.items() if count > 1]
        
        total_duplicates = sum(len(item['files']) - 1 for item in duplicate_items)
        final_count = (list_input.count_dropbox_files() if hasattr(list_input, 'count_dropbox_files') else 0) - total_duplicates


        if data:
            pivot_data = create_pivot_table(data)
            context = {
                'current_user': current_user,
                'data': data,
                'pivot_data': pivot_data,
                'image_urls': image_urls,
                'dropbox_images': dropbox_images,
                'descriptions': descriptions,
                'error_message': error_message,
                'missing_styles': missing_styles,
                'duplicate_styles': duplicate_styles,
                'your_input_was': list_input,
                'clean_list_for_memo': clean_list_for_memo,
                'duplicate_items': duplicate_items,
                'missing_descriptions': missing_descriptions,
                'total_duplicates': total_duplicates,  # Add this line
                'final_count': final_count  # Add this line
            }
            

            return render_template('V6_Listcreator.html', **context, list_input=list_input)
    
    return render_template('V6_Listcreator.html', current_user=current_user, error_message=error_message)

@v6_listcreator_bp.route('/update_descriptions', methods=['POST'])
def update_descriptions():
    descriptions = {}
    original_input = session.get('original_input', '')

    # Collect all style descriptions from form
    for key, value in request.form.items():
        if key.startswith('description_'):
            style_index = key.split('_')[1]
            style = request.form.get(f'style_{style_index}', '')
            if style:
                descriptions[style] = value.upper()
                update_description_in_db(style, value.upper())

    # Redirect with the original input as a query parameter
    return redirect(url_for('v6_listcreator.index') + f'?list_input={original_input}')



def get_mysql_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def get_description_from_mysql(style):
    """Fetches the description for a given style from the MySQL database."""
    connection = get_mysql_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT description FROM onlinedb.description WHERE style = %s", (style,))
            description_row = cursor.fetchone()
            return description_row[0].upper() if description_row else "No description available"
        except Error as e:
            print(f"Error fetching description: {e}")
        finally:
            cursor.close()
            connection.close()
    return "No description available"
    
def update_description_in_db(style_index, description):
    """Updates the description for a given style in the MySQL database.
    If the style does not exist, a new row is created."""
    connection = get_mysql_connection()  # Assuming this function is defined elsewhere to get the DB connection

    if connection:
        try:
            cursor = connection.cursor()
            description = description.upper()

            # Prepare the SQL update query
            update_query = "UPDATE onlinedb.description SET description = %s WHERE style = %s"
            params = (description, style_index)

            # Log the actual query and parameters

            # Update the description for the specified style
            cursor.execute(update_query, params)

            # Check the number of affected rows
            if cursor.rowcount == 0:
                
                # Prepare the SQL insert query
                insert_query = "INSERT INTO onlinedb.description (style, description) VALUES (%s, %s)"
                cursor.execute(insert_query, (style_index, description))
                connection.commit()  # Commit the changes to the database
            else:
                connection.commit()  # Commit the changes to the database

        except Error as e:
            current_app.logger.error(f"Error updating description: {e}")

        finally:
            cursor.close()
            connection.close()
def get_sql_data(style_list):
    # Create two separate lists: one for exact base styles, one for styles with suffix
    base_styles = []
    suffix_styles = []
    
    for style in style_list:
        match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
        if match and match.group(3):  # Has suffix
            suffix_styles.append(style)
        else:  # Base style only
            base_styles.append(style)
    
    # Debug logging
    
    # Modify the query to handle exact matching for base styles
    query = """
    DECLARE @AltCDList NVARCHAR(MAX) = ?;
    DECLARE @ExactStyles NVARCHAR(MAX) = ?;
    DECLARE @ProductList NVARCHAR(MAX) = '';

    -- For styles with suffix, use LIKE with wildcards
    IF LEN(@AltCDList) > 0
    BEGIN
        SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';
        WITH SplitAltCD AS (
            SELECT value
            FROM dbo.SplitString(@AltCDList, ',')
        )
        SELECT @ProductList = COALESCE(@ProductList + ',', '') + PROD_CD
        FROM dbo.inv
        WHERE EXISTS (SELECT 1 FROM SplitAltCD WHERE alt_CD LIKE value);
    END

    -- For exact base styles, use exact matching
    IF LEN(@ExactStyles) > 0
    BEGIN
        WITH SplitExact AS (
            SELECT value
            FROM dbo.SplitString(@ExactStyles, ',')
        )
        SELECT @ProductList = COALESCE(@ProductList + ',', '') + PROD_CD
        FROM dbo.inv
        WHERE EXISTS (SELECT 1 FROM SplitExact WHERE alt_CD = value);
    END

    -- If we have products to query
    IF LEN(@ProductList) > 0 OR LEN(@ExactStyles) > 0
    BEGIN
        WITH SplitValues AS (
            SELECT value
            FROM dbo.SplitString(COALESCE(@ProductList, '') + CASE WHEN LEN(@ProductList) > 0 AND LEN(@ExactStyles) > 0 THEN ',' ELSE '' END + COALESCE(@ExactStyles, ''), ',')
            WHERE value <> ''
        )
        SELECT 
            i.PROD_CD AS SKU,
            i.ALT_CD AS SKU_no_color,
            id.in_stock AS in_stock,
            id.ORDER_QTY AS some_one_ordered,
            (id.in_stock - id.order_qty) AS quantity,
            id.on_order_qty AS ocean,
            i.RETAIL_PRS AS selling_price,
            i.PC_CASE AS QTY_in_box,
            i.UT_WT AS unit_weight,
            i.UNIT_COLOR AS color,
            idt.ut_len AS item_length,
            idt.ut_wi AS item_width,
            idt.UT_hi AS item_height,
            CASE 
                WHEN LT_SL_DT = 0 THEN 'NEVER'
                ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
            END AS last_activity,
            fcd.create_dt AS created_dt
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
        LEFT JOIN (
            SELECT 
                prod_cd,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
            FROM wm_skufile
        ) AS fcd ON i.PROD_CD = fcd.prod_cd
        WHERE i.PROD_CD IN (SELECT value FROM SplitValues)
        OR i.ALT_CD IN (SELECT value FROM dbo.SplitString(@ExactStyles, ','));
    END
    """
    
    queries = []
    try:
        conn_fdc = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        conn_oms_ef = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        
        for conn, database_name in [(conn_fdc, 'omsdata_FDC'), (conn_oms_ef, 'oms_EF')]:
            cursor = conn.cursor()
            suffix_list = ",".join(suffix_styles)
            base_list = ",".join(base_styles)
          
            cursor.execute(query, suffix_list, base_list)
            rows = cursor.fetchall()
            
            # Strip trailing spaces from each row's data
            rows_with_database_name = [
                (database_name,) + tuple(str(value).strip() if isinstance(value, str) else value for value in row)
                for row in rows
            ]
            queries.append(rows_with_database_name)
            cursor.close()
            
        combined_results = queries[0] + queries[1] if len(queries) > 1 else queries[0]
        return combined_results
    except Exception as e:
        current_app.logger.error(f"Error in get_sql_data: {str(e)}")
        
        # Add fallback logic for when the SQL query fails
        # Create placeholder data for each style in the list
        placeholder_data = []
        for style in style_list:
            placeholder_row = ('N/A', f'{style}BK', style, 0, 0, 0, 0, 999.0, 0, 0, 'BK', 0, 0, 0, 'N/A', 'N/A', 'Not in OMS')
            placeholder_data.append(placeholder_row)
        
        return placeholder_data
    finally:
        conn_fdc.close()
        conn_oms_ef.close()





def create_pivot_table(data):
    if not data:
        return []
    
    # Create DataFrame without specifying columns - let pandas infer them
    df = pd.DataFrame(data)
    
    # Rename columns to match expected structure
    if len(df.columns) == 16:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt']
    elif len(df.columns) == 17:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt', 'in_oms_status']
    
    # Create a normalized style column that will be used for grouping
    # This will remove dashes from styles with suffixes for grouping purposes
    df['normalized_style'] = df['SKU_no_color'].apply(lambda x: normalize_style_for_grouping(x))
    
    # Filter rows where normalized_style is not null
    df = df[df['normalized_style'].notna()]
    
    # Sort by normalized_style
    df = df.sort_values('normalized_style')
    
    # Convert numeric columns to integers
    integer_columns = ['quantity', 'ocean', 'pcsBox',
                     'item_length', 'item_width', 'item_height']
    df[integer_columns] = df[integer_columns].fillna(0).astype(int)
    
    # Create dimensions column
    df['dimensions'] = df['item_length'].astype(str) + ' x ' + df['item_width'].astype(str) + ' x ' + df['item_height'].astype(str)
    
    # Group by normalized_style
    style_groups = df.groupby('normalized_style')
    
    style_cards = []
    for style, group in style_groups:
        most_common_price = round(group['selling_price'].mode().iloc[0], 2)
        
        # Use the first SKU_no_color as the display style
        display_style = group['SKU_no_color'].iloc[0]
        
        style_data = group.pivot_table(
            values=['quantity', 'ocean', 'pcsBox', 'dimensions'],
            index=['color'],
            aggfunc={
                'quantity': 'sum',
                'ocean': 'sum',
                'pcsBox': 'first',
                'dimensions': 'first'
            }
        ).reset_index()
        
        style_data['style'] = display_style
        style_data['selling_price'] = most_common_price
        
        bk_rows = style_data[style_data['color'].str.startswith('BK')]
        other_rows = style_data[~style_data['color'].str.startswith('BK')]
        style_data = pd.concat([bk_rows, other_rows]).reset_index(drop=True)
        
        style_cards.append(style_data.to_dict('records'))
    
    return style_cards

def normalize_style_for_grouping(style):
    """
    Normalizes style codes for grouping purposes:
    - For base styles (L1343), returns the exact style
    - For styles with suffix (L1343M or L1343-M), returns the no-dash version (L1343M)
    """
    if not style:
        return None
        
    # Check if the style has a suffix - Fixed the unbalanced parenthesis
    match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
    if not match:
        return style
        
    base = match.group(1)
    suffix = match.group(3)
    
    if suffix:  # If it has a suffix
        return base + suffix  # Return without dash
    else:
        return base  # Return base style as is


def get_images_from_dropbox(dropbox_path):
    image_paths = []
    try:
        for filename in os.listdir(dropbox_path):
            if filename.endswith('.jpg'):
                image_paths.append(os.path.join(dropbox_path, filename))
    except Exception as e:
        current_app.logger.error(f"Error fetching images from Dropbox: {e}")
    
    return image_paths

def get_images_for_selection(sku_list):
    image_urls = {}

    for sku in sku_list:
        try:
            # Extract pattern: 1-2 letters followed by exactly 4 numbers, optionally followed by dash and suffix
            base_match = re.match(r'([A-Za-z]{1,2}\d{4})(?:-[A-Za-z0-9]+)?', sku)
            if base_match:
                base_sku = base_match.group(1).upper()  # Base SKU without suffix
                full_sku = sku.upper()  # Full SKU including any suffix
                
                image_set = []
                
                # Use DAM system to find assets for this SKU
                from py.utilities.dam_utils import search_assets_by_sku_pattern, get_asset_url
                import py.utilities.models
                
                # Search for assets matching the full SKU - only catalog_original source for V6 list creator
                matching_assets = search_assets_by_sku_pattern(full_sku, source_filter='catalog_original')
                
                # Also search for no-dash version if SKU contains dash
                if '-' in full_sku:
                    no_dash_sku = full_sku.replace('-', '')
                    matching_assets.extend(search_assets_by_sku_pattern(no_dash_sku, source_filter='catalog_original'))
                
                # Also search for dash version if SKU doesn't contain dash
                if '-' not in full_sku and len(full_sku) > len(base_sku):
                    suffix = full_sku[len(base_sku):]
                    dash_sku = f"{base_sku}-{suffix}"
                    matching_assets.extend(search_assets_by_sku_pattern(dash_sku, source_filter='catalog_original'))
                
                # Remove duplicates by asset ID
                unique_assets = {}
                for asset in matching_assets:
                    unique_assets[asset.id] = asset
                matching_assets = list(unique_assets.values())
                
                # Sort by canonical first, then by version
                matching_assets.sort(key=lambda x: (not x.is_canonical, x.version))
                
                # Convert assets to image_set format
                for i, asset in enumerate(matching_assets):
                    image_set.append({
                        'filename': asset.filename,
                        'path': get_asset_url(asset.id, width=150),  # Default thumbnail size
                        'fullsize_path': get_asset_url(asset.id),  # Full size for enlargement
                        'asset_id': asset.id,
                        'default': asset.is_canonical or (i == 0 and not any(a.is_canonical for a in matching_assets))
                    })

                # Add SKU and its images to the dictionary
                if image_set:  # Only include SKUs that have images
                    image_urls[full_sku] = sorted(image_set, key=lambda x: x['filename'])  # Sort by filename for consistency

        except UnicodeEncodeError:
            continue

    return image_urls

@v6_listcreator_bp.route('/list_image/dropbox/<path:filename>')
def serve_dropbox_image(filename):
    dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, filename)
    
    # Check if the image exists
    if not os.path.exists(dropbox_path):
        return send_from_directory('static', 'no_image_available.jpg')  # Fallback to placeholder if not found
    return send_file(dropbox_path)

@v6_listcreator_bp.route('/list_image/<path:filename>')
def serve_list_image(filename):
    # Use DAM system to find asset by filename
    import py.utilities.models
    
    db = py.utilities.models.SessionLocal()
    try:
        asset = db.query(py.utilities.models.ImageAsset).filter_by(
            filename=filename,
            is_active=True
        ).first()
        
        if not asset or not os.path.exists(asset.path):
            return send_from_directory('static', 'no_image_available.jpg')
        
        # Get size parameter for optional resizing
        size = request.args.get('size', 'full')
        
        if size == 'full':
            return send_file(asset.path)
        else:
            try:
                # Resize image on demand
                buffer = resize_image(asset.path, int(size) if size.isdigit() else 300)
                return send_file(buffer, mimetype='image/jpeg')
            except Exception as e:
                current_app.logger.error(f"Error serving image {filename}: {e}")
                return send_from_directory('static', 'no_image_available.jpg')
    finally:
        db.close()
    
def set_border(ws, cell_range):
    thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                  top=Side(style='thin'), bottom=Side(style='thin'))
    for row in ws[cell_range]:
        for cell in row:
            cell.border = thin
def set_border(ws, cell_range):
    thin = Side(border_style="thin", color="000000")
    for row in ws[cell_range]:
        for cell in row:
            cell.border = Border(top=thin, left=thin, right=thin, bottom=thin)

@v6_listcreator_bp.app_template_filter('count_dropbox_files')
def count_dropbox_files(dropbox_path):
    match = re.search(r'dropbox\\(.+)', dropbox_path, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        full_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        if os.path.exists(full_path):
            return len([f for f in os.listdir(full_path) if f.endswith('.jpg')])
    return 0


@v6_listcreator_bp.route('/update_descriptions_bulk', methods=['POST'])
def update_descriptions_bulk():
    updates = request.json.get('updates', [])
    connection = get_mysql_connection()
    
    if connection:
        try:
            cursor = connection.cursor()
            
            for update in updates:
                style = update['style']
                description = update['description'].upper()
                
                # Try update first
                update_query = "UPDATE onlinedb.description SET description = %s WHERE style = %s"
                cursor.execute(update_query, (description, style))
                
                # If no rows were updated, insert new record
                if cursor.rowcount == 0:
                    insert_query = "INSERT INTO onlinedb.description (style, description) VALUES (%s, %s)"
                    cursor.execute(insert_query, (style, description))
            
            connection.commit()
            return jsonify({"success": True, "message": "Descriptions updated successfully"})
            
        except Error as e:
            current_app.logger.error(f"Error updating descriptions: {e}")
            return jsonify({"success": False, "message": str(e)})
            
        finally:
            cursor.close()
            connection.close()
            
    return jsonify({"success": False, "message": "Could not connect to database"})

@v6_listcreator_bp.route('/export_images', methods=['POST'])
def export_images():
    request_data = request.get_json()
    image_paths = request_data.get('imagePaths', [])
    image_size = request_data.get('imageSize')
    
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for item in image_paths:
            style = item['style']
            image_path = item['path']
            
            if '/image/' in image_path:
                # DAM system URL - extract asset ID
                asset_id_match = re.search(r'/image/(\d+)/', image_path)
                if asset_id_match:
                    asset_id = int(asset_id_match.group(1))
                    db = py.utilities.models.SessionLocal()
                    try:
                        asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
                        full_path = asset.path if asset else None
                    finally:
                        db.close()
                else:
                    full_path = None
            elif '/list_image/' in image_path:
                # Legacy path format - get filename and find in DAM
                filename = re.sub(r'/list_image/', '', image_path)
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(
                        filename=filename, is_active=True
                    ).first()
                    full_path = asset.path if asset else None
                finally:
                    db.close()
            else:
                # Handle dropbox path
                full_path = image_path
                
            if os.path.exists(full_path):
                if image_size != 'full':
                    img_buffer = resize_image(full_path, int(image_size))
                    img_buffer.seek(0)  # Important: Reset buffer position
                    zf.writestr(f"{style}.jpg", img_buffer.getvalue())
                else:
                    with open(full_path, 'rb') as img_file:
                        zf.writestr(f"{style}.jpg", img_file.read())
    
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'exported_images_{datetime.datetime.now().strftime("%Y%m%d")}.zip'
    )

@v6_listcreator_bp.route('/export_list', methods=['POST'])
def export_list():
    try:
        request_data = request.get_json()
        data = request_data.get('exportData', [])
        image_size_input = request_data.get('image_size', "800")
        display_image_size = image_size_input
        processing_image_size = None if image_size_input == "full" else int(image_size_input)
        removed_columns = request_data.get('removedColumns', {})

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Exported Data"
        sheet.freeze_panes = 'A2'  # Freezes row 1
        highlight_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
        sheet.column_dimensions[get_column_letter(1)].width = 30
        sheet.column_dimensions[get_column_letter(2)].width = 12
        sheet.column_dimensions['B'].width = 15
        for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row, min_col=2, max_col=2):
            cell = row[0]
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        formatted_date = datetime.datetime.now().strftime("%Y-%m-%d")
        filename = f"exported_data_{formatted_date}.xlsx"
        
        headers = ["Image", "Description", "Price", "Style", "Color", "Quantity"]
        if not removed_columns.get('ocean'):
            headers.append("Ocean")
        headers.append("Total")  # Always include Total column
        if not removed_columns.get('pcsBox'):
            headers.append("PCS/BOX")
        if not removed_columns.get('dimensions'):
            headers.append("Dimensions")
            
        sheet.append(headers)
      

        for item in data:
            style = item['style']
            description = item['description']
            image_filename = item['image']
            price = item['price']

            # Handle DAM system URLs and legacy paths
            image_path = None
            
            # Check if it's a local dropbox file path (absolute path)
            if os.path.isabs(image_filename) and os.path.exists(image_filename):
                # It's a local dropbox file path
                image_path = image_filename
            elif '/image/' in image_filename:
                # DAM system URL - extract asset ID
                asset_id_match = re.search(r'/image/(\d+)/', image_filename)
                if asset_id_match:
                    asset_id = int(asset_id_match.group(1))
                    db = py.utilities.models.SessionLocal()
                    try:
                        asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
                        image_path = asset.path if asset else None
                    finally:
                        db.close()
            elif '/list_image/' in image_filename:
                # Legacy path format
                filename = re.sub(r'/list_image/', '', image_filename)
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(
                        filename=filename, is_active=True
                    ).first()
                    image_path = asset.path if asset else None
                finally:
                    db.close()
            else:
                # Direct filename - search in DAM
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(
                        filename=image_filename, is_active=True
                    ).first()
                    image_path = asset.path if asset else None
                finally:
                    db.close()

            if not image_path or not os.path.exists(image_path):
                current_app.logger.error(f"Image not found: {image_filename}")
                image_path = os.path.join('static', 'no_image_available.jpg')

            if processing_image_size is not None:
                image_path = resize_image(image_path, processing_image_size)

            rows = item['rows']
            if not rows:
                # Create a single row with header info when no color/quantity rows exist
                row_data = [
                    "",  # Image placeholder
                    description,
                    price,
                    style,
                    "",  # Empty color
                    "",   # Zero quantity
                ]
                
                # Add ocean if not removed
                if not removed_columns.get('ocean'):
                    row_data.append("")  # Zero ocean
                
                # Add total (always included)
                row_data.append("")  # Zero total
                
                # Add optional columns
                if not removed_columns.get('pcsBox'):
                    row_data.append("")
                if not removed_columns.get('dimensions'):
                    row_data.append("")
                    
                sheet.append(row_data)
                
                # Add image for the empty row case
                img = OpenPyXLImage(image_path)
                img.width = 200
                img.height = 150
                sheet.add_image(img, f"A{sheet.max_row}")

                # Add 7 empty rows to make it 8 rows total
                for _ in range(7):
                    empty_row_data = [""] * len(headers)
                    sheet.append(empty_row_data)

                # Merge cells for the 8 rows
                sheet.merge_cells(start_row=sheet.max_row - 7, start_column=1, end_row=sheet.max_row, end_column=1)
                sheet.merge_cells(start_row=sheet.max_row - 7, start_column=2, end_row=sheet.max_row, end_column=2)
                sheet.merge_cells(start_row=sheet.max_row - 7, start_column=3, end_row=sheet.max_row, end_column=3)

                # Center align merged cells
                for col in [2, 3]:
                    for row in range(sheet.max_row - 7, sheet.max_row + 1):
                        sheet.cell(row=row, column=col).alignment = Alignment(horizontal='center', vertical='center')
            if rows:    
                for index, row in enumerate(rows):
                    quantity = int(float(row['quantity'])) if row['quantity'] else 0
                    ocean = int(float(row['ocean'])) if row.get('ocean') else 0
                    
                    row_data = [
                        "",  # Image placeholder
                        description if index == 0 else "",
                        price if index == 0 else "",
                        style if index == 0 else "",
                        row['color'],
                        quantity
                    ]
                    # Only add ocean if it's not removed
                    if not removed_columns.get('ocean'):
                        row_data.append(ocean)
                    
                    # Always add total (quantity + ocean)
                    row_data.append(quantity + ocean)
                    
                    # Only add PCS/BOX if not removed
                    if not removed_columns.get('pcsBox'):
                        row_data.append(int(float(row['pcsBox'])) if row.get('pcsBox') else 0)
                    
                    # Only add dimensions if not removed
                    if not removed_columns.get('dimensions'):
                        row_data.append(row.get('dimensions', ''))
                        sheet.column_dimensions[get_column_letter(9)].width = 11

                    sheet.append(row_data)

                    if index == 0:
                        img = OpenPyXLImage(image_path)
                        img.width = 200
                        img.height = 150
                        sheet.add_image(img, f"A{sheet.max_row}")

                while len(rows) < 8:
                    empty_row_data = [""] * len(headers)
                    sheet.append(empty_row_data)
                    rows.append(empty_row_data)

                if len(rows) > 8:
                    sheet.merge_cells(start_row=sheet.max_row - len(rows) + 1, start_column=1, end_row=sheet.max_row, end_column=1)
                    sheet.merge_cells(start_row=sheet.max_row - len(rows) + 1, start_column=2, end_row=sheet.max_row, end_column=2)
                    sheet.merge_cells(start_row=sheet.max_row - len(rows) + 1, start_column=3, end_row=sheet.max_row, end_column=3)
                else:
                    sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=1, end_row=sheet.max_row, end_column=1)
                    sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=2, end_row=sheet.max_row, end_column=2)
                    sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=3, end_row=sheet.max_row, end_column=3)

                for col in [2, 3]:
                        for row in range(sheet.max_row - len(rows) + 1, sheet.max_row + 1):
                            sheet.cell(row=row, column=col).alignment = Alignment(horizontal='center', vertical='center')
                
        # Adjust the column range based on removed columns
        last_col = 'G'  # Base columns
        if not removed_columns.get('pcsBox'):
            last_col = 'H'
        if not removed_columns.get('dimensions'):
            last_col = 'J'
        
            
        set_border(sheet, f'A2:{last_col}{sheet.max_row}')

        output = BytesIO()
        workbook.save(output)
        output.seek(0)

        return send_file(output, download_name=filename, as_attachment=True)

    except Exception as e:
        current_app.logger.error(f"Error in export_list: {e}")
        return jsonify({"error": "Internal Server Error"}), 500
    
@v6_listcreator_bp.route('/export_pdf', methods=['POST'])
def export_pdf():
    request_data = request.get_json()
    image_paths = request_data.get('imagePaths', [])
    image_size = request_data.get('imageSize')
    
    output_buffer = BytesIO()
    image_data_list = []
    selected_images = []
    
    for item in image_paths:
        style = item['style']
        image_path = item['path']
        
        if '/image/' in image_path:
            # DAM system URL - extract asset ID
            asset_id_match = re.search(r'/image/(\d+)/', image_path)
            if asset_id_match:
                asset_id = int(asset_id_match.group(1))
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
                    full_path = asset.path if asset else None
                finally:
                    db.close()
            else:
                full_path = None
        elif '/list_image/' in image_path:
            # Legacy path format
            filename = re.sub(r'/list_image/', '', image_path)
            db = py.utilities.models.SessionLocal()
            try:
                asset = db.query(py.utilities.models.ImageAsset).filter_by(
                    filename=filename, is_active=True
                ).first()
                full_path = asset.path if asset else None
            finally:
                db.close()
        else:
            # Handle dropbox path
            full_path = image_path
            
        if os.path.exists(full_path):
            if image_size != 'full':
                img_buffer = resize_image(full_path, int(image_size))
                image_data_list.append(img_buffer)
            else:
                with open(full_path, 'rb') as img_file:
                    image_data_list.append(BytesIO(img_file.read()))
            selected_images.append(f"{style}.jpg")
    
    generate_catalog_pdf(image_data_list, output_buffer, image_size, selected_images)
    
    output_buffer.seek(0)
    return send_file(
        output_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'catalog_{datetime.datetime.now().strftime("%Y%m%d")}.pdf'
    )

def generate_catalog_pdf(image_data_list, output_buffer, image_size='full', selected_images=[]):
        c = canvas.Canvas(output_buffer, pagesize=landscape(letter))
        c.setTitle("Catalog Pictures")
        
        # Cover page
        c.setFont("Helvetica-Bold", 36)
        c.drawString(100, 550, "Catalog Pictures")
        c.setFont("Helvetica-Bold", 24)
        c.drawString(100, 500, "Product Catalog")
        c.setFont("Helvetica", 16)
        c.drawString(100, 450, f"Created Date: {datetime.datetime.now().strftime('%Y-%m-%d')}")
        c.drawString(100, 400, "Exclusive Access – Please Refrain from Sharing outside of your company")
        resolution_text = f"Image Resolution: {image_size}px" if image_size != 'full' else "Image Resolution: Original Size"
        c.drawString(100, 350, resolution_text)
        c.showPage()
        
        # Process each image and add style text
        for img_data, image_name in zip(image_data_list, selected_images):
            img = ImageReader(BytesIO(img_data.getvalue() if hasattr(img_data, 'getvalue') else img_data))
            img_width, img_height = img.getSize()
            aspect = img_height / float(img_width)
            
            max_width = 700
            width = max_width
            height = width * aspect
            
            x = (landscape(letter)[0] - width) / 2
            y = (landscape(letter)[1] - height) / 2 + 50
            
            c.drawImage(img, x, y, width=width, height=height)
            
            style_name = os.path.splitext(image_name)[0]
            c.setFont("Helvetica-Bold", 20)
            text_width = c.stringWidth(style_name, "Helvetica-Bold", 20)
            text_x = (landscape(letter)[0] - text_width) / 2
            text_y = y - 30
            c.drawString(text_x, text_y, style_name)
            
            c.showPage()
        
        c.save()


def get_sku_price(sku):
    """Query the MySQL database for the list price of a specific SKU."""
    connection = get_mysql_connection()
    if connection is None:
        return None
    
    try:
        cursor = connection.cursor(dictionary=True)
        query = "SELECT SKU, listprice FROM onlinedb.loris WHERE SKU = %s"
        cursor.execute(query, (sku,))
        result = cursor.fetchone()
        return result
    except Error as e:
        print(f"Error querying MySQL database: {e}")
        return None
    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()

def get_sku_sold_price(sku):
    """Query the SQL Server database for the sold price of a specific SKU using fuzzy matching."""
    try:
        connection = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        cursor = connection.cursor()
        # Use LIKE operator for fuzzy matching
        query = """
            SELECT TOP 1 ol.prod_cd, ol.unit_prs
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE ol.prod_cd LIKE ? AND ol.cus_id = '00868'
            ORDER BY ol.olg_dt DESC
        """
        # Add wildcard for fuzzy matching
        search_pattern = f"%{sku}%"
        cursor.execute(query, (search_pattern,))
        row = cursor.fetchone()
        if row:
            return {"prod_cd": row[0], "unit_prs": row[1]}
        return None
    except pyodbc.Error as e:
        print(f"Error querying SQL Server database: {e}")
        return None
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()
                    
@v6_listcreator_bp.route('/export_lori_format', methods=['POST'])
def export_lori_format():
    try:
        request_data = request.get_json()
        data = request_data.get('exportData', [])
        image_size_input = request_data.get('image_size', "800")
        processing_image_size = None if image_size_input == "full" else int(image_size_input)
        
        workbook = Workbook()
        # Create OTB sheet
        sheet = workbook.create_sheet("OTB", 0)
        sheet.append([])
        sheet.append([])
        # Add SUM formulas to cells N2, O2, and P2
        sheet['N2'] = "=SUM(N3:N523)"  # Sum of Available Units
        sheet['O2'] = "=SUM(O3:O525)"  # Sum of Ext Cost
        # Leave first two rows empty
        sheet['O2'].number_format = r'$#,##0.0_);[Red]($#,##0)'


        
        # Add headers on row 3 - Added new columns X, Y, Z for SKU, List Price, Sold Price
        headers = [
            "UPC or Item Code", "Image", "Vendor", "Description", "Cost", "Retail", "GM$", "", 
            "Sale Retail", "Event GM$", "Event GP%", "Proj Store Count", "Color", "Available Units",
            "Ext Retail", "Earliest Ship Date", "Terms", "Notes", "Case Pack", "Case Cost",
            "", "", "", "SKU", "List Price", "Sold Price"  # Added new columns
        ]
        sheet.append(headers)
        
        blue_fill = PatternFill(start_color="B8CCE4", end_color="B8CCE4", fill_type="solid")
        green_fill = PatternFill(start_color="C4D79B", end_color="C4D79B", fill_type="solid")
        tan_fill = PatternFill(start_color="F0DEAA", end_color="F0DEAA", fill_type="solid")
        
        # Apply colors to A3:D3 (blue)
        for col in range(1, 5):  # A to D
            sheet.cell(row=3, column=col).fill = blue_fill
            
        # Apply colors to E3:K3 (green)
        for col in range(5, 12):  # E to K
            sheet.cell(row=3, column=col).fill = green_fill
            
        # Apply colors to L3:O3 (tan)
        for col in range(12, 16):  # L to O
            sheet.cell(row=3, column=col).fill = tan_fill
            
        # Apply colors to P3:T3 (blue)
        for col in range(16, 21):  # P to T
            sheet.cell(row=3, column=col).fill = blue_fill

        # Set column widths
        sheet.column_dimensions['A'].width = 15  # UPC/Item Code
        sheet.column_dimensions['B'].width = 25  # Image
        sheet.column_dimensions['C'].width = 10  # Vendor
        sheet.column_dimensions['D'].width = 25  # Description
        sheet.column_dimensions['E'].width = 10  # Cost
        sheet.column_dimensions['F'].width = 10  # Retail
        sheet.column_dimensions['M'].width = 10  # Color
        sheet.column_dimensions['N'].width = 15  # Available Units
        sheet.column_dimensions['X'].width = 15  # SKU
        sheet.column_dimensions['Y'].width = 15  # List Price
        sheet.column_dimensions['Z'].width = 15  # Sold Price
        sheet.column_dimensions['R'].width = 30  # notes
        sheet.column_dimensions['o'].width = 15  # Case Pack
        
        # Set row height and apply text wrapping to header row
        sheet.row_dimensions[3].height = 39
        for col in range(1, len(headers) + 1):
            cell = sheet.cell(row=3, column=col)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        
        # Current row counter (starting after headers)
        current_row = 4
        
        # Process each item
        for item in data:
            style = item['style']
            description = item['description']
            price_str = item['price'].replace('$', '').strip()
            price = float(price_str) if price_str and price_str != 'N/A' else 0
            image_filename = item['image']
            
            # Process image path using DAM system
            image_path = None
            if '/image/' in image_filename:
                # DAM system URL - extract asset ID
                asset_id_match = re.search(r'/image/(\d+)/', image_filename)
                if asset_id_match:
                    asset_id = int(asset_id_match.group(1))
                    db = py.utilities.models.SessionLocal()
                    try:
                        asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
                        image_path = asset.path if asset else None
                    finally:
                        db.close()
            elif '/list_image/' in image_filename:
                # Legacy path format
                filename = re.sub(r'/list_image/', '', image_filename)
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(
                        filename=filename, is_active=True
                    ).first()
                    image_path = asset.path if asset else None
                finally:
                    db.close()
            else:
                # Direct filename - search in DAM
                db = py.utilities.models.SessionLocal()
                try:
                    asset = db.query(py.utilities.models.ImageAsset).filter_by(
                        filename=image_filename, is_active=True
                    ).first()
                    image_path = asset.path if asset else None
                finally:
                    db.close()
                
            if not image_path or not os.path.exists(image_path):
                current_app.logger.error(f"Image not found: {image_filename}")
                image_path = os.path.join('static', 'no_image_available.jpg')
                
            # Resize image if needed
            if processing_image_size is not None:
                image_buffer = resize_image(image_path, processing_image_size)
                img = OpenPyXLImage(image_buffer)
            else:
                img = OpenPyXLImage(image_path)
                
            # Set image dimensions
            img.width = 180
            img.height = 135
            
            # Get rows data or create empty if none
            rows = item['rows']
            if not rows:
                rows = [{"color": "N/A", "quantity": "0"}]
                
            # Ensure we have exactly 8 rows per style (pad with empty rows if needed)
            while len(rows) < 8:
                rows.append({"color": "", "quantity": "0"})
                
            # Process each row (up to 8)
            # Process each row (up to 8)
            for i, row in enumerate(rows[:8]):
                color = row.get('color', '')
                quantity = int(float(row.get('quantity', 0)))
                
                # Calculate extended retail
                ext_retail = quantity * price
                
                # Get SKU with color
                full_sku = f"{style}" if color else style
                
                # Get list price and sold price from MySQL and SQL Server
                list_price_info = get_sku_price(style)
                list_price = f"${list_price_info['listprice']}" if list_price_info and 'listprice' in list_price_info else "Not found"
                
                sold_price_info = get_sku_sold_price(style)
                sold_price = f"${sold_price_info['unit_prs']:.2f}" if sold_price_info and 'unit_prs' in sold_price_info else "No recent sales"
                
                # Determine if we should add "ordered before" to notes
                notes = ""
                if i == 0 and sold_price_info and 'unit_prs' in sold_price_info:
                    notes = "ordered before"
                
                # Create row data with raw values for numeric fields
                row_data = [
                    style if i == 0 else "",  # UPC/Item Code
                    "",  # Image placeholder
                    "FDC",  # Vendor
                    description if i == 0 else "",  # Description - fill if color is not empty
                    price if color else "",  # Cost - fill if color is not empty
                    "",  # Retail - leave empty as requested
                    f"=F{current_row}-E{current_row}" if color else "",  # GM$ formula: F(row#)-E(row#)
                    f"=G{current_row}/F{current_row}",  # Empty column
                    "",  # Sale Retail
                    "",  # Event GM$
                    f"=J{current_row}/I{current_row}",  # Event GP%
                    "",  # Proj Store Count
                    color,  # Color
                    quantity if color else "",  # Available Units
                    f"=N{current_row}*G{current_row}",  # Ext Cost - fill if color is not empty
                    "Fill it out",  # Earliest Ship Date
                    "Net 90",  # Terms
                    notes,  # Notes - "ordered before" if first row and has sold price
                    "",  # Case Pack
                    0,  # Case Cost
                    "",  # Empty column
                    "",  # Empty column
                    "",  # Empty column
                    full_sku,  # SKU with color
                    list_price,  # List Price
                    sold_price  # Sold Price
                ]
                
                # Add row to sheet
                sheet.append(row_data)
                
                # Add image to first row of each style
                if i == 0:
                    sheet.add_image(img, f"B{current_row}")
                
                # Apply cell formatting
                for col_idx, cell_value in enumerate(row_data, 1):
                    cell = sheet.cell(row=current_row, column=col_idx)
                    
                    # Center align all cells
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    
                    # Apply specific formatting based on column
                    if col_idx == 5 or col_idx == 6:  # Cost and Retail (E and F)
                        if isinstance(cell_value, (int, float)) and cell_value > 0:
                            cell.number_format = '"$ "#,##0.00'
                    
                    if col_idx == 7:  # GM$ (G)
                        cell.number_format = r'$#,##0.00_);[Red]($#,##0.00)'
                    elif col_idx == 15:  # Ext Retail (O)
                        cell.number_format = r'$#,##0.0_);[Red]($#,##0)'
                    
                    elif col_idx == 20:  # Case Cost (T)
                        if isinstance(cell_value, (int, float)):
                            cell.number_format = '"$"#,##0.00'
                
                current_row += 1
                
        # Apply borders to all cells
        thin_border = Border(
            left=Side(style='thin'), 
            right=Side(style='thin'), 
            top=Side(style='thin'), 
            bottom=Side(style='thin')
        )
        
        for row in sheet.iter_rows(min_row=3, max_row=sheet.max_row):
            for cell in row:
                cell.border = thin_border
                
        # Create output file
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        
        return send_file(
            output, 
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"Lori_OTB_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
        )
        
    except Exception as e:
        current_app.logger.error(f"Error in export_lori_format: {e}")
        return jsonify({"error": str(e)}), 500

