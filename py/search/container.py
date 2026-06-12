import os
from flask import Blueprint, render_template, request, send_file, current_app,session,redirect,url_for,flash
import pandas as pd
import re
from py.utilities.auth import login_required
from py.utilities.setting import Config

container_bp = Blueprint('container', __name__)


def _filter_system_file_line_items(df):
    """Remove template spacer rows: blank lines, zero OMS/RMB, or zero quantity.

    Many exports alternate data with empty rows; Excel often stores 0 (not NaN) in
    numeric columns for those lines, so dropna(subset=[...]) alone does not drop them.
    """
    out = df.dropna(how='all')
    if 'STYLE NO' in out.columns:
        sty = pd.Series(out['STYLE NO']).astype('string').str.strip()
        keep_style = sty.notna() & (~sty.eq('')) & (~sty.str.lower().isin(('nan', 'none', 'nat')))
        out = out.loc[keep_style]

    def _to_num(col):
        if col not in out.columns:
            return None
        ser = (
            out[col]
            .astype(str)
            .str.replace(r'[\$,]', '', regex=True)
            .str.strip()
        )
        return pd.to_numeric(ser, errors='coerce')

    oms = _to_num('OMS')
    rmb = _to_num('RMB')
    qty = _to_num('QUANTITY')

    mask = pd.Series(True, index=out.index)
    if oms is not None:
        mask &= oms.notna() & (oms > 0)
    if rmb is not None:
        mask &= rmb.notna() & (rmb > 0)
    if qty is not None:
        mask &= qty.notna() & (qty > 0)
    return out.loc[mask]


