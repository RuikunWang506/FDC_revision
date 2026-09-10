import os
from flask import Blueprint, render_template, request, send_file, current_app,session,redirect,url_for,flash
import pandas as pd
import re
from py.utilities.auth import login_required
from py.utilities.color_abbrev import normalize_color_to_abbrev
from py.utilities.setting import Config

container_bp = Blueprint('container', __name__)


def _style_color_case_key(style: str, color: str) -> str:
    style_part = str(style or "").strip()
    color_part = normalize_color_to_abbrev(str(color or "").strip())
    return f"{style_part} {color_part}".strip().upper()


def _parse_measurement_lwh(measurement) -> tuple[float, float, float] | None:
    """Parse packing-list MEASUREMENT values like 22.8*18.5*28 or 22.8x18.5x28."""
    if measurement is None or (isinstance(measurement, float) and pd.isna(measurement)):
        return None
    text = str(measurement).strip()
    if not text or text.lower() in ("nan", "none", "/", "-"):
        return None
    parts = re.split(r"[*xX×]", text)
    if len(parts) < 3:
        return None
    try:
        return float(parts[0].strip()), float(parts[1].strip()), float(parts[2].strip())
    except (TypeError, ValueError):
        return None


def _container_year_from_source(file) -> int | None:
    """Best-effort container year from path/name (F23265 → 2023, folder 2023年 → 2023)."""
    text = ""
    if isinstance(file, str):
        text = file
    else:
        text = str(getattr(file, "name", "") or getattr(file, "filename", "") or "")
    folder = re.search(r"(20\d{2})年", text)
    if folder:
        return int(folder.group(1))
    name = os.path.basename(text)
    match = re.match(r"^FT?(\d{2})", name, re.IGNORECASE)
    if match:
        yy = int(match.group(1))
        return 2000 + yy if yy < 80 else 1900 + yy
    return None


def _normalize_carton_inches(
    length: float,
    width: float,
    height: float,
    *,
    year: int | None = None,
    from_cm_header: bool = False,
) -> tuple[float, float, float]:
    """Return carton dims in inches using era rules + size safety net.

    Eras (from Dropbox packing lists):
      - <=2022 older: K / MEASUREMENT(CM) is real cm
      - 2023 transition: mixed; use size (max>=40 => cm)
      - >=2024 newer: K usually inches even when labeled CM; still convert if max>=40
    """
    max_side = max(length, width, height)
    if year is not None and year <= 2022 and from_cm_header:
        return length / 2.54, width / 2.54, height / 2.54
    if year is not None and year <= 2022 and max_side >= 30.0:
        # Sole CM column on old sheets (no IN unit column).
        return length / 2.54, width / 2.54, height / 2.54
    if max_side >= 40.0:
        return length / 2.54, width / 2.54, height / 2.54
    return length, width, height


