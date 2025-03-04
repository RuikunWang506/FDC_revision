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

    if request.method == "POST":
        try:
            data = request.get_json()
            descriptions = get_tmx_descriptions()

            new_rows = []
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

                new_rows.append([
                    order_number,
                    data['orderDate'],
                    line_number,
                    part_number,
                    description,
                    item['qty'],
                    item['price'].replace('$', '').strip()
                ])

            # Load existing data
            if os.path.exists(excel_path):
                df = pd.read_excel(excel_path, engine='openpyxl')
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

            # Save back to Excel
            df.to_excel(excel_path, index=False, engine='openpyxl')

            return jsonify({"success": True})

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Load existing data
    if os.path.exists(excel_path):
        df = pd.read_excel(excel_path, engine='openpyxl')

        descriptions = get_tmx_descriptions()
        df['Description'] = df.apply(lambda row: descriptions.get(
            f"Honda:{row['Part Number']}" if row['orderNumber'].startswith("CH") else
            f"Hyundai:{row['Part Number']}" if row['orderNumber'].startswith("CU") else
            row['Part Number'], ''
        ), axis=1)

        df = df[['orderNumber', 'orderDate', 'Line Number', 'Part Number', 'Description', 'Quantity', 'Price']]
        table_html = df.to_html(classes='table table-striped', table_id='gpgTable', index=False)

    else:
        table_html = "<p>No data available</p>"

    return render_template('gpginvoice.html', table_html=table_html, current_user=current_user)

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
                f'{base_filename}_{current_datetime.strftime("%H%M%S")}_{count}.xlsx')

        if not os.path.exists(src_excel_path):
            return jsonify({"error": "No gpg_orders.xlsx file exists to archive"}), 404

        import io
        qbo_df = pd.read_csv(io.StringIO(csv_content))

        # Ensure unique sheet name
        sheet_name = 'for_qbo'
        if os.path.exists(src_excel_path):
            existing_sheets = pd.ExcelFile(src_excel_path).sheet_names
            version = 1
            temp_sheet_name = sheet_name
            while temp_sheet_name in existing_sheets:
                temp_sheet_name = f"{sheet_name}_v{version}"
                version += 1
            sheet_name = temp_sheet_name

        # Save to a new sheet
        with pd.ExcelWriter(src_excel_path, engine='openpyxl', mode='a') as writer:
            qbo_df.to_excel(writer, sheet_name=sheet_name, index=False)

        shutil.move(src_excel_path, dst_excel_path)

        return jsonify({
            "success": True,
            "message": f"Excel file archived as {os.path.basename(dst_excel_path)} with QBO data added to sheet '{sheet_name}'"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
