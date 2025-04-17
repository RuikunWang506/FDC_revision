from flask import Blueprint, redirect, url_for, request, jsonify, session, render_template, make_response, current_app, send_file
from flask_cors import CORS
import pandas as pd
import os
import shutil
from datetime import datetime
import time
from googleapiclient.discovery import build
from google.oauth2 import service_account
from config import Config
import fitz
import io
import re
import pytz  # Make sure to install pytz if not already installed
import json


gpgtmx_bp = Blueprint('gpgtmx', __name__, template_folder='templates')
last_data_update = time.time()
today_order_count = 0
last_reset_date = None
count_file_path = os.path.join('output', 'gpginvoice', 'order_count.json')

def load_order_count():
    """Load the order count from a JSON file."""
    if os.path.exists(count_file_path):
        with open(count_file_path, 'r') as f:
            data = json.load(f)
            return data.get("count", 1), data.get("last_reset_date", None)
    return 1, None

def save_order_count(count, last_reset_date):
    """Save the order count to a JSON file."""
    with open(count_file_path, 'w') as f:
        json.dump({"count": count, "last_reset_date": last_reset_date}, f)

# Initialize the counter and last reset date
today_order_count, last_reset_date = load_order_count()

def count_today_orders():
    """Count the number of orders recorded today."""
    global today_order_count, last_reset_date

    # Get the current date in PST
    pst = pytz.timezone('America/Los_Angeles')
    current_time = datetime.now(pst)
    today = current_time.date()

    # Check if the last reset date has changed
    if last_reset_date is None or last_reset_date != str(today):
        # Reset the counter if it's a new day
        today_order_count = 1
        last_reset_date = str(today)
        save_order_count(today_order_count, last_reset_date)  # Save the reset count

    return today_order_count

def add_order():
    """Function to add an order and increment the count."""
    global today_order_count
    today_order_count += 1
    save_order_count(today_order_count, last_reset_date)  # Save the updated count

def register_routes(app):
    CORS(app, resources={
        r"/gpginvoice": {
            "origins": ["https://admin.genuinepartsgiant.com"],
            "methods": ["POST", "OPTIONS"],
            "allow_headers": ["Content-Type"],
            "supports_credentials": True
        }
    })
@gpgtmx_bp.route("/gpg_check_updates")
def check_updates():
    """Check if there have been updates since the client's last check"""
    client_last_check = float(request.args.get('last_check', 0))
    return jsonify({"update_available": last_data_update > client_last_check, 
                   "last_update": last_data_update})

def get_tmx_descriptions():
    """Fetch part descriptions from Google Sheets."""
    spreadsheet_id = Config.TMX_sheet.split('/')[5]  # Extract ID from URL
    creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=Config.SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    sheet = service.spreadsheets()
    
    result = sheet.values().get(
        spreadsheetId=spreadsheet_id,
        range='QBO_sku_report!A:B'  # SKU in Column A, Description in Column B
    ).execute()
    
    descriptions = {}
    values = result.get('values', [])
    for row in values[1:]:  # Skip header row
        if len(row) >= 2:
            sku = row[0].strip()
            description = row[1].strip()
            descriptions[sku] = description
    return descriptions

@gpgtmx_bp.route("/gpg")
def gpg_redirect():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    return redirect(url_for("gpgtmx.gpginvoice"))

