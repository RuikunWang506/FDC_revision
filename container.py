import os
from flask import Blueprint, render_template, request, send_file, current_app,session,redirect,url_for
import pandas as pd
import re

container_bp = Blueprint('container', __name__)

@container_bp.route('/container', methods=['GET', 'POST'])
def upload_container_file():
    if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
    if request.method == 'POST':
        file = request.files.get('container_file')
        if file:
            df_header = pd.read_excel(file, sheet_name='System File', nrows=2, header=None)
            container_number_raw = df_header.iloc[0, 0]
            container_number = container_number_raw.split('-')[-1].strip()
            step3_file_path, step4_file_path, df_step3, df_step4 = process_container_file(file, container_number)
            # Render the template with download links and rendered tables
            return render_template('container.html', 
                                   files=[step3_file_path, step4_file_path],  # Two generated file paths
                                   table1=df_step3.to_html(classes="table table-bordered", index=False), 
                                   table2=df_step4.to_html(classes="table table-bordered", index=False))
    # On GET request, just render the upload form
    return render_template('container.html')

def process_container_file(file, container_number):
    df_f24213 = pd.read_excel(file, sheet_name='System File', header=2)
    df_f24213.columns = df_f24213.columns.str.replace('\n', ' ').str.strip()
    df_f24213 = df_f24213.dropna(subset=['OMS', 'RMB'])
    df_f24213['WT/PCS (LBS)'] = df_f24213['WT/PCS (LBS)'].round(2)
    df_f24213 = df_f24213.applymap(lambda x: re.sub(r'\s+', ' ', str(x)) if isinstance(x, str) else x)

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
    
    # Prepare the data for step3 as a dictionary for faster DataFrame creation
    step3_data = {
        'Item#': df_f24213['STYLE NO'] + ' ' + df_f24213['COLOR'],  # New Item#
        'WHS#': "01",
        'Descrip': container_number,
        'Alt.Item#': df_f24213['STYLE NO'],  # Style No
        'Color': df_f24213['COLOR'],
        'Quantity': df_f24213['QUANTITY'],
        'Class#' : "L1",
        'Unit/Case': df_f24213['PCS /CTN'],
        'Box/Case': "01",
        'Sales Cost':df_f24213['OMS'],
        'Case Wt': df_f24213['WT/CTN (LBS)'],
        'Unit Wt': df_f24213['WT/PCS (LBS)'],
        'Unit Length': df_f24213['SIZE   (L)IN'],
        'Unit Width': df_f24213['SIZE   (W)IN'],
        'Unit Height': df_f24213['SIZE   (H)IN'],
        'Price1': df_f24213['Price1'],
        'Sale Price': df_f24213['Sale Price'],
        'Base Cost':df_f24213['OMS'],
        'Avg.Cost':df_f24213['OMS'],
        'PO#': df_f24213['PO#']
    }

    # Create DataFrame with step3 columns and fill missing ones with empty values
    df_step3 = pd.DataFrame(step3_data)
    for col in step3_columns:
        if col not in df_step3.columns:
            df_step3[col] = ""

    # Reorder columns according to step3_columns
    df_step3 = df_step3[step3_columns]

    # Now, dictionary-based data preparation for Step 4


    # Step 4 columns
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


     # Define the directory for saving files in 'output/container/'
    output_dir = os.path.join(current_app.root_path, 'output', 'container')
    os.makedirs(output_dir, exist_ok=True)

    # Define file paths
    output_step3_path = os.path.join(output_dir, f"{container_number}-styles.xlsx")
    output_step4_path = os.path.join(output_dir, f"{container_number}-items.xlsx")

    # Write Step 3 (styles.xlsx) to 'output/container/'
    with pd.ExcelWriter(output_step3_path, engine='xlsxwriter') as writer:
        df_step3.to_excel(writer, index=False)

    # Write Step 4 (items.xlsx) to 'output/container/'
    with pd.ExcelWriter(output_step4_path, engine='xlsxwriter') as writer:
        df_step4.to_excel(writer, index=False)

    # Return file paths and DataFrames
    return output_step3_path, output_step4_path, df_step3, df_step4

@container_bp.route('/download55/<filename>')
def download_file55(filename):
    # Define the directory for saved files in 'output/container/'
    output_dir = os.path.join(current_app.root_path, 'output', 'container')

    # Create the full file path
    file_path = os.path.join(output_dir, filename)
    
    # Check if the file exists
    if os.path.exists(file_path):
        # Send the file for download using the correct filename
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))

    return "File not found", 404