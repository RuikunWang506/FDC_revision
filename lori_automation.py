
from flask import Blueprint, render_template, request, jsonify, send_file, session, redirect, url_for, current_app
import os, re
import io
import json
import requests
import pandas as pd
from datetime import datetime
from PyPDF2 import PdfReader, PdfMerger
from openpyxl import load_workbook
from decimal import Decimal
import zipfile
import mysql.connector
from mysql.connector import Error
import pyodbc
import base64
import imaplib
import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import shutil
# --- Store Info Management Endpoints ---
import tempfile
# Removed problematic imports - will import inside functions as needed



lori_automation_bp = Blueprint('lori_automation', __name__)
color_dict = {"TL":"Teal","ABL":"ASHY BLUE","FLOWER":"FLOWER","BG":"BEIGE","BK":"BLACK","BL":"BLUE","BP":"BLUSH PINK","BR":"BROWN","CF":"COFFEE","CG":"COGNAC","CO":"CORAL","DM":"DENIM BLUE","DPL":"DARK PLUM","FU":"FUSCHIA","GN":"GREEN","GY":"GREY","KH":"KHAKI","LBL":"LIGHT BLUE","LV":"LAVENDER","MD":"MUSTARD","MV":"MAUVE","NV":"NAVY","OL":"OLIVE","OR":"ORANGE","OW":"OFF WHITE","PK":"PINK","PL":"PLUM","PP":"PURPLE","RD":"RED","TN":"TAN","TP":"TAUPE","TQ":"TEQUISE","WN":"WINE","WT":"WHITE","YL":"YELLOW","ST":"STONE","L.ST":"L.Stone","L":"LARGE"}

# MySQL Database configuration
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'
}

# SQL Server configuration
sql_server_config = {
    'server': '192.168.254.204',
    'database': 'omsdata_FDC',
    'username': 'tristen',
    'password': 'Fdc2022!!',
    'driver': 'ODBC Driver 17 for SQL Server'
}

# Email configuration
email_config = {
    'imap_server': 'imap.fatcow.com',
    'imap_port': 993,
    'email': 'elaine@fdcus.com',
    'password': 'Elaine827$$',
    'sender_filter': 'MI9_Merchant@lorisgifts.com',
    'download_path': r'C:\Users\stream\Dropbox\Elaine\loris 2025\==========LORIS_auto_PENDING====='
}

def parse_pdf_to_database(pdf_content, batch_date, email_date=None):
    """Parse PDF content and store order information in database"""
    try:
        reader = PdfReader(pdf_content)
        text_content = "\n".join([page.extract_text() for page in reader.pages])
        lines = text_content.split("\n")
        
        # Extract order information
        order_date = lines[1].strip() if len(lines) > 1 else "N/A"
        delivery_date = lines[2].strip() if len(lines) > 2 else "N/A"
        cancel_date = lines[3].strip() if len(lines) > 3 else "N/A"
        
        po_match = re.search(r"P\.O\. #:\s*([\d-]+)", text_content)
        po_number = po_match.group(1) if po_match else "N/A"
        
        address = extract_clean_address(text_content)
        
        # Parse items using existing regex pattern
        item_pattern = re.compile(r"\d+\s+([A-Za-z][A-Za-z0-9\-\s\.]*?)\s+1\s+(\d+)\s+(\d{12})\s+(.+?)\s+\$(\d+\.\d+)\s+\$(\d+\.\d+)", re.DOTALL)
        rows = item_pattern.findall(text_content)
        
        # Calculate totals
        total_qty = Decimal(0)
        total_cost = Decimal(0)
        items = []
        
        for entry in rows:
            vendor_item, qty, upc, description, unit_price, ext_cost = entry
            vendor_item = vendor_item.strip()
            qty = Decimal(qty)
            unit_price = Decimal(unit_price)
            ext_cost = Decimal(ext_cost)
            description = ' '.join(description.split())
            
            # Determine color
            if vendor_item.lower() == "special" or vendor_item.lower() == "special - l":
                color = ""
            elif "-" in vendor_item:
                parts = vendor_item.split("-")
                color_key = parts[-1].strip()
                if "." in color_key:
                    color = color_dict.get(color_key, color_key)
                    last_hyphen_index = vendor_item.rfind("-")
                    vendor_item = vendor_item[:last_hyphen_index] + " " + vendor_item[last_hyphen_index+1:]
                else:
                    color = color_dict.get(color_key, color_key)
            else:
                color_key = vendor_item[-2:]
                color = color_dict.get(color_key, "")
            
            items.append({
                'vendor_item': vendor_item,
                'color': color,
                'quantity': qty,
                'description': description,
                'unit_price': unit_price,
                'extended_cost': ext_cost,
                'upc': upc
            })
            
            total_qty += qty
            total_cost += ext_cost
        
        # Store in database
        connection = get_db_connection()
        if not connection:
            return {'error': 'Database connection failed'}
        
        cursor = connection.cursor()
        
        # Check if order already exists
        cursor.execute("SELECT po_number FROM lori_orders WHERE po_number = %s", (po_number,))
        existing_order = cursor.fetchone()
        
        if existing_order:
            return {'message': f'Order {po_number} already exists in database', 'skipped': True}
        
        # Insert order
        insert_order_sql = """
            INSERT INTO lori_orders (po_number, batch_date, email_date, order_date, delivery_date, 
                                   cancel_date, ship_to_address, total_qty, total_cost, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_order_sql, (
            po_number, batch_date, email_date, order_date, delivery_date,
            cancel_date, address, total_qty, total_cost, 'pending'
        ))
        
        # Insert items
        insert_item_sql = """
            INSERT INTO lori_order_items (po_number, vendor_item, color, quantity, description, 
                                        unit_price, extended_cost, upc)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        
        for item in items:
            cursor.execute(insert_item_sql, (
                po_number, item['vendor_item'], item['color'], item['quantity'],
                item['description'], item['unit_price'], item['extended_cost'], item['upc']
            ))
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return {
            'success': True,
            'po_number': po_number,
            'total_items': len(items),
            'total_qty': float(total_qty),
            'total_cost': float(total_cost)
        }
        
    except Exception as e:
        return {'error': f'PDF processing error: {str(e)}'}

