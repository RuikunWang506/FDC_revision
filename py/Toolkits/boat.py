import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for, current_app, jsonify, send_file, request
import pyodbc
import os
from datetime import datetime
import logging
import re
import glob
from io import StringIO
import gspread
from google.oauth2.service_account import Credentials
import requests
from datetime import timezone, timedelta
import imaplib
import email
from email.header import decode_header

# Terminal49 configuration
TERMINAL49_API_URL = "https://api.terminal49.com/v2/tracking_requests"
TERMINAL49_API_KEY = "8zoazZdxmyQS1zuNSKXDuGnm"
TERMINAL49_CUSTOMER_ID = "f7cb530a-9e60-412c-a5bc-205a2f34ba54"  # Default customer ID

boat_service_bp = Blueprint('boat_service', __name__, template_folder='../templates')

# Google Sheets configuration
GOOGLE_SHEETS_URL = "https://docs.google.com/spreadsheets/d/14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE/export?format=csv&gid=0"
GOOGLE_SHEET_ID = "14I8AvXbZM1_O7RXg8EdOSOpFxQ1aU6tGftjhRruKNhE"
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), '..', '..', 'credentials', 'service-account.json')
DROPBOX_CONTAINER_PATH = r"C:\Users\stream\Dropbox\Pictures-China (1)\container catalog\Container\container INFO"
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

