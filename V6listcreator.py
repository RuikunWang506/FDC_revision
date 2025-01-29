from flask import Blueprint, render_template, request, jsonify, current_app, send_file,send_from_directory
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, PatternFill, Border, Side
from openpyxl.drawing.image import Image as OpenPyXLImage

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

v6_listcreator_bp = Blueprint('v6_listcreator', __name__, template_folder='templates')




selected_images = []  # This should be populated based on your application logic
formatted_date = datetime.datetime.now().strftime("%Y%m%d")  # Example date formatting


def list_from_dropbox(list_input, current_user):
    match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        
        current_app.logger.info(f"***{current_user}*** processing dropbox path: {dropbox_path}")
        
        if not os.path.exists(dropbox_path):
            current_app.logger.error(f"***{current_user}*** directory not found: {dropbox_path}")
            return {"error": "Directory not found. Please check your URL."}
        
        file_list = []
        sku_to_image = {}  # Dictionary to map SKUs to their corresponding image paths
        
        for filename in os.listdir(dropbox_path):
            if filename.endswith('.jpg'):
                sku_match = re.match(r'([A-Za-z]{1,2}\d{4})', filename)
                if sku_match:
                    sku = sku_match.group(1).upper()
                    file_list.append(sku)
                    sku_to_image[sku] = os.path.join(dropbox_path, filename)  # Map SKU to image path
                    current_app.logger.info(f"***{current_user}*** found SKU: {sku} from file: {filename}")
        
        if not file_list:
            current_app.logger.warning(f"***{current_user}*** no valid SKU patterns found in jpg files")
            return {"error": "No images found in the specified directory. Please check your URL."}
        
        current_app.logger.info(f"***{current_user}*** complete SKU list: {', '.join(file_list)}")
        current_app.logger.info(f"***{current_user}*** total SKUs found: {len(file_list)}")
        current_app.logger.info(f"***{current_user}*** total images found: {len(sku_to_image)}")
        
        return file_list, sku_to_image  # Return both SKUs and the SKU-to-image mapping

    return list_input, {}  # Return empty list and dictionary if not from Dropbox


def list_from_origin(list_input, current_user):
    # Convert input string to list of SKUs
    style_list = [sku.strip().upper() for sku in list_input.split(',') if sku.strip()]
    current_app.logger.info(f"***{current_user}*** processing regular input: {style_list}")
    return style_list

@v6_listcreator_bp.route('/v6_listcreator', methods=['GET', 'POST'])
def index():
    current_user = "a"  # Replace with actual user logic if needed
    error_message = None  # Initialize error message
    dropbox_images = {}  # Initialize dropbox_images as a dictionary

    if request.method == 'POST':
        list_input = request.form.get('list_input')
        current_app.logger.info(f"***{current_user}*** submitted input: {list_input}")

        if 'dropbox' in list_input.lower():
            result = list_from_dropbox(list_input, current_user)
            if isinstance(result, dict) and 'error' in result:
                error_message = result['error']  # Capture the error message
                return render_template('v6_listcreator.html', current_user=current_user, error_message=error_message)

            style_list, dropbox_images = result  # Unpack the result correctly
        else:
            style_list = list_from_origin(list_input, current_user)

        if style_list:
            descriptions = {}
            for style in style_list:
                descriptions[style] = get_description_from_mysql(style)  # Fetch description for each style

            # Continue with your existing logic...
            data = get_sql_data(style_list)  # Fetch style data
            image_urls = get_images_for_selection(style_list)  # Fetch image URLs for styles

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

                return render_template('v6_listcreator.html', **context)

    # Render default view if no POST request
    return render_template('v6_listcreator.html', current_user=current_user, error_message=error_message)


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
            return description_row[0] if description_row else "No description available"
        except Error as e:
            print(f"Error fetching description: {e}")
        finally:
            cursor.close()
            connection.close()
    return "No description available"



def get_sql_data(style_list):
    server = "192.168.254.204"
    database_fdc = "omsdata_FDC"
    database_oms_ef = "oms_EF"
    username = "tristen"
    password = 'Fdc2022!!'
    driver = 'ODBC Driver 17 for SQL Server'
    
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
        (id.in_stock - id.order_qty) AS Available_now,
        id.on_order_qty AS incoming_quantity,
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
            current_app.logger.info(f"SQL input list: {mylist}")
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
    current_app.logger.info("Creating pivot table from data")

    # Create DataFrame
    df = pd.DataFrame(data, columns=['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                                     'Available_now', 'incoming_quantity', 'selling_price', 'QTY_in_box',
                                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                                     'last_activity', 'Created_dt'])

    # Sort by SKU_no_color first
    df = df.sort_values('SKU_no_color')

    # Convert numeric columns to integers
    integer_columns = ['Available_now', 'incoming_quantity', 'QTY_in_box',
                       'item_length', 'item_width', 'item_height']
    df[integer_columns] = df[integer_columns].fillna(0).astype(int)

    # Create dimensions column
    df['dimensions'] = df['item_length'].astype(str) + ' x ' + df['item_width'].astype(str) + ' x ' + df['item_height'].astype(str)

    # Group by SKU_no_color
    style_groups = df.groupby('SKU_no_color')

    style_cards = []
    for style, group in style_groups:
        # Get most common price and round to 2 decimals
        most_common_price = round(group['selling_price'].mode().iloc[0], 2)

        # Create pivot table with sum aggregation for quantities
        style_data = group.pivot_table(
            values=['Available_now', 'incoming_quantity', 'QTY_in_box', 'dimensions'],
            index=['color'],
            aggfunc={
                'Available_now': 'sum',
                'incoming_quantity': 'sum',
                'QTY_in_box': 'first',  # Assuming you want the first value for QTY_in_box
                'dimensions': 'first'    # Assuming you want the first value for dimensions
            }
        ).reset_index()

        # Add style and selling price to the pivot table
        style_data['style'] = style
        style_data['selling_price'] = most_common_price

        # Move rows with color starting with 'BK' to the top
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
            current_app.logger.info(f"Found image: {filename}, path: {os.path.join(dropbox_path, filename)}")
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
    current_app.logger.info(f"Serving image: {dropbox_path}")
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