def fetch_email_attachments():
    """Fetch PDF attachments from emails and download them to directory"""
    try:
        # Calculate date 1 month ago
        from dateutil.relativedelta import relativedelta
        one_months_ago = datetime.now() - relativedelta(months=1)
        search_date = one_months_ago.strftime('%d-%b-%Y')  # Format: 01-Jan-2023
        
        # Connect to IMAP server
        mail = imaplib.IMAP4_SSL(email_config['imap_server'], email_config['imap_port'])
        mail.login(email_config['email'], email_config['password'])
        # Select inbox
        mail.select('inbox')
        # Search for emails from specific sender in the last 1 month
        search_criteria = f'(FROM "{email_config["sender_filter"]}" SINCE {search_date})'
        result, data = mail.search(None, search_criteria)
        if result != 'OK':
            return {'error': 'Failed to search emails'}
        
        downloaded_files = []
        email_ids = data[0].split() if result == 'OK' else []
        
        # Ensure download directory exists
        download_path = email_config['download_path']
        os.makedirs(download_path, exist_ok=True)
        
        # Process each email and download PDF attachments
        for email_id in email_ids:
            result, data = mail.fetch(email_id, '(RFC822)')
            if result != 'OK':
                continue
            email_message = email.message_from_bytes(data[0][1])
            
            # Get email date
            email_date_str = email_message.get('Date', '')
            try:
                from email.utils import parsedate_to_datetime
                email_date = parsedate_to_datetime(email_date_str)
                batch_date = email_date.strftime('%Y-%m-%d')  # Format: 2025-09-24
            except:
                batch_date = datetime.now().strftime('%Y-%m-%d')
            
            # Create batch directory
            batch_dir = os.path.join(download_path, batch_date)
            os.makedirs(batch_dir, exist_ok=True)
            
            # Process PDF attachments
            for part in email_message.walk():
                if part.get_content_disposition() == 'attachment':
                    filename = part.get_filename()
                    if filename and filename.lower().endswith('.pdf'):
                        # Generate unique filename if it already exists
                        file_path = os.path.join(batch_dir, filename)
                        counter = 1
                        base_name, ext = os.path.splitext(filename)
                        while os.path.exists(file_path):
                            filename = f"{base_name}_{counter}{ext}"
                            file_path = os.path.join(batch_dir, filename)
                            counter += 1
                        
                        # Download PDF file
                        try:
                            with open(file_path, 'wb') as f:
                                f.write(part.get_payload(decode=True))
                            
                            downloaded_files.append({
                                'filename': filename,
                                'filepath': file_path,
                                'batch_date': batch_date
                            })
                        except Exception as e:
                            print(f"Error downloading {filename}: {str(e)}")
                            continue
        
        mail.close()
        mail.logout()
        
        # Create batch summary
        batch_dates = list(set([f['batch_date'] for f in downloaded_files]))
        batch_summary = []
        for batch_date in batch_dates:
            batch_files = [f for f in downloaded_files if f['batch_date'] == batch_date]
            batch_summary.append({
                'date': batch_date,
                'count': len(batch_files),
                'files': [f['filename'] for f in batch_files]
            })
        
        print(f"[fetch_email_attachments] Total emails processed: {len(email_ids)}")
        print(f"[fetch_email_attachments] Total files downloaded: {len(downloaded_files)}")
        print(f"[fetch_email_attachments] Batch dates: {batch_dates}")
        
        return {
            'success': True,
            'total_emails_processed': len(email_ids),
            'total_files': len(downloaded_files),
            'files': downloaded_files,
            'batches': batch_dates,
            'batch_summary': batch_summary,
            'search_period': f'Last 1 month (since {search_date})'
        }
    except Exception as e:
        return {'error': f'Email fetch error: {str(e)}'}

