import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for, current_app, jsonify, send_file
import pyodbc
import os
from datetime import datetime
import logging
import re
import glob
from io import StringIO
import gspread
from google.oauth2.service_account import Credentials


boat_service_bp = Blueprint('boat_service', __name__, template_folder='../templates')

# Google Sheets configuration
GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv&gid=0"
GOOGLE_SHEET_ID = "14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE"
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'credentials', 'service-account.json')
DROPBOX_CONTAINER_PATH = r"C:\Users\stream\Dropbox\Pictures-China (1)\container catalog\Container\container INFO\2025年"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'container_csvs')


# Database connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
username = "tristen"
password = 'Fdc2022!!'
driver = 'ODBC Driver 17 for SQL Server'

def get_db_connection():
    """Get database connection"""
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database_fdc};"
        f"UID={username};"
        f"PWD={password};"
    )
    return pyodbc.connect(conn_str)

def get_sheets_client():
    """Initialize Google Sheets client with service account"""
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        current_app.logger.error(f"Error initializing Google Sheets client: {e}")
        return None

def add_multiple_containers_to_sheets(container_numbers):
    """Add multiple new container rows to Google Sheets in batch"""
    try:
        client = get_sheets_client()
        if not client:
            return []
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get ALL existing containers in ONE read operation (Column A)
        existing_containers = set()
        try:
            # Get all values from column A (Container #) at once
            container_column = sheet.col_values(1)  # Column A = index 1
            existing_containers = set(container_column)
        except Exception as e:
            current_app.logger.error(f"Error reading existing containers: {e}")
        
        # Find containers that need to be added
        # Check for both F##### and FT##### versions
        containers_to_add = []
        for container_num in container_numbers:
            if not container_num:
                continue
            
            # Check if F version exists
            if container_num in existing_containers:
                continue
            
            # Check if FT version exists (convert F25131 -> FT25131)
            if container_num.startswith('F') and len(container_num) > 1:
                ft_version = 'FT' + container_num[1:]  # F25131 -> FT25131
                if ft_version in existing_containers:
                    continue
            
            containers_to_add.append(container_num)
        
        if not containers_to_add:
            return []
        
        # Sort containers before adding (F26108, F26109, F26110, F26111)
        containers_to_add.sort()
        
        # Get container files for reading Invoice data
        container_files = scan_dropbox_containers()
        
        # Get the header row to determine the correct number of columns
        try:
            header_row = sheet.row_values(1)
            num_columns = len(header_row)
        except:
            num_columns = 16  # Default fallback
        
        # Batch add all new containers in ONE write operation
        # Column A = Container #, Column B = Company Code, Column C = Invoice Container #, Column O = fdc_flask_app marker
        new_rows = []
        for container_num in containers_to_add:
            # Create row with the exact number of columns as the sheet
            new_row = [''] * num_columns
            
            # Set specific columns by their 0-based index
            new_row[0] = container_num  # Column A (index 0) = Container #
            
            # Extract invoice data if file exists
            if container_num in container_files:
                try:
                    file_path = container_files[container_num]
                    # Read Invoice tab
                    df_invoice = pd.read_excel(file_path, sheet_name='Invoice', header=None)
                    
                    # Check A5 for T-FASHION
                    if len(df_invoice) > 4:  # A5 is row index 4
                        a5_value = str(df_invoice.iloc[4, 0]) if pd.notna(df_invoice.iloc[4, 0]) else ''
                        if 'T-FASHION' in a5_value:
                            new_row[1] = 'TF'  # Column B (index 1) = Company Code
                    
                    # Get container number from A6
                    if len(df_invoice) > 5:  # A6 is row index 5
                        a6_value = str(df_invoice.iloc[5, 0]) if pd.notna(df_invoice.iloc[5, 0]) else ''
                        # Extract container number after "CONTAINER #："
                        match = re.search(r'CONTAINER\s*#[：:]\s*([A-Z0-9]+)', a6_value, re.IGNORECASE)
                        if match:
                            new_row[2] = match.group(1)  # Column C (index 2) = Invoice Container #
                    
                except Exception as e:
                    current_app.logger.error(f"Error reading Invoice for {container_num}: {e}")
            
            # Set marker and timestamp in correct columns (O and P)
            if num_columns > 14:  # Column O (index 14)
                new_row[14] = 'fdc_flask_app'
            if num_columns > 15:  # Column P (index 15)
                new_row[15] = datetime.now().strftime('%m/%d/%Y %H:%M:%S')
            
            new_rows.append(new_row)
        
        # Append all rows using batch update to ensure correct column placement
        if new_rows:
            # Get current data to find the last row
            all_data = sheet.get_all_values()
            last_row = len(all_data)
            
            # Update each new row explicitly
            for i, new_row in enumerate(new_rows):
                row_num = last_row + i + 1
                # Update from column A to the column corresponding to len(new_row)
                end_col = chr(ord('A') + len(new_row) - 1)
                range_str = f'A{row_num}:{end_col}{row_num}'
                sheet.update(range_str, [new_row], value_input_option='RAW')
            
            # Center align Columns A, B, and C for the new rows
            start_row = last_row + 1
            end_row = last_row + len(new_rows)
            
            try:
                sheet.format(f'A{start_row}:C{end_row}', {
                    'horizontalAlignment': 'CENTER'
                })
            except Exception as e:
                current_app.logger.error(f"Error applying alignment: {e}")
        
        return containers_to_add
        
    except Exception as e:
        current_app.logger.error(f"Error adding containers to Google Sheets: {e}")
        return []

