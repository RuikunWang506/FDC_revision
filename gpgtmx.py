from flask import Blueprint, redirect, url_for, request, jsonify, session, render_template, make_response, current_app
from flask_cors import CORS
import pandas as pd
import os
import shutil
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2 import service_account
from config import Config

gpgtmx_bp = Blueprint('gpgtmx', __name__, template_folder='templates')

def register_routes(app):
    CORS(app, resources={
        r"/gpginvoice": {
            "origins": ["https://admin.genuinepartsgiant.com"],
            "methods": ["POST", "OPTIONS"],
            "allow_headers": ["Content-Type"],
            "supports_credentials": True
        }
    })

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
    return redirect(url_for("gpginvoice"))

@gpgtmx_bp.route("/gpginvoice", methods=["GET", "POST", "OPTIONS"])
def gpginvoice():
    """Main route for displaying and handling GPG invoice data."""
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("login"))
    
    current_user = session['username']
    excel_path = os.path.join('output', 'gpginvoice', 'gpg_orders.xlsx')
    os.makedirs(os.path.dirname(excel_path), exist_ok=True)

    # Handle CORS for browser extensions
    allowed_origins = [
        "https://admin.genuinepartsgiant.com",
        "chrome-extension://ffpfoekiibonojnidahneaclopidhoon"
    ]
    
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin")
        allowed_origin = origin if origin in allowed_origins else "null"
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", allowed_origin)
        response.headers.add("Access-Control-Allow-Headers", "Content-Type")
        response.headers.add("Access-Control-Allow-Methods", "POST, OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response

    if request.method == "POST":
        try:
            data = request.get_json()
            descriptions = get_tmx_descriptions()

            new_rows = []
            for item in data['lineItems']:
                order_number = data['orderNumber']
                part_number = item['partNumber'].strip()
                
                # Determine Manufacturer Prefix
                if order_number.startswith("CH"):
                    prefixed_part_number = f"Honda:{part_number}"
                elif order_number.startswith("CU"):
                    prefixed_part_number = f"Hyundai:{part_number}"
                else:
                    prefixed_part_number = part_number  # No prefix

                # Get description (if exists)
                description = descriptions.get(prefixed_part_number, '')

                new_rows.append([
                    order_number,
                    data['orderDate'],
                    item['lineNumber'],
                    part_number,
                    description,  # Now includes description lookup
                    item['qty'],
                    item['price'].replace('$', '').strip()
                ])

            if os.path.exists(excel_path):
                df = pd.read_excel(excel_path, engine='openpyxl')
            else:
                df = pd.DataFrame(columns=['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price'])

            df_new = pd.DataFrame(new_rows, columns=df.columns)

            # Merge and save
            df = pd.concat([df, df_new], ignore_index=True)
            df = df[['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price']]
            df.to_excel(excel_path, index=False, engine='openpyxl')

            return jsonify({"success": True})

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Load existing data
    if os.path.exists(excel_path):
        df = pd.read_excel(excel_path, engine='openpyxl')

        # Ensure descriptions are mapped correctly
        descriptions = get_tmx_descriptions()
        df['Description'] = df.apply(lambda row: descriptions.get(
            f"Honda:{row['Part Number']}" if row['orderNumber'].startswith("CH") else
            f"Hyundai:{row['Part Number']}" if row['orderNumber'].startswith("CU") else
            row['Part Number'], ''
        ), axis=1)

        df = df[['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price']]
        
        # Convert DataFrame to HTML and add action column
        table_html = df.to_html(classes='table table-striped', table_id='gpgTable', index=False)

    else:
        table_html = "<p>No data available</p>"

    return render_template('gpginvoice.html', table_html=table_html, current_user=current_user)

@gpgtmx_bp.route("/gpg_finish", methods=["POST"])
def gpg_finish():
    """Handle the 'Finish' button click to archive data and prepare for QBO import."""
    try:
        # Get CSV content from request
        data = request.get_json()
        csv_content = data.get('csvContent')
        
        if not csv_content:
            return jsonify({"error": "No CSV content provided"}), 400
        
        # Source and destination paths
        src_excel_path = os.path.join('output', 'gpginvoice', 'gpg_orders.xlsx')
        archive_dir = os.path.join('output', 'gpginvoice', 'gpg_qbo_exports_archive')
        os.makedirs(archive_dir, exist_ok=True)
        
        # Generate filename with date
        current_date = datetime.now().strftime("%m_%d_%Y")
        dst_excel_path = os.path.join(archive_dir, f'gpg_orders_{current_date}.xlsx')
        
        # Save the CSV content to a file
        qbo_csv_path = os.path.join('output', 'gpginvoice', 'qbo_import.csv')
        with open(qbo_csv_path, 'w', newline='') as f:
            f.write(csv_content)
        
        if not os.path.exists(src_excel_path):
            return jsonify({"error": "No gpg_orders.xlsx file exists to archive"}), 404
        
        # Load the existing Excel file with engine specified
        with pd.ExcelWriter(src_excel_path, engine='openpyxl', mode='a') as writer:
            # Read the CSV and write it to a new sheet in the Excel file
            qbo_df = pd.read_csv(qbo_csv_path)
            qbo_df.to_excel(writer, sheet_name='for_qbo', index=False)
        
        # Move the updated file to the archive location
        shutil.move(src_excel_path, dst_excel_path)
        
        return jsonify({
            "success": True, 
            "message": f"Excel file archived as {dst_excel_path} with QBO data added"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500