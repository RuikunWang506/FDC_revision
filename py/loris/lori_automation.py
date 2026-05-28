
from flask import Blueprint, render_template, request, jsonify, send_file, session, redirect, url_for, current_app, flash
from py.utilities.setting import login_required
import os, re
import io
import json
import requests
import pandas as pd
from datetime import datetime
from PyPDF2 import PdfReader, PdfMerger
from openpyxl import load_workbook, Workbook
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection
from decimal import Decimal
import copy
import zipfile
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
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
db_config = Config.DB_CONFIG

# SQL Server configuration
sql_server_config = Config.SQL_SERVER_CONFIG

# Email configuration
email_config = {
    'imap_server': 'imap.gmail.com',
    'imap_port': 993,
    'email': 'elaine@fdcus.com',
    'password': 'acamukrkgoifsrtq',
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
    start_time = datetime.now()
    print(f"[fetch_email_attachments] Started at {start_time}")
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
        # Use UID search to get stable IDs we can cache
        result, data = mail.uid('search', None, search_criteria)
        if result != 'OK':
            return {'error': 'Failed to search emails'}
        
        downloaded_files = []
        all_uids = data[0].split() if result == 'OK' else []
        all_uids = [uid.decode('utf-8') for uid in all_uids]
        
        # Ensure download directory exists
        download_path = email_config['download_path']
        os.makedirs(download_path, exist_ok=True)

        # Pre-scan existing files to avoid repeated disk I/O and clean up duplicates
        print("[fetch_email_attachments] Pre-scanning local files...")
        existing_files_cache = set()
        for root, dirs, files in os.walk(download_path):
            folder_name = os.path.basename(root)
            for file in files:
                # Clean up duplicates immediately
                if re.match(r'.*_\d+\.pdf$', file):
                    try:
                        os.remove(os.path.join(root, file))
                        print(f"[fetch_email_attachments] Cleaned up duplicate: {file}")
                    except Exception as e:
                        print(f"Error removing {file}: {str(e)}")
                    continue
                
                if file.lower().endswith('.pdf'):
                    existing_files_cache.add((folder_name, file))
        print(f"[fetch_email_attachments] Indexed {len(existing_files_cache)} existing files.")

        # Load metadata cache to skip server requests for known emails
        cache_file = os.path.join(download_path, 'email_metadata_cache.json')
        metadata_cache = {}
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    metadata_cache = json.load(f)
            except:
                pass
        
        # Filter UIDs - only fetch what we don't have or what is missing files
        email_ids = []
        skipped_uids_count = 0
        
        for uid in all_uids:
            if uid in metadata_cache:
                entry = metadata_cache[uid]
                batch_date = entry.get('batch_date')
                filenames = entry.get('filenames', [])
                
                # Check if all files for this email exist locally
                all_files_exist = True
                if filenames:
                    for fname in filenames:
                        if (batch_date, fname) not in existing_files_cache:
                            all_files_exist = False
                            break
                
                if all_files_exist:
                    skipped_uids_count += 1
                    # Add to report
                    for fname in filenames:
                        downloaded_files.append({
                            'filename': fname,
                            'filepath': os.path.join(download_path, batch_date, fname),
                            'batch_date': batch_date,
                            'status': 'skipped_existing'
                        })
                    continue
            
            email_ids.append(uid)

        print(f"[fetch_email_attachments] Total emails: {len(all_uids)}. Cached & Valid: {skipped_uids_count}. Need to process: {len(email_ids)}")
        
        # Process each email and download PDF attachments
        # Batch processing to improve speed
        batch_size = 50
        total_emails = len(email_ids)
        if total_emails > 0:
            print(f"[fetch_email_attachments] Processing {total_emails} emails in batches of {batch_size}...")
        
        from email.header import decode_header
        import email.utils

        for i in range(0, total_emails, batch_size):
            batch_ids = email_ids[i:i+batch_size]
            batch_ids_str = b','.join([uid.encode('utf-8') for uid in batch_ids]).decode('utf-8')
            
            skipped_emails = set()
            
            try:
                # Fetch Date and Structure for the whole batch using UID
                res, batch_data = mail.uid('fetch', batch_ids_str, '(UID BODY.PEEK[HEADER.FIELDS (DATE)] BODYSTRUCTURE)')
                if res == 'OK':
                    # Reconstruct full responses for each email ID
                    email_content_map = {}
                    last_uid = None
                    
                    for part in batch_data:
                        if isinstance(part, tuple):
                            resp_head = part[0].decode('utf-8', errors='ignore')
                            resp_body = part[1].decode('utf-8', errors='ignore')
                            
                            # Extract UID from response "UID 1234"
                            uid_match = re.search(r'UID\s+(\d+)', resp_head)
                            if uid_match:
                                last_uid = uid_match.group(1)
                                email_content_map[last_uid] = resp_head + " " + resp_body
                        elif isinstance(part, bytes):
                            if last_uid:
                                email_content_map[last_uid] += part.decode('utf-8', errors='ignore')

                    # Process each reconstructed email response
                    for current_id, full_response in email_content_map.items():
                        # Extract Date
                        date_str = None
                        if 'Date:' in full_response:
                            date_match = re.search(r'Date:\s*(.+?)(?:\r\n|\n)', full_response, re.IGNORECASE)
                            if date_match:
                                date_str = date_match.group(1)
                        
                        batch_date = None
                        if date_str:
                            try:
                                msg_date = email.utils.parsedate_to_datetime(date_str)
                                batch_date = msg_date.strftime('%Y-%m-%d')
                            except:
                                pass
                        
                        if not batch_date:
                            batch_date = datetime.now().strftime('%Y-%m-%d')
                            
                        batch_dir = os.path.join(download_path, batch_date)
                        
                        # Extract PDF filenames
                        potential_matches = re.findall(r'(?:NAME|FILENAME)\s+"([^"]+)"', full_response, re.IGNORECASE)
                        
                        pdf_matches = []
                        if potential_matches:
                            for fname in potential_matches:
                                decoded_fname = fname
                                if '=?' in fname:
                                    try:
                                        decoded_parts = decode_header(fname)
                                        decoded_fname = ''.join([
                                            (part[0].decode(part[1] or 'utf-8') if isinstance(part[0], bytes) else part[0])
                                            for part in decoded_parts
                                        ])
                                    except:
                                        pass
                                
                                if decoded_fname.lower().endswith('.pdf'):
                                    pdf_matches.append(decoded_fname)
                        
                        # Update Metadata Cache
                        metadata_cache[current_id] = {
                            'batch_date': batch_date,
                            'filenames': pdf_matches
                        }

                        if pdf_matches:
                            all_exist = True
                            skipped_files_temp = []
                            for decoded_fname in pdf_matches:
                                # Check cache instead of disk
                                if (batch_date, decoded_fname) not in existing_files_cache:
                                    all_exist = False
                                    break
                                
                                file_path = os.path.join(batch_dir, decoded_fname)
                                skipped_files_temp.append({
                                    'filename': decoded_fname,
                                    'filepath': file_path,
                                    'batch_date': batch_date,
                                    'status': 'skipped_existing'
                                })
                            
                            if all_exist and skipped_files_temp:
                                skipped_emails.add(current_id)
                                for f in skipped_files_temp:
                                    print(f"[fetch_email_attachments] Skipped (already exists): {f['filename']}")
                                downloaded_files.extend(skipped_files_temp)
            except Exception as e:
                print(f"Batch optimization check failed: {e}")
            
            # Process emails in batch
            for email_id in batch_ids:
                if email_id in skipped_emails:
                    continue
                
                # Full download fallback
                try:
                    result, data = mail.uid('fetch', email_id, '(RFC822)')
                    if result != 'OK':
                        continue
                    email_message = email.message_from_bytes(data[0][1])
                    
                    email_date_str = email_message.get('Date', '')
                    try:
                        email_date = email.utils.parsedate_to_datetime(email_date_str)
                        batch_date = email_date.strftime('%Y-%m-%d')
                    except:
                        batch_date = datetime.now().strftime('%Y-%m-%d')
                    
                    batch_dir = os.path.join(download_path, batch_date)
                    os.makedirs(batch_dir, exist_ok=True)
                    
                    # Update cache for this email if not already set (fallback)
                    if email_id not in metadata_cache:
                         metadata_cache[email_id] = {'batch_date': batch_date, 'filenames': []}

                    found_pdfs = []
                    for part in email_message.walk():
                        if part.get_content_disposition() == 'attachment':
                            filename = part.get_filename()
                            if filename and filename.lower().endswith('.pdf'):
                                found_pdfs.append(filename)
                                # Check cache first
                                if (batch_date, filename) in existing_files_cache:
                                    file_path = os.path.join(batch_dir, filename)
                                    print(f"[fetch_email_attachments] Skipped (already exists): {filename}")
                                    downloaded_files.append({
                                        'filename': filename,
                                        'filepath': file_path,
                                        'batch_date': batch_date,
                                        'status': 'skipped_existing'
                                    })
                                    continue
                                
                                file_path = os.path.join(batch_dir, filename)
                                try:
                                    with open(file_path, 'wb') as f:
                                        f.write(part.get_payload(decode=True))
                                    print(f"[fetch_email_attachments] Downloaded: {filename}")
                                    # Update cache
                                    existing_files_cache.add((batch_date, filename))
                                    downloaded_files.append({
                                        'filename': filename,
                                        'filepath': file_path,
                                        'batch_date': batch_date,
                                        'status': 'downloaded'
                                    })
                                except Exception as e:
                                    print(f"Error downloading {filename}: {str(e)}")
                                    continue
                    
                    # Update filenames in metadata cache
                    metadata_cache[email_id]['filenames'] = found_pdfs

                except Exception as e:
                    print(f"Error processing email {email_id}: {e}")
        
        # Save updated metadata cache
        try:
            with open(cache_file, 'w') as f:
                json.dump(metadata_cache, f)
        except Exception as e:
            print(f"Failed to save metadata cache: {e}")

        mail.close()
        mail.logout()
        
        # Cleanup loop removed (moved to start)
        
        # Create batch summary
        batch_dates = list(set([f['batch_date'] for f in downloaded_files]))
        batch_summary = []
        downloaded_count = len([f for f in downloaded_files if f.get('status') == 'downloaded'])
        skipped_count = len([f for f in downloaded_files if f.get('status') == 'skipped_existing'])
        
        for batch_date in batch_dates:
            batch_files = [f for f in downloaded_files if f['batch_date'] == batch_date]
            batch_downloaded = [f for f in batch_files if f.get('status') == 'downloaded']
            batch_skipped = [f for f in batch_files if f.get('status') == 'skipped_existing']
            batch_summary.append({
                'date': batch_date,
                'count': len(batch_files),
                'downloaded': len(batch_downloaded),
                'skipped': len(batch_skipped),
                'files': [f['filename'] for f in batch_files]
            })
        
        print(f"[fetch_email_attachments] Total emails processed: {len(email_ids)}")
        print(f"[fetch_email_attachments] Total files found: {len(downloaded_files)}")
        print(f"[fetch_email_attachments] Files downloaded: {downloaded_count}")
        print(f"[fetch_email_attachments] Files skipped (already exist): {skipped_count}")
        print(f"[fetch_email_attachments] Batch dates: {batch_dates}")
        
        end_time = datetime.now()
        duration = end_time - start_time
        print(f"[fetch_email_attachments] Completed in {duration}")

        # Extract skipped files for detailed reporting
        skipped_files = [{'filename': f['filename'], 'reason': 'File already exists'} for f in downloaded_files if f.get('status') == 'skipped_existing']
        
        return {
            'success': True,
            'total_emails_processed': len(email_ids),
            'total_files': len(downloaded_files),
            'files_downloaded': downloaded_count,
            'files_skipped': skipped_count,
            'skipped_files': skipped_files,
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
        
        # Optimization: Fetch all existing PO numbers from database
        print("[process_email_pdfs] Fetching existing PO numbers from database...")
        existing_pos = set()
        try:
            conn = get_mysql_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT po_number FROM lori_orders")
            rows = cursor.fetchall()
            for row in rows:
                existing_pos.add(row[0])
            cursor.close()
            conn.close()
            print(f"[process_email_pdfs] Found {len(existing_pos)} existing orders in database.")
        except Exception as e:
            print(f"[process_email_pdfs] Warning: Failed to fetch existing POs: {e}")

        processed_orders = []
        skipped_orders = 0
        skipped_files = []
        new_orders = 0
        
        # Process each PDF file and store in database
        for pdf_file in pdf_files:
            try:
                # Optimization: Check filename for PO number before parsing
                filename = pdf_file['filename']
                po_match = re.search(r'PO_([\d-]+)\.pdf$', filename, re.IGNORECASE)
                if po_match:
                    po_from_filename = po_match.group(1)
                    if po_from_filename in existing_pos:
                        skipped_orders += 1
                        skipped_files.append({
                            'filename': filename,
                            'reason': f'Order {po_from_filename} already exists (checked filename)'
                        })
                        continue

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
                    skipped_files.append({
                        'filename': pdf_file['filename'],
                        'reason': result.get('message', 'Order already exists in database')
                    })
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
            'skipped_files': skipped_files,
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

@lori_automation_bp.route("/loris_all_store_information", methods=["GET"])
def loris_all_store_information():
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT * FROM loris_store_info ORDER BY store_number;")
        rows = cursor.fetchall()
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
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
    return get_mysql_connection()

def get_sql_server_connection():
    """Establishes a connection to the SQL Server database."""
    try:
        connection_string = f"DRIVER={sql_server_config['driver']};SERVER={sql_server_config['server']};DATABASE=omsdata_FDC;UID={sql_server_config['username']};PWD={sql_server_config['password']}"
        connection = pyodbc.connect(connection_string)
        return connection
    except pyodbc.Error as e:
        print(f"Error connecting to SQL Server: {e}")
    return None

def get_sku_price(sku):
    """Query the MySQL database for the list price of a specific SKU."""
    try:
        connection = get_db_connection()
        if connection is None:
            return None
        
        cursor = connection.cursor(dictionary=True)
        query = "SELECT SKU, listprice FROM onlinedb.loris WHERE SKU = %s"
        cursor.execute(query, (sku,))
        result = cursor.fetchone()
        return result
    except Exception as e:
        print(f"Error in get_sku_price: {e}")
        return None
    finally:
        try:
            if connection and connection.is_connected():
                cursor.close()
                connection.close()
        except:
            pass

def get_sku_sold_price(sku):
    """Query the SQL Server database for all unique sold prices of a specific SKU using fuzzy matching."""
    try:
        connection = get_sql_server_connection()
        if connection is None:
            print("SQL Server connection failed")
            return None
        
        cursor = connection.cursor()
        # Use LIKE operator for fuzzy matching
        query = """
            SELECT DISTINCT unit_prs
            FROM ord_log
            WHERE prod_cd LIKE ? AND cus_id = '00868' AND unit_prs IS NOT NULL
            ORDER BY unit_prs
        """
        # Add wildcard for fuzzy matching
        search_pattern = f"%{sku.upper()}%"
        print(f"Executing query: {query.strip()} with pattern: {search_pattern}")
        cursor.execute(query, (search_pattern,))
        rows = cursor.fetchall()
        print(f"Found {len(rows)} rows: {rows}")
        if rows:
            prices = []
            for row in rows:
                p = row[0]
                if isinstance(p, str) and ',' in p:
                    prices.extend([x.strip() for x in p.split(',') if x.strip()])
                else:
                    prices.append(p)
            return {"prices": prices}
        return None
    except Exception as e:
        print(f"Error in get_sku_sold_price: {e}")
        return None
    finally:
        try:
            if connection:
                cursor.close()
                connection.close()
        except:
            pass

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
    
    try:
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
            if price_info and 'listprice' in price_info and price_info['listprice'] is not None:
                try:
                    lp = float(price_info['listprice'])
                    list_price = [f"${lp:.1f}"]
                except ValueError:
                    # Handle comma-separated prices
                    if ',' in price_info['listprice']:
                        list_price = [f"${float(x.strip()):.1f}" for x in price_info['listprice'].split(',') if x.strip()]
                    else:
                        list_price = [price_info['listprice']]
            else:
                list_price = ["Not found"]
            
            # Query the SQL Server database for the sold price
            sold_price_info = get_sku_sold_price(sku)
            if sold_price_info and 'prices' in sold_price_info and sold_price_info['prices']:
                sold_price = [f"${float(p):.1f}" for p in sold_price_info['prices']]
            else:
                sold_price = []
            
            # Add to table data
            table_data.append({
                "sku": sku,
                "list_price": list_price,
                "sold_price": sold_price
            })
        
        # Return the data in a format that can be rendered as a table
        return jsonify({'table_data': table_data})
    except Exception as e:
        print(f"Error in process_skus: {str(e)}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500



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
    loris_stickers_ready = False
    error_message = None
    table_data = None
    
    input_data = request.form.get("input_data")
    sku = request.form.get("sku", "")
    barcode = request.form.get("barcode", "")
    quantity = request.form.get("quantity", "")
    
    if request.method == "POST":
        if "submit_data" in request.form:
            try:
                
                # Import the main function from loris_sticker
                from py.loris.loris_sticker import main
                
                # Generate PDFs for both versions
                pdf_30stickers_bytes, pdf_exact_bytes = main(input_data)
                
                
                if not pdf_30stickers_bytes or not pdf_exact_bytes:
                    error_message = "An error occurred while generating the PDFs."
                    current_app.logger.error("PDF generation failed")
                else:
                    loris_stickers_ready = True
                    
            except Exception as e:
                error_message = f"An error occurred: {e}"
                current_app.logger.error("Exception in Loris fedex: %s", e)
                
        elif "show_table" in request.form:
            try:
                # Import the parse function from loris_sticker
                from py.loris.loris_sticker import process_input_data
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
                         loris_stickers_ready=loris_stickers_ready,
                         error_message=error_message, 
                         table_data=table_data, 
                         input_data=input_data, 
                         sku=sku, 
                         barcode=barcode, 
                         quantity=quantity, 
                         current_user=current_user)

def _validate_loris_sticker_input(input_data):
    """Return validated tab-separated sticker lines or an error message."""
    if not input_data or not str(input_data).strip():
        return None, 'No input data provided'

    lines = str(input_data).strip().split('\n')
    valid_lines = []
    for i, line in enumerate(lines):
        parts = line.strip().split('\t')
        if len(parts) != 3:
            continue

        sku, barcode, quantity = parts
        if not sku.strip():
            continue

        try:
            qty = int(quantity)
            if qty <= 0:
                current_app.logger.warning("Line %s has invalid quantity %s: %s", i + 1, qty, line)
                continue
        except ValueError:
            current_app.logger.warning("Line %s has non-numeric quantity: %s", i + 1, line)
            continue

        valid_lines.append(line)

    if not valid_lines:
        return None, (
            'No valid data found. Each line must have SKU, barcode, and quantity separated by tabs.'
        )

    return '\n'.join(valid_lines), None


@lori_automation_bp.route("/generate_stickers_api", methods=["POST"])
def generate_stickers_api():
    """Generate Loris sticker PDF in memory; variant=30_stickers|exact_stickers."""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        input_data = request.form.get("input_data")
        variant = (request.form.get("variant") or "30_stickers").strip()

        validated_input_data, validation_error = _validate_loris_sticker_input(input_data)
        if validation_error:
            return jsonify({'error': validation_error}), 400

        current_app.logger.info(
            "%s generate_stickers_api variant=%s input_data=%s...",
            session['username'],
            variant,
            validated_input_data[:200],
        )

        from py.loris.loris_sticker import main

        pdf_30stickers_bytes, pdf_exact_bytes = main(validated_input_data)
        if not pdf_30stickers_bytes or not pdf_exact_bytes:
            return jsonify({'error': 'PDF generation failed'}), 500

        if variant == "exact_stickers":
            pdf_bytes = pdf_exact_bytes
            download_name = "exact_stickers.pdf"
        else:
            pdf_bytes = pdf_30stickers_bytes
            download_name = "30_stickers.pdf"

        return send_file(
            io.BytesIO(pdf_bytes),
            download_name=download_name,
            as_attachment=False,
            mimetype='application/pdf',
        )

    except Exception as e:
        current_app.logger.error("Error generating stickers: %s", str(e), exc_info=True)
        return jsonify({'error': str(e)}), 500

@lori_automation_bp.route("/loris_shipping", methods=["GET"])
def loris_shipping():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    return render_template("loris_shipping.html", current_user=current_user)

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
            'skipped_files': process_result.get('skipped_files', []),
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
        skipped_files = []
        new_orders = 0
        batch_date = datetime.now().date()  # Use current date as batch date
        
        # Create manual directory for saving uploaded PDFs
        manual_dir = os.path.join(email_config['download_path'], 'manual')
        os.makedirs(manual_dir, exist_ok=True)
        
        for file in files:
            if file and file.filename.lower().endswith('.pdf'):
                try:
                    filename = file.filename
                    
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
                        skipped_files.append({
                            'filename': filename,
                            'reason': result.get('message', 'Order already exists in database')
                        })
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
            'skipped_files': skipped_files,
            'processed_files': len(processed_orders),
            'saved_directory': manual_dir,
            'count': new_orders  # For compatibility with existing frontend
        })
        
    except Exception as e:
        return jsonify({'error': f'处理上传文件时出错: {str(e)}'}), 500

