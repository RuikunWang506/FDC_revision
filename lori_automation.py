from flask import Blueprint, render_template, request, jsonify, send_file, session, redirect, url_for, current_app
import os,re
import io
import json
import requests
import pandas as pd
from datetime import datetime
from PyPDF2 import PdfReader, PdfMerger
from openpyxl import load_workbook
from decimal import Decimal
import zipfile

lori_automation_bp = Blueprint('lori_automation', __name__)
color_dict = {"TL":"Teal","ABL":"ASHY BLUE","FLOWER":"FLOWER","BG":"BEIGE","BK":"BLACK","BL":"BLUE","BP":"BLUSH PINK","BR":"BROWN","CF":"COFFEE","CG":"COGNAC","CO":"CORAL","DM":"DENIM BLUE","DPL":"DARK PLUM","FU":"FUSCHIA","GN":"GREEN","GY":"GREY","KH":"KHAKI","LBL":"LIGHT BLUE","LV":"LAVENDER","MD":"MUSTARD","MV":"MAUVE","NV":"NAVY","OL":"OLIVE","OR":"ORANGE","OW":"OFF WHITE","PK":"PINK","PL":"PLUM","PP":"PURPLE","RD":"RED","TN":"TAN","TP":"TAUPE","TQ":"TEQUISE","WN":"WINE","WT":"WHITE","YL":"YELLOW","ST":"STONE"}

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
    
    item_pattern = re.compile(r"([\w\-]+)\s+1\s+(\d+)\s+(\d{12})\s+(.+?)\s+\$(\d+\.\d+)\s+\$(\d+\.\d+)")
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
        qty = Decimal(qty)
        unit_price = Decimal(unit_price)
        ext_cost = Decimal(ext_cost)
        
        if "-" in vendor_item:
            parts = vendor_item.split("-")
            color_key = parts[-1]  # Get everything after the last hyphen
            color = color_dict.get(color_key, color_key)  # Use the color key itself as fallback
        else:
            # Handle case with no hyphen
            color_key = vendor_item[-2:]
            color = color_dict.get(color_key, "")
            
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

@lori_automation_bp.route("/lori_automation", methods=["GET", "POST"])
def lori_automation():
    if 'username' not in session:
        return redirect(url_for("login"))
    
    if request.method == "POST":
        if 'files[]' not in request.files:
            return jsonify({'error': 'No files uploaded'}), 400
        
        files = request.files.getlist('files[]')
        excel_files = []
        po_numbers = []
        addresses = []
        pdf_merger = PdfMerger()
        
        for file in files:
            if file.filename.endswith('.pdf'):
                pdf_content = io.BytesIO(file.read())
                result = process_pdf(pdf_content)
                
                excel_files.append({
                    'content': result['excel'].getvalue(),
                    'filename': result['filename']
                })
                po_numbers.append(result['po_number'])
                addresses.append(result['address'])
                
                pdf_content.seek(0)
                pdf_merger.append(pdf_content)
        
        # Create ZIP of Excel files
        memory_zip = io.BytesIO()
        with zipfile.ZipFile(memory_zip, 'w') as zf:
            for excel in excel_files:
                zf.writestr(excel['filename'], excel['content'])
        
        # Create combined PDF
        pdf_output = io.BytesIO()
        pdf_merger.write(pdf_output)
        pdf_output.seek(0)
        
        # Prepare ShipStation data
        shipstation_data = []
        for po, addr in zip(po_numbers, addresses):
            addr_parts = addr.split('\n')
            current_time = datetime.now().isoformat()
            
            shipstation_data.append({
                "orderNumber": po,
                "orderDate": current_time,
                "paymentDate": current_time,
                "shipByDate": current_time,
                "orderStatus": "awaiting_shipment",
                "customerUsername": "",
                "customerEmail": "",
                "billTo": {
                    "name": f"Lori's Gift Shop #{po[:4]}"
                },
                "shipTo": {
                    "name": f"Lori's Gift Shop #{po[:4]}",
                    "company": addr_parts[0] if len(addr_parts) > 0 else "",
                    "street1": addr_parts[1] if len(addr_parts) > 1 else "",
                    "street2": "",
                    "street3": "",
                    "city": addr_parts[2].split(',')[0] if len(addr_parts) > 2 else "",
                    "state": addr_parts[2].split(',')[1].split()[0] if len(addr_parts) > 2 else "",
                    "postalCode": addr_parts[2].split(',')[1].split()[1] if len(addr_parts) > 2 else "",
                    "country": "US",
                    "phone": "",
                    "residential": "false"
                },
                "carrierCode": "ups",
                "serviceCode": "ups_ground",
                "packageCode": "package",
                "confirmation": "none",
                "shipDate": datetime.now().strftime("%Y-%m-%d"),
                "insuranceOptions": {
                    "provider": "carrier",
                    "insureShipment": "true",
                    "insuredValue": "0"
                },
                "advancedOptions": {
                    "nonMachinable": "false",
                    "saturdayDelivery": "false",
                    "containsAlcohol": "false",
                    "mergedOrSplit": "false",
                    "customField1": "",
                    "customField2": "",
                    "source": "Webstore",
                    "billToParty": "third_party",
                    "billToAccount": "7504E1",
                    "billToPostalCode": "75006",
                    "billToCountryCode": "US"
                }
            })

        return jsonify({
            'excel_zip': memory_zip.getvalue().decode('latin1'),
            'combined_pdf': pdf_output.getvalue().decode('latin1'),
            'shipstation_data': shipstation_data
        })
    
    return render_template("lori_automation.html", current_user=session['username'])

@lori_automation_bp.route("/send_to_shipstation", methods=["POST"])
def send_to_shipstation():
    data = request.get_json()
    url = "https://ssapi.shipstation.com/orders/createorders"
    headers = {'Content-Type': 'application/json'}

    # Ensure each order has the required fields
    for order in data['shipstation_data']:
        order['orderStatus'] = 'awaiting_shipment'  # Required field
        order['billTo'] = {
            "name": f"Lori's Gift Shop #{order['orderNumber'][:4]}",
            "company": order['shipTo']['company'],
            "street1": order['shipTo']['street1'],
            "city": order['shipTo']['city'],
            "state": order['shipTo']['state'],
            "postalCode": order['shipTo']['postalCode'],
            "country": "US",
            "phone": "",
        }

    response = requests.post(url, headers=headers, json=data['shipstation_data'], auth=(current_app.config['APIKEY'], current_app.config['APIPASS']))
    
    # Log the response
    current_app.logger.info(f"ShipStation Response: {response.status_code}")
    current_app.logger.info(f"Response Content: {response.text}")
    
    return jsonify(response.json())