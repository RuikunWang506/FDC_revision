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
TERMINAL49_API_KEY = "TT78TLzvD4q41D74aapnSUJA"
TERMINAL49_CUSTOMER_ID = "f7cb530a-9e60-412c-a5bc-205a2f34ba54"  # Default customer ID


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
    """Update tracking data in Google Sheets columns P-S for containers with shipment data"""
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
                if container in container_tracking_dict:
                    # Row index in sheet (1-based, +1 because enumerate is 0-based)
                    sheet_row = row_idx + 1
                    tracking_data = container_tracking_dict[container]
                    
                    # Replace "Orient Overseas Container Line" with "OOCL" for shipping line
                    shipping_line = tracking_data.get('shipping_line', '')
                    if shipping_line == 'Orient Overseas Container Line':
                        shipping_line = 'OOCL'
                    
                    # Update columns P, Q, S, T with tracking data
                    updates.append({
                        'range': f'P{sheet_row}',  # Column P = Shipping Line
                        'values': [[shipping_line]]
                    })
                    updates.append({
                        'range': f'Q{sheet_row}',  # Column Q = ETA
                        'values': [[tracking_data.get('real_eta', '')]]
                    })
                    updates.append({
                        'range': f'S{sheet_row}',  # Column S = Voyage
                        'values': [[tracking_data.get('voyage', '')]]
                    })
                    updates.append({
                        'range': f'T{sheet_row}',  # Column T = Last Updated
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
            
            # Check if container already exists
            if container_num in existing_containers:
                continue
            
            # Special logic for specific containers: skip F versions if FT versions exist
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
            
            # Update each row individually with explicit ranges
            for i, row_data in enumerate(new_rows):
                range_str = f'A{next_row + i}:T{next_row + i}'
                sheet.update(range_str, [row_data])
            
            return containers_to_add
            
        return []
        
    except Exception as e:
        current_app.logger.error(f"Error adding containers to Google Sheets: {e}")
        return []

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

def get_terminal49_tracking_data(tracking_id):
    """Get tracking data from Terminal49 using the tracking ID"""
    try:
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }
        
        response = requests.get(f"{TERMINAL49_API_URL}/{tracking_id}", headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            return data
        else:
            current_app.logger.error(f"Terminal49 API error getting tracking data for ID {tracking_id}: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        current_app.logger.error(f"Error getting Terminal49 tracking data for ID {tracking_id}: {e}")
        return None

def get_terminal49_shipments():
    """Get all shipments from Terminal49 API"""
    try:
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }

        response = requests.get(f"{TERMINAL49_API_URL.replace('/tracking_requests', '/shipments')}", headers=headers)
        
        # Log the raw response regardless of status code


        if response.status_code == 200:
            data = response.json()
            return data
        else:
            current_app.logger.error(f"Terminal49 shipments API error: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        current_app.logger.error(f"Error getting Terminal49 shipments: {e}")
        return None

def find_shipment_by_container(container_number):
    """Find a shipment by container number in the shipments list"""
    shipments_data = get_terminal49_shipments()

    if not shipments_data or 'data' not in shipments_data:
        return None

    for shipment in shipments_data['data']:
        # Check bill_of_lading_number (container number)
        if shipment['attributes'].get('bill_of_lading_number') == container_number:
            return shipment

    return None

def get_shipment_details(shipment_id):
    """Get detailed information for a specific shipment"""
    try:
        headers = {
            'Authorization': f'Token {TERMINAL49_API_KEY}',
            'Content-Type': 'application/json'
        }

        response = requests.get(f"{TERMINAL49_API_URL.replace('/tracking_requests', '/shipments')}/{shipment_id}", headers=headers)

        if response.status_code == 200:
            data = response.json()
            return data
        else:
            current_app.logger.error(f"Terminal49 shipment details API error for ID {shipment_id}: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        current_app.logger.error(f"Error getting Terminal49 shipment details for ID {shipment_id}: {e}")
        return None

def get_container_tracking_info(fdc_container):
    """Get tracking information for a container from Terminal49 - uses shipments API when available"""
    try:
        # First, look up the actual container number from the 'link' column in Google Sheets
        try:
            df = pd.read_csv(GOOGLE_SHEETS_URL)
            # Find the row where Container # matches our container_number
            matching_row = df[df['FDC container'] == fdc_container]
            if not matching_row.empty:
                bol_num = str(matching_row['BOL NUM'].iloc[0]).strip()
                if bol_num and bol_num != 'nan':
                    bol_num = bol_num
                else:
                    return {
                        'error': f'No container tracking number found for {fdc_container}',
                        'fdc_container': fdc_container,
                        'raw_json': None
                    }
            else:
                return {
                    'error': f'Container {fdc_container} not found in Google Sheets',
                    'fdc_container': fdc_container,
                    'raw_json': None
                }
        except Exception as e:
            current_app.logger.error(f"Error looking up container tracking number: {e}")
            return {
                'error': f'Failed to look up tracking number: {str(e)}',
                'fdc_container': fdc_container,
                'raw_json': None
            }

        # First check if the container is already being tracked (exists in shipments)
        existing_shipment = find_shipment_by_container(bol_num)

        if existing_shipment:
            # Container is already being tracked - get data directly from shipments
            shipment_details = get_shipment_details(existing_shipment['id'])

            if shipment_details:
                return {
                    'fdc_container': fdc_container,
                    'bol_num': bol_num,
                    'shipment_id': existing_shipment['id'],
                    'raw_json': shipment_details,
                    'source': 'shipments_api'
                }
            else:
                return {
                    'error': 'Failed to retrieve shipment details',
                    'fdc_container': fdc_container,
                    'bol_num': bol_num,
                    'shipment_id': existing_shipment['id'],
                    'raw_json': None
                }
        else:
            # Container not found in shipments - create a tracking request

            # First check if there's already an existing tracking request for this container
            existing_tracking_id = find_existing_tracking_request(bol_num)

            if existing_tracking_id:
                tracking_id = existing_tracking_id
            else:
                # No existing tracking request found, create a new one
                tracking_id = create_terminal49_tracking_request(bol_num)

            if not tracking_id:
                return {
                    'error': f'Unable to create tracking request for container {bol_num}. This container may not be trackable through Terminal49 or may be using an unsupported shipping line.',
                    'fdc_container': fdc_container,
                    'bol_num': bol_num,
                    'raw_json': None
                }

            # Wait for the tracking request to be processed and shipment data to become available
            import time
            max_wait_attempts = 5
            wait_interval = 2  # seconds between checks

            for attempt in range(max_wait_attempts):
                time.sleep(wait_interval)

                # Check if the container now appears in shipments after creating tracking request
                updated_shipment = find_shipment_by_container(bol_num)

                if updated_shipment:
                    # Now get the shipment data
                    shipment_details = get_shipment_details(updated_shipment['id'])
                    if shipment_details:
                        return {
                            'fdc_container': fdc_container,
                            'bol_num': bol_num,
                            'shipment_id': updated_shipment['id'],
                            'tracking_id': tracking_id,
                            'raw_json': shipment_details,
                            'source': 'shipments_api_after_request'
                        }


            # If we still can't get shipment data, return tracking request info
            return {
                'fdc_container': fdc_container,
                'bol_num': bol_num,
                'tracking_id': tracking_id,
                'raw_json': {'tracking_request_created': True, 'message': 'Tracking request created. Container may appear in shipments soon.'},
                'source': 'tracking_request_only'
            }

    except Exception as e:
        current_app.logger.error(f"Error getting tracking info for {fdc_container}: {e}")
    except Exception as e:
        current_app.logger.error(f"Error getting tracking info for {fdc_container}: {e}")
        return {
            'error': str(e),
            'fdc_container': fdc_container,
            'raw_json': None
        }

def get_container_tracking_info_for_new_container(bol_num):
    """Get tracking information for a new container using the actual container number directly"""
    try:
        # First check if the container is already being tracked (exists in shipments)
        existing_shipment = find_shipment_by_container(bol_num)

        if existing_shipment:
            # Container is already being tracked - get data directly from shipments
            shipment_details = get_shipment_details(existing_shipment['id'])

            if shipment_details:
                return {
                    'bol_num': bol_num,
                    'shipment_id': existing_shipment['id'],
                    'raw_json': shipment_details,
                    'source': 'shipments_api'
                }
            else:
                return {
                    'error': 'Failed to retrieve shipment details',
                    'bol_num': bol_num,
                    'shipment_id': existing_shipment['id'],
                    'raw_json': None
                }
        else:
            # Container not found in shipments - create a tracking request

            # First check if there's already an existing tracking request for this container
            existing_tracking_id = find_existing_tracking_request(bol_num)

            if existing_tracking_id:
                tracking_id = existing_tracking_id
            else:
                # No existing tracking request found, create a new one
                tracking_id = create_terminal49_tracking_request(bol_num)

            if not tracking_id:
                return {
                    'error': f'Unable to create tracking request for container {bol_num}. This container may not be trackable through Terminal49 or may be using an unsupported shipping line.',
                    'bol_num': bol_num,
                    'raw_json': None
                }

            # Wait for the tracking request to be processed and shipment data to become available
            import time
            max_wait_attempts = 5
            wait_interval = 2  # seconds between checks

            for attempt in range(max_wait_attempts):
                time.sleep(wait_interval)

                # Check if the container now appears in shipments after creating tracking request
                updated_shipment = find_shipment_by_container(bol_num)

                if updated_shipment:
                    # Now get the shipment data
                    shipment_details = get_shipment_details(updated_shipment['id'])
                    if shipment_details:
                        return {
                            'bol_num': bol_num,
                            'shipment_id': updated_shipment['id'],
                            'tracking_id': tracking_id,
                            'raw_json': shipment_details,
                            'source': 'shipments_api_after_request'
                        }

            current_app.logger.warning(f"Shipment data not available after {max_wait_attempts} attempts, returning tracking request info")

            # If we still can't get shipment data, return tracking request info
            return {
                'bol_num': bol_num,
                'tracking_id': tracking_id,
                'raw_json': {'tracking_request_created': True, 'message': 'Tracking request created. Container may appear in shipments soon.'},
                'source': 'tracking_request_only'
            }

    except Exception as e:
        current_app.logger.error(f"Error getting tracking info for new container {bol_num}: {e}")
        return {
            'error': str(e),
            'bol_num': bol_num,
            'raw_json': None
        }

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

            # Batch add new containers to Google Sheets first
            if new_containers:
                # Sort containers by container number (small to large) before adding
                sorted_new_containers = sorted(list(new_containers))
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

        # Map columns directly by name
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
        if landed_containers_with_dates:
            update_received_dates_in_sheets(landed_containers_with_dates)

        # Drop the 'container_raw' column before rendering (keep the other metadata columns)
        df_cleaned = df_cleaned.drop('container_raw', axis=1)

        # Replace NaN values with empty strings for JSON serialization
        df_cleaned = df_cleaned.fillna('')
        
        # Set Last Updated timestamp for all containers to current fetch time
        df_cleaned['last_updated'] = fetch_timestamp
        
        # Fetch Terminal49 shipment data and create tracking requests for containers without shipments
        try:
            # Get all shipments from Terminal49
            shipments_data = get_terminal49_shipments()
            shipment_lookup = {}
            
            if shipments_data and 'data' in shipments_data:
                for shipment in shipments_data['data']:
                    attrs = shipment.get('attributes', {})
                    bill_of_lading = attrs.get('bill_of_lading_number', '')
                    if bill_of_lading:
                        shipment_lookup[bill_of_lading] = shipment
            
            # For each container in our display data, check if it needs a tracking request
            containers_needing_tracking = []
            for index, row in df_cleaned.iterrows():
                container_num = row['container']
                actual_container = row.get('link', '').strip()
                
                if actual_container and actual_container != 'nan' and actual_container not in shipment_lookup:
                    containers_needing_tracking.append(actual_container)
            
            # Create tracking requests for containers that need them (limit to avoid rate limits)
            if containers_needing_tracking:
                created_count = 0
                failed_containers = []
                for container_num in containers_needing_tracking[:50]:  # Limit to 50 at a time to avoid rate limits
                    try:
                        tracking_id = create_terminal49_tracking_request(container_num)
                        if tracking_id:
                            created_count += 1
                        else:
                            failed_containers.append(container_num)
                    except Exception as e:
                        current_app.logger.error(f"Error creating tracking request for {container_num}: {e}")
                        failed_containers.append(container_num)
                
                # Log summary of tracking request creation
                if failed_containers:
                    current_app.logger.warning(f"Failed to create tracking requests for {len(failed_containers)} containers: {failed_containers[:5]}{'...' if len(failed_containers) > 5 else ''}")
            
            # Add shipment data to the response and collect tracking data for Google Sheets update
            containers_with_tracking_data = {}
            for index, row in df_cleaned.iterrows():
                container_num = row['container']
                actual_container = row.get('link', '').strip()
                
                if actual_container and actual_container in shipment_lookup:
                    shipment = shipment_lookup[actual_container]
                    df_cleaned.at[index, 'has_shipment_data'] = True
                    df_cleaned.at[index, 'shipment_id'] = shipment.get('id')
                    # Add key shipment attributes
                    attrs = shipment.get('attributes', {})
                    shipping_line = attrs.get('shipping_line_name', '')
                    # Replace "Orient Overseas Container Line" with "OOCL"
                    if shipping_line == 'Orient Overseas Container Line':
                        shipping_line = 'OOCL'
                    real_eta = get_los_angeles_time(attrs.get('pod_eta_at', ''))
                    # Strip time part, keep only date
                    if real_eta and ' ' in real_eta:
                        real_eta = real_eta.split(' ')[0]
                    vessel_name = attrs.get('pod_vessel_name', '')
                    voyage_number = attrs.get('pod_voyage_number', '')
                    # Combine vessel name and voyage number for voyage field
                    voyage = f"{vessel_name} {voyage_number}".strip() if vessel_name or voyage_number else ''
                    
                    df_cleaned.at[index, 'shipping_line'] = shipping_line
                    df_cleaned.at[index, 'pod_eta'] = attrs.get('pod_eta_at', '')
                    df_cleaned.at[index, 'voyage'] = voyage
                    # Add new columns with Los Angeles time
                    df_cleaned.at[index, 'Auto ETA'] = real_eta
                    df_cleaned.at[index, 'ship_name'] = vessel_name
                    
                    # Collect tracking data for Google Sheets update
                    containers_with_tracking_data[container_num] = {
                        'shipping_line': shipping_line,
                        'real_eta': real_eta,
                        'voyage': voyage,
                        'last_updated': fetch_timestamp
                    }
                else:
                    df_cleaned.at[index, 'has_shipment_data'] = False
                    df_cleaned.at[index, 'shipment_id'] = ''
                    df_cleaned.at[index, 'shipping_line'] = ''
                    df_cleaned.at[index, 'pod_eta'] = ''
                    df_cleaned.at[index, 'voyage'] = ''
                    df_cleaned.at[index, 'Auto ETA'] = ''
                    df_cleaned.at[index, 'pod_ata_at'] = ''
                    df_cleaned.at[index, 'ship_name'] = ''
        
        except Exception as e:
            current_app.logger.error(f"Error fetching Terminal49 data: {e}")
            # Continue without Terminal49 data if there's an error
        
        # Update Google Sheets with tracking data for containers that have shipment data
        if containers_with_tracking_data:
            update_tracking_data_in_sheets(containers_with_tracking_data)
        
        # Update Status column based on new logic: Auto ETA/Auto Arrv/internal RCD
        for index, row in df_cleaned.iterrows():
            auto_eta = str(row.get('Auto ETA', '')).strip()
            auto_arrv = str(row.get('pod_ata_at', '')).strip()
            internal_rcd = str(row.get('RCD', '')).strip()
            
            if auto_eta:
                # If Auto ETA has value, it's ocean
                df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
            elif auto_arrv and not internal_rcd:
                # If Auto Arrv has value and internal RCD is empty, it's at port
                df_cleaned.at[index, 'Status'] = '⚓port⚓'
            else:
                # Otherwise, keep the existing status (landed or upload)
                pass
        
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
        update_last_updated_for_all_containers(df_cleaned['container'].tolist(), fetch_timestamp)
        
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

@boat_service_bp.route('/boat/tracking/<fdc_container>')
def get_container_tracking_popup(fdc_container):
    """Get tracking information for a container and return HTML for popup display"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        tracking_info = get_container_tracking_info(fdc_container)

        if 'error' in tracking_info:
            return f"""
            <div class="tracking-popup">
                <h3>Container Tracking: {fdc_container}</h3>
                <div class="tracking-error">
                    <p>Error: {tracking_info['error']}</p>
                </div>
            </div>
            """

        # Handle different response types
        source = tracking_info.get('source', 'unknown')
        raw_json = tracking_info.get('raw_json', {})

        if source in ['shipments_api', 'shipments_api_after_request']:
            # We have full shipment data - display nicely formatted info
            shipment_data = raw_json.get('data', {}) if isinstance(raw_json, dict) and 'data' in raw_json else raw_json
            attrs = shipment_data.get('attributes', {}) if isinstance(shipment_data, dict) else {}

            # Format dates
            def format_datetime(dt_str):
                if not dt_str:
                    return 'N/A'
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
                    return dt.strftime('%Y-%m-%d %H:%M UTC')
                except:
                    return dt_str

            html = f"""
            <div class="tracking-popup">
                <h3>Container Tracking: {tracking_info['fdc_container']}</h3>
                <div class="tracking-info">
                    <div class="tracking-row">
                        <span class="label">BOL NUM:</span>
                        <span class="value">{tracking_info.get('bol_num', 'N/A')}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Shipping Line:</span>
                        <span class="value">{attrs.get('shipping_line_name', 'N/A')} ({attrs.get('shipping_line_scac', 'N/A')})</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Origin:</span>
                        <span class="value">{attrs.get('port_of_lading_name', 'N/A')} ({attrs.get('port_of_lading_locode', 'N/A')})</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Destination:</span>
                        <span class="value">{attrs.get('port_of_discharge_name', 'N/A')} ({attrs.get('port_of_discharge_locode', 'N/A')})</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">ETA:</span>
                        <span class="value">{format_datetime(attrs.get('pod_eta_at'))}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">ATA:</span>
                        <span class="value">{format_datetime(attrs.get('pod_ata_at'))}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Departure:</span>
                        <span class="value">{format_datetime(attrs.get('pol_atd_at'))}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Vessel:</span>
                        <span class="value">{attrs.get('pod_vessel_name', 'N/A')} (IMO: {attrs.get('pod_vessel_imo', 'N/A')})</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Voyage:</span>
                        <span class="value">{attrs.get('pod_voyage_number', 'N/A')}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Last Updated:</span>
                        <span class="value">{format_datetime(attrs.get('line_tracking_last_succeeded_at'))}</span>
                    </div>
                </div>
                <div class="tracking-success">
                    <h4>✅ Tracking Information Retrieved</h4>
                    <p>Real-time container tracking data from Terminal49.</p>
                </div>
            </div>
            """
        elif source == 'tracking_request_only':
            # Only tracking request created, no shipment data yet
            html = f"""
            <div class="tracking-popup">
                <h3>Container Tracking: {tracking_info['fdc_container']}</h3>
                <div class="tracking-info">
                    <div class="tracking-row">
                        <span class="label">BOL NUM:</span>
                        <span class="value">{tracking_info.get('bol_num', 'N/A')}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Tracking ID:</span>
                        <span class="value">{tracking_info.get('tracking_id', 'N/A')}</span>
                    </div>
                </div>
                <div class="tracking-note">
                    <p><strong>Note:</strong> {raw_json.get('message', 'Tracking request has been created. Container tracking data should be available soon.')}</p>
                    <p>The container may take a few minutes to appear in the tracking system.</p>
                    <p><em>Alternative tracking options:</em></p>
                    <ul>
                        <li>Check with your shipping line directly</li>
                        <li>Use general container tracking websites</li>
                        <li>Contact freight forwarder for updates</li>
                    </ul>
                </div>
            </div>
            """
        else:
            # Fallback - display raw JSON
            import json
            formatted_json = json.dumps(raw_json, indent=2, default=str)

            html = f"""
            <div class="tracking-popup">
                <h3>Container Tracking: {tracking_info['fdc_container']}</h3>
                <div class="tracking-info">
                    <div class="tracking-row">
                        <span class="label">BOL NUM:</span>
                        <span class="value">{tracking_info.get('bol_num', 'N/A')}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Tracking ID:</span>
                        <span class="value">{tracking_info.get('tracking_id', 'N/A')}</span>
                    </div>
                    <div class="tracking-row">
                        <span class="label">Source:</span>
                        <span class="value">{source}</span>
                    </div>
                </div>
                <div class="tracking-json">
                    <h4>Raw JSON Response</h4>
                    <pre class="json-display">{formatted_json}</pre>
                </div>
            </div>
            """

        return html

    except Exception as e:
        current_app.logger.error(f"Error getting tracking for {fdc_container}: {e}")
        return f"""
        <div class="tracking-popup">
            <h3>Container Tracking: {fdc_container}</h3>
            <div class="tracking-error">
                <p>Error: {str(e)}</p>
            </div>
        </div>
        """, 500

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