def get_filtered_order_summary_by_skus(full_sku_list):
    """Return pending/active ROSS/BURLINGTON order summary keyed by full SKU."""
    if not full_sku_list:
        return {}

    normalized_skus = sorted({str(sku).strip().upper() for sku in full_sku_list if str(sku).strip()})
    if not normalized_skus:
        return {}

    sku_summary_map = {}
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        placeholders = ','.join(['?' for _ in normalized_skus])
        query = f"""
            SELECT
                UPPER(LTRIM(RTRIM(ol.prod_cd))) AS sku,
                LTRIM(RTRIM(ISNULL(c.cus_nm, ''))) AS customer_name,
                ol.ord_num AS order_id,
                CAST(ISNULL(ol.order_qty, 0) AS INT) AS order_qty,
                CASE
                    WHEN o.ship_dt IS NULL OR o.ship_dt = 0 THEN ''
                    ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                         RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                         CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', o.ship_dt) - 81088, '20230101')) AS VARCHAR)
                END AS ship_date
            FROM dbo.ord_log ol
            JOIN dbo.customer c ON ol.cus_id = c.cus_id
            JOIN dbo.orders o ON ol.ord_num = o.ord_num
            WHERE UPPER(LTRIM(RTRIM(ol.prod_cd))) IN ({placeholders})
              AND (
                    UPPER(c.cus_nm) LIKE '%ROSS%'
                    OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACTORY%'
                    OR UPPER(c.cus_nm) LIKE '%BURLINGTON COAT FACOTORY%'
              )
              AND (
                    (o.INVS_NUM IS NULL OR CAST(o.INVS_NUM AS INT) = 0)
                    OR (o.can_dt IS NULL OR CAST(o.can_dt AS INT) = 0)
              )
            ORDER BY UPPER(LTRIM(RTRIM(ol.prod_cd))), ol.ord_num
        """

        cursor.execute(query, normalized_skus)
        rows = cursor.fetchall()

        for row in rows:
            sku = str(row[0]).strip().upper() if row[0] is not None else ''
            if not sku:
                continue

            customer_name = str(row[1]).strip() if row[1] is not None else ''
            if customer_name.upper() in {'BURLINGTON COAT FACTORY', 'BURLINGTON COAT FACOTORY'}:
                customer_name = 'burlington'
            order_id = str(row[2]).strip() if row[2] is not None else ''
            order_qty = int(row[3] or 0)
            ship_date = str(row[4]).strip() if row[4] is not None else ''

            if sku not in sku_summary_map:
                sku_summary_map[sku] = {
                    'qty_in_order': 0,
                    'customer_name': [],
                    'order_id': [],
                    'order_qty': [],
                    'ship_date': []
                }

            sku_summary_map[sku]['qty_in_order'] += order_qty
            if customer_name and customer_name not in sku_summary_map[sku]['customer_name']:
                sku_summary_map[sku]['customer_name'].append(customer_name)
            if order_id and order_id not in sku_summary_map[sku]['order_id']:
                sku_summary_map[sku]['order_id'].append(order_id)
            if order_qty > 0:
                sku_summary_map[sku]['order_qty'].append(str(order_qty))
            if ship_date and ship_date not in sku_summary_map[sku]['ship_date']:
                sku_summary_map[sku]['ship_date'].append(ship_date)

    except Exception as e:
        current_app.logger.warning(f"Failed to fetch Qty in Order data for packslip: {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    for sku, summary in sku_summary_map.items():
        sku_summary_map[sku] = {
            'qty_in_order': summary['qty_in_order'],
            'customer_name': ', '.join(summary['customer_name']),
            'order_id': ', '.join(summary['order_id']),
            'order_qty': ', '.join(summary['order_qty']),
            'ship_date': ', '.join(summary['ship_date'])
        }

    return sku_summary_map

def get_los_angeles_time(dt_str):
    """Convert ISO datetime string to Los Angeles time"""
    if not dt_str:
        return ''
    try:
        # Parse the ISO datetime string (assuming UTC)
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        
        # Los Angeles is UTC-8 (PST) or UTC-7 (PDT)
        # For simplicity, we'll use UTC-8
        la_tz = timezone(timedelta(hours=-8))
        la_time = dt.astimezone(la_tz)
        
        return la_time.strftime('%m/%d/%Y %H:%M')
    except Exception as e:
        # Log error if in Flask context, otherwise just return original
        try:
            current_app.logger.error(f"Error converting time {dt_str}: {e}")
        except RuntimeError:
            # Outside Flask context, just print to console
            print(f"Error converting time {dt_str}: {e}")
        return dt_str

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
                    # Update column O with timestamp
                    updates.append({
                        'range': f'O{sheet_row}',  # Column O
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
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error updating received dates in Google Sheets: {e}")
        return False

def update_last_updated_for_all_containers(container_list, fetch_timestamp):
    """Update the Last Updated column (T) for all containers with the fetch timestamp"""
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
                if container in container_list:
                    # Row index in sheet (1-based, +1 because enumerate is 0-based)
                    sheet_row = row_idx + 1
                    
                    # Update column T = Last Updated
                    updates.append({
                        'range': f'T{sheet_row}',  # Column T = Last Updated
                        'values': [[fetch_timestamp]]
                    })
        
        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True
        
        return False
        
    except Exception as e:
        current_app.logger.error(f"Error updating Last Updated for all containers: {e}")
        return False

def update_tracking_data_in_sheets(container_tracking_dict):
    """Update tracking data in Google Sheets for containers with shipment data.
    The function will try to locate header columns by name (case-insensitive)
    and write the fields into the matching columns. If headers cannot be
    found, it falls back to the legacy columns P-T.
    """
    try:
        client = get_sheets_client()
        if not client:
            return False

        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        # Get all data at once (single read)
        all_data = sheet.get_all_values()

        if not all_data:
            return False

        # Build header lookup from the first row (case-insensitive)
        header_row = all_data[0]
        header_map = {str(h).strip().lower(): idx for idx, h in enumerate(header_row) if h is not None}

        # Helper: convert 1-based column index to A1 letter (1 -> A)
        def idx_to_col(col_idx_1based):
            letters = ''
            while col_idx_1based > 0:
                col_idx_1based, rem = divmod(col_idx_1based - 1, 26)
                letters = chr(65 + rem) + letters
            return letters

        # Helper: find header index by several candidate names, fallback to default 0-based index
        def find_idx(candidates, fallback_0based):
            for c in candidates:
                if c in header_map:
                    return header_map[c]
            return fallback_0based

        # Preferred header keys (lowercase)
        shipping_line_idx = find_idx(['shipping line', 'shipping_line', 'shippingline', 'shipping line name'], 15)  # default P (0-based 15)
        eta_idx = find_idx(['auto eta', 'eta', 'dennis eta'], 16)  # default Q
        ata_idx = find_idx(['ata', 'pod_ata_at'], 17)  # default R
        voyage_idx = find_idx(['voyage', 'voyage number'], 18)  # default S
        last_updated_idx = find_idx(['last updated', 'last_updated', 'written time'], 19)  # default T

        updates = []

        # Iterate all rows and match container (Column A)
        for row_idx, row in enumerate(all_data):
            if not row or len(row) < 1:
                continue
            container = str(row[0]).strip()
            if not container:
                continue

            if container in container_tracking_dict:
                sheet_row = row_idx + 1
                tracking_data = container_tracking_dict.get(container, {})

                shipping_line = tracking_data.get('shipping_line', '')
                if shipping_line == 'Orient Overseas Container Line':
                    shipping_line = 'OOCL'

                # Prepare updates mapping to discovered columns (convert to 1-based for idx_to_col)
                updates.append({
                    'range': f"{idx_to_col(shipping_line_idx+1)}{sheet_row}",
                    'values': [[shipping_line]]
                })

                updates.append({
                    'range': f"{idx_to_col(eta_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('real_eta', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(ata_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('ata', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(voyage_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('voyage', '')]]
                })

                updates.append({
                    'range': f"{idx_to_col(last_updated_idx+1)}{sheet_row}",
                    'values': [[tracking_data.get('last_updated', '')]]
                })

        # Batch update all cells at once
        if updates:
            sheet.batch_update(updates)
            return True

        return False

    except Exception as e:
        current_app.logger.error(f"Error updating tracking data in Google Sheets: {e}")
        return False

def add_multiple_containers_to_sheets(container_list):
    """Add multiple new containers to Google Sheets as new rows"""
    try:
        client = get_sheets_client()
        if not client:
            return []
            
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all existing containers to avoid duplicates
        all_data = sheet.get_all_values()
        existing_containers = set()
        for row in all_data:
            if row and row[0]:  # Column A has container number
                existing_containers.add(row[0])
        
        # Specific containers mentioned by user
        specific_containers = {'F25131', 'F25141', 'F25132', 'F25143', 'F25142'}
        
        # Filter containers - skip F versions if FT versions exist
        containers_to_add = []
        for container_num in container_list:
            if not container_num:
                continue
            
            # Ignore L-prefix containers
            if container_num.startswith('L'):
                continue
            
            # Check if container already exists
            if container_num in existing_containers:
                continue
            
            # Special logic for specific F-prefix containers: skip F versions if FT versions exist
            if container_num in specific_containers:
                # Check if FT version exists (F25131 -> FT25131)
                if container_num.startswith('F') and len(container_num) > 1:
                    ft_version = 'FT' + container_num[1:]  # F25131 -> FT25131
                    if ft_version in existing_containers:
                        continue
            
            containers_to_add.append(container_num)
        
        
        if not containers_to_add:
            return []
        
        # Find the last row with data to append after
        last_row_with_data = 0
        for i, row in enumerate(all_data):
            if any(cell.strip() for cell in row if cell):
                last_row_with_data = i + 1
        
        
        # Get container files for reading Invoice data and tracking info
        container_files = scan_dropbox_containers()
        
        # Prepare new rows - each container gets a new row with full data
        new_rows = []
        for container_num in containers_to_add:
            # Create row with basic structure
            new_row = [container_num, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '']  # Extend to at least column T
            
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
                            actual_container_number = match.group(1)
                            new_row[2] = actual_container_number  # Column C (index 2) = Invoice Container #
                except Exception as e:
                    current_app.logger.error(f"Error reading Invoice for {container_num}: {e}")
                    actual_container_number = None
            
            # Set timestamp in column O
            new_row[14] = datetime.now().strftime('%m/%d/%Y %H:%M:%S')  # Column O (index 14)
            
            new_rows.append(new_row)
        
        # Add all new rows at once using explicit ranges
        if new_rows:
            # Find the next available row (after the last row with data)
            next_row = last_row_with_data + 1
            
            # Write all rows in a single batch update to avoid API quota issues
            if len(new_rows) == 1:
                range_str = f'A{next_row}:T{next_row}'
                sheet.update(range_str, new_rows)
            else:
                # Multiple rows - use range spanning all rows
                range_str = f'A{next_row}:T{next_row + len(new_rows) - 1}'
                sheet.update(range_str, new_rows)
            
            current_app.logger.info(f"Added {len(new_rows)} containers to Google Sheets in a single batch")
            return containers_to_add
            
        return []
        
    except Exception as e:
        current_app.logger.error(f"Error adding containers to Google Sheets: {e}")
        return []

def scan_dropbox_containers():
    """Scan Dropbox folder for all container Excel files and extract container numbers"""
    container_files = {}
    
    try:
        # Only scan 2026年 folder and its subfolders (month folders)
        year_2026_folder = os.path.join(DROPBOX_CONTAINER_PATH, '2026年')
        
        # Build scan locations: root + 2026年 folder + month subfolders within 2026年
        scan_locations = [DROPBOX_CONTAINER_PATH]
        
        # Add 2026年 folder if it exists
        if os.path.isdir(year_2026_folder):
            scan_locations.append(year_2026_folder)
            # Scan month subfolders within 2026年 (e.g., 1月, 2月, etc.)
            month_folders = glob.glob(os.path.join(year_2026_folder, '*'))
            scan_locations.extend([f for f in month_folders if os.path.isdir(f)])
        
        for location in scan_locations:
            # Get all Excel files in this location
            excel_files = glob.glob(os.path.join(location, '*.xlsx'))
            
            for file_path in excel_files:
                filename = os.path.basename(file_path)
                # Extract container number from filename
                # Handle both FT and F versions - store under BOTH keys
                # Priority: F version takes precedence over FT version
                # Ignore L-prefix containers
                
                # First try matching FT pattern (e.g., "FT26119 Packing List...")
                match = re.match(r'^FT(\d+)', filename)
                if match:
                    ft_number = match.group(0)  # Full FT26119
                    f_number = 'F' + match.group(1)  # Convert to F26119
                    # Store under both FT and F keys (only if not already present)
                    if ft_number not in container_files:
                        container_files[ft_number] = file_path
                    if f_number not in container_files:
                        container_files[f_number] = file_path
                    continue
                
                # Then try matching F pattern (e.g., "F26119 Packing List...")
                match = re.match(r'^F(\d+)', filename)
                if match:
                    container_number = match.group(0)  # Get the full F25xxx or F26xxx
                    # F versions take priority - always overwrite
                    container_files[container_number] = file_path
        
        return container_files
    
    except Exception as e:
        current_app.logger.error(f"Error scanning Dropbox containers: {e}")
        return {}

def list_terminal49_shipments():
    """List all shipments from Terminal49 API"""
    try:
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get("https://api.terminal49.com/v2/shipments", headers=headers)
        
        # Log the raw response regardless of status code

        
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            current_app.logger.error(f"Terminal49 API error listing shipments: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        current_app.logger.error(f"Error listing Terminal49 shipments: {e}")
        return None

def find_existing_tracking_request(container_number):
    """Find existing tracking request for a container number by checking shipments"""
    try:
        shipments_data = get_terminal49_shipments()
        if not shipments_data or 'data' not in shipments_data:
            return None

        shipments = shipments_data['data']

        for shipment in shipments:
            attributes = shipment.get('attributes', {})
            bill_of_lading = attributes.get('bill_of_lading_number', '')

            # Check if this shipment matches our container number
            if bill_of_lading == container_number:
                # Get the tracking request relationship if available
                relationships = shipment.get('relationships', {})
                tracking_request = relationships.get('tracking_request', {})
                tracking_request_data = tracking_request.get('data', {})

                if tracking_request_data:
                    tracking_id = tracking_request_data.get('id')
                    return tracking_id

        return None

    except Exception as e:
        current_app.logger.error(f"Error finding existing tracking request for {container_number}: {e}")
        return None

def create_terminal49_tracking_request(container_number, scac=None):
    """Create a tracking request with Terminal49 and return the tracking ID"""
    try:
        import threading
        import queue
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        # Send two concurrent requests - one for OOCL and one for EGLV
        scac_codes = ["OOLU", "EGLV"]
        results_queue = queue.Queue()
        
        def send_request(scac_code):
            """Send a single tracking request for a SCAC code"""
            try:
                payload = {
                    "data": {
                        "attributes": {
                            "request_type": "container",
                            "request_number": container_number,
                            "ref_numbers": [],  # Can be populated with PO/BL numbers if available
                            "shipment_tags": [],
                            "scac": scac_code
                        },
                        "type": "tracking_request"
                    }
                }

                # Retry logic for 500 errors
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        response = requests.post(TERMINAL49_API_URL, headers=headers, json=payload, timeout=30)

                        if response.status_code == 201:
                            data = response.json()
                            tracking_id = data.get('data', {}).get('id')
                            results_queue.put(('success', scac_code, tracking_id))
                            return
                        elif response.status_code == 422:
                            # Check if it's a duplicate error with existing tracking request
                            error_data = response.json()
                            errors = error_data.get('errors', [])

                            for error in errors:
                                if error.get('code') == 'duplicate':
                                    # Extract the existing tracking request ID from meta
                                    meta = error.get('meta', {})
                                    existing_tracking_id = meta.get('tracking_request_id')
                                    if existing_tracking_id:
                                        results_queue.put(('duplicate', scac_code, existing_tracking_id))
                                        return

                            # Check if it's a SCAC-related error
                            scac_error = any('scac' in str(error.get('source', {}).get('pointer', '')) for error in errors)
                            container_error = any('container' in str(error.get('detail', '')).lower() or 'number' in str(error.get('detail', '')).lower() for error in errors)

                            if scac_error or container_error:
                                results_queue.put(('scac_error', scac_code, None))
                                return
                            else:
                                # Other validation error
                                results_queue.put(('validation_error', scac_code, None))
                                return
                        elif response.status_code == 404:
                            results_queue.put(('not_found', scac_code, None))
                            return
                        elif response.status_code == 500:
                            # Server errors may be temporary - use more aggressive retry
                            server_max_retries = 3
                            if retry < server_max_retries:
                                current_app.logger.warning(f"Terminal49 server error (500) for {container_number} with SCAC {scac_code}, retrying ({retry + 1}/{server_max_retries})")
                                import time
                                time.sleep(3)  # Longer wait for server errors
                                continue
                            else:
                                results_queue.put(('server_error', scac_code, None))
                                return
                        elif response.status_code == 429:
                            # Rate limited
                            current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}, waiting before retry")
                            import time
                            time.sleep(5)
                            if retry < max_retries:
                                continue
                            else:
                                results_queue.put(('rate_limited', scac_code, None))
                                return
                        else:
                            results_queue.put(('api_error', scac_code, response.status_code))
                            return

                    except requests.exceptions.Timeout:
                        if retry < max_retries:
                            current_app.logger.warning(f"Timeout error for {container_number} with SCAC {scac_code}, retrying ({retry + 1}/{max_retries})")
                            continue
                        else:
                            results_queue.put(('timeout', scac_code, None))
                            return
                    except requests.exceptions.RequestException as e:
                        results_queue.put(('request_error', scac_code, str(e)))
                        return
                        
            except Exception as e:
                results_queue.put(('exception', scac_code, str(e)))
        
        # Start both requests concurrently
        threads = []
        for scac_code in scac_codes:
            thread = threading.Thread(target=send_request, args=(scac_code,))
            threads.append(thread)
            thread.start()
        
        # Wait for both threads to complete
        for thread in threads:
            thread.join()
        
        # Process results
        results = []
        while not results_queue.empty():
            results.append(results_queue.get())
        
        # Look for successful results first
        for result_type, scac_code, data in results:
            if result_type == 'success':
                return data
            elif result_type == 'duplicate':
                return data
        
        # If no success, log the failures
        for result_type, scac_code, data in results:
            if result_type == 'scac_error':
                current_app.logger.warning(f"SCAC {scac_code} not recognized for {container_number}")
            elif result_type == 'not_found':
                current_app.logger.warning(f"Container {container_number} not found for SCAC {scac_code}")
            elif result_type == 'validation_error':
                current_app.logger.error(f"Terminal49 validation error for {container_number} with SCAC {scac_code}")
            elif result_type == 'server_error':
                current_app.logger.error(f"Terminal49 server error for {container_number} with SCAC {scac_code}")
            elif result_type == 'rate_limited':
                current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}")
            elif result_type == 'timeout':
                current_app.logger.error(f"Timeout error for {container_number} with SCAC {scac_code}")
            elif result_type == 'request_error':
                current_app.logger.error(f"Request error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'api_error':
                current_app.logger.error(f"Terminal49 API error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'exception':
                current_app.logger.error(f"Exception for {container_number} with SCAC {scac_code}: {data}")
        
        # If we get here, all requests failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None

        # If we get here, all SCACs failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None
            
    except Exception as e:
        current_app.logger.error(f"Error creating Terminal49 tracking request for {container_number}: {e}")
        return None


def create_terminal49_tracking_request(container_number, scac=None):
    """Create a tracking request with Terminal49 and return the tracking ID"""
    try:
        import queue
        import threading
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        # Send two concurrent requests - one for OOCL and one for EGLV
        scac_codes = ["OOLU", "EGLV"]
        results_queue = queue.Queue()
        
        def send_request(scac_code):
            """Send a single tracking request for a SCAC code"""
            try:
                payload = {
                    "data": {
                        "attributes": {
                            "request_type": "container",
                            "request_number": container_number,
                            "ref_numbers": [],
                            "shipment_tags": [],
                            "scac": scac_code
                        },
                        "type": "tracking_request"
                    }
                }

                # Retry logic for 500 errors
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        response = requests.post(TERMINAL49_API_URL, headers=headers, json=payload, timeout=30)

                        if response.status_code == 201:
                            data = response.json()
                            tracking_id = data.get('data', {}).get('id')
                            results_queue.put(('success', scac_code, tracking_id))
                            return
                        elif response.status_code == 422:
                            error_data = response.json()
                            errors = error_data.get('errors', [])
                            for error in errors:
                                if 'duplicate' in str(error).lower():
                                    tracking_id = error.get('detail', '').split('tracking_request/')[-1].split(' ')[0] if 'tracking_request/' in str(error) else None
                                    if tracking_id:
                                        results_queue.put(('duplicate', scac_code, tracking_id))
                                        return
                            results_queue.put(('validation_error', scac_code, str(errors)))
                        elif response.status_code == 404:
                            results_queue.put(('not_found', scac_code, f'Container {container_number} not found for SCAC {scac_code}'))
                        elif response.status_code == 500:
                            if retry < max_retries:
                                import time
                                time.sleep(2 ** retry)
                                continue
                            results_queue.put(('server_error', scac_code, f'Server error (attempt {retry + 1}/{max_retries + 1})'))
                        elif response.status_code == 429:
                            wait_time = int(response.headers.get('Retry-After', 60))
                            if retry < max_retries:
                                import time
                                time.sleep(min(wait_time, 5))
                                continue
                            results_queue.put(('rate_limited', scac_code, f'Rate limited, retry after {wait_time}s'))
                        else:
                            results_queue.put(('api_error', scac_code, f'API error: {response.status_code} - {response.text}'))

                    except requests.exceptions.Timeout:
                        if retry < max_retries:
                            import time
                            time.sleep(2 ** retry)
                            continue
                        results_queue.put(('timeout', scac_code, 'Request timeout'))
                    except requests.exceptions.RequestException as e:
                        results_queue.put(('request_error', scac_code, str(e)))
                        
            except Exception as e:
                results_queue.put(('exception', scac_code, str(e)))
        
        # Start both requests concurrently
        threads = []
        for scac_code in scac_codes:
            thread = threading.Thread(target=send_request, args=(scac_code,))
            threads.append(thread)
            thread.start()
        
        # Wait for both threads to complete
        for thread in threads:
            thread.join()
        
        # Process results
        results = []
        while not results_queue.empty():
            results.append(results_queue.get())
        
        # Look for successful results first
        for result_type, scac_code, data in results:
            if result_type == 'success':
                return data
            elif result_type == 'duplicate':
                return data
        
        # If no success, log the failures
        for result_type, scac_code, data in results:
            if result_type == 'scac_error':
                current_app.logger.warning(f"SCAC {scac_code} not recognized for {container_number}")
            elif result_type == 'not_found':
                current_app.logger.warning(f"Container {container_number} not found for SCAC {scac_code}")
            elif result_type == 'validation_error':
                current_app.logger.error(f"Terminal49 validation error for {container_number} with SCAC {scac_code}")
            elif result_type == 'server_error':
                current_app.logger.error(f"Terminal49 server error for {container_number} with SCAC {scac_code}")
            elif result_type == 'rate_limited':
                current_app.logger.warning(f"Rate limited by Terminal49 for {container_number}")
            elif result_type == 'timeout':
                current_app.logger.error(f"Timeout error for {container_number} with SCAC {scac_code}")
            elif result_type == 'request_error':
                current_app.logger.error(f"Request error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'api_error':
                current_app.logger.error(f"Terminal49 API error for {container_number} with SCAC {scac_code}: {data}")
            elif result_type == 'exception':
                current_app.logger.error(f"Exception for {container_number} with SCAC {scac_code}: {data}")
        
        # If we get here, all requests failed
        current_app.logger.error(f"All SCAC options failed for container {container_number}")
        return None
            
    except Exception as e:
        current_app.logger.error(f"Error creating Terminal49 tracking request for {container_number}: {e}")
        return None

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
    last_modified_date = "Loading...takes around 9 seconds"
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
            current_containers = set(df_current['FDC container'].dropna().tolist())
            containers_in_dropbox = set(container_files.keys())
            new_containers = containers_in_dropbox - current_containers

            # Filter out FT-prefixed containers if F-prefixed version exists
            filtered_new_containers = set()
            for container in new_containers:
                if container.startswith('FT'):
                    # FT26132 -> F26132
                    f_version = 'F' + container[2:]
                    # Only add FT if F version doesn't exist in new_containers or current_containers
                    if f_version not in new_containers and f_version not in current_containers:
                        filtered_new_containers.add(container)
                else:
                    # Always add F-prefixed or other containers
                    filtered_new_containers.add(container)

            # Batch add new containers to Google Sheets first
            if filtered_new_containers:
                # Sort containers by container number (small to large) before adding
                sorted_new_containers = sorted(list(filtered_new_containers))
                added_containers = add_multiple_containers_to_sheets(sorted_new_containers)

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

        # Store the fetch timestamp for Last Updated column
        fetch_timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Map columns directly by name - explicitly ignore PQRS (Shipping Line, Auto ETA, Auto Arrival, Voyage)
        # Only read: Container, BOL, Memo, Exam, Dennis ETA (Column E), Internal RCD (Column F)
        df_cleaned = df[['FDC container', 'BOL NUM', 'Memo', 'Exam', 'ETA', 'ATA', 'RCD']].copy()
        
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
        
        # Rename and reorder columns
        # NOTE: ETA = Dennis ETA (user input), ATA = Internal RCD (actual arrival)
        df_cleaned = df_cleaned.rename(columns={'FDC container': 'container', 'BOL NUM': 'link', 'ETA': 'Dennis ETA', 'ATA': 'pod_ata_at'})
        df_cleaned = df_cleaned[['container', 'link', 'Dennis ETA', 'pod_ata_at', 'RCD', 'MEMO']]
        

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

        df_cleaned['Dennis ETA'] = df_cleaned['Dennis ETA'].apply(convert_date)
        df_cleaned['pod_ata_at'] = df_cleaned['pod_ata_at'].apply(convert_date)
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
                    df_cleaned.at[index, 'Status'] = '✔️827✔️'
                    # Hide process button and CSV buttons for landed containers
                    df_cleaned.at[index, 'can_process'] = False
                    df_cleaned.at[index, 'has_styles_csv'] = False
                    df_cleaned.at[index, 'has_po_csv'] = False
                    # Store received date for Google Sheets update
                    received_date = date_lookup.get(container_raw, '')
                    if received_date:
                        landed_containers_with_dates[container_raw] = received_date
                elif rcvd_qty is not None and rcvd_qty == 0:
                    df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
                    # Hide process button and CSV buttons for ocean containers
                    df_cleaned.at[index, 'can_process'] = False
                    df_cleaned.at[index, 'has_styles_csv'] = False
                    df_cleaned.at[index, 'has_po_csv'] = False
                else:
                    df_cleaned.at[index, 'Status'] = '🔄upload🔄'
                    # Keep can_process as set earlier (based on Dropbox file existence)
                    # Keep has_styles_csv and has_po_csv as set earlier (based on file existence)
        
        # Close database connection
        conn.close()
        
        # Update Google Sheets with received dates for landed containers
        # Only update if there are new landed containers (not already updated)
        if landed_containers_with_dates:
            # DISABLED: This causes API writes on every page load
            # Re-enable only if you need to sync RCD dates from OMS to Google Sheets
            # update_received_dates_in_sheets(landed_containers_with_dates)
            pass

        # Drop the 'container_raw' column before rendering (keep the other metadata columns)
        df_cleaned = df_cleaned.drop('container_raw', axis=1)

        # Replace NaN values with empty strings for JSON serialization
        df_cleaned = df_cleaned.fillna('')
        
        # Set Last Updated timestamp for all containers to current fetch time
        df_cleaned['last_updated'] = fetch_timestamp
        
        # Terminal49 API is write-only - we only create tracking requests, cannot read data
        # Status determined by local Google Sheets data only
        # IMPORTANT: Do not overwrite containers already marked as landed ('✔️827✔️')
        for index, row in df_cleaned.iterrows():
            current_status = str(row.get('Status', '')).strip()
            # Preserve landed status - do not overwrite
            if current_status == '✔️827✔️':
                continue

            # Use only local ETA from Google Sheets (Dennis ETA column)
            internal_eta = str(row.get('Dennis ETA', '')).strip()
            internal_rcd = str(row.get('RCD', '')).strip()      # RCD from sheet

            if internal_eta and not internal_rcd:
                # ETA present and no RCD → ocean
                df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
            else:
                # Keep existing status (upload, landed, etc.)
                pass
        
        # Ensure process / styles / po flags only show for upload status
        for index, row in df_cleaned.iterrows():
            status = str(row.get('Status', '')).strip()
            if status != '🔄upload🔄':
                df_cleaned.at[index, 'can_process'] = False
                df_cleaned.at[index, 'has_styles_csv'] = False
                df_cleaned.at[index, 'has_po_csv'] = False
            else:
                # keep previously-determined booleans (based on file existence)
                df_cleaned.at[index, 'can_process'] = bool(row.get('can_process', False))
                df_cleaned.at[index, 'has_styles_csv'] = bool(row.get('has_styles_csv', False))
                df_cleaned.at[index, 'has_po_csv'] = bool(row.get('has_po_csv', False))

        # Fetch email counts for port containers
        port_containers = df_cleaned[df_cleaned['Status'] == '⚓port⚓']
        
        # Initialize email_count column for all containers
        df_cleaned['email_count'] = 0
        
        if not port_containers.empty:
            try:
                # Email credentials
                EMAIL_USER = 'yihao@fdcus.com'
                EMAIL_PASS = 'Fdc2640$$'
                IMAP_SERVER = 'imap.fatcow.com'
                IMAP_PORT = 993

                # Connect to IMAP server
                mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
                mail.login(EMAIL_USER, EMAIL_PASS)
                mail.select('inbox')

                # Calculate date one month ago
                one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%d-%b-%Y')

                # Get email counts for each port container
                for index, row in port_containers.iterrows():
                    bol_num = str(row.get('link', '')).strip()
                    if bol_num and bol_num != 'nan':
                        try:
                            # Search for emails containing the BOL number
                            search_criteria = f'(OR SUBJECT "{bol_num}" BODY "{bol_num}" SINCE {one_month_ago})'
                            status, messages = mail.search(None, search_criteria)
                            
                            email_count = 0
                            if status == 'OK' and messages[0]:
                                message_nums = messages[0].split()
                                email_count = len(message_nums)
                            
                            df_cleaned.at[index, 'email_count'] = int(email_count)
                        except Exception as e:
                            current_app.logger.error(f"Error fetching email count for BOL {bol_num}: {e}")
                            df_cleaned.at[index, 'email_count'] = 0
                    else:
                        df_cleaned.at[index, 'email_count'] = 0

                mail.logout()
            except Exception as e:
                current_app.logger.error(f"Error connecting to email server: {e}")
                # email_count is already initialized to 0 for all containers
        
        # Update Last Updated column for ALL containers with the fetch timestamp
        # DISABLED: This causes excessive API writes on every page load
        # update_last_updated_for_all_containers(df_cleaned['container'].tolist(), fetch_timestamp)
        
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
    current_user = session.get('username', 'unknown')
    
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

            # Keep an audit log of who processed which container.
            current_app.logger.info(f"User '{current_user}' processed container {container_number}")
            
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


@boat_service_bp.route('/boat/redo_oms/<container_number>', methods=['POST'])
def redo_oms(container_number):
    """Reset generated OMS CSV files so container can be reprocessed from original Excel."""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    normalized_container = str(container_number).strip().upper()

    try:
        styles_file = os.path.join(OUTPUT_DIR, f"{normalized_container}-styles.csv")
        po_file = os.path.join(OUTPUT_DIR, f"{normalized_container}-PO.csv")

        deleted_files = []
        for file_path in [styles_file, po_file]:
            if os.path.exists(file_path):
                os.remove(file_path)
                deleted_files.append(os.path.basename(file_path))

        return jsonify({
            'success': True,
            'message': f'Reset OMS files for {normalized_container}',
            'deleted_files': deleted_files
        })
    except Exception as e:
        current_app.logger.error(f"Error resetting OMS files for {normalized_container}: {e}")
        return jsonify({'error': str(e)}), 500


@boat_service_bp.route('/boat/download/<container_number>/<file_type>', methods=['GET'])
def download_boat_file(container_number, file_type):
    """Download original Excel or generated CSVs for a container.

    file_type: 'original' | 'styles' | 'po'
    """
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Styles and PO files are stored in OUTPUT_DIR
        if file_type == 'styles':
            filename = f"{container_number}-styles.csv"
            file_path = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True, download_name=filename, mimetype='text/csv')
            else:
                return (f"Styles CSV for {container_number} not found", 404)

        elif file_type == 'po':
            filename = f"{container_number}-PO.csv"
            file_path = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(file_path):
                return send_file(file_path, as_attachment=True, download_name=filename, mimetype='text/csv')
            else:
                return (f"PO CSV for {container_number} not found", 404)

        elif file_type == 'original':
            # Try to find the original Excel file in Dropbox
            container_files = scan_dropbox_containers()
            if container_number in container_files:
                orig_path = container_files[container_number]
                if os.path.exists(orig_path):
                    filename = os.path.basename(orig_path)
                    # Serve the original file (don't force CSV mimetype)
                    return send_file(orig_path, as_attachment=True, download_name=filename)
                else:
                    return (f"Original file for {container_number} not found on disk", 404)
            else:
                return (f"Original file for {container_number} not found in Dropbox scan", 404)

        else:
            return ("Invalid file type", 400)

    except Exception as e:
        current_app.logger.error(f"Error downloading file for {container_number} type {file_type}: {e}")
        return jsonify({'error': str(e)}), 500

@boat_service_bp.route('/boat/update_dennis_eta', methods=['POST'])
def update_dennis_eta():
    """Update Dennis ETA for a container in Google Sheets"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        container = data.get('container')
        dennis_eta = data.get('dennis_eta', '').strip()
        
        if not container:
            return jsonify({'success': False, 'error': 'Container number is required'}), 400
        
        # Get current data from Google Sheets to find the original value
        try:
            df = pd.read_csv(GOOGLE_SHEETS_URL)
            matching_row = df[df['FDC container'] == container]
            if matching_row.empty:
                return jsonify({'success': False, 'error': 'Container not found in Google Sheets'}), 404
            
            original_value = str(matching_row['ETA'].iloc[0]) if pd.notna(matching_row['ETA'].iloc[0]) else ''
            
        except Exception as e:
            current_app.logger.error(f"Error reading Google Sheets: {e}")
            return jsonify({'success': False, 'error': 'Failed to read current data'}), 500
        
        # Update Google Sheets
        client = get_sheets_client()
        if not client:
            return jsonify({'success': False, 'error': 'Failed to connect to Google Sheets'}), 500
        
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Get all data at once
        all_data = sheet.get_all_values()
        
        # Find the row for this container
        for row_idx, row in enumerate(all_data):
            if len(row) >= 1 and row[0] == container:
                sheet_row = row_idx + 1  # 1-based indexing
                
                # Update column E (ETA column, 0-indexed as 4)
                sheet.update(f'E{sheet_row}', [[dennis_eta]])
                
                current_app.logger.info(f"Updated Dennis ETA for container {container} from '{original_value}' to '{dennis_eta}'")
                return jsonify({
                    'success': True,
                    'original_value': original_value
                })
        
        return jsonify({'success': False, 'error': 'Container not found in sheet'}), 404
        
    except Exception as e:
        current_app.logger.error(f"Error updating Dennis ETA: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@boat_service_bp.route('/boat/emails/<bol_num>')
def get_bol_emails(bol_num):
    """Get emails related to BOL number from the last month"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Email credentials
        EMAIL_USER = 'yihao@fdcus.com'
        EMAIL_PASS = 'Fdc2640$$'
        IMAP_SERVER = 'imap.fatcow.com'
        IMAP_PORT = 993

        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASS)

        # Select inbox
        mail.select('inbox')

        # Calculate date one month ago
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%d-%b-%Y')

        # Search for emails containing the BOL number in subject or body
        search_criteria = f'(OR SUBJECT "{bol_num}" BODY "{bol_num}" SINCE {one_month_ago})'
        status, messages = mail.search(None, search_criteria)

        email_list = []

        if status == 'OK':
            message_nums = messages[0].split()

            # Get last 20 emails to avoid too many results
            for num in message_nums[-20:]:
                # Fetch email
                status, msg_data = mail.fetch(num, '(RFC822)')
                if status != 'OK':
                    continue

                # Parse email
                email_body = msg_data[0][1]
                email_message = email.message_from_bytes(email_body)

                # Decode subject
                subject, encoding = decode_header(email_message['Subject'])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or 'utf-8')

                # Get sender
                sender = email_message.get('From', '')

                # Get date
                date_str = email_message.get('Date', '')
                try:
                    # Parse email date
                    email_date = email.utils.parsedate_to_datetime(date_str)
                    formatted_date = email_date.strftime('%Y-%m-%d %H:%M')
                except:
                    formatted_date = date_str

                # Get body content
                body_content = ""
                if email_message.is_multipart():
                    for part in email_message.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                            except:
                                continue
                else:
                    try:
                        body_content = email_message.get_payload(decode=True).decode('utf-8', errors='ignore')
                    except:
                        body_content = "Unable to decode email content"

                # Truncate body if too long
                if len(body_content) > 1000:
                    body_content = body_content[:1000] + "..."

                email_list.append({
                    'subject': subject,
                    'sender': sender,
                    'date': formatted_date,
                    'body': body_content,
                    'message_id': num.decode()
                })

        mail.logout()

        # Sort by date descending
        email_list.sort(key=lambda x: x['date'], reverse=True)

        return render_template('boat_yihao_email.html',
                             bol_num=bol_num,
                             emails=email_list)

    except Exception as e:
        current_app.logger.error(f"Error fetching emails for BOL {bol_num}: {e}")
        return render_template('boat_yihao_email.html',
                             bol_num=bol_num,
                             emails=[],
                             error=str(e))

@boat_service_bp.route('/boat/emails/<bol_num>/count')
def get_bol_emails_count(bol_num):
    """Get email count for BOL number from the last month"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        # Email credentials
        EMAIL_USER = 'yihao@fdcus.com'
        EMAIL_PASS = 'Fdc2640$$'
        IMAP_SERVER = 'imap.fatcow.com'
        IMAP_PORT = 993

        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASS)

        # Select inbox
        mail.select('inbox')

        # Calculate date one month ago
        one_month_ago = (datetime.now() - timedelta(days=30)).strftime('%d-%b-%Y')

        # Search for emails containing the BOL number in subject or body
        search_criteria = f'(OR SUBJECT "{bol_num}" BODY "{bol_num}" SINCE {one_month_ago})'
        status, messages = mail.search(None, search_criteria)

        email_count = 0
        if status == 'OK' and messages[0]:
            message_nums = messages[0].split()
            email_count = len(message_nums)

        mail.logout()

        return jsonify({'count': email_count})

    except Exception as e:
        current_app.logger.error(f"Error fetching email count for BOL {bol_num}: {e}")
        return jsonify({'error': str(e), 'count': 0}), 500

@boat_service_bp.route('/boat/packslip/<container_number>', methods=['GET'])
def boat_packslip(container_number):
    """Generate and download packslip Excel for a container"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    try:
        # Get BOL NUM from Google Sheets
        bol_num = ''
        try:
            df_sheets = pd.read_csv(GOOGLE_SHEETS_URL)
            matching_row = df_sheets[df_sheets['FDC container'] == container_number]
            if not matching_row.empty:
                bol_num = str(matching_row['BOL NUM'].iloc[0]).strip() if pd.notna(matching_row['BOL NUM'].iloc[0]) else ''
        except Exception as e:
            current_app.logger.warning(f"Could not fetch BOL NUM from Google Sheets: {e}")
        
        # Get container files from Dropbox
        container_files = scan_dropbox_containers()
        
        if container_number not in container_files:
            return jsonify({'error': f'Container {container_number} not found in Dropbox'}), 404
        
        file_path = container_files[container_number]
        
        # Try different sheet names and header positions for packing list (similar to container.py)
        df_packing = None
        packing_sheet = None
        
        try:
            excel_file = pd.ExcelFile(file_path)
            
            # Prefer known packing list tabs, but keep a fallback for real-world file variations.
            packing_sheet = None
            preferred_sheet_names = ['placking list', 'packing list', 'system file']

            for preferred_name in preferred_sheet_names:
                for sheet_name in excel_file.sheet_names:
                    if str(sheet_name).strip().lower() == preferred_name:
                        packing_sheet = sheet_name
                        break
                if packing_sheet:
                    break

            if not packing_sheet:
                for sheet_name in excel_file.sheet_names:
                    normalized = str(sheet_name).strip().lower()
                    if 'pack' in normalized and 'list' in normalized:
                        packing_sheet = sheet_name
                        break
            
            if not packing_sheet:
                current_app.logger.error(f"No 'Placking list' sheet found. Available sheets: {excel_file.sheet_names}")
                return jsonify({'error': f'Could not find "Placking list" sheet in {container_number}. Available sheets: {excel_file.sheet_names}'}), 400
            
            # Read the placking list sheet with no header (raw data)
            df_packing = pd.read_excel(file_path, sheet_name=packing_sheet, header=None)
        
        except Exception as e:
            current_app.logger.error(f"Error reading Excel file sheets: {e}")
            return jsonify({'error': f'Error reading Excel file: {str(e)}'}), 400
        
        if df_packing is None:
            current_app.logger.error(f"Could not read packing list from {container_number}")
            return jsonify({'error': f'Could not read packing list from {container_number}'}), 400
        
        
        # Find the header row by scanning more columns for CTN/STYLE markers.
        header_row_idx = None
        for idx in range(min(20, len(df_packing))):
            row_str = ' '.join([str(df_packing.iloc[idx, i]).upper() for i in range(min(12, len(df_packing.columns)))])
            if 'CTN' in row_str and ('STYLE' in row_str or 'QTY' in row_str):
                header_row_idx = idx
                break
        
        if header_row_idx is None:
            current_app.logger.warning("Could not find header row with 'CTN'. Checking for data starting at row 0...")
            header_row_idx = -1  # Data starts at row 0, no real header
        
        
        # DEBUG: Dump more rows to understand structure
        for row_idx in range(min(10, len(df_packing))):
            row_data = []
            for col_idx in range(min(8, len(df_packing.columns))):
                val = df_packing.iloc[row_idx, col_idx]
                row_data.append(f"C{col_idx}:'{val}'")
        
        # Determine actual column positions for Packing List sheet
        # Packing list structure: CTN NO | STYLE NO | QTY/PER CTN | QTY/CTN | TOTAL | weights...
        ctn_no_col = 0      # CTN NO (e.g., "1-16", "17", "18-33")
        style_sku_col = 1   # STYLE NO (e.g., "EN1980 BK" - contains both style and sku)
        case_qty_col = 2    # QTY/PER CTN (quantity per carton)
        num_cartons_col = 3 # QTY/CTN (number of cartons)
        total_qty_col = 4   # TOTAL (total quantity)
        
        # DEBUG: Log the actual data range and column contents

        for row_idx in range(min(15, len(df_packing))):
            row_vals = []
            for col_idx in range(min(12, len(df_packing.columns))):
                val = df_packing.iloc[row_idx, col_idx]
                row_vals.append(f"[{col_idx}]:'{val}'")
        
        # Parse packing list data starting AFTER the header row (row 7, index 6)
        packslip_data = []
        
        start_row = header_row_idx + 1 if header_row_idx >= 0 else 0
        
        last_ctn_no = ''
        for idx in range(start_row, len(df_packing)):
            try:
                row = df_packing.iloc[idx]
                
                # Extract values using hardcoded column positions
                ctn_no = str(row.iloc[ctn_no_col]).strip() if pd.notna(row.iloc[ctn_no_col]) else ''
                style_sku = str(row.iloc[style_sku_col]).strip() if pd.notna(row.iloc[style_sku_col]) else ''
                total_qty_str = str(row.iloc[total_qty_col]).strip() if pd.notna(row.iloc[total_qty_col]) else '0'
                case_qty_str = str(row.iloc[case_qty_col]).strip() if pd.notna(row.iloc[case_qty_col]) else '0'
                num_cartons_str = str(row.iloc[num_cartons_col]).strip() if pd.notna(row.iloc[num_cartons_col]) else '0'
                

                # Skip rows where ctn_no is "/" (placeholder data)
                if ctn_no == '/':
                    continue

                # Many packing sheets use merged CTN cells; carry forward last non-empty CTN.
                if not ctn_no and last_ctn_no:
                    ctn_no = last_ctn_no
                elif ctn_no:
                    last_ctn_no = ctn_no
                
                # Skip empty rows
                if not style_sku:

                    continue
                
                # Skip header-like rows
                if any(x in style_sku.upper() for x in ['STYLE', 'ITEM', 'CONTAINER', 'CTN', 'NO']):
                    current_app.logger.info(f"  -> Skipping: header row")
                    continue
                
                # Parse style and SKU from "Style SKU" format (e.g., "EN1980 BK")
                parts = style_sku.split()
                if len(parts) >= 2:
                    style = parts[0]
                    sku = ' '.join(parts[1:])
                elif len(parts) == 1:
                    style = parts[0]
                    sku = ''
                else:
                    style = style_sku
                    sku = ''
                
                # Skip if no style or sku
                if not style or not sku:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: could not parse style/sku from '{style_sku}'")
                    continue
                
                # Convert to numbers
                try:
                    total_qty = int(float(total_qty_str)) if total_qty_str else 0
                    case_qty = int(float(case_qty_str)) if case_qty_str else 0
                    num_cartons = int(float(num_cartons_str)) if num_cartons_str else 0
                except:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: could not parse quantities")
                    continue
                
                # Skip rows with no quantity or case qty of 0
                if total_qty == 0 or case_qty == 0:
                    if idx <= start_row + 5:
                        current_app.logger.info(f"  -> Skipping: total_qty={total_qty}, case_qty={case_qty}")
                    continue
                
                # Calculate SKU Boxes: simple box count
                sku_boxes_count = total_qty // case_qty
                
                # Use the num_cartons value from the spreadsheet directly
                
                packslip_data.append({
                    'Style': style,
                    'SKU': sku,
                    'Full SKU': f"{style} {sku}".strip(),
                    'Quantity': total_qty,
                    'Case Qty': case_qty,
                    'SKU Boxes': sku_boxes_count,
                    'CTN No': ctn_no,
                    'Num Cartons': num_cartons,
                    'Total Boxes': None  # Will be filled in based on carton data
                })
            except Exception as e:
                current_app.logger.warning(f"Error parsing row {idx}: {e}")
                continue
        
        if not packslip_data:
            return jsonify({'error': f'No valid packing list data found in {container_number}'}), 400
        
        # Calculate Total Boxes per style = SUM of QTY/CTN column for each style
        style_carton_totals = {}  # {style: sum_of_qty_ctn}
        
        for item in packslip_data:
            style = item['Style']
            num_cartons = item.get('Num Cartons', 0)
            
            if style not in style_carton_totals:
                style_carton_totals[style] = 0
            style_carton_totals[style] += num_cartons
            current_app.logger.info(f"Style {style}: adding {num_cartons} cartons, total now = {style_carton_totals[style]}")
        
        # Fill in Total Boxes for each item using the summed carton count
        for item in packslip_data:
            style = item['Style']
            item['Total Boxes'] = style_carton_totals.get(style, 0)
            current_app.logger.info(f"Item {style} {item['SKU']}: Total Boxes = {item['Total Boxes']}")

        # Add order summary fields from pending/active ROSS/BURLINGTON orders.
        full_skus = [item.get('Full SKU', '') for item in packslip_data]
        order_summary_map = get_filtered_order_summary_by_skus(full_skus)
        for item in packslip_data:
            full_sku = str(item.get('Full SKU', '')).strip().upper()
            summary = order_summary_map.get(full_sku, {})
            item['Qty in Order'] = summary.get('qty_in_order', 0)
            item['customer name'] = summary.get('customer_name', '')
            item['order id'] = summary.get('order_id', '')
            item['order qty'] = summary.get('order_qty', '')
            item['ship date'] = summary.get('ship_date', '')

        # Consolidate duplicate rows with the same Style/SKU/Case Qty.
        # This collapses repeated lines (e.g. same SKU with identical case pack)
        # into one row by summing Quantity and SKU Boxes.
        consolidated_map = {}
        consolidated_order = []
        for item in packslip_data:
            key = (item.get('Style'), item.get('SKU'), item.get('Case Qty'))
            if key not in consolidated_map:
                consolidated_map[key] = dict(item)
                consolidated_order.append(key)
                continue

            existing = consolidated_map[key]
            existing['Quantity'] = int(existing.get('Quantity', 0) or 0) + int(item.get('Quantity', 0) or 0)
            existing['SKU Boxes'] = int(existing.get('SKU Boxes', 0) or 0) + int(item.get('SKU Boxes', 0) or 0)
            existing['Num Cartons'] = int(existing.get('Num Cartons', 0) or 0) + int(item.get('Num Cartons', 0) or 0)

        packslip_data = [consolidated_map[k] for k in consolidated_order]

        # Exclude sample-only styles: Total Boxes = 0 and Total Quantity = 1.
        style_totals_for_filter = {}
        style_boxes_for_filter = {}
        for item in packslip_data:
            style = item.get('Style')
            if not style:
                continue
            style_totals_for_filter[style] = style_totals_for_filter.get(style, 0) + int(item.get('Quantity', 0) or 0)
            if style not in style_boxes_for_filter:
                style_boxes_for_filter[style] = int(item.get('Total Boxes', 0) or 0)

        excluded_sample_styles = {
            style for style, total_qty in style_totals_for_filter.items()
            if style_boxes_for_filter.get(style, 0) == 0 and total_qty == 1
        }

        if excluded_sample_styles:
            current_app.logger.info(f"Excluding sample-only styles from packslip: {sorted(excluded_sample_styles)}")
            packslip_data = [item for item in packslip_data if item.get('Style') not in excluded_sample_styles]

        if not packslip_data:
            return jsonify({'error': f'No non-sample packing list data found in {container_number}'}), 400
        
        # Create output Excel file using openpyxl for merged cells
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Side, Font
        from openpyxl.worksheet.pagebreak import Break
        
        wb = Workbook()
        ws = wb.active
        ws.title = 'Packslip'
        
        # Set page setup for US Letter and fit to one page
        ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
        ws.page_margins.left = 0.5
        ws.page_margins.right = 0.5
        ws.page_margins.top = 0.5
        ws.page_margins.bottom = 0.5
        ws.print_options.horizontalCentered = False
        
        # Fit all columns to one page width
        ws.page_setup.fitToPage = True
        ws.page_setup.fitToHeight = 0
        ws.page_setup.fitToWidth = 1
        
        # Define border style
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # Define center alignment to match the reference workbook style.
        center_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        
        # Add header section (rows 1-2)
        # Row 1: Headers
        cell = ws.cell(row=1, column=2, value='Internal')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)
        
        cell = ws.cell(row=1, column=3, value='BOL NUM')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)
        
        # Row 2: Values
        cell = ws.cell(row=2, column=2, value=container_number)
        cell.alignment = center_alignment
        cell.border = thin_border
        
        cell = ws.cell(row=2, column=3, value=bol_num if bol_num else 'N/A')
        cell.alignment = center_alignment
        cell.border = thin_border
        
        # Row 3: Blank
        # Row 4: Summary headers
        cell = ws.cell(row=4, column=1, value='Style')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)
        
        cell = ws.cell(row=4, column=2, value='Total Boxes')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)
        
        cell = ws.cell(row=4, column=3, value='Total Quantity')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)

        cell = ws.cell(row=4, column=4, value='in order')
        cell.alignment = center_alignment
        cell.border = thin_border
        cell.font = Font(bold=True)
        
        # Add summary data (one row per unique style)
        summary_row = 5
        styles_summary = {}  # {style: {total_boxes, total_qty, has_in_order}}
        
        for item in packslip_data:
            style = item['Style']
            if style not in styles_summary:
                styles_summary[style] = {'total_boxes': item['Total Boxes'], 'total_qty': 0, 'has_in_order': False}
            styles_summary[style]['total_qty'] += item['Quantity']
            if int(item.get('Qty in Order', 0) or 0) > 0:
                styles_summary[style]['has_in_order'] = True
        
        for style, data in styles_summary.items():
            cell = ws.cell(row=summary_row, column=1, value=style)
            cell.alignment = center_alignment
            cell.border = thin_border
            
            cell = ws.cell(row=summary_row, column=2, value=data['total_boxes'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            cell = ws.cell(row=summary_row, column=3, value=data['total_qty'])
            cell.alignment = center_alignment
            cell.border = thin_border

            cell = ws.cell(row=summary_row, column=4, value='yes' if data.get('has_in_order') else '')
            cell.alignment = center_alignment
            cell.border = thin_border
            
            summary_row += 1
        
        # Add blank row before chart
        chart_start_row = summary_row + 1
        
        # Exact widths copied from reference workbook (A-J).
        ws.column_dimensions['A'].width = 12
        ws.column_dimensions['B'].width = 12
        ws.column_dimensions['C'].width = 14
        ws.column_dimensions['D'].width = 11
        ws.column_dimensions['E'].width = 11
        ws.column_dimensions['F'].width = 12
        ws.column_dimensions['G'].width = 22
        ws.column_dimensions['H'].width = 12
        ws.column_dimensions['I'].width = 12
        ws.column_dimensions['J'].width = 12
        
        # Add headers for packing list data chart
        # Match requested order: Style | Total Boxes | SKU | Quantity | Case Qty | SKU Boxes | customer name | order id | order qty | ship date
        headers = ['Style', 'Total Boxes', 'SKU', 'Quantity', 'Case Qty', 'SKU Boxes', 'customer name', 'order id', 'order qty', 'ship date']
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=chart_start_row, column=col_idx, value=header)
            cell.alignment = center_alignment
            cell.border = thin_border
            cell.font = Font(bold=True)
        
        # First pass: count rows per style
        style_row_counts = {}
        for item in packslip_data:
            style = item['Style']
            if style not in style_row_counts:
                style_row_counts[style] = 0
            style_row_counts[style] += 1
        
        # Add data and merge cells for same styles
        row_idx = chart_start_row + 1
        current_style = None
        merge_start_row = None
        # With 0.5" margins on US Letter, ~50 rows fit per page
        # But first page has header/summary, so calculate based on absolute row numbers
        rows_per_page = 50
        first_page_end = rows_per_page  # First page ends at row 50 (includes header + summary + some data)
        current_page_end_row = first_page_end
        
        for item in packslip_data:
            style = item['Style']
            
            # Before processing any item, check if we're starting a NEW style
            if style != current_style:
                # If this is NOT the first style, finalize the previous style
                if current_style is not None and merge_start_row is not None:
                    # Merge column A (Style column)
                    if merge_start_row < row_idx - 1:
                        ws.merge_cells(f'A{merge_start_row}:A{row_idx - 1}')
                        style_cell = ws.cell(row=merge_start_row, column=1)
                        style_cell.alignment = center_alignment
                        style_cell.border = thin_border
                    
                    # Merge column B (Total Boxes column)
                    if merge_start_row < row_idx - 1:
                        ws.merge_cells(f'B{merge_start_row}:B{row_idx - 1}')
                        for prev_item in packslip_data:
                            if prev_item['Style'] == current_style:
                                total_cell = ws.cell(row=merge_start_row, column=2)
                                total_cell.value = prev_item['Total Boxes']
                                total_cell.alignment = center_alignment
                                total_cell.border = thin_border
                                break
                    else:
                        for prev_item in packslip_data:
                            if prev_item['Style'] == current_style:
                                cell = ws.cell(row=merge_start_row, column=2, value=prev_item['Total Boxes'])
                                cell.alignment = center_alignment
                                cell.border = thin_border
                                break
                    
                    # NOW check if the new style will fit on current page
                    new_style_rows = style_row_counts.get(style, 1)
                    new_style_end_row = row_idx + new_style_rows - 1
                    
                    # If new style extends past current page end, add page break BEFORE it
                    if new_style_end_row > current_page_end_row:
                        ws.row_breaks.append(Break(id=row_idx - 1))
                        current_page_end_row = row_idx - 1 + rows_per_page  # Next page has full row capacity
                
                # Start new style
                current_style = style
                merge_start_row = row_idx
            
            # Match requested detail order: Style | Total Boxes | SKU | Quantity | Case Qty | SKU Boxes | customer name | order id | order qty | ship date
            # Column A: Style
            cell = ws.cell(row=row_idx, column=1, value=item['Style'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            # Column B: Total Boxes
            cell = ws.cell(row=row_idx, column=2, value=item['Total Boxes'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            # Column C: SKU with Style prepended (e.g., "EN1980 BK")
            sku_with_style = f"{item['Style']} {item['SKU']}"
            cell = ws.cell(row=row_idx, column=3, value=sku_with_style)
            cell.alignment = center_alignment
            cell.border = thin_border
            
            # Column D: Quantity
            cell = ws.cell(row=row_idx, column=4, value=item['Quantity'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            # Column E: Case Qty
            cell = ws.cell(row=row_idx, column=5, value=item['Case Qty'])
            cell.alignment = center_alignment
            cell.border = thin_border
            
            # Column F: SKU Boxes
            cell = ws.cell(row=row_idx, column=6, value=item['SKU Boxes'])
            cell.alignment = center_alignment
            cell.border = thin_border

            # Column G: customer name
            cell = ws.cell(row=row_idx, column=7, value=item.get('customer name', ''))
            cell.alignment = center_alignment
            cell.border = thin_border

            # Column H: order id
            cell = ws.cell(row=row_idx, column=8, value=item.get('order id', ''))
            cell.alignment = center_alignment
            cell.border = thin_border

            # Column I: order qty
            cell = ws.cell(row=row_idx, column=9, value=item.get('order qty', ''))
            cell.alignment = center_alignment
            cell.border = thin_border

            # Column J: ship date
            cell = ws.cell(row=row_idx, column=10, value=item.get('ship date', ''))
            cell.alignment = center_alignment
            cell.border = thin_border
            
            row_idx += 1
        
        # Handle the last style group
        if current_style is not None and merge_start_row is not None:
            # Merge column A (Style column)
            if merge_start_row < row_idx - 1:
                ws.merge_cells(f'A{merge_start_row}:A{row_idx - 1}')
                style_cell = ws.cell(row=merge_start_row, column=1)
                style_cell.alignment = center_alignment
                style_cell.border = thin_border
            
            # Merge column B (Total Boxes column) - now in position 2
            if merge_start_row < row_idx - 1:
                ws.merge_cells(f'B{merge_start_row}:B{row_idx - 1}')
                # Get total from the first item of this style group
                for prev_item in packslip_data:
                    if prev_item['Style'] == current_style:
                        total_cell = ws.cell(row=merge_start_row, column=2)
                        total_cell.value = prev_item['Total Boxes']
                        total_cell.alignment = center_alignment
                        total_cell.border = thin_border
                        break
            else:
                for prev_item in packslip_data:
                    if prev_item['Style'] == current_style:
                        cell = ws.cell(row=merge_start_row, column=2, value=prev_item['Total Boxes'])
                        cell.alignment = center_alignment
                        cell.border = thin_border
                        break
        
        # Save to bytes
        from io import BytesIO
        output = BytesIO()
        wb.save(output)
        output.seek(0)
        
        # Return as download
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'{container_number}_Packslip.xlsx'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error generating packslip for {container_number}: {e}")
        return jsonify({'error': str(e)}), 500