def batch_update_column_o(container_numbers):
    """Update column O for multiple containers in batch"""
    try:
        client = get_sheets_client()
        if not client:
            return False
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once (single read)
        all_data = sheet.get_all_values()
        
        # Find rows to update (Container # is in Column A now)
        updates = []
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1:  # Make sure row has at least column A
                container = row[0]  # Column A (index 0) = Container #
                if container in container_numbers:
                    # Row index in sheet (1-based, +1 because enumerate is 0-based)
                    sheet_row = row_idx + 1
                    # Update both column O and column P
                    updates.append({
                        'range': f'O{sheet_row}',  # Column O
                        'values': [['fdc_flask_app']]
                    })
                    updates.append({
                        'range': f'P{sheet_row}',  # Column P
                        'values': [[datetime.now().strftime('%m/%d/%Y %H:%M:%S')]]
                    })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error batch updating Google Sheets: {e}")
        return False

def update_received_dates_in_sheets(container_dates_dict):
    """Update RCD (Received Date) in Google Sheets Column F for landed containers"""
    try:
        client = get_sheets_client()
        if not client:
            return False
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once (single read)
        all_data = sheet.get_all_values()
        
        # Find rows to update (Container # is in Column A)
        updates = []
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1:  # Make sure row has at least column A
                container = row[0]  # Column A (index 0) = Container #
                if container in container_dates_dict:
                    # Check if Column F (index 5) already has data
                    existing_rcd = row[5].strip() if len(row) > 5 else ''
                    
                    # Only update if the cell is empty
                    if not existing_rcd:
                        # Row index in sheet (1-based, +1 because enumerate is 0-based)
                        sheet_row = row_idx + 1
                        received_date = container_dates_dict[container]
                        updates.append({
                            'range': f'F{sheet_row}',  # Column F = RCD
                            'values': [[received_date]]
                        })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            current_app.logger.info(f"Updated {len(updates)} received dates in Google Sheets")
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error updating received dates in Google Sheets: {e}")
        return False

def scan_dropbox_containers():
    """Scan Dropbox folder for all container Excel files and extract container numbers"""
    container_files = {}
    
    try:
        # Get all month folders
        month_folders = glob.glob(os.path.join(DROPBOX_CONTAINER_PATH, '*'))
        
        for month_folder in month_folders:
            if os.path.isdir(month_folder):
                # Get all Excel files in this month folder
                excel_files = glob.glob(os.path.join(month_folder, '*.xlsx'))
                
                for file_path in excel_files:
                    filename = os.path.basename(file_path)
                    # Extract container number from filename (e.g., "FT25276 Packing List..." -> "F25276")
                    match = re.match(r'^FT(\d+)', filename)
                    if match:
                        container_number = 'F' + match.group(1)  # Replace FT with F
                        container_files[container_number] = file_path
        
        return container_files
    
    except Exception as e:
        current_app.logger.error(f"Error scanning Dropbox containers: {e}")
        return {}