def add_data_to_excel_sheet(sku_name, ws, row, data, image_size):
    global selected_images

    # Find the matching selected image for this SKU
    matching_image = None
    for item in data:
        if sku_name == item['style']:
            matching_image = item['image']  # Get the selected image filename from the data
            break

    # Construct the full image path using CATALOG_ORIGINAL
    if matching_image:
        image_path = os.path.join(Config.CATALOG_ORIGINAL, matching_image)
    else:
        image_path = os.path.join('static', 'no_image_available.jpg')  # Fallback to placeholder

    # Check if the image exists
    if not os.path.exists(image_path):
        image_path = os.path.join('static', 'no_image_available.jpg')  # Use placeholder if not found

    # Open the original image and handle it based on image_size
    with open(image_path, 'rb') as img_file:
        if image_size is None:
            buffer = BytesIO(img_file.read())
            buffer.seek(0)
        else:
            buffer = resize_image(img_file, image_size)

    excelImg = OpenPyXLImage(buffer)
    excelImg.height = 150
    excelImg.width = 200
    excelImg.anchor = 'A' + str(row)
    ws.add_image(excelImg)

    ws['G' + str(row)] = sku_name
    price = ""

    # Find the price and other details from the provided data
    for item in data:
        if sku_name == item['style']:
            price = item.get('price', '')
            break

    ws['F' + str(row)] = "$" + str(price)

    detail_row_num = 0

    # Assuming data contains a list of dictionaries with relevant details
    for item in data:
        if sku_name == item['style']:  # Check if the SKU matches
            ws["H" + str(row + detail_row_num)] = item.get('color', '')
            ws["I" + str(row + detail_row_num)] = item.get('quantity', '')
            ws["N" + str(row + detail_row_num)] = item.get('sku', '')

            # Collect last modified and created dates
            last_modified = item.get('last_modified', 'No Date')
            created_date = item.get('created_date', 'No Date')

            ws["K" + str(row + detail_row_num)] = last_modified
            ws["R" + str(row + detail_row_num)] = created_date

            detail_row_num += 1

    detail_row_num += 1

    print(sku_name + " completed")
    ws.merge_cells("A" + str(row) + ":" + "D" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws.merge_cells("E" + str(row) + ":" + "E" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws.merge_cells("F" + str(row) + ":" + "F" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws['F' + str(row)].alignment = Alignment(horizontal='center', vertical='center')

    row += 8 if detail_row_num < 8 else detail_row_num

    return row

@v6_listcreator_bp.route('/export_list', methods=['POST'])
def export_list():
    try:
        data = request.get_json()
        current_app.logger.info(f"Exported list data: {data}")

        # Create a new Excel workbook and select the active worksheet
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Exported Data"

        # Define headers
        headers = ["Image", "Style", "Description", "Color", "Available Now", "Incoming Quantity", "QTY in Box", "Dimensions"]
        sheet.append(headers)

        # Populate the Excel sheet with data
        for item in data:
            style = item['style']
            description = item['description']
            image_filename = item['image']  # This should be the filename without the '/list_image/' prefix

            # Modify the image path if it contains 'list_image'
            if '/list_image/' in image_filename:
                image_filename = re.sub(r'/list_image/', '', image_filename)  # Remove '/list_image/'
                image_path = os.path.join(Config.CATALOG_ORIGINAL, image_filename)  # Construct the full path
            else:
                image_path = os.path.join(Config.CATALOG_ORIGINAL, image_filename)  # Default path if no '/list_image/'

            # Check if the image exists
            if not os.path.exists(image_path):
                current_app.logger.error(f"Image not found: {image_path}")
                image_path = os.path.join('static', 'no_image_available.jpg')  # Use placeholder if not found

            # Add rows for each style
            rows = item['rows']
            for index, row in enumerate(rows):
                row_data = [
                    "",  # Placeholder for the image column
                    style if index == 0 else "",  # Only add style for the first row
                    description if index == 0 else "",  # Only add description for the first row
                    row['color'],
                    row['availableNow'],
                    row['incomingQuantity'],
                    row['qtyInBox'],
                    row['dimensions']
                ]
                sheet.append(row_data)

                if index == 0:
                    # Insert the image in the first column for the first row
                    img = OpenPyXLImage(image_path)
                    img.width = 100  # Set image width
                    img.height = 100  # Set image height
                    sheet.add_image(img, f"A{sheet.max_row}")

        # Save the workbook to a BytesIO object
        output = BytesIO()
        workbook.save(output)
        output.seek(0)

        # Send the Excel file as a response
        return send_file(output, download_name="exported_data.xlsx", as_attachment=True)

    except Exception as e:
        current_app.logger.error(f"Error in export_list: {e}")
        return jsonify({"error": "Internal Server Error"}), 500