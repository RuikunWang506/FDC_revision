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
from config import Config
from util import resize_image
import datetime
import mysql.connector
from mysql.connector import Error
import zipfile
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

v6_listcreator_bp = Blueprint('v6_listcreator', __name__, template_folder='templates')
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Fdc2022!!'
driver = 'ODBC Driver 17 for SQL Server'

 

selected_images = []  # This should be populated based on your application logic
formatted_date = datetime.datetime.now().strftime("%Y%m%d")  # Example date formatting


def list_from_dropbox(list_input, current_user):
    match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        
        
        if not os.path.exists(dropbox_path):
            return {"error": "Directory not found. Please check your URL."}
        
        file_list = set()  # Changed to set to avoid duplicates
        sku_to_image = {}
        
        for filename in os.listdir(dropbox_path):
            if filename.endswith('.jpg'):
                sku_match = re.match(r'([A-Za-z]{1,2}\d{4})', filename)
                if sku_match:
                    sku = sku_match.group(1).upper()  # Just use the base SKU pattern
                    file_list.add(sku)  # Add to set to ensure uniqueness
                    sku_to_image[sku] = os.path.join(dropbox_path, filename)
        
        if not file_list:
            return {"error": "No images found in the specified directory. Please check your URL."}
        
        file_list = sorted(list(file_list))  # Convert set back to sorted list
        
        return file_list, sku_to_image

    return list_input, {}


def list_from_origin(list_input, current_user):
    # Convert input string to list of SKUs
    style_list = [sku.strip().upper() for sku in list_input.split(',') if sku.strip()]
    current_app.logger.info(f"***{current_user}*** processing regular input: {style_list}")
    return style_list




@v6_listcreator_bp.route('/v6_listcreator', methods=['GET', 'POST'])
def index():
    if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
    current_user = session['username']
    error_message = None  # Initialize error message
    dropbox_images = {}  # Initialize dropbox_images as a dictionary
    missing_descriptions = []  # Initialize missing descriptions list
    image_urls = {}  # Add this line to initialize image_urls

    # Handle GET request with list_input parameter
    if request.method == 'GET' and request.args.get('list_input'):
        list_input = request.args.get('list_input')
        
        if 'dropbox' in list_input.lower():
            result = list_from_dropbox(list_input, current_user)
            if isinstance(result, dict) and 'error' in result:
                error_message = result['error']
                return render_template('v6_listcreator.html', current_user=current_user, error_message=error_message)
            style_list, dropbox_images = result
        else:
            style_list = list_from_origin(list_input, current_user)

        if style_list:
            image_urls = get_images_for_selection(style_list)
            descriptions = {}
            for style in style_list:
                description = get_description_from_mysql(style)
                descriptions[style] = description
                if description == "No description available":
                    missing_descriptions.append(style)

            data = get_sql_data(style_list)
            if data:
                pivot_data = create_pivot_table(data)
                context = {
                    'current_user': current_user,
                    'data': data,
                    'pivot_data': pivot_data,
                    'image_urls': image_urls,
                    'dropbox_images': dropbox_images,
                    'descriptions': descriptions,
                    'error_message': error_message
                }
                return render_template('v6_listcreator.html', **context,list_input=list_input)

    if request.method == 'POST':
        list_input = request.form.get('list_input')
        session['original_input'] = list_input  # Store the original input



        if 'dropbox' in list_input.lower():
            result = list_from_dropbox(list_input, current_user)
            if isinstance(result, dict) and 'error' in result:
                error_message = result['error']  # Capture the error message
                return render_template('v6_listcreator.html', current_user=current_user, error_message=error_message)

            style_list, dropbox_images = result  # Unpack the result correctly
        else:
            style_list = list_from_origin(list_input, current_user)

        if style_list:
            image_urls = get_images_for_selection(style_list)  # Get images for all styles
            descriptions = {}
             # Verify styles in database
            conn_str = f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
            conn = pyodbc.connect(conn_str)
            cursor = conn.cursor()

            style_list_str = ",".join([f"'{style}'" for style in style_list])
            verify_query = f"""
            SELECT DISTINCT ALT_CD 
            FROM dbo.inv 
            WHERE ALT_CD IN ({style_list_str})
            """
            
            cursor.execute(verify_query)
            verified_styles = [row[0].strip() for row in cursor.fetchall()]

            cursor.close()
            conn.close()
            for style in verified_styles:
                session[f'verified_{style.strip()}'] = True

                
            for style in style_list:
                
                description = get_description_from_mysql(style)  # Fetch description for each style
                descriptions[style] = description
                if description == "No description available":
                    missing_descriptions.append(style)  # Collect styles with missing descriptions

            # If there are missing descriptions, store them in the session
            if missing_descriptions:
                session['missing_descriptions'] = missing_descriptions
                return render_template('v6_listcreator.html', 
                    current_user=current_user, 
                    missing_descriptions=missing_descriptions,
                    image_urls=image_urls,
                    dropbox_images=dropbox_images,
                    list_input=list_input
)
            # Continue with your existing logic...
            data = get_sql_data(style_list)  # Fetch style data

            if data:
                pivot_data = create_pivot_table(data)  # Create pivot table

                # Prepare the context for rendering
                context = {
                    'current_user': current_user,
                    'data': data,
                    'pivot_data': pivot_data,
                    'image_urls': image_urls,
                    'dropbox_images': dropbox_images,  # Pass the SKU-to-image mapping
                    'descriptions': descriptions,  # Pass the descriptions to the template
                    'error_message': error_message  # Pass error message if any
                }

                return render_template('v6_listcreator.html', **context,list_input=list_input)

    # Render default view if no POST request
    return render_template('v6_listcreator.html', current_user=current_user, error_message=error_message)