def process_container_file(file_path, container_number):
    """Process a container Excel file and generate CSV files"""
    try:
        # Read the Excel file
        df_header = pd.read_excel(file_path, sheet_name='System File', nrows=2, header=None)
        df_f24213 = pd.read_excel(file_path, sheet_name='System File', header=2)
        
        # Clean column names
        df_f24213.columns = df_f24213.columns.str.replace('\n', ' ').str.strip()
        df_f24213 = df_f24213.dropna(subset=['OMS', 'RMB'])
        df_f24213['WT/PCS (LBS)'] = df_f24213['WT/PCS (LBS)'].round(2)
        df_f24213 = df_f24213.map(lambda x: re.sub(r'\s+', ' ', str(x)) if isinstance(x, str) else x)

        # Step 3 columns (styles CSV)
        step3_columns = [
            'Item#', 'WHS#', 'Descrip', 'Unit', 'OrdSize', 'MinStk', 'Price Type', 'Price1', 'Price2', 'Price3', 
            'Price4', 'Price5', 'QtyPrice1', 'QtyPrice2', 'QtyPrice3', 'QtyPrice4', 'QtyPrice5', 'QtyPrice6', 
            'Range1', 'Range2', 'Range3', 'Range4', 'Range5', 'Range6', 'Range7', 'CreateDate', 'Note1', 'Note2', 
            'Item Def. Bin Location', 'Warehouse Bin Location', 'Product Year', 'Comp', 'Class#', 'Vend#', 
            'Vend Item#', 'Image Name', 'Unit/Case', 'Box/Case', 'Case Vol', 'Unit Vol', 'Sales Cost', 'Discount', 
            'CommRt', 'Taxable', 'Stkable', 'UPC Code', 'Updt Date', 'Updt By', 'Dept#', 'Case Wt', 'Unit Wt', 
            'OEM#', 'Alt.Item#', 'Lead Date', 'Color', 'Size', 'In Stock', 'Last Rcv Q', 'Last Rcv Dat', 
            'Base Cost', 'Duty', 'Handling fee', 'Ship Fee', 'Avg.Cost', 'Last Sale Da', 'Last Order', 'Order Date', 
            'So Qty', 'PO Qty', 'l2yrsqty', 'lyrsqty', 'ThisYrSQty', 'Descrip2', 'Descrip3', 'Notes Line 1', 
            'Notes Line 2', 'Notes Line 3', 'Notes Line 4', 'Notes Line 5', 'Notes Line 6', 'Notes Line 7', 
            'Notes Line 8', 'Case Length', 'Case Height', 'Case Width', 'Unit Length', 'Unit Height', 'Unit Width', 
            'Division', 'Case/Pallet', 'Catalog Page', 'Status', 'Default Unit', 'Release', 'Last Sale Qty', 
            'Allocate Qty', 'More Notes 1', 'More Notes 2', 'More Notes 3', 'More Notes 4', 'More Notes 5', 
            'More Notes 6', 'More Notes 7', 'More Notes 8'
        ]
        
        # Prepare step3 data
        step3_data = {
            'Item#': df_f24213['STYLE NO'] + ' ' + df_f24213['COLOR'],
            'WHS#': "01",
            'Descrip': container_number,
            'Alt.Item#': df_f24213['STYLE NO'],
            'Color': df_f24213['COLOR'],
            'Class#': "L1",
            'Unit/Case': df_f24213['PCS /CTN'],
            'Box/Case': "01",
            'Sales Cost': df_f24213['OMS'],
            'Case Wt': df_f24213['WT/CTN (LBS)'],
            'Unit Wt': df_f24213['WT/PCS (LBS)'],
            'Unit Length': df_f24213['SIZE   (L)IN'],
            'Unit Width': df_f24213['SIZE   (W)IN'],
            'Unit Height': df_f24213['SIZE   (H)IN'],
            'Price1': df_f24213['Price1'],
            'Base Cost': df_f24213['OMS'],
            'Avg.Cost': df_f24213['OMS']
        }

        df_step3 = pd.DataFrame(step3_data)
        for col in step3_columns:
            if col not in df_step3.columns:
                df_step3[col] = ""
        df_step3 = df_step3[step3_columns]

        # Step 4 data (PO CSV)
        step4_columns = ['P/O#', 'Item#', 'W#', 'P/O Qty', 'Price', 'Receive Qty', 'Receive Price', 'UPC']
        step4_data = {
            'P/O#': container_number,
            'Item#': df_f24213['STYLE NO'] + ' ' + df_f24213['COLOR'],
            'W#': "01",
            'P/O Qty': df_f24213['QUANTITY'],
            'Price': df_f24213['OMS'],
        }

        df_step4 = pd.DataFrame(step4_data)
        for col in step4_columns:
            if col not in df_step4.columns:
                df_step4[col] = ""
        df_step4 = df_step4[step4_columns]

        # Save CSV files
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        
        step3_filename = f"{container_number}-styles.csv"
        step4_filename = f"{container_number}-PO.csv"
        step3_path = os.path.join(OUTPUT_DIR, step3_filename)
        step4_path = os.path.join(OUTPUT_DIR, step4_filename)
        
        df_step3.to_csv(step3_path, index=False)
        df_step4.to_csv(step4_path, index=False)
        
        return {
            'success': True,
            'styles_file': step3_filename,
            'po_file': step4_filename
        }
    
    except Exception as e:
        current_app.logger.error(f"Error processing container {container_number}: {e}")
        return {
            'success': False,
            'error': str(e)
        }