@lori_automation_bp.route("/", methods=["GET", "POST"])
def lori_automation():
    if 'username' not in session:
        # For API requests, return JSON error
        if request.headers.get('Content-Type') == 'application/json' or 'application/json' in request.headers.get('Accept', ''):
            return jsonify({'error': 'Not authenticated'}), 401
        return redirect(url_for("auth.login"))
    
    if request.method == "GET":
        # Check if this is an API request for orders data
        if request.headers.get('Content-Type') == 'application/json' or 'application/json' in request.headers.get('Accept', ''):
            # Return the same data as get_orders endpoint
            connection = get_db_connection()
            if not connection:
                return jsonify({'error': 'Database connection failed'}), 500
            
            try:
                cursor = connection.cursor(dictionary=True)
                
                # Get all orders with their details and package info status (same query as get_orders)
                query = """
                    SELECT o.po_number, o.batch_date, o.email_date, o.order_date, 
                           o.delivery_date, o.cancel_date, o.ship_to_address, o.total_qty, 
                           o.total_cost, o.status, o.tracking, o.created_at, o.updated_at,
                           COUNT(oi.id) as item_count,
                           CASE WHEN p.po_number IS NOT NULL THEN 1 ELSE 0 END as has_box_info
                    FROM lori_orders o
                    LEFT JOIN lori_order_items oi ON o.po_number = oi.po_number
                    LEFT JOIN loris_pkg_info p ON o.po_number = p.po_number
                    GROUP BY o.po_number
                    ORDER BY o.batch_date DESC, o.created_at DESC
                """
                cursor.execute(query)
                orders = cursor.fetchall()
                
                # Process orders (same as get_orders)
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
                    
                    # Format dates for JSON serialization
                    def format_date(date_value, format_str='%Y-%m-%d'):
                        if date_value is None:
                            return None
                        if hasattr(date_value, 'strftime'):
                            return date_value.strftime(format_str)
                        else:
                            return str(date_value)
                    
                    order['batch_date'] = format_date(order['batch_date'])
                    order['email_date'] = format_date(order['email_date'], '%Y-%m-%d %H:%M:%S')
                    order['order_date'] = format_date(order['order_date'])
                    order['delivery_date'] = format_date(order['delivery_date'])
                    order['cancel_date'] = format_date(order['cancel_date'])
                    order['created_at'] = format_date(order['created_at'], '%Y-%m-%d %H:%M:%S')
                    order['updated_at'] = format_date(order['updated_at'], '%Y-%m-%d %H:%M:%S')
                
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
                if connection and connection.is_connected():
                    connection.close()
        else:
            # Regular web request - render template
            return render_template("loris_fedex.html", current_user=session['username'])
    
    elif request.method == "POST":
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
        
        # Get all orders with their details and package info status
        query = """
            SELECT o.po_number, o.batch_date, o.email_date, o.order_date, 
                   o.delivery_date, o.cancel_date, o.ship_to_address, o.total_qty, 
                   o.total_cost, o.status, o.tracking, o.created_at, o.updated_at,
                   COUNT(oi.id) as item_count,
                   CASE WHEN p.po_number IS NOT NULL THEN 1 ELSE 0 END as has_box_info
            FROM lori_orders o
            LEFT JOIN lori_order_items oi ON o.po_number = oi.po_number
            LEFT JOIN loris_pkg_info p ON o.po_number = p.po_number
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
            
            # Convert datetime objects to strings for JSON serialization (handle both datetime objects and strings)
            def format_date(date_value, format_str='%Y-%m-%d'):
                if date_value is None:
                    return None
                if hasattr(date_value, 'strftime'):
                    return date_value.strftime(format_str)
                else:
                    # Already a string, return as-is
                    return str(date_value)
            
            order['batch_date'] = format_date(order['batch_date'])
            order['email_date'] = format_date(order['email_date'], '%Y-%m-%d %H:%M:%S')
            order['order_date'] = format_date(order['order_date'])
            order['delivery_date'] = format_date(order['delivery_date'])
            order['cancel_date'] = format_date(order['cancel_date'])
            order['created_at'] = format_date(order['created_at'], '%Y-%m-%d %H:%M:%S')
            order['updated_at'] = format_date(order['updated_at'], '%Y-%m-%d %H:%M:%S')
        
        cursor.close()
        
        return jsonify({
            'success': True,
            'orders': orders,
            'total_orders': len(orders)
        })
        
    except Exception as e:
        current_app.logger.error("Error in get_orders: %s", str(e))
        import traceback
        current_app.logger.error("Traceback: %s", traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        if connection and connection.is_connected():
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
            cursor.close()
            connection.close()

@lori_automation_bp.route("/download_pivot_table", methods=["POST"])
def download_pivot_table():
    """Generate and download pivot table Excel file for selected orders"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cursor = connection.cursor(dictionary=True)
            
            # Fetch all items for selected orders
            placeholders = ','.join(['%s'] * len(po_numbers))
            query = f"""
                SELECT po_number, vendor_item, quantity, unit_price, upc
                FROM lori_order_items
                WHERE po_number IN ({placeholders})
                ORDER BY po_number, vendor_item
            """
            cursor.execute(query, po_numbers)
            items = cursor.fetchall()
            
            cursor.close()
            connection.close()
            
            if not items:
                return jsonify({'error': 'No items found for selected orders'}), 400
            
            from openpyxl.styles import Font, PatternFill, Alignment

            def style_header_row(worksheet, columns):
                header_font = Font(bold=True, size=12)
                header_fill = PatternFill(start_color='CCCCCC', end_color='CCCCCC', fill_type='solid')
                header_alignment = Alignment(horizontal='center', vertical='center')
                for col in columns:
                    cell = worksheet[f'{col}1']
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = header_alignment

            def style_total_row(worksheet, row_num, columns):
                total_font = Font(bold=True, size=11)
                total_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')
                total_alignment = Alignment(horizontal='right', vertical='center')
                for col in columns:
                    cell = worksheet[f'{col}{row_num}']
                    cell.font = total_font
                    cell.fill = total_fill
                    cell.alignment = total_alignment

            # Create pivot table data structure
            pivot_data = {}
            total_quantity = 0
            
            # Group by vendor_item (SKU)
            for item in items:
                sku = item['vendor_item'] or 'N/A'
                
                if sku not in pivot_data:
                    pivot_data[sku] = {
                        'sku': sku,
                        'quantity': 0,
                        'unit_price': item['unit_price'] or 0,
                        'upc': item['upc'] or ''
                    }
                
                qty = int(item['quantity']) or 0
                pivot_data[sku]['quantity'] += qty
                total_quantity += qty
                
                if item['unit_price']:
                    pivot_data[sku]['unit_price'] = item['unit_price']
                if item['upc']:
                    pivot_data[sku]['upc'] = item['upc']
            
            wb = Workbook()

            # Sheet 1: SKU pivot summary
            ws_pivot = wb.active
            ws_pivot.title = 'Pivot Summary'
            ws_pivot['A1'] = 'Vendor Item SKU'
            ws_pivot['B1'] = 'Sum of Quantity'
            ws_pivot['C1'] = 'Unit Price'
            ws_pivot['D1'] = 'UPC'
            style_header_row(ws_pivot, ['A', 'B', 'C', 'D'])
            
            row = 2
            for sku_data in sorted(pivot_data.values(), key=lambda x: x['sku']):
                ws_pivot[f'A{row}'] = sku_data['sku']
                ws_pivot[f'B{row}'] = sku_data['quantity']
                ws_pivot[f'C{row}'] = float(sku_data['unit_price']) if sku_data['unit_price'] else 0
                ws_pivot[f'D{row}'] = sku_data['upc']
                ws_pivot[f'C{row}'].number_format = '$#,##0.00'
                row += 1
            
            total_row = row
            ws_pivot[f'A{total_row}'] = 'TOTAL'
            ws_pivot[f'B{total_row}'] = total_quantity
            style_total_row(ws_pivot, total_row, ['A', 'B', 'C', 'D'])
            ws_pivot.column_dimensions['A'].width = 25
            ws_pivot.column_dimensions['B'].width = 18
            ws_pivot.column_dimensions['C'].width = 15
            ws_pivot.column_dimensions['D'].width = 20

            # Sheet 2: PO-level line item breakdown
            ws_detail = wb.create_sheet('PO Breakdown')
            ws_detail['A1'] = 'PO Number'
            ws_detail['B1'] = 'Vendor Item Code'
            ws_detail['C1'] = 'Quantity'
            style_header_row(ws_detail, ['A', 'B', 'C'])
            
            detail_row = 2
            detail_total = 0
            for item in items:
                qty = int(item['quantity']) or 0
                ws_detail[f'A{detail_row}'] = item['po_number'] or ''
                ws_detail[f'B{detail_row}'] = item['vendor_item'] or 'N/A'
                ws_detail[f'C{detail_row}'] = qty
                detail_total += qty
                detail_row += 1
            
            detail_total_row = detail_row
            ws_detail[f'A{detail_total_row}'] = 'TOTAL'
            ws_detail[f'C{detail_total_row}'] = detail_total
            style_total_row(ws_detail, detail_total_row, ['A', 'B', 'C'])
            ws_detail.column_dimensions['A'].width = 18
            ws_detail.column_dimensions['B'].width = 25
            ws_detail.column_dimensions['C'].width = 12
            
            # Save to memory
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            
            # Generate filename
            filename = f'Orders_Pivot_Table_{datetime.now().strftime("%Y-%m-%d")}.xlsx'
            
            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=filename
            )
            
        except Exception as e:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
            raise e
            
    except Exception as e:
        current_app.logger.error(f"Error generating pivot table: {str(e)}")
        return jsonify({'error': f'Failed to generate pivot table: {str(e)}'}), 500

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

