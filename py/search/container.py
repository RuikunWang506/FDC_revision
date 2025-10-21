import os
from flask import Blueprint, render_template, request, send_file, current_app,session,redirect,url_for,flash
import pandas as pd
import re
from py.utilities.auth import login_required
from py.utilities.setting import Config

container_bp = Blueprint('container', __name__)

@container_bp.route('/container', methods=['GET', 'POST'])
@login_required
def upload_container_file():
    current_user = session['username']
    if request.method == 'POST':
        files = request.files.getlist('container_files')  # Changed to support multiple files
        if files and files[0].filename:  # Check if files are selected
            all_files = []
            all_tables = []
            processing_errors = []
            
            for file in files:
                try:
                    df_header = pd.read_excel(file, sheet_name='System File', nrows=2, header=None)
                    container_number_raw = df_header.iloc[0, 0]
                    container_number = container_number_raw.split('-')[-1].strip().replace('FT', 'F')
                    step3_csv_data, step4_csv_data, df_step3, df_step4 = process_container_file(file, container_number)
                    
                    # Store CSV data in session for download
                    session[f'container_{container_number}_step3'] = step3_csv_data
                    session[f'container_{container_number}_step4'] = step4_csv_data
                    session[f'container_{container_number}_processed'] = True
                    
                    # Add download links to the list
                    all_files.extend([
                        f"{container_number}-styles.csv",
                        f"{container_number}-PO.csv"
                    ])
                    
                    # Add table data with container info
                    all_tables.append({
                        'container_number': container_number,
                        'table1': df_step3.to_html(classes="table table-bordered", index=False),
                        'table2': df_step4.to_html(classes="table table-bordered", index=False),
                        'success': True
                    })
                    
                    current_app.logger.info(f"Successfully processed container file: {file.filename}")
                    
                except Exception as e:
                    error_msg = f"Error processing file {file.filename}: {str(e)}"
                    current_app.logger.error(error_msg)
                    processing_errors.append(error_msg)
                    continue
            
            # Render the template with all processed files and tables
            return render_template('container.html', 
                                   files=all_files,
                                   tables=all_tables,
                                   errors=processing_errors,
                                   current_user=current_user)
    # On GET request, just render the upload form
    return render_template('container.html', current_user=current_user)

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


    # Generate CSV data in memory instead of saving to disk
    from io import StringIO
    step3_csv = StringIO()
    df_step3.to_csv(step3_csv, index=False)
    step3_csv_data = step3_csv.getvalue()

    step4_csv = StringIO()
    df_step4.to_csv(step4_csv, index=False)
    step4_csv_data = step4_csv.getvalue()

    # Return CSV data and DataFrames
    return step3_csv_data, step4_csv_data, df_step3, df_step4

@container_bp.route('/container/download/<container_number>/<file_type>')
@login_required
def download_container_csv(container_number, file_type):
    """Download CSV file for a processed container"""
    # Check if this container was processed in the current session
    if not session.get(f'container_{container_number}_processed'):
        flash('Container data not found. Please re-upload the file.', 'error')
        return redirect(url_for('container.upload_container_file'))
    
    # Get the stored CSV data from session
    if file_type == 'styles':
        csv_data = session.get(f'container_{container_number}_step3')
        filename = f"{container_number}-styles.csv"
    elif file_type == 'po':
        csv_data = session.get(f'container_{container_number}_step4') 
        filename = f"{container_number}-PO.csv"
    else:
        return "Invalid file type", 400
    
    if not csv_data:
        flash('CSV data not found. Please re-upload the file.', 'error')
        return redirect(url_for('container.upload_container_file'))
    
    # Convert to bytes and send
    from io import BytesIO
    bio = BytesIO(csv_data.encode('utf-8'))
    return send_file(bio, as_attachment=True, download_name=filename, mimetype='text/csv')