def extract_packing_list_case_dimensions(file) -> dict[str, dict] | None:
    """Read Placking/Packing List MEASUREMENT (masterbox) into style+color → L/W/H.

    Carton / masterbox source (normalized to inches):
      - Prefer MEASUREMENT/(CM) column when present (older/intermediate column K)
      - Else 2nd IN MEASUREMENT column (newer dual-IN: unit then carton)
      - Else 2nd MEASUREMENT / only MEASUREMENT column
      - Convert cm→in via year cutoff + size heuristic

    Returns:
      - dict keyed by uppercase ``STYLE COLOR`` (color abbreviated), values
        ``{'length': float, 'width': float, 'height': float}``
      - empty dict if the packing sheet is found but no usable measurements
      - None if the packing sheet cannot be parsed at all
    """
    if hasattr(file, "seek"):
        try:
            file.seek(0)
        except Exception:
            pass
    elif hasattr(file, "stream") and hasattr(file.stream, "seek"):
        try:
            file.stream.seek(0)
        except Exception:
            pass

    source_year = _container_year_from_source(file)

    try:
        excel_file = pd.ExcelFile(file)
    except Exception:
        return None

    packing_sheet = None
    packing_list = None
    preferred_sheet_names = ["placking list", "packing list"]

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
            if "pack" in normalized and "list" in normalized:
                packing_sheet = sheet_name
                break

    candidate_sheets = []
    if packing_sheet is not None:
        candidate_sheets.append(packing_sheet)
    if 0 not in candidate_sheets and len(excel_file.sheet_names) > 0:
        candidate_sheets.append(0)

    for sheet_name in candidate_sheets:
        try:
            for header_row in [6, 5, 7, 4, 3, 2, 1, 0]:
                try:
                    if hasattr(file, "seek"):
                        try:
                            file.seek(0)
                        except Exception:
                            pass
                    temp_df = pd.read_excel(file, sheet_name=sheet_name, header=header_row)
                    col_str = " ".join([str(c).upper() for c in temp_df.columns])
                    has_style = "STYLE" in col_str
                    has_qty = "QTY" in col_str
                    has_ctn = "CTN" in col_str
                    has_measurement = "MEASUREMENT" in col_str or "DIMENSION" in col_str
                    if (
                        (has_style and (has_qty or has_ctn or has_measurement))
                        or (has_ctn and has_measurement)
                        or (sum([has_style, has_qty, has_ctn, has_measurement]) >= 3)
                    ):
                        if len(temp_df) > 0:
                            packing_list = temp_df
                            packing_sheet = sheet_name
                            break
                except Exception:
                    continue
            if packing_list is not None:
                break
        except Exception:
            continue

    if packing_list is None:
        return None

    packing_list = packing_list.dropna(how="all")
    measurement_cols = [
        col
        for col in packing_list.columns
        if "MEASUREMENT" in str(col).upper() or "DIMENSION" in str(col).upper()
    ]
    style_color_col = None
    qty_ctn_col = None
    total_col = None

    for col in packing_list.columns:
        col_str = str(col).upper()
        if "STYLE" in col_str and style_color_col is None:
            style_color_col = col
        elif "QTY" in col_str and "CTN" in col_str and qty_ctn_col is None:
            qty_ctn_col = col
        elif col_str == "TOTAL" and total_col is None:
            total_col = col

    cm_cols = [col for col in measurement_cols if "CM" in str(col).upper()]
    in_cols = [
        col
        for col in measurement_cols
        if "IN" in str(col).upper() and "CM" not in str(col).upper()
    ]
    from_cm_header = False
    if cm_cols:
        # Older/intermediate: carton in K (CM header).
        carton_measurement_col = cm_cols[0]
        from_cm_header = True
    elif len(in_cols) >= 2:
        # New: two IN columns — unit then carton.
        carton_measurement_col = in_cols[1]
    elif len(measurement_cols) >= 2:
        carton_measurement_col = measurement_cols[1]
    elif len(measurement_cols) == 1:
        carton_measurement_col = measurement_cols[0]
        from_cm_header = "CM" in str(carton_measurement_col).upper()
    else:
        carton_measurement_col = None

    if carton_measurement_col is None or style_color_col is None:
        return {}

    packing_dict: dict[str, dict] = {}
    for _, row in packing_list.iterrows():
        try:
            style_color = (
                str(row[style_color_col]).strip() if pd.notna(row[style_color_col]) else ""
            )
            parts = style_color.rsplit(" ", 1) if style_color else []
            style = parts[0].strip() if len(parts) > 0 else ""
            color = parts[1].strip() if len(parts) > 1 else ""
            if not style or not color:
                continue

            dims = _parse_measurement_lwh(row[carton_measurement_col])
            if not dims:
                continue

            qty_ctn = 0.0
            total = 0.0
            if qty_ctn_col is not None and qty_ctn_col in row.index:
                try:
                    qty_ctn = float(row[qty_ctn_col]) if pd.notna(row[qty_ctn_col]) else 0.0
                except (TypeError, ValueError):
                    qty_ctn = 0.0
            if total_col is not None and total_col in row.index:
                try:
                    total = float(row[total_col]) if pd.notna(row[total_col]) else 0.0
                except (TypeError, ValueError):
                    total = 0.0

            if qty_ctn_col is not None and qty_ctn <= 1:
                continue

            key = _style_color_case_key(style, color)
            length, width, height = _normalize_carton_inches(
                *dims, year=source_year, from_cm_header=from_cm_header
            )
            existing = packing_dict.get(key)
            if existing is None or total > existing.get("total", 0):
                packing_dict[key] = {
                    "length": length,
                    "width": width,
                    "height": height,
                    "total": total,
                    "qty_ctn": qty_ctn,
                }
        except Exception:
            continue

    return {
        key: {
            "length": value["length"],
            "width": value["width"],
            "height": value["height"],
        }
        for key, value in packing_dict.items()
    }