@lori_automation_bp.route("/upload_tracking", methods=["POST"])
def upload_tracking():
    """Upload tracking numbers from Excel file and update database"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if not file.filename.endswith('.xlsx'):
        return jsonify({'error': 'Please upload an Excel (.xlsx) file'}), 400
    
    try:
        # Read Excel file
        wb = load_workbook(file)
        ws = wb.active
        
        # Get headers from first row
        headers = [cell.value for cell in ws[1]]
        
        # Find column indices
        reference_idx = headers.index('reference') if 'reference' in headers else None
        tracking_idx = headers.index('masterTrackingNumber') if 'masterTrackingNumber' in headers else None
        
        if reference_idx is None or tracking_idx is None:
            return jsonify({'error': 'Excel file must contain "reference" and "masterTrackingNumber" columns'}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor()
        updated_count = 0
        skipped_count = 0
        errors = []
        
        # Process each row (skip header)
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            try:
                reference = row[reference_idx]
                tracking_number = row[tracking_idx]
                
                if not reference or not tracking_number:
                    continue
                
                # Extract PO number from reference (format: 0588-005282)
                po_number = str(reference).strip()
                tracking_number = str(tracking_number).strip()
                
                # Update database
                cursor.execute("""
                    UPDATE lori_orders 
                    SET tracking = %s 
                    WHERE po_number = %s
                """, (tracking_number, po_number))
                
                if cursor.rowcount > 0:
                    updated_count += 1
                else:
                    skipped_count += 1
                    errors.append(f"PO {po_number} not found in database")
                
            except Exception as e:
                errors.append(f"Row {row_idx}: {str(e)}")
        
        connection.commit()
        cursor.close()
        connection.close()
        
        message = f'Successfully updated {updated_count} orders with tracking numbers'
        if skipped_count > 0:
            message += f', skipped {skipped_count} orders'
        
        return jsonify({
            'success': True,
            'message': message,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'errors': errors[:10]  # Return first 10 errors
        })
        
    except Exception as e:
        return jsonify({'error': f'Error processing file: {str(e)}'}), 500

@lori_automation_bp.route("/download_tracking_info", methods=["POST"])
def download_tracking_info():
    """Download tracking information for selected orders with tracking numbers"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        po_numbers = data.get('po_numbers', [])
        
        if not po_numbers:
            return jsonify({'error': 'No orders selected'}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor(dictionary=True)
        
        # Get selected orders that have tracking numbers
        placeholders = ', '.join(['%s'] * len(po_numbers))
        query = f"""
            SELECT po_number, tracking 
            FROM lori_orders 
            WHERE po_number IN ({placeholders})
            AND tracking IS NOT NULL AND tracking != ''
            ORDER BY po_number
        """
        cursor.execute(query, po_numbers)
        orders = cursor.fetchall()
        
        cursor.close()
        connection.close()
        
        if not orders:
            return jsonify({'error': 'No tracking information found for selected orders'}), 404
        
        # Create Excel workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Tracking Info"
        
        # Add headers
        ws['A1'] = 'Store Number'
        ws['B1'] = 'PO Number'
        ws['C1'] = 'Tracking Number'
        
        # Style headers
        from openpyxl.styles import Font, Alignment
        header_font = Font(bold=True)
        for col in ['A', 'B', 'C']:
            ws[f'{col}1'].font = header_font
            ws[f'{col}1'].alignment = Alignment(horizontal='center')
        
        # Add data
        row = 2
        for order in orders:
            po_number = order['po_number']
            tracking = order['tracking']
            
            # Extract store number (first 4 digits of PO)
            store_number = po_number[:4] if po_number else ''
            
            ws[f'A{row}'] = store_number
            ws[f'B{row}'] = po_number
            ws[f'C{row}'] = tracking
            row += 1
        
        # Adjust column widths
        ws.column_dimensions['A'].width = 15
        ws.column_dimensions['B'].width = 20
        ws.column_dimensions['C'].width = 20
        
        # Save to BytesIO
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        
        # Generate filename with current date
        filename = f'tracking_info_{datetime.now().strftime("%Y%m%d")}.xlsx'
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        return jsonify({'error': f'Error generating file: {str(e)}'}), 500

@lori_automation_bp.route("/add_tracking_number", methods=["POST"])
def add_tracking_number():
    """Add or update tracking number for a single order"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        po_number = data.get('po_number', '').strip()
        tracking_number = data.get('tracking_number', '').strip()
        
        if not po_number or not tracking_number:
            return jsonify({'error': 'PO number and tracking number are required'}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor()
        
        # Update the tracking number for this order
        cursor.execute("""
            UPDATE lori_orders 
            SET tracking = %s 
            WHERE po_number = %s
        """, (tracking_number, po_number))
        
        connection.commit()
        
        if cursor.rowcount > 0:
            cursor.close()
            connection.close()
            return jsonify({
                'success': True,
                'message': f'Tracking number updated successfully for PO {po_number}',
                'po_number': po_number,
                'tracking_number': tracking_number
            }), 200
        else:
            cursor.close()
            connection.close()
            return jsonify({'error': f'PO number {po_number} not found in database'}), 404
            
    except Exception as e:
        return jsonify({'error': f'Error updating tracking number: {str(e)}'}), 500

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
        files_downloaded = fetch_result.get('files_downloaded', 0)
        files_skipped = fetch_result.get('files_skipped', 0)
        
        message = f'完成！处理了 {fetch_result["total_emails_processed"]} 封邮件'
        if files_downloaded > 0:
            message += f'，下载了 {files_downloaded} 个PDF文件'
        if files_skipped > 0:
            message += f'，跳过了 {files_skipped} 个已存在的文件'
        message += f'，添加了 {process_result["new_orders"]} 个新订单到数据库'
        
        return jsonify({
            'success': True,
            'message': message,
            'total_emails_processed': fetch_result['total_emails_processed'],
            'total_files_found': fetch_result['total_files'],
            'files_downloaded': files_downloaded,
            'files_skipped': files_skipped,
            'skipped_files': fetch_result.get('skipped_files', []) + process_result.get('skipped_files', []),
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
            'skipped_files': fetch_result.get('skipped_files', []) + process_result.get('skipped_files', []),
            'new_orders': process_result['new_orders'],
            'skipped_orders': process_result['skipped_orders'],
            'batches': fetch_result['batches']
        })
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500


# --- Delete Operations Endpoints ---




@lori_automation_bp.route("/delete_pdf_data", methods=["POST", "GET"])
def delete_pdf_data():
    """Delete all PDF files and order data. Always returns JSON. Requires session auth."""
    # Require authentication to avoid accidental/unauthorized deletions
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    try:
        deleted_files = 0
        deleted_orders = 0
        deleted_items = 0
        
        # Connect to the database
        connection = mysql.connector.connect(**db_config)
        cursor = connection.cursor()
        
        # Delete from orders and related tables
        cursor.execute("DELETE FROM lori_order_items")
        deleted_items = cursor.rowcount
        
        cursor.execute("DELETE FROM lori_orders")
        deleted_orders = cursor.rowcount
        
        connection.commit()
        cursor.close()
        connection.close()
        
        # PDF cleanup no longer needed since files aren't saved to disk
        
        # Also clean up any temporary Excel files
        excel_directory = str(Config.OUTPUT_BASE_DIR)
        if os.path.exists(excel_directory):
            for filename in os.listdir(excel_directory):
                if filename.endswith(('.xlsx', '.xls')) and 'extracted_' in filename:
                    file_path = os.path.join(excel_directory, filename)
                    try:
                        os.remove(file_path)
                        deleted_files += 1
                    except Exception as e:
                        print(f"Error deleting Excel file {filename}: {str(e)}")
        
        return jsonify({
            'success': True,
            'message': f'Successfully deleted {deleted_orders} orders, {deleted_items} order items, and {deleted_files} files'
        }), 200
        
    except Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        # Ensure we never emit HTML content to callers
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

# --- FedEx Integration Endpoints (Alternative Implementation) ---


def load_fedex_credentials():
    """Load FedEx API credentials from credentials.json"""
    try:
        with open('credentials.json', 'r') as f:
            credentials = json.load(f)
            # Accept multiple possible keys for billing/account to be tolerant of different credential formats
            billing_default = (
                credentials.get('fedex_billing_account')
                or credentials.get('fedex_billing_number')
                or credentials.get('fedex_billing')
            )

            account_number = (
                credentials.get('fedex_account_number')
                or credentials.get('fedex_account')
                or credentials.get('account_number')
            )

            return {
                'api_key': credentials.get('fedex_api_key'),
                'secret_key': credentials.get('fedex_secret_key'),
                'account_number': account_number,
                # credentials-level billing default (optional)
                'billing_account_default': billing_default,
                # Production-only integration: default environment is 'production'
                'environment': credentials.get('fedex_environment', 'production')
            }
    except Exception as e:
        current_app.logger.error(f"Error loading FedEx credentials: {str(e)}")
        return None

def get_fedex_auth_token(credentials):
    """Get OAuth token from FedEx API"""
    try:
        # Choose OAuth endpoint based on credentials environment
        # supported values: 'sandbox' or 'production' (default)
        env = (credentials.get('environment') or 'production').lower()
        if env == 'sandbox' or 'sandbox' in env:
            auth_url = "https://apis-sandbox.fedex.com/oauth/token"
        else:
            auth_url = "https://apis.fedex.com/oauth/token"


        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {
            'grant_type': 'client_credentials',
            'client_id': credentials['api_key'],
            'client_secret': credentials['secret_key']
        }

        response = requests.post(auth_url, headers=headers, data=data)
        # Avoid logging full token/secret; log masked token info
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get('access_token')
            masked = f"***{access_token[-8:]}" if access_token else None
            return access_token
        else:
            current_app.logger.error(f"FedEx auth failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        current_app.logger.error(f"Error getting FedEx auth token: {str(e)}")
        return None





@lori_automation_bp.route("/save_package_info", methods=["POST"])
def save_package_info():
    """Save package information for a PO number"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_number = data.get('poNumber')
        box_dimensions = data.get('boxDimensions', [])
        fedex_service_type = data.get('fedexServiceType')
        fedex_packaging_type = data.get('fedexPackagingType')
        special_instructions = data.get('specialInstructions', '')
        
        if not po_number:
            return jsonify({'error': 'PO number is required'}), 400
        
        if not box_dimensions:
                       return jsonify({'error': 'At least one box is required'}), 400
        
        # Calculate total weight
        total_weight = sum(float(box.get('weight', 0)) for box in box_dimensions)
        
        # Create table if it doesn't exist
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor()
        
        # Insert or update package info
        upsert_query = """
            INSERT INTO loris_pkg_info 
            (po_number, box_count, box_dimensions, total_weight, 
             fedex_service_type, fedex_packaging_type, special_instructions, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
            box_count = VALUES(box_count),
            box_dimensions = VALUES(box_dimensions),
            total_weight = VALUES(total_weight),
            fedex_service_type = VALUES(fedex_service_type),
            fedex_packaging_type = VALUES(fedex_packaging_type),
            special_instructions = VALUES(special_instructions),
            updated_at = CURRENT_TIMESTAMP
        """
        
        cursor.execute(upsert_query, (
            po_number, len(box_dimensions), json.dumps(box_dimensions), 
            total_weight, fedex_service_type, fedex_packaging_type, 
            special_instructions, session.get('username', 'unknown')
        ))
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': 'Package information saved successfully',
            'po_number': po_number,
            'box_count': len(box_dimensions),
            'total_weight': total_weight
        })
        
    except Exception as e:
        current_app.logger.error(f"Error saving package info: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route("/get_package_info/<po_number>", methods=["GET"])
def get_package_info(po_number):
    """Get package information for a PO number"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor(dictionary=True)
        
        query = "SELECT * FROM loris_pkg_info WHERE po_number = %s"
        cursor.execute(query, (po_number,))
        result = cursor.fetchone()
        
        cursor.close()
        connection.close()
        
        if result:
            # Parse JSON box_dimensions
            if result['box_dimensions']:
                result['box_dimensions'] = json.loads(result['box_dimensions'])
            return jsonify(result)
        else:
            return jsonify({'exists': False})
        
    except Exception as e:
        current_app.logger.error(f"Error getting package info: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route("/delete_package_info/<po_number>", methods=["DELETE"])
def delete_package_info(po_number):
    """Delete package information for a PO number"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        if not po_number:
            return jsonify({'error': 'PO number is required'}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor()
        
        # Check if package info exists
        check_query = "SELECT COUNT(*) FROM loris_pkg_info WHERE po_number = %s"
        cursor.execute(check_query, (po_number,))
        count = cursor.fetchone()[0]
        
        if count == 0:
            cursor.close()
            connection.close()
            return jsonify({'error': 'Package info not found for this PO number'}), 404
        
        # Delete package info
        delete_query = "DELETE FROM loris_pkg_info WHERE po_number = %s"
        cursor.execute(delete_query, (po_number,))
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': f'Package information deleted successfully for PO {po_number}',
            'po_number': po_number
        })
        
    except Exception as e:
        current_app.logger.error(f"Error deleting package info: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route("/generate_batch_fedex_csv", methods=["POST"])
def generate_batch_fedex_csv():
    """Generate FedEx CSV for multiple selected PO numbers"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('poNumbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Get all order and package information
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        cursor = connection.cursor(dictionary=True)
        
        csv_rows = []
        processed_orders = []
        
        for po_number in po_numbers:
            # Get order info
            order_query = """
                SELECT o.po_number, o.batch_date, o.ship_to_address, o.total_qty, o.total_cost, o.status,
                       s.store_name, s.company, s.address1, s.address2, s.city, s.state, s.zip_code, s.phone
                FROM lori_orders o
                LEFT JOIN loris_store_info s ON o.po_number LIKE CONCAT(s.store_number, '-%')
                WHERE o.po_number = %s
            """
            cursor.execute(order_query, (po_number,))
            order = cursor.fetchone()
            
            if not order:
                continue
            
            # Get package info
            pkg_query = "SELECT * FROM loris_pkg_info WHERE po_number = %s"
            cursor.execute(pkg_query, (po_number,))
            pkg_info = cursor.fetchone()
            
            if not pkg_info:
                continue
            
            # Parse recipient address
            ship_to_lines = order['ship_to_address'].split('\n') if order['ship_to_address'] else []
            recipient_info = parse_fedex_address(ship_to_lines)
            
            # Parse box dimensions
            box_dimensions = json.loads(pkg_info['box_dimensions']) if pkg_info['box_dimensions'] else []
            
            # Create CSV row
            csv_row = {
                'Recipient Company': recipient_info['company'],
                'Recipient Name': recipient_info['name'],
                'Recipient Address 1': recipient_info['address1'],
                'Recipient Address 2': recipient_info['address2'],
                'Recipient City': recipient_info['city'],
                'Recipient State': recipient_info['state'],
                'Recipient Postal Code': recipient_info['zip'],
                'Recipient Country': 'US',
                'Recipient Phone': recipient_info['phone'],
                'Service Type': map_fedex_service_type(pkg_info['fedex_service_type']),
                'Package Type': map_fedex_packaging_type(pkg_info['fedex_packaging_type']),
                'Weight': pkg_info['total_weight'],
                'Declared Value': order['total_cost'],
                'Reference 1': po_number,
                'Reference 2': f"Batch: {order['batch_date']}",
                'Special Instructions': pkg_info['special_instructions'] or '',
                'Length': box_dimensions[0]['length'] if box_dimensions else '12',
                'Width': box_dimensions[0]['width'] if box_dimensions else '12',
                'Height': box_dimensions[0]['height'] if box_dimensions else '6'
            }
            
            csv_rows.append(csv_row)
            processed_orders.append(po_number)
        
        if not csv_rows:
            return jsonify({'error': 'No valid orders found with package information'}), 400
        
        # Generate CSV file in temporary directory
        import tempfile
        temp_dir = tempfile.mkdtemp()
        output_dir = temp_dir
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_filename = f"fedex_batch_import_{timestamp}.csv"
        csv_filepath = os.path.join(output_dir, csv_filename)
        
        import csv
        with open(csv_filepath, 'w', newline='', encoding='utf-8') as csvfile:
            if csv_rows:
                writer = csv.DictWriter(csvfile, fieldnames=csv_rows[0].keys())
                writer.writeheader()
                writer.writerows(csv_rows)
        
        # Update order statuses to 'SHIPPED'
        #if processed_orders:
        #    placeholders = ','.join(['%s'] * len(processed_orders))
        #    update_query = f"UPDATE lori_orders SET status = 'SHIPPED' WHERE po_number IN ({placeholders})"
        #    cursor.execute(update_query, processed_orders)
        #    connection.commit()
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': f'Generated FedEx CSV for {len(processed_orders)} orders',
            'filename': csv_filename,
            'download_url': f'/download_fedex_file/{csv_filename}',
            'processed_orders': processed_orders
        })
        
    except Exception as e:
        current_app.logger.error(f"Error generating batch FedEx CSV: {str(e)}")
        return jsonify({'error': str(e)}), 500


def parse_fedex_address(ship_to_lines):
    """Improved address parsing for FedEx format"""
    recipient_info = {
        'name': 'Recipient',
        'company': '',
        'address1': '',
        'address2': '',
        'city': '',
        'state': '',
        'zip': '',
        'phone': ''
    }
    
    if not ship_to_lines or len(ship_to_lines) == 0:
        return recipient_info
    
    # Clean up lines - remove empty lines and strip whitespace
    clean_lines = [line.strip() for line in ship_to_lines if line.strip()]
    
    if not clean_lines:
        return recipient_info
    
    # First line is usually company/name
    company = clean_lines[0]
    recipient_info['company'] = company
    recipient_info['name'] = company

    # If we only have one line, return it as the name
    if len(clean_lines) == 1:
        return recipient_info

    # Find the line with city, state, zip (usually the last line)
    city_state_zip_line = ''
    index_csz = -1

    # Look for the line that contains state and ZIP pattern (more flexible regex)
    for i, line in enumerate(clean_lines[1:], 1):  # Skip first line (name/company)
        # Look for patterns like "Houston TX 77074" or "Houston, TX 77074-1234"
        if re.search(r'\b[A-Z]{2}\b\s+\d{5}', line):
            city_state_zip_line = line
            index_csz = i
            break

    # Determine candidates between company and city/state/zip (or all remaining lines)
    address_candidates = clean_lines[1:index_csz] if index_csz != -1 else clean_lines[1:]

    street_lines = []
    company_continuations = []

    # Heuristic to detect street lines: start with number or contain common street tokens
    street_token_re = re.compile(r'(^\d+\s)|\b(AVE|AVENUE|ST|STREET|RD|ROAD|HWY|HIGHWAY|BLVD|DR|DRIVE|LN|LANE|PKWY|PARKWAY|CT|COURT|WAY|PL|PLACE)\b', re.IGNORECASE)

    for line in address_candidates:
        if street_token_re.search(line):
            street_lines.append(line)
        else:
            company_continuations.append(line)

    # Merge any company continuation lines into the company field
    if company_continuations:
        merged = ' '.join(company_continuations).strip()
        # Avoid duplicating if already present
        if merged and merged not in recipient_info['company']:
            recipient_info['company'] = (recipient_info['company'] + ' ' + merged).strip()
            recipient_info['name'] = recipient_info['company']

    # If no street lines detected, fallback: use the first candidate as address1
    if not street_lines and address_candidates:
        street_lines = [address_candidates[0]]

    # Assign address lines (max 2)
    if street_lines:
        recipient_info['address1'] = street_lines[0]
        if len(street_lines) > 1:
            recipient_info['address2'] = street_lines[1]
        # If more street lines exist, append to address2
        if len(street_lines) > 2:
            extra = ' '.join(street_lines[2:])
            recipient_info['address2'] = (recipient_info['address2'] + ' ' + extra).strip() if recipient_info['address2'] else extra

    # Parse city, state, zip from the identified line
    if city_state_zip_line:
        # More comprehensive regex to handle various formats:
        # "Houston TX 77074", "Houston, TX 77074", "Houston TX 77074-1234"
        patterns = [
            r'^(.+?)\s*,?\s*\b([A-Z]{2})\b\s+(\d{5}(?:-\d{4})?)\b.*$',  # With optional extended ZIP
            r'^(.+?)\s+([A-Z]{2})\s+(\d{5})\b.*$',  # Simple format
        ]

        for pattern in patterns:
            match = re.search(pattern, city_state_zip_line)
            if match:
                recipient_info['city'] = match.group(1).strip().rstrip(',')
                recipient_info['state'] = match.group(2)
                recipient_info['zip'] = match.group(3)
                break
    
    # Look for phone numbers in any line
    for line in clean_lines:
        # Look for phone number patterns
        phone_match = re.search(r'(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})', line)
        if phone_match:
            recipient_info['phone'] = phone_match.group(1)
            break
    
    return recipient_info


@lori_automation_bp.route("/fedexuploadtodraft", methods=["POST"])  # Original route
@lori_automation_bp.route("/fedexupload", methods=["POST"])          # Alias for frontend
@lori_automation_bp.route("/api/fedexupload", methods=["POST"])      # Alias for proxy/path prefix
def fedex_upload_to_draft():
    """Create FedEx shipping draft using simplified file-based approach"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_number = data.get('poNumber')
        service_type = data.get('serviceType')
        packaging_type = data.get('packagingType')
        special_instructions = data.get('specialInstructions', '')
        boxes = data.get('boxes', [])
        
        # Validate required fields
        if not po_number or not service_type or not packaging_type or not boxes:
            return jsonify({'error': 'Missing required fields'}), 400
        
        
        # Get order details from database
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cursor = connection.cursor(dictionary=True)
            
            # Get order info
            order_query = """
                SELECT po_number, batch_date, ship_to_address, total_qty, total_cost, status
                FROM lori_orders 
                WHERE po_number = %s
            """
            cursor.execute(order_query, (po_number,))
            order = cursor.fetchone()
            
            if not order:
                return jsonify({'error': 'Order not found'}), 404
            
            # Get order items
            items_query = """
                SELECT vendor_item, color, quantity, description, unit_price, extended_cost
                FROM lori_order_items 
                WHERE po_number = %s
            """
            cursor.execute(items_query, (po_number,))
            items = cursor.fetchall()
            
            # Get store information for shipper address
            store_number = po_number[:4] if po_number else ''
            store_query = """
                SELECT store_name, company, phone, address1, address2, address3, 
                       city, state, zip_code
                FROM loris_store_info 
                WHERE store_number = %s
            """
            cursor.execute(store_query, (store_number,))
            store_info = cursor.fetchone()
            
            # Parse recipient address from ship-to lines
            ship_to_lines = order['ship_to_address'].split('\n') if order['ship_to_address'] else []
            
            recipient_info = {
                'name': 'Recipient',
                'company': '',
                'address1': '',
                'address2': '',
                'city': '',
                'state': '',
                'zip': '',
                'phone': ''
            }
            
            # Simple address parsing
            if ship_to_lines:
                recipient_info['company'] = ship_to_lines[0].strip() if len(ship_to_lines) > 0 else ''
                recipient_info['name'] = recipient_info['company']  # Use company as name
                
                # Find address lines and city/state/zip
                address_lines = []
                city_state_zip = ''
                
                for i, line in enumerate(ship_to_lines[1:], 1):
                    line = line.strip()
                    # Check if this looks like city, state, zip
                    if re.match(r'^.+\s+[A-Z]{2}\s+\d{5}', line):
                        city_state_zip = line
                        break
                    else:
                        address_lines.append(line)
                
                # Assign address lines
                if address_lines:
                    recipient_info['address1'] = address_lines[0] if len(address_lines) > 0 else ''
                    recipient_info['address2'] = address_lines[1] if len(address_lines) > 1 else ''
                
                # Parse city, state, zip
                if city_state_zip:
                    match = re.match(r'^(.+?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?).*$', city_state_zip)
                    if match:
                        recipient_info['city'] = match.group(1).strip()
                        recipient_info['state'] = match.group(2)
                        recipient_info['zip'] = match.group(3)
            
            # Format shipper info from store data
            shipper_info = {
                'name': store_info.get('store_name', 'Loris Store') if store_info else 'Loris Store',
                'company': store_info.get('company', 'Loris') if store_info else 'Loris',
                'address1': store_info.get('address1', '') if store_info else '',
                'address2': store_info.get('address2', '') if store_info else '',
                'city': store_info.get('city', '') if store_info else '',
                'state': store_info.get('state', '') if store_info else '',
                'zip': store_info.get('zip_code', '') if store_info else '',
                'phone': store_info.get('phone', '') if store_info else ''
            }
            
            # Create shipment data structure
            shipment_data = {
                'order_info': {
                    'po_number': po_number,
                    'batch_date': str(order.get('batch_date', '')),
                    'total_qty': order.get('total_qty', 0),
                    'total_cost': float(order.get('total_cost', 0))
                },
                'shipper': shipper_info,
                'recipient': recipient_info,
                'service': {
                    'type': service_type,
                    'packaging': packaging_type,
                    'instructions': special_instructions
                },
                'packages': boxes,
                'items': [dict(item) for item in items] if items else []
            }
            
            # Generate tracking number placeholder
            import time
            tracking_number = f"FX{int(time.time())}{po_number[-4:]}"
            
            # Define pkg_info and box_dimensions for CSV
            pkg_info = {
                'fedex_service_type': service_type,
                'fedex_packaging_type': packaging_type,
                'total_weight': sum(float(box.get('weight', 0)) for box in boxes),
                'special_instructions': special_instructions
            }
            box_dimensions = boxes
            
            # Create temporary output directory
            import tempfile
            output_dir = tempfile.mkdtemp()
            
            # Save detailed JSON
            json_filename = f"shipment_{po_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            json_filepath = os.path.join(output_dir, json_filename)
            
            with open(json_filepath, 'w') as f:
                json.dump(shipment_data, f, indent=2, default=str)
            
            # Generate FedEx Ship Manager CSV format
            csv_data = {
                'Recipient Company': shipment_data['recipient']['company'],
                'Recipient Name': shipment_data['recipient']['name'],
                'Recipient Address 1': shipment_data['recipient']['address1'],
                'Recipient Address 2': shipment_data['recipient']['address2'],
                'Recipient City': shipment_data['recipient']['city'],
                'Recipient State': shipment_data['recipient']['state'],
                'Recipient Postal Code': shipment_data['recipient']['zip'],
                'Recipient Country': 'US',
                'Recipient Phone': shipment_data['recipient']['phone'],
                'Service Type': map_fedex_service_type(pkg_info['fedex_service_type']),
                'Package Type': map_fedex_packaging_type(pkg_info['fedex_packaging_type']),
                'Weight': pkg_info['total_weight'],
                'Declared Value': order['total_cost'],
                'Reference 1': po_number,
                'Reference 2': f"Batch: {order['batch_date']}",
                'Special Instructions': pkg_info['special_instructions'] or '',
                'Length': box_dimensions[0]['length'] if box_dimensions else '12',
                'Width': box_dimensions[0]['width'] if box_dimensions else '12',
                'Height': box_dimensions[0]['height'] if box_dimensions else '6'
            }
            
            csv_filename = f"fedex_import_{po_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            csv_filepath = os.path.join(output_dir, csv_filename)
            
            import csv
            with open(csv_filepath, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=csv_data.keys())
                writer.writeheader()
                writer.writerow(csv_data)
            
            # Save to database
            try:
                create_table_query = """
                    CREATE TABLE IF NOT EXISTS fedex_shipments (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        po_number VARCHAR(50),
                        tracking_number VARCHAR(50),
                        service_type VARCHAR(100),
                        packaging_type VARCHAR(100),
                        special_instructions TEXT,
                        boxes_data JSON,
                        shipment_data JSON,
                        json_file_path TEXT,
                        csv_file_path TEXT,
                        status VARCHAR(50) DEFAULT 'DRAFT',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_by VARCHAR(100)
                    )
                """
                cursor.execute(create_table_query)
                
                # Insert shipment record
                insert_query = """
                    INSERT INTO fedex_shipments 
                    (po_number, tracking_number, service_type, packaging_type, 
                     special_instructions, boxes_data, shipment_data, json_file_path, 
                     csv_file_path, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                cursor.execute(insert_query, (
                    po_number, tracking_number, service_type, packaging_type, 
                    special_instructions, json.dumps(boxes), json.dumps(shipment_data, default=str),
                    json_filepath, csv_filepath,
                    session.get('username', 'unknown')
                ))
                
                connection.commit()
                
            except Exception as db_error:
                current_app.logger.error(f"Error saving to database: {str(db_error)}")
            
            cursor.close()
            connection.close()
            
            return jsonify({
                'success': True,
                'message': 'FedEx shipment draft created successfully',
                'trackingNumber': tracking_number,
                'jsonFile': json_filename,
                'csvFile': csv_filename,
                'downloadUrl': f'/download_fedex_files/{po_number}',
                'instructions': [
                    'Shipment files have been generated successfully!',
                    f'📄 Detailed data: {json_filename}',
                    f'📊 FedEx CSV import: {csv_filename}',
                    '',
                    '📋 Next steps:',
                    '1. Download the CSV file below',
                    '2. Open FedEx Ship Manager website',
                    '3. Use "Import" function to upload the CSV',
                    '4. Print shipping labels',
                    '5. Update tracking number when available'
                ]
            })
            
        except Exception as db_error:
            current_app.logger.error(f"Database error: {str(db_error)}")
            return jsonify({'error': f'Database error: {str(db_error)}'}), 500
        
        finally:
            if connection and connection.is_connected():
                connection.close()
    
    except Exception as e:
        current_app.logger.error(f"Error in fedex_upload_to_draft: {str(e)}")
        return jsonify({'error': str(e)}), 500


def map_fedex_service_type(service_type):
    """Map internal service types to FedEx Ship Manager values"""
    mapping = {
        'FEDEX_GROUND': 'FedEx Ground',
        'FEDEX_EXPRESS_SAVER': 'FedEx Express Saver',
        'FEDEX_2_DAY': 'FedEx 2Day',
        'STANDARD_OVERNIGHT': 'FedEx Standard Overnight',
        'PRIORITY_OVERNIGHT': 'FedEx Priority Overnight',
        'FIRST_OVERNIGHT': 'FedEx First Overnight'
    }
    return mapping.get(service_type, service_type)


def map_fedex_packaging_type(packaging_type):
    """Map internal packaging types to FedEx Ship Manager values"""
    mapping = {
        'YOUR_PACKAGING': 'Your Packaging',
        'FEDEX_BOX': 'FedEx Box',
        'FEDEX_ENVELOPE': 'FedEx Envelope',
        'FEDEX_PAK': 'FedEx Pak',
        'FEDEX_TUBE': 'FedEx Tube'
    }
    return mapping.get(packaging_type, packaging_type)

@lori_automation_bp.route('/download_fedex_files/<po_number>', methods=['GET'])
def download_fedex_files(po_number):
    """Download FedEx shipment files for a given PO number"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        # Find the latest files for this PO number
        output_dir = os.path.join(os.getcwd(), 'output', 'fedex_shipments')
        
        if not os.path.exists(output_dir):
            return jsonify({'error': 'No FedEx files found'}), 404
        
        # Find files matching the PO number pattern
        json_files = []
        csv_files = []
        
        for filename in os.listdir(output_dir):
            if po_number in filename:
                if filename.endswith('.json'):
                    json_files.append(filename)
                elif filename.endswith('.csv'):
                    csv_files.append(filename)
        
        if not json_files and not csv_files:
            return jsonify({'error': f'No files found for PO {po_number}'}), 404
        
        # Get the most recent files
        latest_json = max(json_files) if json_files else None
        latest_csv = max(csv_files) if csv_files else None
        
        return jsonify({
            'success': True,
            'po_number': po_number,
            'json_file': latest_json,
            'csv_file': latest_csv,
            'json_download': f'/download_fedex_file/{latest_json}' if latest_json else None,
            'csv_download': f'/download_fedex_file/{latest_csv}' if latest_csv else None
        })
        
    except Exception as e:
        current_app.logger.error(f"Error in download_fedex_files: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route('/download_fedex_file/<filename>', methods=['GET'])
def download_fedex_file(filename):
    """Download a specific FedEx file"""
    if 'username' not in session:
        return redirect('/login')
    
    try:
        output_dir = os.path.join(os.getcwd(), 'output', 'fedex_shipments')
        file_path = os.path.join(output_dir, filename)
        
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
        
        from flask import send_file
        return send_file(file_path, as_attachment=True, download_name=filename)
        
    except Exception as e:
        current_app.logger.error(f"Error downloading file {filename}: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route('/download_original_pdf/<po_number>', methods=['GET'])
def download_original_pdf(po_number):
    """Download original PDF file for a given PO number"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        # Get the base download path
        base_path = email_config['download_path']
        
        if not os.path.exists(base_path):
            return jsonify({'error': 'PDF directory not found'}), 404
        
        # Search for PDF file containing this PO number
        # First, try to find the PO number in the database to get batch date
        found_file = None
        batch_date = None
        
        try:
            connection = mysql.connector.connect(**db_config)
            cursor = connection.cursor(dictionary=True)
            
            # Get batch date for this PO number
            cursor.execute("""
                SELECT DISTINCT batch_date 
                FROM loris_orders 
                WHERE po_number = %s
                LIMIT 1
            """, (po_number,))
            
            result = cursor.fetchone()
            if result:
                batch_date = result['batch_date']
                
        except Error as e:
            current_app.logger.error(f"Database error when looking up batch date: {str(e)}")
        finally:
            if 'connection' in locals() and connection.is_connected():
                cursor.close()
                connection.close()
        
        # Search strategy 1: If we have batch date, look in that specific directory first
        if batch_date:
            batch_dir = os.path.join(base_path, batch_date)
            if os.path.exists(batch_dir):
                for filename in os.listdir(batch_dir):
                    if filename.lower().endswith('.pdf'):
                        filepath = os.path.join(batch_dir, filename)
                        # Check if PO number appears in the PDF content or filename
                        if po_number in filename or check_pdf_contains_po(filepath, po_number):
                            found_file = filepath
                            break
        
        # Search strategy 2: If not found in batch dir, search all directories
        if not found_file:
            for item in os.listdir(base_path):
                item_path = os.path.join(base_path, item)
                if os.path.isdir(item_path):
                    for filename in os.listdir(item_path):
                        if filename.lower().endswith('.pdf'):
                            filepath = os.path.join(item_path, filename)
                            # Check if PO number appears in the PDF content or filename
                            if po_number in filename or check_pdf_contains_po(filepath, po_number):
                                found_file = filepath
                                break
                    if found_file:
                        break
        
        if not found_file:
            return jsonify({'error': f'Original PDF not found for PO {po_number}'}), 404
        
        # Generate download filename
        original_filename = os.path.basename(found_file)
        download_name = f"{po_number}_original_{original_filename}"
        
        return send_file(found_file, as_attachment=True, download_name=download_name)
        
    except Exception as e:
        current_app.logger.error(f"Error downloading original PDF for {po_number}: {str(e)}")
        return jsonify({'error': str(e)}), 500


def check_pdf_contains_po(filepath, po_number):
    """Check if PDF file contains the given PO number"""
    try:
        with open(filepath, 'rb') as file:
            reader = PdfReader(file)
            text_content = ""
            for page in reader.pages:
                text_content += page.extract_text()
            
            # Look for PO number in the text content
            return po_number in text_content
            
    except Exception as e:
        current_app.logger.error(f"Error reading PDF {filepath}: {str(e)}")
        return False


@lori_automation_bp.route("/process_selected_orders", methods=["POST"])
def process_selected_orders():
    """Process selected orders: generate Excel file and update status to shipped"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Get all order and package information
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
        
        try:
            cursor = connection.cursor(dictionary=True)
            
            # Prepare Excel data
            excel_data = []
            processed_orders = []
            
            # Process each order
            for po_number in po_numbers:
                # Get order info
                order_query = """
                    SELECT po_number, batch_date, ship_to_address, total_qty, total_cost, status
                    FROM lori_orders 
                    WHERE po_number = %s
                """
                cursor.execute(order_query, (po_number,))
                order = cursor.fetchone()
                
                if not order:
                    current_app.logger.warning(f"Order not found: {po_number}")
                    continue
                
                # Get order items
                items_query = """
                    SELECT vendor_item, quantity, upc, description, unit_price, extended_cost
                    FROM lori_order_items 
                    WHERE po_number = %s
                """
                cursor.execute(items_query, (po_number,))
                items = cursor.fetchall()
                
                # Get store information for additional contact details
                store_number = po_number[:4] if po_number else ''
                store_query = """
                    SELECT store_name, company, phone, address1, address2, address3, 
                           city, state, zip_code
                    FROM loris_store_info 
                    WHERE store_number = %s
                """
                cursor.execute(store_query, (store_number,))
                store_info = cursor.fetchone()
                
                # Get package information
                package_query = "SELECT * FROM loris_pkg_info WHERE po_number = %s"
                cursor.execute(package_query, (po_number,))
                package_info = cursor.fetchone()
                
                # Build recipient info primarily from DB (store_info). Fallback to parsed ship_to only if DB missing.
                recipient_from_db = {}
                if store_info:
                    recipient_from_db = {
                        'name': store_info.get('store_name') or store_info.get('company') or '',
                        'company': store_info.get('company') or store_info.get('store_name') or '',
                        'address1': store_info.get('address1') or '',
                        'address2': store_info.get('address2') or '',
                        'address3': store_info.get('address3') or '',
                        'city': store_info.get('city') or '',
                        'state': store_info.get('state') or '',
                        'zip': store_info.get('zip_code') or '',
                        'phone': store_info.get('phone') or ''
                    }
                
                # If DB is missing, fallback to parsed HTML address
                if not store_info:
                    ship_to_lines = order['ship_to_address'].split('\n') if order['ship_to_address'] else []
                    recipient_from_db = parse_fedex_address(ship_to_lines)
                
                # Normalize minimal fields (avoid None)
                for k in ['name','company','address1','address2','address3','city','state','zip','phone']:
                    if recipient_from_db.get(k) is None:
                        recipient_from_db[k] = ''
                
                # Parse box dimensions
                if package_info and package_info.get('box_dimensions'):
                    try:
                        box_dimensions = json.loads(package_info['box_dimensions']) if isinstance(package_info['box_dimensions'], str) else package_info['box_dimensions']
                    except:
                        box_dimensions = [{'length': 0, 'width': 0, 'height': 0, 'weight': 0}]
                else:
                    box_dimensions = [{'length': 0, 'width': 0, 'height': 0, 'weight': 0}]
                
                # Build one shipment-level row, then package rows per box (multi-package format)
                sender_name = "Elaine Xu"
                sender_company = "Fashion Distribution Center"
                sender_phone = "6265665243"
                sender_address1 = "827 S Lawson St"
                sender_address2 = ''
                sender_city = "City of Industry"
                sender_state = "CA"
                sender_zip = "91748"

                # Determine service and package type codes
                service_code = (package_info.get('fedex_service_type')
                                if package_info and package_info.get('fedex_service_type') else 'FEDEX_GROUND')
                package_type_code = (package_info.get('fedex_packaging_type')
                                     if package_info and package_info.get('fedex_packaging_type') else 'YOUR_PACKAGING')

                # Shipment-level row (sender/recipient + service)
                shipment_row = {
                    'shipmentReference': po_number,
                    'recordType': '',
                    'numberOfPackages': '',
                    'length': '',
                    'width': '',
                    'height': '',
                    'packageWeight': '',
                    'packageType': package_type_code,
                    'weightUnits': 'LBS',
                    'currencyType': 'USD',
                    'senderContactName': sender_name,
                    'senderCompany': sender_company,
                    'senderContactNumber': sender_phone,
                    'senderEmail': '',
                    'senderLine1': sender_address1,
                    'senderLine2': sender_address2,
                    'senderCity': sender_city,
                    'senderState': sender_state,
                    'senderPostcode': sender_zip,
                    'senderCountry': 'US',
                    'recipientContactName': recipient_from_db.get('name') or recipient_from_db.get('company'),
                    'recipientCompany': recipient_from_db.get('company', ''),
                    'recipientContactNumber': recipient_from_db.get('phone', ''),
                    'recipientEmail': '',
                    'recipientLine1': recipient_from_db.get('address1', ''),
                    'recipientLine2': recipient_from_db.get('address2', ''),
                    'recipientLine3': recipient_from_db.get('address3', ''),
                    'recipientCity': recipient_from_db.get('city', ''),
                    'recipientState': recipient_from_db.get('state', ''),
                    'recipientPostcode': recipient_from_db.get('zip', ''),
                    'recipientCountry': 'US',
                    'serviceType': service_code,
                    'paymentType': 'THIRD_PARTY',
                    'payerAccountNumber': '205200696'
                }
                excel_data.append(shipment_row)

                # Package-level rows (dimensions/weight only)
                for box in box_dimensions:
                    package_row = {
                        'shipmentReference': po_number,
                        'recordType': 'PACKAGE',
                        'numberOfPackages': 1,
                        'length': box.get('length', 0),
                        'width': box.get('width', 0),
                        'height': box.get('height', 0),
                        'packageWeight': box.get('weight', 0),
                        'packageType': '',
                        'weightUnits': '',
                        'currencyType': '',
                        'senderContactName': '',
                        'senderCompany': '',
                        'senderContactNumber': '',
                        'senderEmail': '',
                        'senderLine1': '',
                        'senderLine2': '',
                        'senderCity': '',
                        'senderState': '',
                        'senderPostcode': '',
                        'senderCountry': '',
                        'recipientContactName': '',
                        'recipientCompany': '',
                        'recipientContactNumber': '',
                        'recipientEmail': '',
                        'recipientLine1': '',
                        'recipientLine2': '',
                        'recipientLine3': '',
                        'recipientCity': '',
                        'recipientState': '',
                        'recipientPostcode': '',
                        'recipientCountry': '',
                        'serviceType': '',
                        'paymentType': '',
                        'payerAccountNumber': ''
                    }
                    excel_data.append(package_row)
                
                processed_orders.append(po_number)
            
            if not excel_data:
                return jsonify({'error': 'No valid orders with package info found'}), 400
            
            # Create Excel file with enforced column order matching FedEx sample
            columns = [
                'shipmentReference','recordType','numberOfPackages','length','width','height','packageWeight',
                'packageType','weightUnits','currencyType',
                'senderContactName','senderCompany','senderContactNumber','senderEmail','senderLine1','senderLine2','senderCity','senderState','senderPostcode','senderCountry',
                'recipientContactName','recipientCompany','recipientContactNumber','recipientEmail','recipientLine1','recipientLine2','recipientLine3','recipientCity','recipientState','recipientPostcode','recipientCountry',
                'serviceType','paymentType','payerAccountNumber'
            ]
            df = pd.DataFrame(excel_data, columns=columns)
            
            # Create Excel file in memory
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='FedEx Labels', index=False)
                
                # Auto-adjust column widths
                worksheet = writer.sheets['FedEx Labels']
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = column[0].column_letter
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width
            
            # Update order statuses to 'shipped'
            update_query = "UPDATE lori_orders SET status = 'shipped', updated_at = NOW() WHERE po_number = %s"
            
            # Update order statuses to 'shipped'
            update_query = "UPDATE lori_orders SET status = 'shipped', updated_at = NOW() WHERE po_number = %s"
            for po_number in processed_orders:
                cursor.execute(update_query, (po_number,))
            
            connection.commit()
            cursor.close()
            connection.close()
            
            # Prepare file for download
            output.seek(0)
            
            
            return send_file(
                output,
                as_attachment=True,
                download_name=f'FedEx_Labels_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            
        except Exception as e:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
            raise e
            
    except Exception as e:
        current_app.logger.error(f"Error processing selected orders: {str(e)}")
        return jsonify({'error': str(e)}), 500


@lori_automation_bp.route("/combine_pdfs", methods=["POST"])
def combine_pdfs():
    """Combine original PDF files from selected orders into one downloadable PDF"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Initialize PDF merger
        merger = PdfMerger()
        found_pdfs = []
        missing_pdfs = []
        
        # Search for PDF files corresponding to each PO number
        base_path = email_config['download_path']
        
        for po_number in po_numbers:
            pdf_found = False
            
            # Search through all subdirectories in the download path
            if os.path.exists(base_path):
                for root, dirs, files in os.walk(base_path):
                    for filename in files:
                        if filename.lower().endswith('.pdf') and po_number in filename:
                            pdf_path = os.path.join(root, filename)
                            try:
                                # Try to add the PDF to merger
                                merger.append(pdf_path)
                                found_pdfs.append({
                                    'po_number': po_number,
                                    'filename': filename,
                                    'path': pdf_path
                                })
                                pdf_found = True
                                break  # Found the PDF for this PO, move to next PO
                            except Exception as e:
                                current_app.logger.warning(f"Failed to add PDF {pdf_path}: {str(e)}")
                                continue
                    
                    if pdf_found:
                        break  # Found PDF for this PO, move to next PO
            
            if not pdf_found:
                missing_pdfs.append(po_number)
                current_app.logger.warning(f"PDF not found for PO: {po_number}")
        
        # Check if we found any PDFs
        if not found_pdfs:
            return jsonify({
                'error': f'No PDF files found for any of the selected orders: {", ".join(po_numbers)}'
            }), 404
        
        # Create combined PDF in memory
        output = io.BytesIO()
        merger.write(output)
        merger.close()
        
        # Prepare file for download
        output.seek(0)
        
        # Log results
        success_pos = [pdf['po_number'] for pdf in found_pdfs]
        
        if missing_pdfs:
            current_app.logger.warning(f"Missing PDFs for orders: {missing_pdfs}")
        
        # Return the combined PDF
        return send_file(
            output,
            as_attachment=True,
            download_name=f'Combined_Orders_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf',
            mimetype='application/pdf'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error combining PDFs: {str(e)}")
        return jsonify({'error': f'Failed to combine PDFs: {str(e)}'}), 500


@lori_automation_bp.route("/custom_picklist", methods=["POST"])
def custom_picklist():
    """Create custom picklist with summary and orders, sorted by SKU groupings"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Group orders by SKU combinations for summary
        grouped_orders = group_orders_by_sku_combination(po_numbers)
        if not grouped_orders:
            return jsonify({'error': 'Failed to group orders by SKU'}), 500
            
        # Create custom picklist: Summary only (no PDFs)
        summary_only_pdf = create_summary_only_pdf(grouped_orders)
        
        # Return the custom picklist PDF
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_only_pdf.seek(0)
        return send_file(
            summary_only_pdf,
            as_attachment=True,
            download_name=f'Custom_Picklist_Summary_{timestamp}.pdf',
            mimetype='application/pdf'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error in custom_picklist: {str(e)}")
        return jsonify({'error': f'Failed to create custom picklist: {str(e)}'}), 500


@lori_automation_bp.route("/duplex_printing_pdf", methods=["POST"])
def duplex_printing_pdf():
    """Generate just the duplex printing PDF for easy opening in new window"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Step 1: Group orders by SKU combinations
        grouped_orders = group_orders_by_sku_combination(po_numbers)
        if not grouped_orders:
            return jsonify({'error': 'Failed to group orders by SKU'}), 500
            
        # Step 2: Reorder PO numbers based on groups
        reordered_po_numbers = []
        for group in grouped_orders:
            reordered_po_numbers.extend(group['po_numbers'])
            
        # Step 3: Find and process PDFs
        base_path = email_config['download_path']
        processed_pdfs = []
        missing_pdfs = []
        
        for po_number in reordered_po_numbers:
            pdf_found = False
            
            if os.path.exists(base_path):
                for root, dirs, files in os.walk(base_path):
                    for filename in files:
                        if filename.lower().endswith('.pdf') and po_number in filename:
                            pdf_path = os.path.join(root, filename)
                            try:
                                # Trim PDF after TOTAL COST
                                trimmed_pdf = trim_pdf_after_total_cost(pdf_path)
                                if trimmed_pdf:
                                    # Validate the trimmed PDF before adding
                                    page_count = get_pdf_page_count(trimmed_pdf)
                                    if page_count > 0:
                                        processed_pdfs.append({
                                            'po_number': po_number,
                                            'filename': filename,
                                            'pdf_content': trimmed_pdf,
                                            'page_count': page_count
                                        })
                                        pdf_found = True
                                        break
                                    else:
                                        current_app.logger.warning(f"Trimmed PDF for PO {po_number} has 0 pages")
                                else:
                                    current_app.logger.warning(f"Failed to trim PDF for PO {po_number}")
                            except Exception as e:
                                current_app.logger.error(f"Failed to process PDF {pdf_path}: {str(e)}")
                                continue
                    
                    if pdf_found:
                        break
            
            if not pdf_found:
                missing_pdfs.append(po_number)
                current_app.logger.warning(f"PDF not found for PO: {po_number}")
        
        if not processed_pdfs:
            return jsonify({
                'error': f'No PDF files found for any of the selected orders: {", ".join(po_numbers)}'
            }), 404
            
        # Create processed PDFs only version (for duplex printing)
        processed_only_pdf = create_processed_pdfs_only(processed_pdfs)
        processed_only_pdf.seek(0)
        
        if missing_pdfs:
            current_app.logger.warning(f"Missing PDFs for orders: {missing_pdfs}")
        
        # Return just the duplex printing PDF
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            processed_only_pdf,
            as_attachment=False,  # Don't force download, allow inline viewing
            download_name=f'Orders_Only_for_Duplex_Printing_{timestamp}.pdf',
            mimetype='application/pdf'
        )
        
    except Exception as e:
        current_app.logger.error(f"Error in duplex_printing_pdf: {str(e)}")
        return jsonify({'error': f'Failed to process PDFs: {str(e)}'}), 500


def group_orders_by_sku_combination(po_numbers):
    """Group PO numbers by identical SKU combinations"""
    try:
        connection = get_db_connection()
        if not connection:
            return None
            
        cursor = connection.cursor(dictionary=True)
        
        # Get all items for the selected orders
        placeholders = ','.join(['%s'] * len(po_numbers))
        query = f"""
            SELECT poi.po_number, poi.vendor_item, poi.color, poi.quantity, lo.batch_date
            FROM lori_order_items poi
            JOIN lori_orders lo ON poi.po_number = lo.po_number
            WHERE poi.po_number IN ({placeholders})
            ORDER BY poi.po_number, poi.vendor_item, poi.color
        """
        cursor.execute(query, po_numbers)
        items = cursor.fetchall()
        
        cursor.close()
        connection.close()
        
        # Create SKU fingerprints for each order
        order_fingerprints = {}
        order_batch_dates = {}
        
        for item in items:
            po_number = item['po_number']
            sku_key = f"{item['vendor_item']}-{item['color']}-{item['quantity']}"
            
            if po_number not in order_fingerprints:
                order_fingerprints[po_number] = []
                order_batch_dates[po_number] = item['batch_date']
                
            order_fingerprints[po_number].append(sku_key)
        
        # Convert fingerprints to sortable strings
        for po_number in order_fingerprints:
            order_fingerprints[po_number] = '|'.join(sorted(order_fingerprints[po_number]))
        
        # Group orders with identical fingerprints
        fingerprint_groups = {}
        for po_number, fingerprint in order_fingerprints.items():
            if fingerprint not in fingerprint_groups:
                fingerprint_groups[fingerprint] = []
            fingerprint_groups[fingerprint].append(po_number)
        
        # Create grouped orders with SKU details for summary
        grouped_orders = []
        for fingerprint, po_list in fingerprint_groups.items():
            # Get SKU details for the first order in the group (they're all identical)
            first_po = po_list[0]
            sku_details = []
            
            for item in items:
                if item['po_number'] == first_po:
                    sku_details.append({
                        'vendor_item': item['vendor_item'],
                        'color': item['color'], 
                        'quantity': item['quantity']
                    })
            
            group_info = {
                'fingerprint': fingerprint,
                'po_numbers': sorted(po_list),  # Sort PO numbers within group
                'sku_details': sku_details,
                'batch_date': order_batch_dates[first_po]
            }
            grouped_orders.append(group_info)
        
        # Sort groups: multiple store numbers first, then by batch date, then by fingerprint
        grouped_orders.sort(key=lambda x: (-len(x['po_numbers']), x['batch_date'], x['fingerprint']))
        
        return grouped_orders
        
    except Exception as e:
        current_app.logger.error(f"Error grouping orders by SKU: {str(e)}")
        return None


def trim_pdf_after_total_cost(pdf_path):
    """Trim PDF by removing pages after 'TOTAL COST:' is found"""
    try:
        reader = PdfReader(pdf_path)
        total_cost_page = None
        
        # Find the page with "TOTAL COST:"
        for page_num, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text()
                if "TOTAL COST:" in page_text.upper():
                    total_cost_page = page_num
                    break
            except Exception as e:
                current_app.logger.warning(f"Could not extract text from page {page_num + 1}: {str(e)}")
                continue
        
        # If no TOTAL COST found, return the original PDF
        if total_cost_page is None:
            current_app.logger.warning(f"'TOTAL COST:' not found in PDF {pdf_path}, using original PDF")
            with open(pdf_path, 'rb') as f:
                original_content = f.read()
            return io.BytesIO(original_content)
        
        # Create new PDF with only pages up to and including the TOTAL COST page
        output = io.BytesIO()
        merger = PdfMerger()
        
        # Use page range to include pages from 0 to total_cost_page (inclusive)
        merger.append(pdf_path, pages=(0, total_cost_page + 1))
        merger.write(output)
        merger.close()
        output.seek(0)
        
        return output
        
    except Exception as e:
        current_app.logger.error(f"Error trimming PDF {pdf_path}: {str(e)}")
        # Return original PDF on error
        try:
            with open(pdf_path, 'rb') as f:
                original_content = f.read()
            return io.BytesIO(original_content)
        except:
            return None


def get_pdf_page_count(pdf_content):
    """Get the number of pages in a PDF"""
    try:
        pdf_content.seek(0)  # Reset to beginning
        reader = PdfReader(pdf_content)
        count = len(reader.pages)
        pdf_content.seek(0)  # Reset again for later use
        return count
    except Exception as e:
        current_app.logger.error(f"Error counting PDF pages: {str(e)}")
        return 0  # Return 0 to indicate error - don't assume 1


def create_blank_pdf_page():
    """Create a blank PDF page in US Letter landscape size (11" x 8.5")"""
    buffer = io.BytesIO()
    # US Letter landscape size: 11" x 8.5" (792 x 612 points)
    from reportlab.lib.pagesizes import landscape
    c = canvas.Canvas(buffer, pagesize=landscape(letter))
    c.showPage()  # Create blank page
    c.save()
    buffer.seek(0)
    return buffer


def create_summary_only_pdf(grouped_orders):
    """Create PDF with only the summary page (no orders)"""
    try:
        # Create summary PDF
        summary_buffer = io.BytesIO()
        doc = SimpleDocTemplate(summary_buffer, pagesize=letter,
                               rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30,
                               title="Custom Picklist Summary",
                               author="FDC Lori Automation",
                               subject="Order Picking Summary")
        styles = getSampleStyleSheet()
        story = []
        
        # Title (English and Spanish)
        title_en = Paragraph("Custom Picklist Summary", styles['Title'])
        story.append(title_en)
        story.append(Spacer(1, 5))
        title_es = Paragraph("Resumen de Lista de Selección Personalizada", styles['Heading2'])
        story.append(title_es)
        story.append(Spacer(1, 20))
        
        # Create summary table data with header - English first row, Spanish second row
        header_row_english = ['PO Numbers', 'SKU Items', 'Dim', 'Weight']
        header_row_spanish = ['Números de PO', 'Artículos SKU', 'Dim', 'Peso']
        summary_data = [header_row_english, header_row_spanish]
        
        for i, group in enumerate(grouped_orders, 1):
            po_numbers = group['po_numbers']
            sku_details = group['sku_details']
            
            # Create full PO numbers list with store count summary
            po_list = sorted(po_numbers)
            unique_stores = list(set([po[:4] for po in po_numbers if len(po) >= 4]))
            store_count = len(unique_stores)
            
            # Create SKU items string - improved format with integer quantities
            sku_items = []
            total_qty = 0
            total_styles = len(sku_details)  # Count unique styles
            total_bags = total_qty  # Total bags is sum of all quantities
            for item in sku_details:
                # Format: Bold integer quantity + clear SKU with better spacing
                qty = int(float(item['quantity']))  # Convert to integer
                sku = item['vendor_item']
                sku_line = f"<b>{qty}</b> × {sku}"  # Using multiplication symbol for better clarity
                sku_items.append(sku_line)
                total_qty += qty
            
            # Update total_bags after calculating total_qty
            total_bags = total_qty
            
            # Add total summary at bottom like PO numbers (English/Spanish)
            # Limit SKU display to prevent overflow
            if len(sku_items) > 15:
                display_sku_items = sku_items[:15] + [f"... and {len(sku_items) - 15} more items"]
            else:
                display_sku_items = sku_items
            sku_items_with_summary = '<br/>'.join(display_sku_items) + f'<br/><br/><b>Total: {total_styles} styles/estilos</b><br/><b>Total: {total_bags} bags/bolsas</b>'
            
            # Format PO numbers - put all in one cell with word wrapping
            po_numbers_text = ', '.join(po_list) + f'<br/><br/><b>Total: {store_count} stores/tiendas</b>'
            
            # Create wrapped paragraphs for cells with improved formatting
            store_cell = Paragraph(po_numbers_text, styles['Normal'])
            sku_cell = Paragraph(sku_items_with_summary, styles['Normal'])
            dimensions_cell = Paragraph('', styles['Normal'])  # Blank dimensions column
            weight_cell = Paragraph('', styles['Normal'])  # Blank weight column
            
            summary_data.append([
                store_cell,
                sku_cell,
                dimensions_cell,
                weight_cell
            ])
        
        # Create table with improved warehouse-friendly formatting (4 columns now)
        table = Table(summary_data, colWidths=[4.0*inch, 2.5*inch, 1.0*inch, 1.0*inch], 
                     repeatRows=2, splitByRow=True)  # Allow table to split across pages
        table.setStyle(TableStyle([
            # Header rows (English and Spanish)
            ('BACKGROUND', (0, 0), (-1, 1), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 1), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 1), 10),  # Smaller header font
            ('BOTTOMPADDING', (0, 0), (-1, 1), 6),
            ('TOPPADDING', (0, 0), (-1, 1), 6),
            # Data rows
            ('BACKGROUND', (0, 2), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),  # Thinner grid lines
            ('FONTSIZE', (0, 2), (-1, -1), 6),  # Smaller data font
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('WORDWRAP', (0, 0), (-1, -1), True),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
            ('TOPPADDING', (0, 2), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 2), (-1, -1), 2),
        ]))
        
        story.append(table)
        story.append(Spacer(1, 20))

        # Build PDF
        doc.build(story)
        summary_buffer.seek(0)
        return summary_buffer
        
    except Exception as e:
        current_app.logger.error(f"Error creating summary PDF: {str(e)}")
        raise e


def create_combined_pdf_with_summary(grouped_orders, processed_pdfs):
    """Create final PDF with summary page followed by processed PDFs with blank page insertion"""
    try:
        # Create summary PDF
        summary_buffer = io.BytesIO()
        doc = SimpleDocTemplate(summary_buffer, pagesize=letter,
                               title="Order Processing Summary",
                               author="FDC Lori Automation",
                               subject="Combined Order Processing Summary")
        styles = getSampleStyleSheet()
        story = []
        
        # Title
        title = Paragraph("Order Processing Summary", styles['Title'])
        story.append(title)
        story.append(Spacer(1, 20))
        
        # Process each batch/group
        for group_idx, group in enumerate(grouped_orders):
            # Group header
            batch_title = Paragraph(f"Batch {group_idx + 1} - Date: {group['batch_date']}", styles['Heading2'])
            story.append(batch_title)
            story.append(Spacer(1, 10))
            
            # Create table data
            table_data = [['Store Numbers', 'SKU Details']]
            
            # Store numbers column
            stores_list = [po[:4] for po in group['po_numbers']]  # First 4 chars of PO number
            stores_text = '<br/>'.join(stores_list)
            
            # SKU details column - improved format with integer quantities
            sku_lines = []
            for sku in group['sku_details']:
                # Format: Bold integer quantity + clear SKU with better spacing
                qty = int(float(sku['quantity']))  # Convert to integer
                vendor_item = sku['vendor_item']
                sku_line = f"<b>{qty}</b> × {vendor_item}"  # Using multiplication symbol for better clarity
                sku_lines.append(sku_line)
            sku_text = '<br/>'.join(sku_lines)
            
            # Create Paragraph objects for proper HTML rendering
            stores_cell = Paragraph(stores_text, styles['Normal'])
            sku_cell = Paragraph(sku_text, styles['Normal'])
            
            table_data.append([stores_cell, sku_cell])
            
            # Create table with improved warehouse-friendly formatting
            table = Table(table_data, colWidths=[2.2*inch, 4.3*inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 15),
                ('TOPPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 11),  # Larger font for better readability
                ('GRID', (0, 0), (-1, -1), 2, colors.black),  # Thicker grid lines
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 1), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
            ]))
            
            story.append(table)
            story.append(Spacer(1, 20))
        
        doc.build(story)
        summary_buffer.seek(0)
        
        # Combine summary with processed PDFs
        final_merger = PdfMerger()
        
        # Add summary page
        final_merger.append(summary_buffer)
        
        # Add processed PDFs with blank page insertion
        for pdf_info in processed_pdfs:
            pdf_content = pdf_info['pdf_content']
            page_count = pdf_info['page_count']
            po_number = pdf_info['po_number']
            
            try:
                # Validate and add the processed PDF
                pdf_content.seek(0)
                
                # Test if PDF is readable before adding
                test_reader = PdfReader(pdf_content)
                actual_pages = len(test_reader.pages)
                
                # Reset and add to merger
                pdf_content.seek(0)
                final_merger.append(pdf_content)
                
                # Add blank page if odd number of pages
                if actual_pages % 2 == 1:
                    blank_page = create_blank_pdf_page()
                    final_merger.append(blank_page)
                    blank_page.close()
                    
            except Exception as e:
                current_app.logger.error(f"Error adding PDF for PO {po_number} to summary: {str(e)}")
                continue  # Skip corrupted PDFs
        
        # Create final output
        final_output = io.BytesIO()
        final_merger.write(final_output)
        final_merger.close()
        final_output.seek(0)
        
        # Close summary buffer
        summary_buffer.close()
        
        return final_output
        
    except Exception as e:
        current_app.logger.error(f"Error creating combined PDF with summary: {str(e)}")
        raise e


def create_processed_pdfs_only(processed_pdfs):
    """Create PDF with only processed PDFs and blank pages for duplex printing"""
    try:
        # Create merger for processed PDFs only
        processed_merger = PdfMerger()
        
        # Add processed PDFs with blank page insertion
        for pdf_info in processed_pdfs:
            pdf_content = pdf_info['pdf_content']
            page_count = pdf_info['page_count']
            po_number = pdf_info['po_number']
            
            try:
                # Reset to beginning and validate PDF
                pdf_content.seek(0)
                
                # Test if PDF is readable
                test_reader = PdfReader(pdf_content)
                actual_page_count = len(test_reader.pages)
                
                # Reset again for merger
                pdf_content.seek(0)
                
                # Add the processed PDF
                processed_merger.append(pdf_content)

                
                # Add blank page if odd number of pages
                if actual_page_count % 2 == 1:
                    blank_page = create_blank_pdf_page()
                    processed_merger.append(blank_page)
                    blank_page.close()

                    
            except Exception as e:
                current_app.logger.error(f"Error processing PDF for PO {po_number}: {str(e)}")
                continue  # Skip this PDF if corrupted
        
        # Create final output
        processed_output = io.BytesIO()
        processed_merger.write(processed_output)
        processed_merger.close()
        processed_output.seek(0)
        
        return processed_output
        
    except Exception as e:
        current_app.logger.error(f"Error creating processed PDFs only: {str(e)}")
        raise e


@lori_automation_bp.route("/download_invoices", methods=["POST"])
def download_invoices():
    """Generate invoices for selected orders from database data"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        po_numbers = data.get('po_numbers', [])
        if not po_numbers:
            return jsonify({'error': 'No PO numbers provided'}), 400
        
        # Get invoice date from request, default to today if not provided
        invoice_date = data.get('invoice_date', datetime.now().strftime("%m/%d/%Y"))
        
        # Get database connection
        connection = get_db_connection()
        if not connection:
            return jsonify({'error': 'Database connection failed'}), 500
            
        try:
            cursor = connection.cursor(dictionary=True)
            
            invoice_files = []
            processed_orders = []
            
            # Process each order
            for po_number in po_numbers:
                try:
                    # Get order information
                    order_query = """
                        SELECT po_number, batch_date, ship_to_address, total_qty, total_cost, status,
                               order_date, delivery_date, cancel_date
                        FROM lori_orders 
                        WHERE po_number = %s
                    """
                    cursor.execute(order_query, (po_number,))
                    order = cursor.fetchone()
                    
                    if not order:

                        continue
                    
                    # Get order items
                    items_query = """
                        SELECT vendor_item, quantity, upc, description, unit_price, extended_cost
                        FROM lori_order_items 
                        WHERE po_number = %s
                    """
                    cursor.execute(items_query, (po_number,))
                    items = cursor.fetchall()
                    
                    if not items:
                        continue
                    
                    # Generate invoice using the existing logic
                    invoice_data = generate_invoice_from_data(order, items, invoice_date)
                    if invoice_data:
                        invoice_files.append(invoice_data)
                        processed_orders.append(po_number)
                        
                except Exception as e:
                    current_app.logger.error(f"Error processing order {po_number}: {str(e)}")
                    continue
            
            cursor.close()
            connection.close()
            
            if not invoice_files:
                return jsonify({'error': 'No valid invoices could be generated'}), 400
            
            # --- Build summary file ---
            if len(invoice_files) > 1:
                import openpyxl
                from openpyxl import Workbook
                summary_wb = Workbook()
                summary_ws = summary_wb.active
                total_amount = Decimal(0)
                total_quantity = Decimal(0)
                summary_rows = []
                for invoice in invoice_files:
                    invoice['excel'].seek(0)
                    wb = openpyxl.load_workbook(invoice['excel'])
                    ws = wb.active
                    found_qty = 0
                    found_amt = 0
                    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
                        if row[2].value == "Total Quantity":
                            found_qty = row[3].value or 0
                        if row[6].value == "Total Amount":
                            amt_val = row[7].value
                            if amt_val and isinstance(amt_val, str) and amt_val.startswith("$"):
                                amt_val = amt_val.replace("$", "")
                            found_amt = Decimal(amt_val or 0)
                    summary_rows.append([invoice['filename'], invoice.get('po_number', ''), float(found_amt), float(found_qty)])
                    total_amount += found_amt
                    total_quantity += Decimal(found_qty)
                    
                # Add total row
                summary_ws.append(['Invoice File Name', 'Invoice Number', 'Total Amount', 'Total Quantity'])
                for row in summary_rows:
                    summary_ws.append(row)
                summary_ws.append(['TOTAL', '', float(total_amount), float(total_quantity)])
            # --- End summary file build ---

            # Return single Excel file or ZIP based on count
            if len(invoice_files) == 1:
                invoice = invoice_files[0]
                return send_file(
                    invoice['excel'],
                    as_attachment=True,
                    download_name=invoice['filename'],
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                )
            else:
                # Create ZIP file for multiple invoices
                zip_buffer = io.BytesIO()
                summary_stream = io.BytesIO()
                summary_wb.save(summary_stream)
                summary_stream.seek(0)
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                    for invoice in invoice_files:
                        invoice['excel'].seek(0)
                        zip_file.writestr(invoice['filename'], invoice['excel'].read())
                    # Add summary file
                    zip_file.writestr('aaa_summary.xlsx', summary_stream.read())
                zip_buffer.seek(0)
                return send_file(
                    zip_buffer,
                    as_attachment=True,
                    download_name=f'invoices_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
                    mimetype='application/zip'
                )
                
        except Exception as e:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
            raise e
            
    except Exception as e:
        current_app.logger.error(f"Error generating invoices: {str(e)}")
        return jsonify({'error': f'Failed to generate invoices: {str(e)}'}), 500


def generate_invoice_from_data(order, items, invoice_date=None):
    """Generate invoice Excel file from database data"""
    try:
        # Load Excel template
        template_path = os.path.join(current_app.static_folder, 'new_sample.xlsx')
        if not os.path.exists(template_path):
            current_app.logger.error(f"Template file not found: {template_path}")
            return None
            
        memory_file = io.BytesIO()
        wb = load_workbook(template_path)
        ws = wb.active
        
        # Extract data from order
        po_number = order['po_number']
        order_date = order.get('order_date', 'N/A')
        delivery_date = order.get('delivery_date', 'N/A')
        cancel_date = order.get('cancel_date', 'N/A')
        ship_to_address = order.get('ship_to_address', 'N/A')
        
        # Use provided invoice date or default to today
        if invoice_date is None:
            invoice_date = datetime.now().strftime("%m/%d/%Y")
        
        # Generate filename
        file_date = invoice_date.replace("/", "").lstrip("0")
        target_filename = f"{file_date}_{po_number[:4]}"
        
        # Fill Excel template
        ws['F7'] = order_date if order_date != 'N/A' else invoice_date
        ws['F8'] = invoice_date
        ws['H6'] = target_filename.replace("_", "-")
        ws['G11'] = f"GIFT STORE {po_number[:4]}"
        ws['F9'] = cancel_date if cancel_date != 'N/A' else ''
        ws['H8'] = invoice_date
        ws['H7'] = po_number
        ws['F12'] = ship_to_address
        
        # Process items
        starting_row = 20
        total_qty = Decimal(0)
        total_cost = Decimal(0)
        
        for item in items:
            vendor_item = item.get('vendor_item', '')
            qty = Decimal(str(item.get('quantity', 0)))
            upc = item.get('upc', '')
            description = item.get('description', '')
            unit_price = Decimal(str(item.get('unit_price', 0)))
            ext_cost = Decimal(str(item.get('extended_cost', 0)))
            
            # Extract color from vendor item
            if "-" in vendor_item:
                parts = vendor_item.split("-")
                color_key = parts[-1]
                color = color_dict.get(color_key, color_key)
            else:
                color_key = vendor_item[-2:] if len(vendor_item) >= 2 else vendor_item
                color = color_dict.get(color_key, "")
            
            # Fill row data
            ws[f"A{starting_row}"] = starting_row - 19
            ws[f"B{starting_row}"] = vendor_item
            ws[f"C{starting_row}"] = color
            ws[f"D{starting_row}"] = qty
            ws[f"E{starting_row}"] = description
            ws[f"G{starting_row}"] = f"${unit_price:.2f}"
            ws[f"H{starting_row}"] = f"${ext_cost:.2f}"
            
            total_qty += qty
            total_cost += ext_cost
            starting_row += 1
        
        # Add totals
        ws[f"C{starting_row + 1}"] = "Total Quantity"
        ws[f"D{starting_row + 1}"] = total_qty
        ws[f"G{starting_row + 1}"] = "Total Amount"
        ws[f"H{starting_row + 1}"] = f"${total_cost:.2f}"
        
        # Save to memory
        wb.save(memory_file)
        memory_file.seek(0)
        
        return {
            'excel': memory_file,
            'filename': f"{target_filename}.xlsx",
            'po_number': po_number,
            'address': ship_to_address
        }
        
    except Exception as e:
        current_app.logger.error(f"Error generating invoice for {order.get('po_number', 'unknown')}: {str(e)}")
        return None


## Removed test-only endpoint /test_fedx_auth to keep production surface minimal

@lori_automation_bp.route("/send_to_shipped_only", methods=["POST"])
def send_to_shipped_only():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    po_numbers = data.get('po_numbers', [])
    print(f"DEBUG send_to_shipped: Received po_numbers: {po_numbers}")
    if not po_numbers:
        return jsonify({'error': 'No PO numbers provided'}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor()
        updated = 0
        for po in po_numbers:
            print(f"DEBUG send_to_shipped: Updating po {po} to shipped")
            cursor.execute("UPDATE lori_orders SET status = 'shipped' WHERE po_number = %s", (po,))
            print(f"DEBUG send_to_shipped: Rowcount: {cursor.rowcount}")
            if cursor.rowcount > 0:
                updated += 1
        connection.commit()
        print(f"DEBUG send_to_shipped: Total updated: {updated}")
        return jsonify({'success': True, 'message': f'Status updated to shipped for {updated} orders.'})
    except Exception as e:
        print(f"DEBUG send_to_shipped: Error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/send_back_pending", methods=["POST"])
def send_back_pending():
    """Send orders back to pending status, but only those without package info"""
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    po_numbers = data.get('po_numbers', [])
    print(f"DEBUG send_back_pending: Received po_numbers: {po_numbers}")
    if not po_numbers:
        return jsonify({'error': 'No PO numbers provided'}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor(dictionary=True)
        updated = 0
        skipped = 0
        for po in po_numbers:
            # Check if this PO has package info
            cursor.execute("SELECT COUNT(*) as count FROM loris_pkg_info WHERE po_number = %s", (po,))
            result = cursor.fetchone()
            has_pkg_info = result['count'] > 0
            
            if has_pkg_info:
                print(f"DEBUG send_back_pending: Skipping po {po} - has package info")
                skipped += 1
                continue
            
            # Update to pending if no package info
            print(f"DEBUG send_back_pending: Updating po {po} to pending")
            cursor.execute("UPDATE lori_orders SET status = 'pending' WHERE po_number = %s", (po,))
            print(f"DEBUG send_back_pending: Rowcount: {cursor.rowcount}")
            if cursor.rowcount > 0:
                updated += 1
        connection.commit()
        print(f"DEBUG send_back_pending: Total updated: {updated}, skipped: {skipped}")
        
        message = f'Status updated to pending for {updated} order(s).'
        if skipped > 0:
            message += f' {skipped} order(s) skipped (have package info).'
        
        return jsonify({'success': True, 'message': message})
    except Exception as e:
        print(f"DEBUG send_back_pending: Error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()

@lori_automation_bp.route("/bulk_modify_batch_date", methods=["POST"])
def bulk_modify_batch_date():
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json()
    po_numbers = data.get('po_numbers', [])
    new_batch_date = data.get('new_batch_date', None)
    print(f"DEBUG: Received po_numbers: {po_numbers}, new_batch_date: {new_batch_date}")
    if not po_numbers or not new_batch_date:
        return jsonify({'error': 'PO numbers and new batch date required'}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({'error': 'Database connection failed'}), 500
    try:
        cursor = connection.cursor()
        updated = 0
        for po in po_numbers:
            print(f"DEBUG: Updating po {po} to {new_batch_date}")
            cursor.execute("UPDATE lori_orders SET batch_date = %s WHERE po_number = %s", (new_batch_date, po))
            print(f"DEBUG: Rowcount: {cursor.rowcount}")
            if cursor.rowcount > 0:
                updated += 1
        connection.commit()
        print(f"DEBUG: Total updated: {updated}")
        return jsonify({'success': True, 'message': f'Batch date updated for {updated} orders.'})
    except Exception as e:
        print(f"DEBUG: Error: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        connection.close()