def _trim_blank_export_rows(df_styles: pd.DataFrame, df_po: pd.DataFrame):
    """Drop spacer lines from the two export tables right before CSV write (same row index in both)."""
    if df_styles.shape[0] != df_po.shape[0] or df_styles.empty:
        return df_styles.copy(), df_po.copy()
    if 'Item#' not in df_styles.columns:
        return df_styles.copy(), df_po.copy()

    it = df_styles['Item#'].astype(str).str.strip()
    lo = it.str.lower()
    bad = it.eq('') | lo.isin(
        ('nan', 'none', 'nat', 'nan nan', 'nan none', 'none nan', '<na>')
    )
    good = ~bad
    df_styles = df_styles.loc[good].reset_index(drop=True)
    df_po = df_po.loc[good].reset_index(drop=True)
    return df_styles, df_po


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
            
            # Ensure output directory exists
            output_dir = os.path.join(current_app.root_path, 'output', 'container_csvs')
            os.makedirs(output_dir, exist_ok=True)
            for file in files:
                try:
                    df_header = pd.read_excel(file, sheet_name='System File', nrows=2, header=None)
                    container_number_raw = df_header.iloc[0, 0]
                    container_number = container_number_raw.split('-')[-1].strip().replace('FT', 'F')
                    if hasattr(file, 'seek'):
                        file.seek(0)
                    elif hasattr(file, 'stream') and hasattr(file.stream, 'seek'):
                        file.stream.seek(0)
                    step3_csv_data, step4_csv_data, df_step3, df_step4 = process_container_file(file, container_number)

                    # Save CSVs to disk
                    step3_filename = f"{container_number}-styles.csv"
                    step4_filename = f"{container_number}-PO.csv"
                    step3_path = os.path.join(output_dir, step3_filename)
                    step4_path = os.path.join(output_dir, step4_filename)
                    with open(step3_path, 'w', encoding='utf-8') as f3:
                        f3.write(step3_csv_data)
                    with open(step4_path, 'w', encoding='utf-8') as f4:
                        f4.write(step4_csv_data)

                    # Store only filenames in session
                    session[f'container_{container_number}_step3_file'] = step3_filename
                    session[f'container_{container_number}_step4_file'] = step4_filename
                    session[f'container_{container_number}_processed'] = True

                    # Add download links to the list
                    all_files.extend([step3_filename, step4_filename])

                    # Add table data with container info
                    all_tables.append({
                        'container_number': container_number,
                        'table1': df_step3.to_html(classes="table table-bordered", index=False),
                        'table2': df_step4.to_html(classes="table table-bordered", index=False),
                        'success': True
                    })


                except Exception as e:
                    error_msg = f"Error processing file {file.filename}: {str(e)}"
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
    if hasattr(file, 'seek'):
        try:
            file.seek(0)
        except Exception:
            pass
    elif hasattr(file, 'stream') and hasattr(file.stream, 'seek'):
        try:
            file.stream.seek(0)
        except Exception:
            pass

    System_file_tab = pd.read_excel(file, sheet_name='System File', header=2)
    System_file_tab.columns = System_file_tab.columns.str.replace('\n', ' ').str.strip()
    System_file_tab = _filter_system_file_line_items(System_file_tab)
    System_file_tab['WT/PCS (LBS)'] = pd.to_numeric(System_file_tab['WT/PCS (LBS)'], errors='coerce').round(2)
    System_file_tab = System_file_tab.applymap(
        lambda x: re.sub(r'\s+', ' ', str(x)) if isinstance(x, str) else x
    )
    
    # Try different sheet names for packing list
    packing_sheet = None
    PackingList = None
    
    # First, get available sheets
    if hasattr(file, 'seek'):
        try:
            file.seek(0)
        except Exception:
            pass
    elif hasattr(file, 'stream') and hasattr(file.stream, 'seek'):
        try:
            file.stream.seek(0)
        except Exception:
            pass
    excel_file = pd.ExcelFile(file)
    
    for sheet_name in ['Packing List', 'Placking List', 0]:
        if packing_sheet:
            break
        try:
            # Try different header row positions (0-7) to find actual column headers
            for header_row in [6, 5, 7, 4, 3, 2, 1, 0]:
                try:
                    temp_df = pd.read_excel(file, sheet_name=sheet_name, header=header_row)
                    
                    # Check if this looks like valid headers
                    col_str = ' '.join([str(c).upper() for c in temp_df.columns])
                    
                    # More flexible validation: look for any key column names
                    has_style = 'STYLE' in col_str
                    has_qty = 'QTY' in col_str
                    has_ctn = 'CTN' in col_str
                    has_measurement = 'MEASUREMENT' in col_str or 'DIMENSION' in col_str
                    
                    # Must have at least 2-3 of these indicators
                    if (has_style and (has_qty or has_ctn or has_measurement)) or \
                       (has_ctn and has_measurement) or \
                       (sum([has_style, has_qty, has_ctn, has_measurement]) >= 3):
                        
                        # Additional validation: check if we have numeric data in expected rows
                        if len(temp_df) > 0:
                            PackingList = temp_df
                            packing_sheet = sheet_name
                            break
                except Exception as e:
                    continue
            
            if PackingList is not None:
                break
                
        except Exception as e:
            continue
    
    if PackingList is None:
        raise ValueError("Could not parse packing list sheet - valid headers not found")

    PackingList = PackingList.dropna(how='all')
    
    # Extract case dimensions from MEASUREMENT column (format: "L*W*H")
    case_length_list = []
    case_width_list = []
    case_height_list = []
    
    # Find the carton measurement column (second MEASUREMENT column or by name pattern)
    measurement_cols = [col for col in PackingList.columns if 'MEASUREMENT' in str(col).upper() or 'DIMENSION' in str(col).upper()]
    carton_measurement_col = None
    style_color_col = None
    qty_ctn_col = None
    total_col = None
    
    # Find relevant columns - be flexible with naming
    for col in PackingList.columns:
        col_str = str(col).upper()
        if 'STYLE' in col_str and style_color_col is None:
            style_color_col = col
        elif 'QTY' in col_str and 'CTN' in col_str and qty_ctn_col is None:
            qty_ctn_col = col
        elif col_str == 'TOTAL' and total_col is None:
            total_col = col
    
    # Use second MEASUREMENT column if available (carton measurement), otherwise first
    # Pandas adds .1, .2 etc. to duplicate column names
    if len(measurement_cols) >= 2:
        carton_measurement_col = measurement_cols[1]
    elif len(measurement_cols) == 1:
        carton_measurement_col = measurement_cols[0]
    
    # Create a mapping of Style+Color from PackingList to dimensions
    # Strategy: Use rows where QTY/CTN > 1 and prefer rows with highest TOTAL value
    packing_dict = {}
    
    if carton_measurement_col is not None and style_color_col is not None:
        for idx, row in PackingList.iterrows():
            try:
                measurement = row[carton_measurement_col]
                style_color = str(row[style_color_col]).strip() if pd.notna(row[style_color_col]) else ''
                
                # Extract style and color from combined format (e.g., "N2532  BK")
                # Handle cases where style and color are separate or combined
                parts = style_color.rsplit(' ', 1) if style_color else []
                style = parts[0].strip() if len(parts) > 0 else ''
                color = parts[1].strip() if len(parts) > 1 else ''
                
                if style and color and pd.notna(measurement):
                    key = f"{style} {color}"
                    measurement_str = str(measurement).strip()
                    
                    # Only process rows where QTY/CTN > 1 (not single-pack entries) or if TOTAL is not provided
                    qty_ctn = 0
                    total = 0
                    
                    if qty_ctn_col and qty_ctn_col in row.index:
                        try:
                            qty_ctn = float(row[qty_ctn_col]) if pd.notna(row[qty_ctn_col]) else 0
                        except (ValueError, TypeError):
                            qty_ctn = 0
                    
                    if total_col and total_col in row.index:
                        try:
                            total = float(row[total_col]) if pd.notna(row[total_col]) else 0
                        except (ValueError, TypeError):
                            total = 0
                    
                    # Accept if QTY/CTN > 1 or if we don't have QTY/CTN column
                    if (qty_ctn > 1 or qty_ctn_col is None) and '*' in measurement_str:
                        dim_parts = measurement_str.split('*')
                        if len(dim_parts) >= 3:
                            try:
                                length = float(dim_parts[0].strip())
                                width = float(dim_parts[1].strip())
                                height = float(dim_parts[2].strip())
                                
                                # Store this dimension, preferring rows with higher TOTAL value
                                if key not in packing_dict or total > packing_dict[key].get('total', 0):
                                    packing_dict[key] = {
                                        'length': length,
                                        'width': width,
                                        'height': height,
                                        'total': total,
                                        'qty_ctn': qty_ctn
                                    }
                            except ValueError as e:
                                pass
            except Exception as e:
                continue
    
    # Now align with System_file_tab using the mapping
    for idx, row in System_file_tab.iterrows():
        style = str(row['STYLE NO']).strip() if pd.notna(row['STYLE NO']) else ''
        color = str(row['COLOR']).strip() if pd.notna(row['COLOR']) else ''
        key = f"{style} {color}"
        
        if key in packing_dict:
            case_length_list.append(packing_dict[key]['length'])
            case_width_list.append(packing_dict[key]['width'])
            case_height_list.append(packing_dict[key]['height'])
        else:
            case_length_list.append('')
            case_width_list.append('')
            case_height_list.append('')
    
    
    
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
        'Item#': System_file_tab['STYLE NO'] + ' ' + System_file_tab['COLOR'],  # New Item#
        'WHS#': "01",
        'Descrip': container_number,
        'Alt.Item#': System_file_tab['STYLE NO'],  # Style No
        'Color': System_file_tab['COLOR'],
        'Quantity': System_file_tab['QUANTITY'],
        'Class#' : "L1",
        'Unit/Case': System_file_tab['PCS /CTN'],
        'Box/Case': "01",
        'Sales Cost':System_file_tab['OMS'],
        'Case Wt': System_file_tab['WT/CTN (LBS)'],
        'Unit Wt': System_file_tab['WT/PCS (LBS)'],
        'Case Length': case_length_list,
        'Case Height': case_height_list,
        'Case Width': case_width_list,
        'Unit Length': System_file_tab['SIZE   (L)IN'],
        'Unit Width': System_file_tab['SIZE   (W)IN'],
        'Unit Height': System_file_tab['SIZE   (H)IN'],
        'Price1': System_file_tab['Price1'],
        'Sale Price': System_file_tab['Sale Price'],
        'Base Cost':System_file_tab['OMS'],
        'Avg.Cost':System_file_tab['OMS'],
        'PO#': System_file_tab['PO#']
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
        'Item#': System_file_tab['STYLE NO'] + ' ' + System_file_tab['COLOR'],
        'W#': "01",
        'P/O Qty': System_file_tab['QUANTITY'],
        'Price': System_file_tab['OMS'],
    }

    df_step4 = pd.DataFrame(step4_data)
    for col in step4_columns:
        if col not in df_step4.columns:
            df_step4[col] = ""
    df_step4 = df_step4[step4_columns]

    df_step3, df_step4 = _trim_blank_export_rows(df_step3, df_step4)


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
    
    # Get the stored filename from session
    if file_type == 'styles':
        filename = session.get(f'container_{container_number}_step3_file')
    elif file_type == 'po':
        filename = session.get(f'container_{container_number}_step4_file')
    else:
        return "Invalid file type", 400

    if not filename:
        flash('CSV file not found. Please re-upload the file.', 'error')
        return redirect(url_for('container.upload_container_file'))

    # Read file from disk and send
    output_dir = os.path.join(current_app.root_path, 'output', 'container_csvs')
    file_path = os.path.join(output_dir, filename)
    if not os.path.exists(file_path):
        flash('CSV file not found on server. Please re-upload the file.', 'error')
        return redirect(url_for('container.upload_container_file'))
    return send_file(file_path, as_attachment=True, download_name=filename, mimetype='text/csv')