@gpgtmx_bp.route("/gpginvoice", methods=["GET", "POST", "OPTIONS"])
def gpginvoice():
    """Main route for displaying and handling GPG invoice data."""
    global last_data_update

    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    excel_path = os.path.join('output', 'gpginvoice', 'gpg_orders.xlsx')
    os.makedirs(os.path.dirname(excel_path), exist_ok=True)

    if request.method == "POST":
        try:
            data = request.get_json()
            descriptions = get_tmx_descriptions()

            new_rows = []
            duplicates = []  # List to track duplicates
            for item in data['lineItems']:
                order_number = data['orderNumber'].strip()
                part_number = item['partNumber'].strip().upper()
                line_number = str(item['lineNumber']).strip()

                # Determine Manufacturer Prefix
                if order_number.startswith("CH"):
                    prefixed_part_number = f"Honda:{part_number}"
                elif order_number.startswith("CU"):
                    prefixed_part_number = f"Hyundai:{part_number}"
                else:
                    prefixed_part_number = part_number  # No prefix

                description = descriptions.get(prefixed_part_number, '')

                new_row = [
                    order_number,
                    data['orderDate'],
                    line_number,
                    part_number,
                    description,
                    item['qty'],
                    item['price'].replace('$', '').strip()
                ]

                # Check for duplicates
                if os.path.exists(excel_path):
                    try:
                        with pd.ExcelFile(excel_path) as xls:
                            df = pd.read_excel(xls, engine='openpyxl')
                            # Normalize the DataFrame for comparison
                            df['orderNumber'] = df['orderNumber'].astype(str).str.strip().str.upper()
                            df['Part Number'] = df['Part Number'].astype(str).str.strip().str.upper()
                            df['Line Number'] = df['Line Number'].astype(str).str.strip()

                            # Check if the new row already exists in the DataFrame
                            if not df[(df['orderNumber'] == order_number) & 
                                       (df['Part Number'] == part_number) & 
                                       (df['Line Number'] == line_number)].empty:
                                duplicates.append(new_row)  # Track duplicates
                            else:
                                new_rows.append(new_row)  # Add to new rows if not a duplicate
                    except PermissionError:
                        return jsonify({"error": "File is currently in use. Please try again."}), 503
                else:
                    new_rows.append(new_row)  # If file doesn't exist, add the new row

            # Load existing data with proper file handling
            if os.path.exists(excel_path):
                try:
                    with pd.ExcelFile(excel_path) as xls:
                        df = pd.read_excel(xls, engine='openpyxl')
                except PermissionError:
                    return jsonify({"error": "File is currently in use. Please try again."}), 503
            else:
                df = pd.DataFrame(columns=['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price'])

            df_new = pd.DataFrame(new_rows, columns=df.columns)

            # Normalize Data for Deduplication
            for col in ['orderNumber', 'Part Number', 'Line Number']:
                df[col] = df[col].astype(str).str.strip().str.upper()
                df_new[col] = df_new[col].astype(str).str.strip().str.upper()

            # Merge & Remove Duplicates
            df = pd.concat([df, df_new], ignore_index=True)
            df = df.drop_duplicates(subset=['orderNumber', 'Part Number', 'Line Number'], keep='first')

            # Save back to Excel with explicit file closing
            try:
                with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                last_data_update = time.time()
            except PermissionError:
                return jsonify({"error": "Cannot save file - it is being used by another process."}), 503

            # Count today's orders
            if new_rows:
                add_order()
            total_orders_today = count_today_orders()

            # Prepare response message
            response_message = f"Row is indeed added to the Excel file. This is order number {total_orders_today} of the day, you have {total_orders_today} total today."
            if duplicates:
                response_message = f" Skipped {len(duplicates)} duplicate entries."

            return jsonify({
                "success": True,
                "message": response_message
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Load existing data for GET request
    if os.path.exists(excel_path):
        try:
            with pd.ExcelFile(excel_path) as xls:
                df = pd.read_excel(xls, engine='openpyxl')
            
            descriptions = get_tmx_descriptions()
            df['Description'] = df.apply(lambda row: descriptions.get(
                f"Honda:{row['Part Number']}" if row['orderNumber'].startswith("CH") else
                f"Hyundai:{row['Part Number']}" if row['orderNumber'].startswith("CU") else
                row['Part Number'], ''
            ), axis=1)

            df = df[['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price']]
            table_html = df.to_html(classes='table table-striped', table_id='gpgTable', index=False)
        except PermissionError:
            table_html = "<p>Data file is currently in use. Please refresh the page.</p>"
    else:
        table_html = "<p>No data available</p>"
        
    current_time = time.time()
    return render_template('gpginvoice.html', table_html=table_html, current_user=current_user,
                          current_time=current_time)

@gpgtmx_bp.route("/gpg_finish", methods=["POST"])
def gpg_finish():
    """Handle the 'Finish' button click to archive data and prepare for QBO import."""
    try:
        data = request.get_json()
        csv_content = data.get('csvContent')

        if not csv_content:
            return jsonify({"error": "No CSV content provided"}), 400

        src_excel_path = os.path.join('output', 'gpginvoice', 'gpg_orders.xlsx')
        archive_dir = os.path.join('output', 'gpginvoice', 'gpg_qbo_exports_archive')
        os.makedirs(archive_dir, exist_ok=True)

        current_datetime = datetime.now()
        base_filename = f'gpg_orders_{current_datetime.strftime("%m_%d_%Y")}'
        count = 0
        dst_excel_path = os.path.join(archive_dir, f'{base_filename}.xlsx')

        while os.path.exists(dst_excel_path):
            count += 1
            dst_excel_path = os.path.join(archive_dir, 
                f'{base_filename}_{count}.xlsx')

        if not os.path.exists(src_excel_path):
            return jsonify({"error": "No gpg_orders.xlsx file exists to archive"}), 404

        import io
        qbo_df = pd.read_csv(io.StringIO(csv_content))

        # Ensure unique sheet name
        sheet_name = 'for_qbo'

        # Check if the sheet already exists and remove it before adding the new one
        if os.path.exists(src_excel_path):
            with pd.ExcelFile(src_excel_path) as xls:
                existing_sheets = xls.sheet_names
                
            if sheet_name in existing_sheets:
                # Load the workbook, remove the existing sheet, and save
                from openpyxl import load_workbook
                wb = load_workbook(src_excel_path)
                if sheet_name in wb.sheetnames:
                    del wb[sheet_name]
                wb.save(src_excel_path)

        # Save to the fixed sheet name
        with pd.ExcelWriter(src_excel_path, engine='openpyxl', mode='a') as writer:
            qbo_df.to_excel(writer, sheet_name=sheet_name, index=False)

        # Move the file to archive
        shutil.move(src_excel_path, dst_excel_path)
        
        # Update the last_data_update timestamp
        global last_data_update
        last_data_update = time.time()

        return jsonify({
            "success": True,
            "message": f"Excel file archived as {os.path.basename(dst_excel_path)} with QBO data added to sheet '{sheet_name}'. Data table has been cleared."
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
@gpgtmx_bp.route("/upload_pdf", methods=["POST"])
def upload_pdf():
    """Handle PDF upload, extract data, and generate a pivot table in Excel."""
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "No file uploaded"}), 400
        
        # Read PDF and extract text from each page
        pdf_text = []
        with fitz.open(stream=file.read(), filetype="pdf") as doc:
            for page in doc:
                pdf_text.append(page.get_text("text"))
        
        extracted_data = []
        current_order = None
        
        # Regex for part numbers
        part_number_pattern = re.compile(r"\b\d{5}-[A-Z0-9]{3}-[A-Z0-9]{3}\b")
        
        print("Processing PDF content...")
        
        for text in pdf_text:
            lines = text.split("\n")
            
            for i, line in enumerate(lines):
                line = line.strip()
                
                # Detect Order Number
                if "Order#" in line:
                    current_order = line.split("Order#")[1].strip()
                    print(f"Detected Order #: {current_order}")
                
                # Look for part numbers
                part_match = part_number_pattern.search(line)
                if part_match and current_order:
                    part_no = part_match.group()
                    line_no = i  # Define line_no here at the part number detection level
                    
                    # Search for quantity in the next few lines
                    # Looking for a line that contains just a number (quantity)
                    for j in range(i+1, min(i+8, len(lines))):
                        qty_line = lines[j].strip()
                        # Check if the line is just a number (the quantity)
                        if qty_line.isdigit():
                            quantity = int(qty_line/4)
                            
                            # Then look for price in the next line
                            if j+1 < len(lines) and j+2 < len(lines):
                                # Check if next line has a decimal (price)
                                if '.' in lines[j+1].strip():
                                    price = lines[j+1].strip()
                                    subtotal = lines[j+2].strip()  # The second price is the subtotal
                                    
                                    extracted_data.append([current_order, part_no, line_no, quantity, subtotal])
                                    print(f"Extracted: {current_order}, {part_no}, {quantity}, {subtotal}")
                                    break

                    if quantity is not None:
                        extracted_data.append([current_order, part_no, line_no, quantity, subtotal])
                        print(f"Extracted: {current_order}, {part_no}, {quantity}, {subtotal}")
                    else:
                        print(f"Skipping: {part_no} (No valid quantity line found)")

        print("Extracted Data:", extracted_data)

        if not extracted_data:
            return jsonify({"error": "No valid order data extracted from PDF."}), 400

        # Create DataFrame from extracted data
        df = pd.DataFrame(extracted_data, columns=["Order #", "Part No", "Line No", "Qty", "Subtotal"])
        print("DataFrame Head:")
        print(df.head())

        # Create Pivot Table: Sum of Qty per Part No
        if not df.empty:
            pivot_df = df.pivot_table(index="Part No", values="Qty", aggfunc="sum")
            print("Pivot Table Head:")
            print(pivot_df.head())
        else:
            return jsonify({"error": "Failed to create pivot table, no valid data found."}), 400

        # Convert the pivot table DataFrame to an Excel file in-memory
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            pivot_df.to_excel(writer, sheet_name="Pivot Table")
            
            # Get the workbook and sheet
            workbook = writer.book
            worksheet = writer.sheets['Pivot Table']
            
            # Set column A width to 15 inches (approximately 120 units in Excel's width system)
            worksheet.column_dimensions['A'].width = 20
            
            # Set page setup for 4x6 inches with 0.25 inch margins
            worksheet.page_setup.paperSize = worksheet.PAPERSIZE_LETTER
            worksheet.page_margins.left = 0.25
            worksheet.page_margins.right = 0.25
            worksheet.page_margins.top = 0.25
            worksheet.page_margins.bottom = 0.25
            
            # Set custom page size (4x6 inches)
            worksheet.page_setup.paperHeight = "6in"
            worksheet.page_setup.paperWidth = "4in"

        output.seek(0)
        print("Excel File Created Successfully with custom formatting!")
        print("Excel File Created Successfully!")

        # Send the Excel file for download
        try:
            response = send_file(output, as_attachment=True, download_name="pivot_table.xlsx",
                                 mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            print("File Sent Successfully!")
            return response
        except Exception as e:
            print("Error Sending File:", str(e))
            return jsonify({"error": "Error generating download file."}), 500

    except Exception as e:
        print(f"General Error: {e}")
        return jsonify({"error": str(e)}), 500