@v6_listcreator_bp.route('/update_descriptions', methods=['POST'])
def update_descriptions():
    descriptions = {}
    missing_descriptions = session.get('missing_descriptions', [])
    original_input = session.get('original_input', '')

    # Collect all style names that were updated
    style_names = []
    for key in request.form:
        if key.startswith('description_'):
            style_index = key.split('_')[1]
            description = request.form[key]
            style_name = missing_descriptions[int(style_index) - 1]
            descriptions[style_name] = description
            style_names.append(style_name)

    # Update database for all styles and store verification status
    for style_name, description in descriptions.items():
        update_description_in_db(style_name, description)
        current_app.logger.info(f"****** updated description for style {style_name} with: {description}")

    session.pop('missing_descriptions', None)
    
    # Return to index with original input
    return redirect(url_for('v6_listcreator.index', list_input=original_input))


def get_mysql_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'  # Replace with your actual database name
}
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

    
    query = """
    DECLARE @AltCDList NVARCHAR(MAX) = ?;
    DECLARE @ProductList NVARCHAR(MAX);

    SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';
    WITH SplitAltCD AS (
        SELECT value
        FROM dbo.SplitString(@AltCDList, ',')
    )
    SELECT @ProductList = COALESCE(@ProductList + ',', '') + PROD_CD
    FROM dbo.inv
    WHERE EXISTS (SELECT 1 FROM SplitAltCD WHERE alt_CD LIKE value);

    WITH SplitValues AS (
        SELECT value
        FROM dbo.SplitString(@ProductList, ',')
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
    WHERE i.PROD_CD IN (SELECT value FROM SplitValues);
    """
    
    queries = []
    try:
        conn_fdc = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}')
        conn_oms_ef = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}')
        
        for conn, database_name in [(conn_fdc, 'omsdata_FDC'), (conn_oms_ef, 'oms_EF')]:
            cursor = conn.cursor()
            mylist =  ",".join(style_list)
            cursor.execute(query, mylist)
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
    finally:
        conn_fdc.close()
        conn_oms_ef.close()