def process_email_pdfs(selected_batch_date=None):
    """Parse downloaded PDFs and store order data in database"""
    try:
        # Get all PDF files from download directory
        pdf_files = []
        seen_files = set()
        
        if selected_batch_date:
            # Process only files from specific date batch
            batch_dir = os.path.join(email_config['download_path'], selected_batch_date)
            if os.path.exists(batch_dir):
                for filename in os.listdir(batch_dir):
                    if filename.lower().endswith('.pdf'):
                        filepath = os.path.join(batch_dir, filename)
                        if filename not in seen_files:
                            pdf_files.append({
                                'filepath': filepath,
                                'filename': filename,
                                'batch_date': selected_batch_date
                            })
                            seen_files.add(filename)
        else:
            # Process all PDF files from all batches
            if os.path.exists(email_config['download_path']):
                for root, dirs, files in os.walk(email_config['download_path']):
                    # Extract batch date from directory name
                    batch_date = os.path.basename(root)
                    if batch_date == os.path.basename(email_config['download_path']):
                        batch_date = datetime.now().strftime('%Y-%m-%d')
                    
                    for filename in files:
                        if filename.lower().endswith('.pdf'):
                            if filename not in seen_files:
                                filepath = os.path.join(root, filename)
                                pdf_files.append({
                                    'filepath': filepath,
                                    'filename': filename,
                                    'batch_date': batch_date
                                })
                                seen_files.add(filename)
        
        if not pdf_files:
            return {'error': 'No PDF files found in download directory'}
        
        processed_orders = []
        skipped_orders = 0
        new_orders = 0
        
        # Process each PDF file and store in database
        for pdf_file in pdf_files:
            try:
                with open(pdf_file['filepath'], 'rb') as f:
                    pdf_content = io.BytesIO(f.read())
                    
                # Parse batch date
                try:
                    batch_date = datetime.strptime(pdf_file['batch_date'], '%Y-%m-%d').date()
                except:
                    batch_date = datetime.now().date()
                
                # Parse PDF and store in database
                result = parse_pdf_to_database(pdf_content, batch_date, datetime.now())
                
                if 'error' in result:
                    print(f"Error processing {pdf_file['filename']}: {result['error']}")
                    continue
                elif result.get('skipped'):
                    skipped_orders += 1
                else:
                    processed_orders.append({
                        'filename': pdf_file['filename'],
                        'po_number': result['po_number'],
                        'total_items': result['total_items'],
                        'batch_date': pdf_file['batch_date']
                    })
                    new_orders += 1
                    
            except Exception as e:
                print(f"Error processing file {pdf_file['filename']}: {str(e)}")
                continue
        
        print(f"[process_email_pdfs] Total PDF files processed: {len(pdf_files)}")
        print(f"[process_email_pdfs] New orders added to database: {new_orders}")
        print(f"[process_email_pdfs] Orders skipped (already exist): {skipped_orders}")
        
        return {
            'success': True,
            'processed_orders': processed_orders,
            'total_files': len(pdf_files),
            'new_orders': new_orders,
            'skipped_orders': skipped_orders,
            'processed_files': len(processed_orders)
        }
        
    except Exception as e:
        return {'error': f'PDF processing error: {str(e)}'}

# --- Store Info Management Endpoints ---
@lori_automation_bp.route("/download_store_info", methods=["GET"])
def download_store_info():
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM loris_store_info;")
        rows = cursor.fetchall()
        df = pd.DataFrame(rows)
        
        # Ensure we have proper column headers
        if not df.empty:
            # Rename columns to have proper English headers
            column_mapping = {
                'store_number': 'Store Number',
                'store_name': 'Store Name',
                'company': 'Company',
                'phone': 'Phone',
                'address1': 'Address 1',
                'address2': 'Address 2',
                'address3': 'Address 3',
                'city': 'City',
                'state': 'State',
                'zip_code': 'Zip Code',
                'status': 'Status',
                'memo': 'Memo',
                'created_at': 'Created At',
                'updated_at': 'Updated At'
            }
            df = df.rename(columns=column_mapping)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='store_info', header=True)
        output.seek(0)
        return send_file(output, as_attachment=True,
                         download_name="loris_store_info.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/upload_store_info", methods=["POST"])