def lookup_case_dimensions(
    packing_dims: dict[str, dict], style: str, color: str
) -> tuple[float | None, float | None, float | None]:
    """Return (length, width, height) for a style/color, or (None, None, None)."""
    if not packing_dims:
        return None, None, None
    key = _style_color_case_key(style, color)
    hit = packing_dims.get(key)
    if not hit:
        # Fallback: raw key without re-abbrev in case abbrev already applied upstream.
        raw = f"{str(style or '').strip()} {str(color or '').strip()}".strip().upper()
        hit = packing_dims.get(raw)
    if not hit:
        return None, None, None
    return hit.get("length"), hit.get("width"), hit.get("height")


def excel_recency_key(path: str) -> tuple[int, int, str]:
    """Sort key for packing-list Excels: (year, container_seq, path). Higher = newer."""
    folder = re.search(r"(20\d{2})年", path)
    year = int(folder.group(1)) if folder else (_container_year_from_source(path) or 0)
    name = os.path.basename(path)
    match = re.match(r"^FT?(\d+)", name, re.IGNORECASE)
    if not match:
        match = re.match(r"^L(\d+)", name, re.IGNORECASE)
    seq = int(match.group(1)) if match else 0
    return (year, seq, path)


def build_masterbox_dim_catalog(
    excel_paths: list[str],
) -> tuple[dict[str, dict], dict[str, dict], dict]:
    """Build STYLE+COLOR and STYLE masterbox maps from packing-list Excels.

    Newest Excel wins (by year folder + container number). Older files are read
    first so later/newer paths overwrite the same keys.
    """
    exact: dict[str, dict] = {}
    style_map: dict[str, dict] = {}
    exact_recency: dict[str, tuple] = {}
    style_recency: dict[str, tuple] = {}
    stats = {"files": 0, "parsed": 0, "empty": 0, "failed": 0}

    for path in sorted(excel_paths, key=excel_recency_key):
        stats["files"] += 1
        recency = excel_recency_key(path)
        try:
            dims = extract_packing_list_case_dimensions(path)
        except Exception:
            stats["failed"] += 1
            continue
        if dims is None:
            stats["failed"] += 1
            continue
        if not dims:
            stats["empty"] += 1
            continue
        stats["parsed"] += 1

        for key, value in dims.items():
            length = float(value["length"])
            width = float(value["width"])
            height = float(value["height"])
            if length <= 0 or width <= 0 or height <= 0:
                continue

            prev = exact_recency.get(key)
            if prev is None or recency >= prev:
                exact[key] = {"length": length, "width": width, "height": height}
                exact_recency[key] = recency

            style = key.rsplit(" ", 1)[0].strip().upper() if " " in key else key.upper()
            prev_style = style_recency.get(style)
            if prev_style is None or recency >= prev_style:
                style_map[style] = {"length": length, "width": width, "height": height}
                style_recency[style] = recency

    stats["exact_keys"] = len(exact)
    stats["styles"] = len(style_map)
    return exact, style_map, stats


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

    packing_dims = extract_packing_list_case_dimensions(file)
    if packing_dims is None:
        raise ValueError("Could not parse packing list sheet - valid headers not found")

    case_length_list = []
    case_width_list = []
    case_height_list = []
    for _, row in System_file_tab.iterrows():
        style = str(row['STYLE NO']).strip() if pd.notna(row['STYLE NO']) else ''
        color = str(row['COLOR']).strip() if pd.notna(row['COLOR']) else ''
        length, width, height = lookup_case_dimensions(packing_dims, style, color)
        case_length_list.append(length if length is not None else '')
        case_width_list.append(width if width is not None else '')
        case_height_list.append(height if height is not None else '')


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