def create_pivot_table(data):

    # Create DataFrame
    df = pd.DataFrame(data, columns=['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                                     'quantity', 'ocean', 'selling_price', 'pcsBox',
                                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                                     'last_activity', 'Created_dt'])

    # Extract base SKU pattern (2 letters + 4 numbers) from SKU_no_color
    df['base_sku'] = df['SKU_no_color'].str.extract(r'([A-Za-z]{1,2}\d{4})')
    
    # Filter rows where base_sku matches the original pattern
    df = df[df['base_sku'].notna()]

    # Sort by base_sku first
    df = df.sort_values('base_sku')

    # Convert numeric columns to integers
    integer_columns = ['quantity', 'ocean', 'pcsBox',
                       'item_length', 'item_width', 'item_height']
    df[integer_columns] = df[integer_columns].fillna(0).astype(int)

    # Create dimensions column
    df['dimensions'] = df['item_length'].astype(str) + ' x ' + df['item_width'].astype(str) + ' x ' + df['item_height'].astype(str)

    # Group by base_sku instead of SKU_no_color
    style_groups = df.groupby('base_sku')

    style_cards = []
    for style, group in style_groups:
        most_common_price = round(group['selling_price'].mode().iloc[0], 2)

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

        style_data['style'] = style
        style_data['selling_price'] = most_common_price

        bk_rows = style_data[style_data['color'].str.startswith('BK')]
        other_rows = style_data[~style_data['color'].str.startswith('BK')]
        style_data = pd.concat([bk_rows, other_rows]).reset_index(drop=True)

        style_cards.append(style_data.to_dict('records'))

    return style_cards

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
            # Extract pattern: 1-2 letters followed by exactly 4 numbers
            match = re.match(r'([A-Za-z]{1,2}\d{4})', sku)
            if match:
                sku = match.group(1).upper()
                image_set = []

                # Fetch base image
                base_image_path = os.path.join(Config.CATALOG_ORIGINAL, f"{sku}.jpg")
                if os.path.exists(base_image_path):
                    image_set.append({
                        'filename': f"{sku}.jpg",
                        'path': f"/list_image/{sku}.jpg",
                        'default': True
                    })

                # Dynamically fetch all versioned images
                for filename in os.listdir(Config.CATALOG_ORIGINAL):
                    versioned_match = re.match(rf'{sku}_v\d+\.jpg', filename, re.IGNORECASE)
                    if versioned_match:
                        image_set.append({
                            'filename': filename,
                            'path': f"/list_image/{filename}",
                            'default': False
                        })

                # Add SKU and its images to the dictionary
                if image_set:  # Only include SKUs that have images
                    image_urls[sku] = sorted(image_set, key=lambda x: x['filename'])  # Sort by filename for consistency

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
    image_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
    
    # Check if image exists
    if not os.path.exists(image_path):
        return send_from_directory('static', 'no_image_available.jpg')
    
    # Get size parameter for optional resizing
    size = request.args.get('size', 'full')
    
    if size == 'full':
        return send_from_directory(Config.CATALOG_ORIGINAL, filename)
    
    try:
        # Resize image on demand
        buffer = resize_image(image_path, int(size) if size.isdigit() else 300)
        return send_file(buffer, mimetype='image/jpeg')
    except Exception as e:
        current_app.logger.error(f"Error serving image {filename}: {e}")
        return send_from_directory('static', 'no_image_available.jpg')
    
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
            
            if '/list_image/' in image_path:
                image_path = re.sub(r'/list_image/', '', image_path)
                full_path = os.path.join(Config.CATALOG_ORIGINAL, image_path)
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

            if '/list_image/' in image_filename:
                image_filename = re.sub(r'/list_image/', '', image_filename)
                image_path = os.path.join(Config.CATALOG_ORIGINAL, image_filename)
            else:
                image_path = os.path.join(Config.CATALOG_ORIGINAL, image_filename)

            if not os.path.exists(image_path):
                current_app.logger.error(f"Image not found: {image_path}")
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
                    ""    # Zero ocean
                ]
                
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
                    row_data = [
                        "",  # Image placeholder
                        description if index == 0 else "",
                        price if index == 0 else "",
                        style if index == 0 else "",
                        row['color'],
                        int(float(row['quantity'])) if row['quantity'] else 0
                    ]
                    # Only add ocean if it's not removed
                    if not removed_columns.get('ocean'):
                        row_data.append(int(float(row['ocean'])) if row.get('ocean') else 0)
                    
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
            last_col = 'I'
        
            
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
        
        if '/list_image/' in image_path:
            image_path = re.sub(r'/list_image/', '', image_path)
            full_path = os.path.join(Config.CATALOG_ORIGINAL, image_path)
        else:
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