@boat_service_bp.route('/boat', methods=['GET'])
def boat_service():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    current_user = session['username']
    current_app.logger.info(f"***{current_user}***__@@@checked boat page@@@")
    
    # Render page immediately with loading state
    last_modified_date = "Loading..."
    return render_template('boat.html', table_data=None, last_modified_date=last_modified_date, current_user=current_user)

@boat_service_bp.route('/boat/data', methods=['GET'])
def boat_data():
    """API endpoint to fetch boat data asynchronously"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        # First, scan Dropbox for container files
        container_files = scan_dropbox_containers()
        
        # Get current containers from Google Sheets to check for missing ones
        try:
            df_current = pd.read_csv(GOOGLE_SHEETS_URL)
            current_containers = set(df_current['Container #'].dropna().tolist())
            containers_in_dropbox = set(container_files.keys())
            new_containers = containers_in_dropbox - current_containers
            
            # Batch add new containers to Google Sheets first
            if new_containers:
                added_containers = add_multiple_containers_to_sheets(list(new_containers))
                if added_containers:
                    current_app.logger.info(f"Successfully added {len(added_containers)} containers")
        except Exception as e:
            current_app.logger.error(f"Error checking/updating Google Sheets: {e}")
            # Continue with data fetching even if update fails
        
        # Now fetch the updated data from Google Sheets
        try:
            df = pd.read_csv(GOOGLE_SHEETS_URL)
            last_modified_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S') + " (Live from Google Sheets)"
        except Exception as e:
            current_app.logger.error(f"Error fetching data from Google Sheets: {e}")
            return jsonify({'error': str(e)}), 500

        # Map columns directly by name
        df_cleaned = df[['Container #', 'COMPANY', 'CONTAINER Tracking', 'Memo', 'Exam', 'ETA', 'RCD']].copy()
        
        # Combine Memo and Exam columns with "-" separator
        def combine_memo_exam(row):
            memo = str(row['Memo']).strip() if pd.notna(row['Memo']) and str(row['Memo']).strip() else ''
            exam = str(row['Exam']).strip() if pd.notna(row['Exam']) and str(row['Exam']).strip() else ''
            
            if memo and exam:
                return f"{memo} - {exam}"
            elif memo:
                return memo
            elif exam:
                return exam
            else:
                return ''
        
        df_cleaned['MEMO'] = df_cleaned.apply(combine_memo_exam, axis=1)
        
        # Add Reserved column (empty for now)
        df_cleaned['Reserved'] = ''
        
        # Rename and reorder columns
        df_cleaned = df_cleaned.rename(columns={'Container #': 'container', 'CONTAINER Tracking': 'link', 'COMPANY': 'company'})
        df_cleaned = df_cleaned[['Reserved', 'container', 'link', 'company', 'ETA', 'RCD', 'MEMO']]
        

        # Convert dates - handle both Excel serial dates and date strings
        def convert_date(date_val):
            if pd.isnull(date_val) or date_val == '':
                return ''
            # Try Excel serial format first
            if isinstance(date_val, (int, float)):
                try:
                    return (pd.to_datetime('1899-12-30') + pd.to_timedelta(date_val, 'D')).strftime('%m/%d/%Y')
                except:
                    return ''
            # Try parsing as date string
            try:
                return pd.to_datetime(date_val).strftime('%m/%d/%Y')
            except:
                return str(date_val)

        df_cleaned['ETA'] = df_cleaned['ETA'].apply(convert_date)
        df_cleaned['RCD'] = df_cleaned['RCD'].apply(convert_date)

        # Get the last 30 rows first, then sort by 'container' in descending order
        df_cleaned = df_cleaned.tail(30).sort_values(by='container', ascending=False)

        # Store raw container values for SQL query use
        df_cleaned['container_raw'] = df_cleaned['container']
        
        # Remove empty containers to avoid SQL errors
        df_cleaned = df_cleaned[df_cleaned['container_raw'].notna() & (df_cleaned['container_raw'] != '')]

        # Check which containers have CSV files already generated and which can be processed
        df_cleaned['has_styles_csv'] = False
        df_cleaned['has_po_csv'] = False
        df_cleaned['can_process'] = False
        
        for index, row in df_cleaned.iterrows():
            container_num = row['container_raw']
            styles_file = os.path.join(OUTPUT_DIR, f"{container_num}-styles.csv")
            po_file = os.path.join(OUTPUT_DIR, f"{container_num}-PO.csv")
            
            df_cleaned.at[index, 'has_styles_csv'] = os.path.exists(styles_file)
            df_cleaned.at[index, 'has_po_csv'] = os.path.exists(po_file)
            
            # Check if container can be processed (exists in Dropbox)
            df_cleaned.at[index, 'can_process'] = container_num in container_files

        # Get database connection and optimize SQL query
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Build batch query for all containers
        container_list = df_cleaned['container_raw'].tolist()
        
        if not container_list:
            df_cleaned['Status'] = '🔄upload🔄'
        else:
            placeholders = ','.join(['?' for _ in container_list])
            
            # Query bin_pur.RCVD_QTY - DIRECT and FAST method
            # RCVD_QTY > 0 means items have been physically received (landed)
            # RCVD_QTY = 0 means items haven't been received yet (ocean)
            query = f"SELECT PUR_NUM, SUM(RCVD_QTY) as total_rcvd FROM dbo.bin_pur WHERE PUR_NUM IN ({placeholders}) GROUP BY PUR_NUM"
            cursor.execute(query, container_list)
            results = cursor.fetchall()
            rcvd_lookup = {row[0].strip(): row[1] for row in results}
            
            # Get received dates for landed containers (from inv_data.LASTRCV_DT)
            # Use the same date format as SQL_connector.py
            date_query = f"""
                SELECT DESCRIP, 
                       MAX(LASTRCV_DT) as last_rcv_dt,
                       RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', MAX(LASTRCV_DT)) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                       RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', MAX(LASTRCV_DT)) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                       CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', MAX(LASTRCV_DT)) - 81088, '20230101')) AS VARCHAR) AS formatted_date
                FROM dbo.inv_data 
                WHERE DESCRIP IN ({placeholders})
                GROUP BY DESCRIP
            """
            cursor.execute(date_query, container_list)
            date_results = cursor.fetchall()
            date_lookup = {row[0].strip(): row[2] if row[1] and row[1] > 0 else '' for row in date_results}
            
            # Collect landed containers with dates for Google Sheets update
            landed_containers_with_dates = {}
            
            # Apply status based on RCVD_QTY
            for index, row in df_cleaned.iterrows():
                container_raw = row['container_raw']
                rcvd_qty = rcvd_lookup.get(container_raw)
                
                if rcvd_qty is not None and rcvd_qty > 0:
                    df_cleaned.at[index, 'Status'] = '📍landed📍'
                    # Hide process button for landed containers
                    df_cleaned.at[index, 'can_process'] = False
                    # Store received date for Google Sheets update
                    received_date = date_lookup.get(container_raw, '')
                    if received_date:
                        landed_containers_with_dates[container_raw] = received_date
                elif rcvd_qty is not None and rcvd_qty == 0:
                    df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
                    # Hide process button for ocean containers
                    df_cleaned.at[index, 'can_process'] = False
                else:
                    df_cleaned.at[index, 'Status'] = '🔄upload🔄'
                    # Keep can_process as set earlier (based on Dropbox file existence)
        
        # Close database connection
        conn.close()
        
        # Update Google Sheets with received dates for landed containers
        if landed_containers_with_dates:
            update_received_dates_in_sheets(landed_containers_with_dates)

        # Drop the 'container_raw' column before rendering (keep the other metadata columns)
        df_cleaned = df_cleaned.drop('container_raw', axis=1)

        # Replace NaN values with empty strings for JSON serialization
        df_cleaned = df_cleaned.fillna('')
        
        # Log columns being sent for debugging
        
        # Convert DataFrame to list of dictionaries for JSON response
        # Don't add HTML here - it will be added in JavaScript
        data_rows = df_cleaned.to_dict('records')
        
        return jsonify({
            'success': True,
            'data': data_rows,
            'last_modified': last_modified_date
        })
        
    except Exception as e:
        error_msg = f"Error processing Google Sheets data: {str(e)}"
        current_app.logger.error(error_msg)
        current_app.logger.exception("Full traceback:")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@boat_service_bp.route('/boat/process/<container_number>', methods=['POST'])
def process_container(container_number):
    """Process a specific container and generate CSV files"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        # Scan Dropbox for the container file
        container_files = scan_dropbox_containers()
        
        if container_number not in container_files:
            return jsonify({'error': f'Container {container_number} not found in Dropbox'}), 404
        
        # Process the container
        result = process_container_file(container_files[container_number], container_number)
        
        if result['success']:
            # Update Google Sheets column O
            batch_update_column_o([container_number])
            
            return jsonify({
                'success': True,
                'message': f'Successfully processed {container_number}',
                'files': {
                    'styles': result['styles_file'],
                    'po': result['po_file']
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            }), 500
    
    except Exception as e:
        current_app.logger.error(f"Error processing container {container_number}: {e}")
        return jsonify({'error': str(e)}), 500

@boat_service_bp.route('/boat/download/<container_number>/<file_type>')
def download_container_csv(container_number, file_type):
    """Download generated CSV files or original Excel file for a container"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    try:
        if file_type == 'original':
            # Download original Excel file from Dropbox
            container_files = scan_dropbox_containers()
            
            if container_number not in container_files:
                return f"Original file not found for {container_number}", 404
            
            file_path = container_files[container_number]
            filename = os.path.basename(file_path)
            
            return send_file(file_path, as_attachment=True, download_name=filename)
            
        elif file_type == 'styles':
            filename = f"{container_number}-styles.csv"
        elif file_type == 'po':
            filename = f"{container_number}-PO.csv"
        else:
            return "Invalid file type", 400
        
        file_path = os.path.join(OUTPUT_DIR, filename)
        
        if not os.path.exists(file_path):
            return f"File not found: {filename}", 404
        
        return send_file(file_path, as_attachment=True, download_name=filename)
    
    except Exception as e:
        current_app.logger.error(f"Error downloading file: {e}")
        return str(e), 500