def upload_store_info():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    try:
        df = pd.read_excel(file)
        
        # Define column mapping from English headers to database columns
        column_mapping = {
            'Store Number': 'store_number',
            'Store Name': 'store_name',
            'Company': 'company',
            'Phone': 'phone',
            'Address 1': 'address1',
            'Address 2': 'address2', 
            'Address 3': 'address3',
            'City': 'city',
            'State': 'state',
            'Zip Code': 'zip_code',
            'Status': 'status',
            'Memo': 'memo',
            'Created At': 'created_at',
            'Updated At': 'updated_at'
        }
        
        # Check if we have either the old format (store_number) or new format (Store Number)
        if 'Store Number' in df.columns:
            # New format with English headers - rename columns to database format
            df = df.rename(columns=column_mapping)
        elif 'store_number' not in df.columns:
            return jsonify({'error': 'File must contain either "Store Number" or "store_number" column.'}), 400
        
        # Handle backward compatibility for old 'address' column
        if 'address' in df.columns and 'address1' not in df.columns:
            # If old format has single address column, map it to address1
            df['address1'] = df['address']
            df = df.drop('address', axis=1)
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        cursor = connection.cursor(dictionary=True)
        updated = 0
        inserted = 0
        # Define columns that should be excluded from updates (timestamps are auto-managed)
        excluded_columns = {'store_number', 'created_at', 'updated_at'}
        
        for _, row in df.iterrows():
            store_number = str(row['store_number']).strip()
            if not store_number:
                continue
            # Build update set (exclude store_number and timestamp columns)
            update_fields = []
            update_values = []
            for col in df.columns:
                if col not in excluded_columns:
                    update_fields.append(f"{col} = %s")
                    update_values.append(row[col] if pd.notnull(row[col]) else None)
            
            if not update_fields:  # Skip if no updateable fields
                continue
                
            # Try update first
            sql_update = f"UPDATE loris_store_info SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP WHERE store_number = %s"
            cursor.execute(sql_update, update_values + [store_number])
            if cursor.rowcount == 0:
                # Insert if not exists - exclude timestamp columns as they're auto-generated
                insert_columns = [col for col in df.columns if col not in {'created_at', 'updated_at'}]
                columns = ','.join(insert_columns)
                placeholders = ','.join(['%s'] * len(insert_columns))
                values = [row[col] if pd.notnull(row[col]) else None for col in insert_columns]
                sql_insert = f"INSERT INTO loris_store_info ({columns}) VALUES ({placeholders})"
                cursor.execute(sql_insert, values)
                inserted += 1
            else:
                updated += 1
        connection.commit()
        return jsonify({'message': f'Upload complete. Updated: {updated}, Inserted: {inserted}.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()

@lori_automation_bp.route("/get_store_info", methods=["GET"])
def get_store_info():
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM loris_store_info ORDER BY store_number;")
        rows = cursor.fetchall()
        return jsonify({'data': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/edit_store_info", methods=["POST"])
def edit_store_info():
    data = request.get_json()
    store_number = data.get('store_number')
    if not store_number:
        return jsonify({'error': 'Missing store_number'}), 400
    update_fields = []
    update_values = []
    for key, value in data.items():
        if key != 'store_number':
            update_fields.append(f"{key} = %s")
            update_values.append(value)
    if not update_fields:
        return jsonify({'error': 'No fields to update'}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor()
        sql = f"UPDATE loris_store_info SET {', '.join(update_fields)}, updated_at = CURRENT_TIMESTAMP WHERE store_number = %s"
        cursor.execute(sql, update_values + [store_number])
        connection.commit()
        return jsonify({'message': 'Store info updated.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/delete_store_info", methods=["POST"])
def delete_store_info():
    data = request.get_json()
    store_number = data.get('store_number')
    if not store_number:
        return jsonify({'error': 'Missing store_number'}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM loris_store_info WHERE store_number = %s", (store_number,))
        connection.commit()
        return jsonify({'message': 'Store deleted.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/add_store_info", methods=["POST"])
def add_store_info():
    data = request.get_json()
    required_fields = ['store_number', 'store_name', 'address1', 'city', 'state', 'zip_code', 'status']
    
    # Validate required fields
    for field in required_fields:
        if not data.get(field):
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    
    try:
        cursor = connection.cursor()
        
        # Check if store number already exists
        cursor.execute("SELECT store_number FROM loris_store_info WHERE store_number = %s", (data['store_number'],))
        if cursor.fetchone():
            return jsonify({'error': 'Store number already exists'}), 400
        
        # Insert new store
        sql = """INSERT INTO loris_store_info 
                 (store_number, store_name, company, phone, address1, address2, address3, city, state, zip_code, status, memo) 
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
        values = (
            data['store_number'],
            data['store_name'],
            data.get('company', ''),
            data.get('phone', ''),
            data['address1'],
            data.get('address2', ''),
            data.get('address3', ''),
            data['city'],
            data['state'],
            data['zip_code'],
            data['status'],
            data.get('memo', '')
        )
        cursor.execute(sql, values)
        connection.commit()
        
        return jsonify({'message': 'Store added successfully'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def get_sql_server_connection():
    """Establishes a connection to the SQL Server database."""
    try:
        connection_string = f"DRIVER={sql_server_config['driver']};SERVER={sql_server_config['server']};DATABASE={sql_server_config['database']};UID={sql_server_config['username']};PWD={sql_server_config['password']}"
        connection = pyodbc.connect(connection_string)
        return connection
    except pyodbc.Error as e:
        print(f"Error connecting to SQL Server: {e}")
    return None

def get_sku_price(sku):
    """Query the MySQL database for the list price of a specific SKU."""
    connection = get_db_connection()
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
    connection = get_sql_server_connection()
    if connection is None:
        return None
    
    try:
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
        if connection:
            cursor.close()
            connection.close()

def extract_clean_address(text_content):
    ship_to_match = re.search(r"SHIP TO\s*:\s*\d*\s*(.*?)(?:TOTAL UNITS:|Ship to Arrive:)", text_content, re.DOTALL)
    if not ship_to_match:
        return "N/A"
    
    full_address_block = ship_to_match.group(1).strip()
    lines = [line.strip() for line in full_address_block.split('\n')]
    lines = [line for line in lines if line and line != "US" and "Attn: Lori's Gifts" not in line]
    
    company = lines[0] if len(lines) > 0 else ""
    street = lines[1] if len(lines) > 1 else ""
    city = ""
    state = ""
    zip_candidate = ""
    
    i = 2
    while i < len(lines):
        line = lines[i]
        if re.fullmatch(r"[A-Z]{2}", line):
            state = line
        elif line.isdigit():
            zip_candidate = zip_candidate + line if zip_candidate else line
        elif ',' in line:
            parts = line.split(',')
            city = parts[0].strip()
            if len(parts) > 1:
                candidate = parts[1].strip()
                if candidate.endswith('-'):
                    j = i + 1
                    while j < len(lines) and (not lines[j] or not lines[j].isdigit()):
                        j += 1
                    if j < len(lines) and lines[j].isdigit():
                        candidate = candidate + lines[j].strip()
                        lines[j] = ""
                elif len(candidate) not in [5, 9]:
                    j = i + 1
                    while j < len(lines) and (not lines[j] or not lines[j].isdigit()):
                        j += 1
                    if j < len(lines) and lines[j].isdigit():
                        candidate = candidate + lines[j].strip()
                        lines[j] = ""
                zip_candidate = candidate
        i += 1

    if zip_candidate.isdigit() and len(zip_candidate) == 9:
        zip_candidate = f"{zip_candidate[:5]}-{zip_candidate[5:]}"
    
    final_address = [company, street, f"{city}, {state} {zip_candidate}".strip()]
    return "\n".join(line for line in final_address if line)

def process_pdf(pdf_content):
    memory_file = io.BytesIO()
    wb = load_workbook('static/new_sample.xlsx')
    ws = wb.active
    
    reader = PdfReader(pdf_content)
    text_content = "\n".join([page.extract_text() for page in reader.pages])
    lines = text_content.split("\n")
    
    order_date = lines[1].strip() if len(lines) > 1 else "N/A"
    delivery_date = lines[2].strip() if len(lines) > 2 else "N/A"
    cancel_date = lines[3].strip() if len(lines) > 3 else "N/A"
    
    po_match = re.search(r"P\.O\. #:\s*([\d-]+)", text_content)
    po_number = po_match.group(1) if po_match else "N/A"
    
    address = extract_clean_address(text_content)
    
    # Updated regex pattern to handle line numbers and various vendor item formats including dots
    item_pattern = re.compile(r"\d+\s+([A-Za-z][A-Za-z0-9\-\s\.]*?)\s+1\s+(\d+)\s+(\d{12})\s+(.+?)\s+\$(\d+\.\d+)\s+\$(\d+\.\d+)", re.DOTALL)
    rows = item_pattern.findall(text_content)
    
    today = datetime.now().strftime("%m/%d/%Y")
    file_date = today.replace("/", "").lstrip("0")  # Remove leading zero if present
    target_filename = f"{file_date}_{po_number[:4]}"
    
    # Fill Excel template
    ws['F7'] = order_date
    ws['F8'] = today
    ws['H6'] = target_filename.replace("_", "-")
    ws['G11'] = f"GIFT STORE {po_number[:4]}"
    ws['F9'] = cancel_date
    ws['H8'] = delivery_date
    ws['H7'] = po_number
    ws['F12'] = address
    
    starting_row = 20
    total_qty = Decimal(0)
    total_cost = Decimal(0)
    
    for entry in rows:
        vendor_item, qty, upc, description, unit_price, ext_cost = entry
        vendor_item = vendor_item.strip()  # Clean up any extra whitespace
        qty = Decimal(qty)
        unit_price = Decimal(unit_price)
        ext_cost = Decimal(ext_cost)
        
        # Clean up description - remove extra whitespace and newlines
        description = ' '.join(description.split())
        
        # Check for Special items first (both "Special" and "Special - L" types)
        if vendor_item.lower() == "special" or vendor_item.lower() == "special - l":
            color = ""  # Leave color field empty for Special items
        elif "-" in vendor_item:
            parts = vendor_item.split("-")
            color_key = parts[-1].strip()  # Get everything after the last hyphen
            
            # Special handling for items like "N2366-L.ST" where color is "L.ST"
            if "." in color_key:
                # For cases like "L.ST", use the full color_key and modify vendor_item
                color = color_dict.get(color_key, color_key)
                # Replace the last hyphen with a space for compound color codes
                last_hyphen_index = vendor_item.rfind("-")
                vendor_item = vendor_item[:last_hyphen_index] + " " + vendor_item[last_hyphen_index+1:]
            else:
                # Normal case - look up in color dictionary
                color = color_dict.get(color_key, color_key)
        else:
            # Handle case with no hyphen - try last 2 characters
            color_key = vendor_item[-2:]
            color = color_dict.get(color_key, "")
            
        ws[f"A{starting_row}"] = starting_row - 19
        ws[f"B{starting_row}"] = vendor_item  # Keep original vendor_item unchanged
        ws[f"C{starting_row}"] = color
        ws[f"D{starting_row}"] = qty
        ws[f"E{starting_row}"] = description
        ws[f"G{starting_row}"] = f"${unit_price:.2f}"
        ws[f"H{starting_row}"] = f"${ext_cost:.2f}"
        
        total_qty += qty
        total_cost += ext_cost
        starting_row += 1
    
    ws[f"C{starting_row + 1}"] = "Total Quantity"
    ws[f"D{starting_row + 1}"] = total_qty
    ws[f"G{starting_row + 1}"] = "Total Amount"
    ws[f"H{starting_row + 1}"] = f"${total_cost:.2f}"
    
    wb.save(memory_file)
    memory_file.seek(0)
    
    return {
        'excel': memory_file,
        'filename': f"{target_filename}.xlsx",
        'po_number': po_number,
        'address': address
    }

@lori_automation_bp.route("/process_skus", methods=["POST"])
def process_skus():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    sku_list = data.get('skus', '')
    
    if not sku_list:
        return jsonify({'error': 'No SKUs provided'}), 400
    
    # Replace newlines with commas and then split by comma
    sku_list = sku_list.replace('\n', ',')
    skus = [sku.strip() for sku in sku_list.split(',')]
    
    # Prepare data for table format
    table_data = []
    
    # Process each SKU
    for sku in skus:
        if not sku:  # Skip empty SKUs
            continue
        
        # Query the MySQL database for the list price
        price_info = get_sku_price(sku)
        list_price = f"${price_info['listprice']}" if price_info and 'listprice' in price_info else "Not found"
        
        # Query the SQL Server database for the sold price
        sold_price_info = get_sku_sold_price(sku)
        sold_price = f"${sold_price_info['unit_prs']:.2f}" if sold_price_info and 'unit_prs' in sold_price_info else "No recent sales"
        
        # Add to table data
        table_data.append({
            "sku": sku,
            "list_price": list_price,
            "sold_price": sold_price
        })
    
    # Return the data in a format that can be rendered as a table
    return jsonify({'table_data': table_data})



@lori_automation_bp.route("/download_database", methods=["GET"])
def download_database():
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT SKU, listprice FROM onlinedb.loris;")
        rows = cursor.fetchall()
        df = pd.DataFrame(rows)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='loris')
        output.seek(0)
        return send_file(output, as_attachment=True,
                         download_name="loris_database.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/upload_database", methods=["POST"])
def upload_database():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    try:
        df = pd.read_excel(file)
        if 'SKU' not in df.columns or 'listprice' not in df.columns:
            return jsonify({'error': 'File must contain SKU and listprice columns.'}), 400
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        cursor = connection.cursor()
        cursor.execute("DELETE FROM onlinedb.loris;")
        for _, row in df.iterrows():
            cursor.execute("INSERT INTO onlinedb.loris (SKU, listprice) VALUES (%s, %s);",
                           (row['SKU'], row['listprice']))
        connection.commit()
        return jsonify({'message': 'Database updated successfully.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()

@lori_automation_bp.route("/upload_otb", methods=["POST"])
def upload_otb():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if not file.filename.endswith('.xlsx'):
        return jsonify({'error': 'Only Excel (.xlsx) files are supported'}), 400
    
    try:
        # Log the file being processed
        current_app.logger.info(f"Processing OTB file: {file.filename}")
        
        # Read the Excel file
        df = pd.read_excel(file, sheet_name=0, header=None)
        
        # Column A: index 0 (SKU), Column E: index 4 (listprice)
        df_otb = df.iloc[:, [0, 4]].dropna(subset=[0])
        df_otb.columns = ['SKU', 'listprice']
        df_otb = df_otb.drop_duplicates(subset=['SKU'])
        
        # Get database connection
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor(dictionary=True)
        
        # Process each row in the OTB file
        records_updated = 0
        records_inserted = 0
        records_skipped = 0
        
        for _, row in df_otb.iterrows():
            try:
                sku = str(row['SKU']).strip()
                # Convert to string and ensure it's a valid number
                try:
                    new_price = float(row['listprice'])
                    new_price_str = f"{new_price:.2f}"
                except (ValueError, TypeError):
                    new_price_str = str(row['listprice']).strip()
                
                # Skip empty SKUs
                if not sku:
                    continue
                    
                cursor.execute("SELECT listprice FROM onlinedb.loris WHERE SKU = %s;", (sku,))
                result = cursor.fetchone()
                
                if result:
                    # Parse existing prices into a list
                    old_price_str = str(result['listprice'])
                    old_prices = [p.strip() for p in old_price_str.split(',')]
                    
                    # Check if the new price already exists in the list
                    if new_price_str not in old_prices:
                        # Add the new price to the list
                        old_prices.append(new_price_str)
                        
                        # Join the prices back into a string, limited to a reasonable length
                        updated_price = ', '.join(old_prices)
                        
                        # Limit string length to avoid database column size issues
                        max_length = 250  # Assuming VARCHAR(255)
                        if len(updated_price) > max_length:
                            # If too long, keep only the most recent prices
                            updated_price = ', '.join(old_prices[-5:])  # Keep last 5 prices
                        
                        cursor.execute("UPDATE onlinedb.loris SET listprice = %s WHERE SKU = %s;", (updated_price, sku))
                        records_updated += 1
                    else:
                        # Skip if price already exists
                        records_skipped += 1
                else:
                    cursor.execute("INSERT INTO onlinedb.loris (SKU, listprice) VALUES (%s, %s);", (sku, new_price_str))
                    records_inserted += 1
            except Exception as row_error:
                current_app.logger.error(f"Error processing row with SKU {sku}: {str(row_error)}")
                records_skipped += 1
                continue
        
        connection.commit()
        
        return jsonify({
            'message': f'OTB file processed successfully. Updated: {records_updated}, Inserted: {records_inserted}, Skipped: {records_skipped} records.',
            'updated': records_updated,
            'inserted': records_inserted,
            'skipped': records_skipped
        })
        
    except Exception as e:
        current_app.logger.error(f"Error processing OTB file: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'connection' in locals() and connection.is_connected():
            connection.close()
            
@lori_automation_bp.route("/loris_fedex", methods=["GET", "POST"])
def loris_fedex():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    filename_30stickers = None
    filename_exact = None
    error_message = None
    table_data = None
    
    input_data = request.form.get("input_data")
    sku = request.form.get("sku", "")
    barcode = request.form.get("barcode", "")
    quantity = request.form.get("quantity", "")
    
    if request.method == "POST":
        if "submit_data" in request.form:
            try:
                current_app.logger.info("Processing data in Loris fedex for %s", current_user)
                
                # Import the main function from loris_sticker
                from loris_sticker import main
                
                # Generate PDFs for both versions
                filename_30stickers, filename_exact = main(input_data)
                
                if not filename_30stickers or not filename_exact:
                    error_message = "An error occurred while generating the PDFs."
            except Exception as e:
                error_message = f"An error occurred: {e}"
                current_app.logger.error("Exception in Loris fedex: %s", e)
                
        elif "show_table" in request.form:
            try:
                # Import the parse function from loris_sticker
                from loris_sticker import process_input_data
                table_data = process_input_data(input_data)
                # Convert to the format expected by the template
                table_data = [[item[0], item[1], item[2]] for item in table_data]
            except Exception as e:
                error_message = f"An error occurred while parsing the data: {e}"
                current_app.logger.error("Error parsing data in Loris fedex: %s", e)
    
    # Ensure table_data is a list
    if table_data is None:
        table_data = []
    
    return render_template("loris_fedex.html", 
                         filename_30stickers=filename_30stickers, 
                         filename_exact=filename_exact, 
                         error_message=error_message, 
                         table_data=table_data, 
                         input_data=input_data, 
                         sku=sku, 
                         barcode=barcode, 
                         quantity=quantity, 
                         current_user=current_user)


# New endpoint: process local PDFs only (no email fetch)
@lori_automation_bp.route("/process_local_pdfs", methods=["POST"])
def process_local_pdfs():
    """Process PDFs from the local download directory, skipping email fetching."""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        request_data = request.get_json() or {}
        selected_batch_date = request_data.get('batch_date', None)
        # Only process local files, do not fetch emails
        process_result = process_email_pdfs(selected_batch_date)
        if 'error' in process_result:
            return jsonify(process_result), 500
        batch_info = ""
        if selected_batch_date:
            batch_info = f" from batch {selected_batch_date}"
        return jsonify({
            'success': True,
            'message': f'Successfully processed {process_result["processed_files"]} local PDF files{batch_info}',
            'excel_zip': process_result['excel_zip'],
            'combined_pdf': process_result['combined_pdf'],
            'shipstation_data': process_result['shipstation_data'],
            'individual_excel_files': process_result['individual_excel_files'],
            'selected_batch': selected_batch_date
        })
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@lori_automation_bp.route("/upload_and_process_pdfs", methods=["POST"])
def upload_and_process_pdfs():
    """Process uploaded PDF files directly into database and save to manual folder"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        files = request.files.getlist('files[]')
        if not files:
            return jsonify({'error': 'No files uploaded'}), 400
        
        processed_orders = []
        skipped_orders = 0
        new_orders = 0
        batch_date = datetime.now().date()  # Use current date as batch date
        
        # Create manual directory for saving uploaded PDFs
        manual_dir = os.path.join(email_config['download_path'], 'manual')
        os.makedirs(manual_dir, exist_ok=True)
        
        # Helper function to check if filename exists in any folder
        def file_exists_anywhere(filename, base_path):
            """Check if a filename exists in any subfolder of base_path"""
            if not os.path.exists(base_path):
                return False
            
            for root, dirs, files in os.walk(base_path):
                if filename in files:
                    return True
            return False
        
        for file in files:
            if file and file.filename.lower().endswith('.pdf'):
                try:
                    filename = file.filename
                    
                    # Check if this filename already exists anywhere in the download directory
                    if file_exists_anywhere(filename, email_config['download_path']):
                        print(f"Skipping duplicate file: {filename} (already exists in download directory)")
                        skipped_orders += 1
                        continue
                    
                    # Read file content
                    file_content = file.read()
                    pdf_content = io.BytesIO(file_content)
                    
                    # Save PDF file to manual directory
                    file_path = os.path.join(manual_dir, filename)
                    
                    # Save the PDF file
                    with open(file_path, 'wb') as f:
                        f.write(file_content)
                    
                    # Process the PDF into database
                    result = parse_pdf_to_database(pdf_content, batch_date, datetime.now())
                    
                    if 'error' in result:
                        print(f"Error processing {file.filename}: {result['error']}")
                        continue
                    elif result.get('skipped'):
                        skipped_orders += 1
                    else:
                        processed_orders.append({
                            'filename': filename,
                            'saved_path': file_path,
                            'po_number': result['po_number'],
                            'total_items': result['total_items']
                        })
                        new_orders += 1
                        
                except Exception as e:
                    print(f"Error processing file {file.filename}: {str(e)}")
                    continue
        
        # Create appropriate message based on results
        message_parts = []
        if new_orders > 0:
            message_parts.append(f'成功处理了 {new_orders} 个新订单到数据库，PDF文件已保存到 manual 文件夹')
        if skipped_orders > 0:
            message_parts.append(f'跳过了 {skipped_orders} 个重复文件')
        
        message = '；'.join(message_parts) if message_parts else '没有处理任何文件'
        
        return jsonify({
            'success': True,
            'message': message,
            'new_orders': new_orders,
            'skipped_orders': skipped_orders,
            'processed_files': len(processed_orders),
            'saved_directory': manual_dir,
            'count': new_orders  # For compatibility with existing frontend
        })
        
    except Exception as e:
        return jsonify({'error': f'处理上传文件时出错: {str(e)}'}), 500

@lori_automation_bp.route("/lori_automation", methods=["GET", "POST"])
def lori_automation():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    if request.method == "POST":
        if 'files[]' not in request.files:
            return jsonify({'error': 'No files uploaded'}), 400
        
        files = request.files.getlist('files[]')
        processed_orders = []
        upload_errors = []
        
        # Use current date as batch date for user uploads
        batch_date = datetime.now().date()
        
        for file in files:
            if file.filename.endswith('.pdf'):
                try:
                    pdf_content = io.BytesIO(file.read())
                    result = parse_pdf_to_database(pdf_content, batch_date)
                    
                    if 'error' in result:
                        upload_errors.append({
                            'filename': file.filename,
                            'error': result['error']
                        })
                    elif result.get('skipped'):
                        processed_orders.append({
                            'filename': file.filename,
                            'po_number': result.get('po_number', 'N/A'),
                            'status': 'skipped',
                            'message': 'Order already exists in database'
                        })
                    else:
                        processed_orders.append({
                            'filename': file.filename,
                            'po_number': result['po_number'],
                            'order_id': result['order_id'],
                            'total_items': result['total_items'],
                            'total_qty': result['total_qty'],
                            'total_cost': result['total_cost'],
                            'status': 'success',
                            'batch_date': batch_date.strftime('%Y-%m-%d')
                        })
                except Exception as e:
                    upload_errors.append({
                        'filename': file.filename,
                        'error': str(e)
                    })
        
        return jsonify({
            'success': True,
            'message': f'Processed {len(processed_orders)} files',
            'processed_orders': processed_orders,
            'errors': upload_errors,
            'total_files': len(files),
            'batch_date': batch_date.strftime('%Y-%m-%d')
        })
    return render_template("loris_fedex.html", current_user=session['username'])

@lori_automation_bp.route("/get_orders", methods=["GET"])
def get_orders():
    """Get all orders from database with internal store addresses"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    
    try:
        cursor = connection.cursor(dictionary=True)
        
        # Get all orders with their details
        query = """
            SELECT o.po_number, o.batch_date, o.email_date, o.order_date, 
                   o.delivery_date, o.cancel_date, o.ship_to_address, o.total_qty, 
                   o.total_cost, o.status, o.created_at, o.updated_at,
                   COUNT(oi.id) as item_count
            FROM lori_orders o
            LEFT JOIN lori_order_items oi ON o.po_number = oi.po_number
            GROUP BY o.po_number
            ORDER BY o.batch_date DESC, o.created_at DESC
        """
        cursor.execute(query)
        orders = cursor.fetchall()
        
        # For each order, get the internal address using first 4 digits
        for order in orders:
            store_number = order['po_number'][:4] if order['po_number'] else ''
            if store_number:
                cursor.execute("""
                    SELECT store_number, store_name, company, phone, address1, address2, 
                           address3, city, state, zip_code, status, memo
                    FROM loris_store_info 
                    WHERE store_number = %s
                """, (store_number,))
                store_info = cursor.fetchone()
                order['store_info'] = store_info
            else:
                order['store_info'] = None
            
            # Format dates for display
            if order['batch_date']:
                order['batch_date_formatted'] = order['batch_date'].strftime('%Y-%m-%d')
            if order['email_date']:
                order['email_date_formatted'] = order['email_date'].strftime('%Y-%m-%d %H:%M')
            if order['created_at']:
                order['created_at_formatted'] = order['created_at'].strftime('%Y-%m-%d %H:%M')
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'orders': orders,
            'total_orders': len(orders)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if connection.is_connected():
            connection.close()

@lori_automation_bp.route("/get_order_items/<string:po_number>", methods=["GET"])
def get_order_items(po_number):
    """Get items for a specific order"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    
    try:
        cursor = connection.cursor(dictionary=True)
        
        query = """
            SELECT id, vendor_item, color, quantity, description, 
                   unit_price, extended_cost, upc
            FROM lori_order_items 
            WHERE po_number = %s
            ORDER BY id
        """
        cursor.execute(query, (po_number,))
        items = cursor.fetchall()
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'items': items
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if connection.is_connected():
            connection.close()

@lori_automation_bp.route("/update_order_status", methods=["POST"])
def update_order_status():
    """Update order status"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    po_number = data.get('order_id')  # Keep 'order_id' for compatibility with frontend
    status = data.get('status')
    
    if not po_number or not status:
        return jsonify({'error': 'Missing order_id or status'}), 400
    
    valid_statuses = ['pending', 'processed', 'shipped', 'completed']
    if status not in valid_statuses:
        return jsonify({'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}), 400
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    
    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE lori_orders 
            SET status = %s, updated_at = CURRENT_TIMESTAMP 
            WHERE po_number = %s
        """, (status, po_number))
        
        if cursor.rowcount == 0:
            return jsonify({'error': 'Order not found'}), 404
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': 'Order status updated successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if connection.is_connected():
            connection.close()

@lori_automation_bp.route("/delete_order", methods=["POST"])
def delete_order():
    """Delete an order and all its items"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    data = request.get_json()
    po_number = data.get('order_id')  # Keep 'order_id' for compatibility with frontend
    
    if not po_number:
        return jsonify({'error': 'Missing order_id'}), 400
    
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    
    try:
        cursor = connection.cursor()
        
        # Check if order exists
        cursor.execute("SELECT po_number FROM lori_orders WHERE po_number = %s", (po_number,))
        order = cursor.fetchone()
        
        if not order:
            return jsonify({'error': 'Order not found'}), 404
        
        # Delete order (items will be deleted automatically due to CASCADE)
        cursor.execute("DELETE FROM lori_orders WHERE po_number = %s", (po_number,))
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': f'Order {po_number} deleted successfully'
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if connection.is_connected():
            connection.close()

@lori_automation_bp.route("/fetch_and_process_emails", methods=["POST"])
def fetch_and_process_emails():
    """Simplified endpoint: Fetch emails, download PDFs, and process into database automatically"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        # Step 1: Fetch attachments from email
        fetch_result = fetch_email_attachments()
        if 'error' in fetch_result:
            return jsonify(fetch_result), 500
        
        # Step 2: Process all downloaded PDFs into database
        process_result = process_email_pdfs()  # Process all batches
        if 'error' in process_result:
            return jsonify(process_result), 500
        
        # Return success with summary
        return jsonify({
            'success': True,
            'message': f'完成！处理了 {fetch_result["total_emails_processed"]} 封邮件，添加了 {process_result["new_orders"]} 个新订单到数据库',
            'total_emails_processed': fetch_result['total_emails_processed'],
            'total_files_downloaded': fetch_result['total_files'],
            'new_orders': process_result['new_orders'],
            'skipped_orders': process_result['skipped_orders'],
            'count': process_result['new_orders']  # For compatibility with existing frontend
        })
    except Exception as e:
        return jsonify({'error': f'处理过程中出错: {str(e)}'}), 500

# Restore the original /fetch_email_pdfs endpoint for compatibility
@lori_automation_bp.route("/fetch_email_pdfs", methods=["POST"])
def fetch_email_pdfs():
    """Fetch PDF attachments from email and process them into database"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        # Get request data
        request_data = request.get_json() or {}
        selected_batch_date = request_data.get('batch_date', None)
        fetch_only = request_data.get('fetch_only', False)  # If true, only fetch without processing
        
        # First, fetch attachments from email
        fetch_result = fetch_email_attachments()
        if 'error' in fetch_result:
            return jsonify(fetch_result), 500
        
        # If fetch_only is True, return just the fetch results with batch information
        if fetch_only:
            return jsonify({
                'success': True,
                'message': f'Successfully fetched {fetch_result["total_files"]} PDF files from {fetch_result["total_emails_processed"]} emails',
                'fetched_files': fetch_result['files'],
                'total_files': fetch_result['total_files'],
                'total_emails_processed': fetch_result['total_emails_processed'],
                'batches': fetch_result['batches'],
                'batch_summary': fetch_result['batch_summary']
            })
        
        # Process the downloaded PDFs and store in database
        process_result = process_email_pdfs(selected_batch_date)
        if 'error' in process_result:
            return jsonify(process_result), 500
        
        # Return simplified response for database storage
        return jsonify({
            'success': True,
            'message': f'Successfully processed {process_result["new_orders"]} new orders from {fetch_result["total_emails_processed"]} emails',
            'total_emails_processed': fetch_result['total_emails_processed'],
            'total_files_downloaded': fetch_result['total_files'],
            'new_orders': process_result['new_orders'],
            'skipped_orders': process_result['skipped_orders'],
            'batches': fetch_result['batches']
        })
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500