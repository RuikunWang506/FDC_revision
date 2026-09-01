import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from collections import defaultdict
from reportlab.lib import colors as rl_colors
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from py.order.order_search import (
    fetch_order_notes,
    fetch_order_ship_to_lines,
    get_recent_fdc_orders,
    parse_boxes_from_notes,
)
from py.utilities.setting import Config
from flask import request, session, send_file, flash, redirect, url_for, render_template, jsonify, current_app
from py.utilities.setting import login_required
import io
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

WAREHOUSE_LABEL_PRINTER = os.getenv("WAREHOUSE_LABEL_PRINTER", "warehouse printer").strip()

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

# Fonts and styles
ft = Font(name="Arial", size=12)
ft_bold = Font(name="Arial", size=12, bold=True)
ft_summary = Font(name="Arial", size=14, bold=True)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

LABEL_PAGE_W = 6 * inch
LABEL_PAGE_H = 4 * inch
LABEL_MARGIN_L = 0.18 * inch
LABEL_MARGIN_T = 0.12 * inch
LABEL_MARGIN_B = 0.20 * inch
LABEL_FONT_SIZE = 11
LABEL_HIGHLIGHT_FONT_SIZE = LABEL_FONT_SIZE + 1
LABEL_SUMMARY_FONT_SIZE = 14
# Column X positions from Excel widths (A–E), scaled to printable width
_EXCEL_COL_W = (11.71, 12.29, 12.43, 6.57, 8.43)
_print_w = LABEL_PAGE_W - (2 * LABEL_MARGIN_L)
_col_x = LABEL_MARGIN_L
LABEL_COL_X = []
for _cw in _EXCEL_COL_W:
    LABEL_COL_X.append(_col_x)
    _col_x += (_cw / sum(_EXCEL_COL_W)) * _print_w
# Shorthand: A=left labels, B=style/carton#, C=right labels, D=values, E=PCS
LABEL_COL_A = LABEL_COL_X[0]
LABEL_COL_B = LABEL_COL_X[1]
LABEL_COL_C = LABEL_COL_X[2]
LABEL_COL_D = LABEL_COL_X[3]
LABEL_COL_E = LABEL_COL_X[4]

# Landscape letter 11×8.5: four 5.5×4.25 boxes (2×2), matches 6×4 label aspect
AVERY_PAGE_SIZE = landscape(letter)
AVERY_BOX_W = 5.5 * inch
AVERY_BOX_H = 4.25 * inch
AVERY_POSITIONS = (
    (0, AVERY_BOX_H),
    (AVERY_BOX_W, AVERY_BOX_H),
    (0, 0),
    (AVERY_BOX_W, 0),
)
AVERY_LABEL_SCALE = min(AVERY_BOX_W / LABEL_PAGE_W, AVERY_BOX_H / LABEL_PAGE_H)
LABEL_INTERNAL_SKU_TEXT = "内部单号internal Invoice:"

# Portrait 4×6 ez简易贴纸 (vertical)
EZ_PAGE_W = 4 * inch
EZ_PAGE_H = 6 * inch
EZ_FROM_LINE = "827 S LAWSON ST, CITY OF INDUSTRY, CA 91748"
EZ_GREY = HexColor("#6e6e6e")
EZ_ETA_FASHION_CUSTOMER_ID = "00951"
EZ_ETA_DEFAULT_SERVICE = "BTG ACCT#EC3377"
EZ_ETA_SHIP_TO_NAME = "ETA Fashion"
EZ_SERVICE_MAX_LINES = 3
EZ_SERVICE_SIDE_MARGIN = 0.20 * inch
EZ_BOX_NUM_BASELINE = 1.05 * inch
EZ_BOX_OF_BASELINE = 0.40 * inch
EZ_MIDDLE_GAP_AFTER_FROM = 0.10 * inch
EZ_MIDDLE_GAP_ABOVE_BOX = 0.12 * inch
_LABEL_CJK_REGISTERED = False


def _label_ensure_cjk_fonts():
    global _LABEL_CJK_REGISTERED
    if _LABEL_CJK_REGISTERED:
        return
    import os
    candidates = (
        (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc"), 0),
        (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyhbd.ttc"), 0),
        (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf"), None),
    )
    regular_path = None
    bold_path = None
    for path, _ in candidates:
        if os.path.isfile(path):
            if "bd" in path.lower() or "bold" in path.lower():
                bold_path = bold_path or path
            else:
                regular_path = regular_path or path
    if regular_path:
        pdfmetrics.registerFont(TTFont("LabelCJK", regular_path))
        pdfmetrics.registerFont(TTFont("LabelCJK-Bold", bold_path or regular_path))
        _LABEL_CJK_REGISTERED = True


def _label_has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text))


def _label_font_name(text, bold=False):
    if _label_has_cjk(text):
        _label_ensure_cjk_fonts()
        if _LABEL_CJK_REGISTERED:
            return "LabelCJK-Bold" if bold else "LabelCJK"
    return "Helvetica-Bold" if bold else "Helvetica"


def extract_label_numbers(ut_desc):
    """
    Extracts individual label numbers from the UT_DESC string.
    Handles ranges (e.g., '1-30') and individual numbers separated by commas.
    """
    parts = ut_desc.split(',')
    label_numbers = []
    for part in parts:
        part = re.sub(r'-+', '-', part)  # Normalize multiple dashes to single dash
        range_parts = part.split('-')
        if len(range_parts) == 2:
            try:
                start, end = int(range_parts[0].strip()), int(range_parts[1].strip())
                label_numbers.extend(range(start, end + 1))
            except ValueError:
                print(f"extract_label_numbers: Unable to parse range from '{part}'")
        else:
            try:
                label_numbers.append(int(range_parts[0].strip()))
            except ValueError:
                print(f"extract_label_numbers: Unable to parse label number from '{part}'")
    return label_numbers

def get_data_from_sql(order_number):
    print("get_data_from_sql: Starting data retrieval for order number:", order_number)
    try:
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            error_msg = "Invalid input length. Please provide a 4 or 5-digit input."
            print("get_data_from_sql:", error_msg)
            return None, error_msg

        conn_str = (
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        print("get_data_from_sql: Connecting to database:", database_name)
        conn = pyodbc.connect(conn_str)
        print("get_data_from_sql: Connection successful.")

        query = f"""
            WITH ExtractedNumbers AS (
                SELECT DISTINCT
                    ord.UT_DESC,
                    ord.PROD_CD,
                    ord.PC_UNIT,
                    ord.order_qty,
                    orders.SP_ADR,
                    orders.SP_ADR_2,
                    orders.SP_ADR_22,
                    orders.SP_ADR_3,
                    orders.ord_num,
                    orders.PO_NUM,
                    ntfile.NOTES
                FROM 
                    DBO.ORD_LOG AS ord
                JOIN 
                    dbo.orders AS orders ON ord.ord_num = orders.ord_num
                LEFT JOIN
                    dbo.ntfile AS ntfile ON orders.ord_num = ntfile.nt_num
                WHERE  
                    ord.ord_num = {order_number}
            )
            SELECT 
                UT_DESC, 
                PROD_CD, 
                PC_UNIT, 
                SP_ADR, 
                SP_ADR_2, 
                SP_ADR_22, 
                SP_ADR_3,
                ord_num,
                PO_NUM,
                NOTES,
                order_qty,
                order_qty % PC_UNIT AS Remainder
            FROM 
                ExtractedNumbers
            ORDER BY 
                CASE 
                    WHEN ISNUMERIC(
                        LEFT(
                            UT_DESC,
                            CASE
                                WHEN CHARINDEX('-', UT_DESC) > 0 THEN CHARINDEX('-', UT_DESC) - 1
                                WHEN CHARINDEX(',', UT_DESC) > 0 THEN CHARINDEX(',', UT_DESC) - 1
                                ELSE LEN(UT_DESC)
                            END
                        )
                    ) = 1 THEN CAST(
                        LEFT(
                            UT_DESC,
                            CASE
                                WHEN CHARINDEX('-', UT_DESC) > 0 THEN CHARINDEX('-', UT_DESC) - 1
                                WHEN CHARINDEX(',', UT_DESC) > 0 THEN CHARINDEX(',', UT_DESC) - 1
                                ELSE LEN(UT_DESC)
                            END
                        ) AS INT)
                    ELSE 1
                END;
        """

        print("get_data_from_sql: Executing query.")
        df = pd.read_sql_query(query, conn)
        conn.close()
        print("get_data_from_sql: Query executed and connection closed.")

        if df.empty:
            no_data_msg = f"No data found for order number: {order_number}"
            print("get_data_from_sql:", no_data_msg)
            return None, no_data_msg

        print(f"get_data_from_sql: Retrieved {len(df)} rows.")
        return df, None
    except Exception as e:
        error_msg = f"Exception in get_data_from_sql: {str(e)}"
        print(error_msg)
        return None, error_msg

def process_orders(order_number):
    print(f"\nprocess_orders: Processing order number: {order_number}")
    df, error_message = get_data_from_sql(order_number)
    
    if error_message:
        print("process_orders: Error encountered:", error_message)
        return None, error_message, None, None, None, None, None  # Added an extra None for warnings
    
    # Drop rows with missing 'UT_DESC' or 'PROD_CD'
    df = df.dropna(subset=['UT_DESC', 'PROD_CD'])
    
    # Dictionary to map label_number to list of (sku, color, quantity)
    label_mapping = defaultdict(list)
    
    print("process_orders: Parsing UT_DESC and mapping label numbers to SKUs and Colors.")
    
    # Initialize a list to track unassigned remainders
    unassigned_remainders = []
    pc_unit_dict = {}

    for idx, row in df.iterrows():
        ut_desc = row['UT_DESC'].strip()
        sku = row['PROD_CD'].split()[0]
        color = row['PROD_CD'].split()[1].strip() if len(row['PROD_CD'].split()) > 1 else "Unknown"
        pc_unit = int(row['PC_UNIT'])
        order_qty = int(row['order_qty'])
        style_color = f"{row['PROD_CD'].split()[0]}_{row['PROD_CD'].split()[1].strip()}"
        pc_unit_dict[style_color] = int(row['PC_UNIT'])
        
        label_numbers = extract_label_numbers(ut_desc)
        total_labels = len(label_numbers)
        
        # Calculate full labels and assign quantities
        full_labels = order_qty // pc_unit
        rem = order_qty % pc_unit
        
        # Assign pc_unit to full labels
        for i in range(full_labels):
            if i < total_labels:
                label_num = label_numbers[i]
                label_mapping[label_num].append((sku, color, pc_unit))
            else:
                print(f"process_orders: Not enough label numbers for SKU {sku}.")
        
        # Assign remainder to the next label number if exists
        if rem > 0:
            if full_labels < total_labels:
                label_num = label_numbers[full_labels]
                label_mapping[label_num].append((sku, color, rem))
            else:
                # Remainder cannot be assigned to any label
                unassigned_remainders.append({
                    'Style': sku,
                    'Color': color,
                    'Remainder': rem,
                    'UT_DESC': ut_desc
                })
                print(f"process_orders: Warning - Remainder {rem} for Style {sku} not assigned to any label.")
    
    # Identify duplicate label numbers (assigned to multiple SKUs/colors)
    duplicate_label_numbers = [label for label, assignments in label_mapping.items() if len(assignments) > 1]
    print(f"process_orders: Duplicate label numbers identified: {duplicate_label_numbers}")
    
    # Total number of labels is the number of unique label numbers
    total_unique_labels = len(label_mapping)
    print(f"process_orders: Total number of unique labels: {total_unique_labels}")
    
    # Initialize Excel workbook
    print("process_orders: Creating Excel workbook.")
    wb = Workbook()
    ws_labels = wb.active
    ws_labels.title = "Labels"
    ws_labels.page_setup.orientation = "landscape"
    ws_labels.page_setup.paperSize = "1"
    
    # Restore original column widths
    ws_labels.column_dimensions['A'].width = 11.71
    ws_labels.column_dimensions['B'].width = 12.29
    ws_labels.column_dimensions['C'].width = 12.43
    ws_labels.column_dimensions['D'].width = 6.57
    ws_labels.column_dimensions['E'].width = 8.43
    
    ws_labels.page_margins.left = 1
    ws_labels.page_margins.right = 0
    ws_labels.page_margins.top = 0
    ws_labels.page_margins.bottom = 0
    ws_labels.page_margins.header = 0
    ws_labels.page_margins.footer = 0

    # Add empty rows at the top
    ws_labels.append([])  # Row 1
    ws_labels.append([])  # Row 2
    output_line_number = 3  # Start from row 3

    # Set up addresses
    print("process_orders: Setting up addresses.")
    from_address = (
        ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 5 else
        ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 4 else
        ["FROM: N/A", "Address Line 2", "Address Line 3", "Address Line 4"]
    )
    to_address = [
        "TO: " + df.iloc[0]['SP_ADR'], 
        df.iloc[0]['SP_ADR_2'], 
        df.iloc[0]['SP_ADR_22'], 
        df.iloc[0]['SP_ADR_3']
    ]
    po_number = str(df.iloc[0]['PO_NUM'])
    notes = str(df.iloc[0]['NOTES'])
    text_after_boxes_match = re.search(r'BOXES\s+(.*)', notes, re.IGNORECASE)
    text_after_boxes = text_after_boxes_match.group(1) if text_after_boxes_match else "N/A"

    # Reference chart to track label assignments
    label_reference = []

    # Initialize label counters
    sl_num = 1

    print("process_orders: Generating labels.")

    for label_num in sorted(label_mapping.keys()):
        assignments = label_mapping[label_num]
        if len(assignments) == 1:
            # Single SKU and Color assignment
            sku, color, quantity = assignments[0]
            style_color = f"{sku}_{color}"
            label_info = {
                'label_number': sl_num,
                'Style': sku,
                'Color': color,
                'Duplicate': "No",
                'Duplicate_SKUs': "",
                'PC_unit': pc_unit_dict.get(style_color, 0),  # Use the correct PC_unit for this style-color combination
                'quantity': quantity,
            }
        else:
            # Multiple SKU and/or Color assignments - create MIX label
            total_quantity = sum([q for _, _, q in assignments])
            duplicate_skus = ", ".join([f"{sku} ({color})" for sku, color, _ in assignments])
            label_info = {
                'label_number': sl_num,
                'Style': sku,
                'Color': "MIX",
                'Duplicate': "重复数字",
                'Duplicate_SKUs': duplicate_skus,
                'PC_unit': row['PC_UNIT'],
                'quantity': total_quantity,
            }
        
        label_reference.append(label_info)
        # Create label
        output_line_number = create_label(
            ws_labels, from_address, to_address, output_line_number,
            sku, color if len(assignments) == 1 else "MIX", po_number, label_num, quantity if len(assignments) == 1 else total_quantity, total_unique_labels
        )
        sl_num += 1
        # Add an empty row after each label for readability
        output_line_number +=2  # Empty row

    # Now, handle summary row at the end
    print("\nprocess_orders: Adding summary row.")
    summary_start_row = output_line_number + 7
    summary_end_row = summary_start_row + 5  # Adjust as needed

    print(f"process_orders: Summary will start at row {summary_start_row}")
    print(f"process_orders: Summary will end at row {summary_end_row}")

    print("process_orders: Adding summary information.")
    ws_labels["B" + str(summary_start_row)] = "FDC"
    ws_labels["C" + str(summary_start_row)].font = ft_summary
    ws_labels["B" + str(summary_start_row + 1)] = LABEL_INTERNAL_SKU_TEXT
    ws_labels["C" + str(summary_start_row + 1)] = str(order_number)
    ws_labels["C" + str(summary_start_row + 1)].font = ft_summary
    ws_labels["B" + str(summary_start_row + 3)] = "Boxes #:"
    ws_labels["C" + str(summary_start_row + 3)] = str(total_unique_labels)
    ws_labels["C" + str(summary_start_row + 3)].font = ft_summary
    ws_labels["B" + str(summary_start_row + 4)] = "PO#:"
    ws_labels["C" + str(summary_start_row + 4)] = po_number
    ws_labels["C" + str(summary_start_row + 4)].font = ft_summary
    ws_labels["B" + str(summary_start_row + 5)] = "LOCATION: "
    ws_labels["C" + str(summary_start_row + 5)] = text_after_boxes
    ws_labels["C" + str(summary_start_row + 5)].font = ft_summary

    # Optionally, add an empty row after summary
    output_line_number = summary_end_row + 1

    # Save reference chart to a new tab within the same workbook
    print("process_orders: Saving reference chart to 'Reference' tab.")
    ws_reference = wb.create_sheet(title="Reference")
    reference_df = pd.DataFrame(label_reference)
    
    # Define headers
    headers = [
        'Label Number', 'Style', 'Color', 'Duplicate',
        'Duplicate SKUs', 'PC Unit', 'quantity' ]
    
    # Add title to Reference sheet
    ws_reference.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    ws_reference.cell(row=1, column=1, value="Reference Chart").font = Font(name="Arial", size=16, bold=True)
    ws_reference.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    
    # Write headers
    for col_num, header in enumerate(headers, start=1):
        cell = ws_reference.cell(row=2, column=col_num, value=header)
        cell.font = Font(name="Arial", size=12, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    
    # Write data starting from row 3
    for r_idx, row in enumerate(reference_df.itertuples(index=False), start=3):
        for c_idx, value in enumerate(row, start=1):
            ws_reference.cell(row=r_idx, column=c_idx, value=value)
    
    # Restore original column widths for the "Reference" sheet
    column_widths = {
        'A': 15,  # Label Number
        'B': 15,  # Style
        'C': 15,  # Color
        'D': 12,  # Duplicate
        'E': 25,  # Duplicate SKUs
        'F': 20,  # Duplicate Remainders
        'H': 15,  # Total Quantity
        'I': 15,  # PC Unit
        'J': 20,  # Quantity to Put in Label
        'K': 15,  # Quantity
        'L': 20   # Remaining Quantity
    }
    for col, width in column_widths.items():
        ws_reference.column_dimensions[col].width = width

    # Adjust row heights for better readability
    for row in ws_labels.iter_rows(min_row=1, max_row=ws_labels.max_row):
        ws_labels.row_dimensions[row[0].row].height = 15
    
    # Set print area to include all labels and the summary
    ws_labels.print_area = f"A1:E{summary_end_row}"

    # Save the Excel workbook to memory instead of disk
    from io import BytesIO
    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    excel_data = bio.getvalue()
    print(f"process_orders: Excel file generated in memory")

    # Prepare warning messages
    if unassigned_remainders:
        warnings = []
        print("\n=== WARNING ===")
        for rem in unassigned_remainders:
            warning_msg = f"Style: {rem['Style']}, Color: {rem['Color']}, UT_DESC: {rem['UT_DESC']}, Unassigned Remainder: {rem['Remainder']}"
            warnings.append(warning_msg)
            print(warning_msg)
        print("Please check if you missed any box assignments for the above styles.")
    else:
        warnings = []
        print("\nAll remainders have been successfully assigned to labels.")

    print(f"process_orders: Final output_line_number = {output_line_number}, sl_num = {sl_num}")

    return excel_data, None, total_unique_labels, duplicate_label_numbers, label_reference, to_address, warnings

def create_label(ws, from_address, to_address, output_line_number, sku, color, po_number, label_number, quantity, total_labels):
    """
    Creates a single label in the Excel worksheet.
    """
    print(f"create_label: Creating label for label number {label_number} with quantity {quantity}.")


    for address in from_address:
        cell = f"A{output_line_number}"
        ws[cell] = address
        ws[cell].font = ft
        output_line_number +=1
    output_line_number +=1  # Blank line

    # Add to_address
    for address in to_address:
        cell = f"A{output_line_number}"
        ws[cell] = address
        ws[cell].font = ft
        output_line_number +=1
    output_line_number +=1  # Blank line

    # Add DEPT# and PO#
    ws[f"A{output_line_number}"] = "DEPT#"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"C{output_line_number}"] = f"P.O. # {po_number}"
    ws[f"C{output_line_number}"].font = ft_bold
    ws.merge_cells(f"C{output_line_number}:E{output_line_number}")
    ws[f"C{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    output_line_number +=1

    # Add STYLE#, QUANTITY, and PCS
    ws[f"A{output_line_number}"] = "STYLE#"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = sku
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "QUANTITY"
    ws[f"C{output_line_number}"].font = ft_bold
    ws[f"D{output_line_number}"] = quantity
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"] = "PCS"
    ws[f"E{output_line_number}"].font = ft_bold
    output_line_number +=1

    # Add SIZE and COLOR
    ws[f"A{output_line_number}"] = "SIZE:N-A"
    ws[f"A{output_line_number}"].font = ft
    ws.merge_cells(f"A{output_line_number}:B{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"C{output_line_number}"] = "COLOR:"
    ws[f"C{output_line_number}"].font = ft_bold
    ws[f"D{output_line_number}"] = color
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    output_line_number +=1

    # Add COORDINATE and PRE-TICKETED
    ws[f"A{output_line_number}"] = "COORDINATE: NO"
    ws[f"A{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "PRE-TICKETED: NO"
    ws[f"C{output_line_number}"].font = ft
    output_line_number +=2  # Blank line

    # Add TOTAL QUANTITY IN CARTON
    ws[f"A{output_line_number}"] = "TOTAL QUANTITY IN CARTON"
    ws.merge_cells(f"A{output_line_number}:C{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"D{output_line_number}"] = f"=D{output_line_number-4}"
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"] = ""
    ws[f"E{output_line_number}"].font = ft
    output_line_number +=2  # Blank line

    # Add CARTON information
    ws[f"A{output_line_number}"] = "CARTON"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = label_number
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].alignment = Alignment(horizontal="center", vertical="center")
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "OF"
    ws[f"C{output_line_number}"].font = ft_bold
    ws[f"D{output_line_number}"] = total_labels  # Placeholder filled with total labels
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    output_line_number +=2  # Blank line

    print(f"create_label: Label for label number {label_number} created at line {output_line_number}.")
    return output_line_number

def generate_summary_page(ws, from_address, to_address, output_line_number, po_number, text_after_boxes, label_reference, total_unique_labels):
    """
    Generates a summary page at the end of the labels.
    """
    print("generate_summary_page: Creating summary page.")

    # Add summary header
    ws[f"A{output_line_number}"] = "SUMMARY"
    ws[f"A{output_line_number}"].font = Font(name="Arial", size=16, bold=True)
    output_line_number +=2  # Blank line

    # Add PO Number
    ws[f"A{output_line_number}"] = "P.O. #"
    ws[f"B{output_line_number}"] = po_number
    ws[f"B{output_line_number}"].font = Font(name="Arial", size=14, bold=True)
    output_line_number +=1

    # Add Text After Boxes
    ws[f"A{output_line_number}"] = "LOCATION:"
    ws[f"B{output_line_number}"] = text_after_boxes
    ws[f"B{output_line_number}"].font = Font(name="Arial", size=14, bold=True)
    output_line_number +=2  # Blank line

    # Add Total Labels
    ws[f"A{output_line_number}"] = "Total Labels:"
    ws[f"B{output_line_number}"] = total_unique_labels
    ws[f"B{output_line_number}"].font = Font(name="Arial", size=14, bold=True)
    output_line_number +=2  # Blank line

    # Add Detailed Summary
    ws[f"A{output_line_number}"] = "Detailed Summary"
    ws[f"A{output_line_number}"].font = Font(name="Arial", size=14, bold=True)
    output_line_number +=1

    # Add headers
    headers = ["Label Number", "Style", "Color", "Duplicate", "Quantity"]
    for col_num, header in enumerate(headers, start=1):
        cell = ws.cell(row=output_line_number, column=col_num)
        cell.value = header
        cell.font = Font(name="Arial", size=12, bold=True)
    output_line_number +=1

    # Add label details
    for label in label_reference:
        ws[f"A{output_line_number}"] = label['label_number']
        ws[f"B{output_line_number}"] = label['Style']
        ws[f"C{output_line_number}"] = label['Color']
        ws[f"D{output_line_number}"] = label['Duplicate']
        ws[f"E{output_line_number}"] = label['Quantity']
        output_line_number +=1

    print("generate_summary_page: Summary page created.")
    return output_line_number


def _label_cell_merge_origin(ws, row, col):
    """Return top-left (row, col) when cell is inside a merged range."""
    for merged in ws.merged_cells.ranges:
        if row >= merged.min_row and row <= merged.max_row and col >= merged.min_col and col <= merged.max_col:
            return merged.min_row, merged.min_col
    return row, col


def _label_cell_display_value(ws, cell):
    val = cell.value
    if isinstance(val, str) and val.startswith("="):
        ref = val[1:].strip()
        try:
            return ws[ref].value
        except Exception:
            return val
    return val


def _label_cell_is_yellow(cell):
    fill = cell.fill
    if not fill or fill.fill_type != "solid":
        return False
    rgb = (fill.start_color.rgb or fill.fgColor.rgb or "") if fill.start_color else ""
    if isinstance(rgb, str):
        rgb = rgb.replace("FF", "", 1) if rgb.startswith("FF") else rgb
        return rgb.upper() in ("FFFF00", "FFFFFF00", "00FFFF00")
    return False


def _label_ws_cell(ws, row, col):
    origin_r, origin_c = _label_cell_merge_origin(ws, row, col)
    if origin_r != row or origin_c != col:
        return None, None
    cell = ws.cell(row=row, column=col)
    return _label_cell_display_value(ws, cell), cell


def _label_find_starts(ws, max_row):
    starts = []
    for row in range(1, max_row + 1):
        val, _ = _label_ws_cell(ws, row, 1)
        if val and str(val).strip().upper().startswith("FROM:"):
            starts.append(row)
    return starts


def _label_find_summary_start(ws, max_row):
    """Row where the end-of-workbook stats block begins (FDC / internal sku line)."""
    for row in range(1, max_row + 1):
        b_val, _ = _label_ws_cell(ws, row, 2)
        if not b_val:
            continue
        b_text = str(b_val).strip()
        if b_text == LABEL_INTERNAL_SKU_TEXT or b_text == "内部单号:" or b_text.startswith(
            "内部单号"
        ):
            return row - 1
    return None


def _label_block_end(ws, start_row, next_start, max_row):
    end_row = (next_start - 1) if next_start else max_row
    while end_row > start_row:
        if any(
            _label_ws_cell(ws, end_row, col)[0]
            for col in range(1, 6)
        ):
            break
        end_row -= 1
    return end_row


def _label_parse_block(ws, start_row, end_row):
    data = {
        "from_lines": [],
        "to_lines": [],
        "po": "",
        "style_val": "",
        "style_yellow": False,
        "qty_val": "",
        "qty_yellow": False,
        "size": "",
        "color_val": "",
        "color_yellow": False,
        "coordinate": "",
        "preticketed": "",
        "total_val": "",
        "carton_num": "",
        "carton_num_yellow": False,
        "carton_total": "",
        "carton_total_yellow": False,
    }
    mode = None

    for row in range(start_row, end_row + 1):
        a_val, a_cell = _label_ws_cell(ws, row, 1)
        a_text = str(a_val).strip() if a_val is not None else ""

        if a_text.upper().startswith("FROM:"):
            mode = "from"
            data["from_lines"].append(a_text)
            continue
        if a_text.upper().startswith("TO:"):
            mode = "to"
            data["to_lines"].append(a_text)
            continue
        if a_text == "DEPT#":
            mode = "fields"
            po_val, _ = _label_ws_cell(ws, row, 3)
            data["po"] = str(po_val).strip() if po_val is not None else ""
            continue
        if a_text == "STYLE#":
            mode = "fields"
            b_val, b_cell = _label_ws_cell(ws, row, 2)
            d_val, d_cell = _label_ws_cell(ws, row, 4)
            data["style_val"] = "" if b_val is None else str(b_val)
            data["style_yellow"] = _label_cell_is_yellow(b_cell) if b_cell else False
            data["qty_val"] = "" if d_val is None else str(d_val)
            data["qty_yellow"] = _label_cell_is_yellow(d_cell) if d_cell else False
            continue
        if a_text.startswith("SIZE"):
            mode = "fields"
            data["size"] = a_text
            d_val, d_cell = _label_ws_cell(ws, row, 4)
            data["color_val"] = "" if d_val is None else str(d_val)
            data["color_yellow"] = _label_cell_is_yellow(d_cell) if d_cell else False
            continue
        if a_text.startswith("COORDINATE"):
            mode = "fields"
            data["coordinate"] = a_text
            c_val, _ = _label_ws_cell(ws, row, 3)
            data["preticketed"] = str(c_val).strip() if c_val is not None else ""
            continue
        if a_text.startswith("TOTAL QUANTITY"):
            mode = "fields"
            d_val, _ = _label_ws_cell(ws, row, 4)
            data["total_val"] = "" if d_val is None else str(d_val)
            continue
        if a_text == "CARTON":
            mode = "fields"
            b_val, b_cell = _label_ws_cell(ws, row, 2)
            d_val, d_cell = _label_ws_cell(ws, row, 4)
            data["carton_num"] = "" if b_val is None else str(b_val)
            data["carton_num_yellow"] = _label_cell_is_yellow(b_cell) if b_cell else False
            data["carton_total"] = "" if d_val is None else str(d_val)
            data["carton_total_yellow"] = _label_cell_is_yellow(d_cell) if d_cell else False
            continue

        if mode == "from" and a_text:
            data["from_lines"].append(a_text)
        elif mode == "to" and a_text:
            data["to_lines"].append(a_text)

    return data


def _label_row_baseline(row_idx, total_rows, font_size=LABEL_FONT_SIZE):
    usable = LABEL_PAGE_H - LABEL_MARGIN_T - LABEL_MARGIN_B
    step = usable / total_rows
    return LABEL_PAGE_H - LABEL_MARGIN_T - (row_idx + 1) * step + font_size * 0.32


def _label_draw_at(c, x, y, text, bold=False, yellow=False, font_size=None):
    if text is None or text == "":
        return 0.0
    s = str(text).strip()
    if not s:
        return 0.0
    if font_size is None:
        font_size = LABEL_HIGHLIGHT_FONT_SIZE if yellow else LABEL_FONT_SIZE
    font = _label_font_name(s, bold=bold)
    draw_y = y - (font_size - LABEL_FONT_SIZE) * 0.35 if yellow else y
    if yellow:
        w = pdfmetrics.stringWidth(s, font, font_size)
        c.setFillColor(rl_colors.yellow)
        c.rect(x - 2, draw_y - 3, w + 5, font_size + 5, fill=1, stroke=0)
        c.setFillColor(rl_colors.black)
    c.setFont(font, font_size)
    c.drawString(x, draw_y, s)
    return pdfmetrics.stringWidth(s, font, font_size)


def _label_draw_page(c, data):
    # Each tuple is a row slot so vertical space fills the 4" label evenly.
    rows = []
    rows.extend(("addr", a) for a in data["from_lines"][:3])
    rows.append(("gap",))
    rows.extend(("addr", a) for a in data["to_lines"][:4])
    rows.append(("gap",))
    rows.append(("dept",))
    rows.append(("style",))
    rows.append(("size",))
    rows.append(("coord",))
    rows.append(("gap",))
    rows.append(("total",))
    rows.append(("gap",))
    rows.append(("carton",))

    total_rows = len(rows)
    fs = LABEL_FONT_SIZE

    for row_idx, row in enumerate(rows):
        y = _label_row_baseline(row_idx, total_rows, fs)
        kind = row[0]

        if kind == "addr":
            _label_draw_at(c, LABEL_COL_A, y, row[1])
        elif kind == "gap":
            pass
        elif kind == "dept":
            _label_draw_at(c, LABEL_COL_A, y, "DEPT#", bold=True)
            _label_draw_at(c, LABEL_COL_C, y, data["po"], bold=True)
        elif kind == "style":
            _label_draw_at(c, LABEL_COL_A, y, "STYLE#", bold=True)
            _label_draw_at(
                c, LABEL_COL_B, y, data["style_val"], yellow=data["style_yellow"]
            )
            qty_lbl = "QUANTITY"
            qty_w = _label_draw_at(c, LABEL_COL_C, y, qty_lbl, bold=True)
            qty_x = LABEL_COL_C + qty_w + 6
            val_w = _label_draw_at(
                c, qty_x, y, data["qty_val"], yellow=data["qty_yellow"]
            )
            _label_draw_at(c, qty_x + val_w + 6, y, "PCS", bold=True)
        elif kind == "size":
            _label_draw_at(c, LABEL_COL_A, y, data["size"] or "SIZE:N-A")
            _label_draw_at(c, LABEL_COL_C, y, "COLOR:", bold=True)
            _label_draw_at(
                c, LABEL_COL_D, y, data["color_val"], yellow=data["color_yellow"]
            )
        elif kind == "coord":
            _label_draw_at(c, LABEL_COL_A, y, data["coordinate"] or "COORDINATE: NO")
            _label_draw_at(
                c, LABEL_COL_C, y, data["preticketed"] or "PRE-TICKETED: NO"
            )
        elif kind == "total":
            _label_draw_at(c, LABEL_COL_A, y, "TOTAL QUANTITY IN CARTON", bold=True)
            _label_draw_at(c, LABEL_COL_D, y, data["total_val"])
        elif kind == "carton":
            _label_draw_at(c, LABEL_COL_A, y, "CARTON", bold=True)
            _label_draw_at(
                c, LABEL_COL_B, y, data["carton_num"], yellow=data["carton_num_yellow"]
            )
            _label_draw_at(c, LABEL_COL_C, y, "OF", bold=True)
            _label_draw_at(
                c, LABEL_COL_D, y, data["carton_total"], yellow=data["carton_total_yellow"]
            )


def _label_parse_summary(ws, start_row):
    lines = []
    for offset in range(6):
        row = start_row + offset
        b_val, _ = _label_ws_cell(ws, row, 2)
        c_val, _ = _label_ws_cell(ws, row, 3)
        b_text = str(b_val).strip() if b_val is not None else ""
        c_text = str(c_val).strip() if c_val is not None else ""
        if not b_text and not c_text:
            continue
        if b_text in ("内部单号:", LABEL_INTERNAL_SKU_TEXT) or b_text.startswith("内部单号"):
            b_text = LABEL_INTERNAL_SKU_TEXT
        if b_text and not c_text:
            lines.append(("heading", b_text))
        else:
            lines.append(("pair", b_text, c_text))
    return lines


def _label_draw_summary_pair(c, y, label, value, font_size):
    """Draw summary label/value on one row without overlap."""
    label_s = str(label).strip()
    value_s = str(value).strip()
    label_font = _label_font_name(label_s, bold=True)
    label_w = pdfmetrics.stringWidth(label_s, label_font, font_size)
    value_x = max(LABEL_COL_D, LABEL_COL_B + label_w + 10)
    _label_draw_at(c, LABEL_COL_B, y, label_s, bold=True, font_size=font_size)
    _label_draw_at(c, value_x, y, value_s, bold=True, font_size=font_size)


def _label_draw_summary_page(c, ws, start_row):
    lines = _label_parse_summary(ws, start_row)
    if not lines:
        return
    total_rows = len(lines)
    fs = LABEL_SUMMARY_FONT_SIZE

    for row_idx, line in enumerate(lines):
        y = _label_row_baseline(row_idx, total_rows, fs)
        kind = line[0]
        if kind == "heading":
            _label_draw_at(c, LABEL_COL_B, y, line[1], bold=True, font_size=fs)
        else:
            _label_draw_summary_pair(c, y, line[1], line[2], fs)


def _label_collect_from_excel(excel_data):
    wb = load_workbook(io.BytesIO(excel_data), data_only=False)
    if "Labels" not in wb.sheetnames:
        raise ValueError("Workbook is missing the Labels sheet.")
    ws = wb["Labels"]
    max_row = ws.max_row or 1
    starts = _label_find_starts(ws, max_row)
    summary_start = _label_find_summary_start(ws, max_row)
    labels = []

    for idx, start_row in enumerate(starts):
        next_start = starts[idx + 1] if idx + 1 < len(starts) else None
        end_row = _label_block_end(ws, start_row, next_start, max_row)
        if summary_start and (next_start is None or end_row >= summary_start):
            end_row = min(end_row, summary_start - 1)
            while end_row > start_row and not any(
                _label_ws_cell(ws, end_row, col)[0] for col in range(1, 6)
            ):
                end_row -= 1
        labels.append(_label_parse_block(ws, start_row, end_row))

    return ws, labels, summary_start


def _should_include_summary_on_4up(num_labels, summary_start):
    """
    Omit the summary when it would be alone on a new Avery sheet (e.g. 60 labels
    on 15 sheets → summary would waste 3 of 4 slots on sheet 16).
    """
    if not summary_start or num_labels == 0:
        return False
    return num_labels % 4 != 0


def _label_draw_in_avery_box(c, box_x, box_y, draw_fn, *args):
    c.saveState()
    c.translate(box_x, box_y)
    c.scale(AVERY_LABEL_SCALE, AVERY_LABEL_SCALE)
    draw_fn(c, *args)
    c.restoreState()


def excel_labels_to_pdf(excel_data):
    """
    Convert the Labels worksheet to a landscape 6×4 inch PDF (4×6 label horizontal),
    one page per shipping label, matching the Excel/printed layout.
    """
    ws, labels, summary_start = _label_collect_from_excel(excel_data)

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(LABEL_PAGE_W, LABEL_PAGE_H))
    page_drawn = False

    for data in labels:
        if page_drawn:
            pdf.showPage()
        _label_draw_page(pdf, data)
        page_drawn = True

    if summary_start:
        if page_drawn:
            pdf.showPage()
        _label_draw_summary_page(pdf, ws, summary_start)
        page_drawn = True

    if not page_drawn:
        pdf.showPage()

    pdf.save()
    buf.seek(0)
    return buf.getvalue()


def excel_labels_to_pdf_4perpage(excel_data):
    """
    US Letter landscape 11×8.5 with four 5.5×4.25 labels per page (2×2).
    Summary is included unless it would occupy a sheet alone (label count divisible by 4).
    """
    ws, labels, summary_start = _label_collect_from_excel(excel_data)
    items = [("label", data) for data in labels]
    if _should_include_summary_on_4up(len(labels), summary_start):
        items.append(("summary", summary_start))

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=AVERY_PAGE_SIZE)

    for page_start in range(0, len(items), 4):
        if page_start:
            pdf.showPage()
        for slot, item in enumerate(items[page_start : page_start + 4]):
            box_x, box_y = AVERY_POSITIONS[slot]
            kind, payload = item
            if kind == "label":
                _label_draw_in_avery_box(pdf, box_x, box_y, _label_draw_page, payload)
            else:
                _label_draw_in_avery_box(
                    pdf, box_x, box_y, _label_draw_summary_page, ws, payload
                )

    if not items:
        pdf.showPage()

    pdf.save()
    buf.seek(0)
    return buf.getvalue()


def _record_jennifer_label_download(order_number, username, logger):
    try:
        from py.other.label_status import mark_jennifer_label
        mark_jennifer_label(order_number, username)
    except Exception:
        logger.exception("Failed to record Jennifer label status")


def _resolve_sumatra_pdf_path():
    explicit = os.getenv("SUMATRA_PDF_PATH", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    for candidate in (
        r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _resolve_adobe_reader_path():
    explicit = os.getenv("ADOBE_READER_PATH", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    for candidate in (
        r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
        r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
        r"C:\Program Files\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _warehouse_label_print_rotation(pdf_landscape, printable_w, printable_h):
    """Align 6×4 PDF with portrait 4×6 driver page (override via WAREHOUSE_LABEL_PRINT_ROTATION)."""
    explicit = os.getenv("WAREHOUSE_LABEL_PRINT_ROTATION", "").strip()
    if explicit.isdigit():
        return int(explicit) % 360
    page_landscape = printable_w >= printable_h
    if pdf_landscape and not page_landscape:
        return 90
    if not pdf_landscape and page_landscape:
        return 270
    return 0


def _print_pdf_via_sumatra(path, printer_name, logger):
    sumatra = _resolve_sumatra_pdf_path()
    if not sumatra:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [
            sumatra,
            "-print-to",
            printer_name,
            "-silent",
            "-print-settings",
            "landscape,shrink",
            path,
        ],
        check=True,
        timeout=120,
        creationflags=flags,
    )
    logger.info("Queued label PDF via SumatraPDF to %s", printer_name)
    return True


def _print_pdf_via_adobe(path, printer_name, logger):
    adobe = _resolve_adobe_reader_path()
    if not adobe:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [adobe, "/t", path, printer_name],
        check=True,
        timeout=120,
        creationflags=flags,
    )
    logger.info("Queued label PDF via Adobe Reader to %s", printer_name)
    return True


def _print_pdf_via_shell_execute(path, printer_name, logger):
    import win32api

    try:
        import pywintypes
    except ImportError:
        pywintypes = None

    attempts = (
        ("printto", f'"{printer_name}"'),
        ("print", f'/d:"{printer_name}"'),
    )
    errors = []
    for verb, params in attempts:
        try:
            rc = win32api.ShellExecute(0, verb, path, params, ".", 0)
            if rc > 32:
                logger.info("Queued label PDF via ShellExecute(%s) to %s", verb, printer_name)
                return True
            errors.append(f"{verb}: Windows code {rc}")
        except Exception as exc:
            if pywintypes and isinstance(exc, pywintypes.error):
                errors.append(f"{verb}: {exc}")
            else:
                raise
    logger.warning("ShellExecute print attempts failed: %s", "; ".join(errors))
    return False


def _print_pdf_via_win32_gdi(path, printer_name, logger):
    """Rasterize PDF pages and print via Win32 GDI (no Sumatra/Adobe required)."""
    try:
        import pypdfium2 as pdfium
        import win32ui
        from PIL import ImageWin
    except ImportError:
        return False

    dpi = max(72, min(600, int(os.getenv("WAREHOUSE_LABEL_PRINT_DPI", "200"))))
    scale = dpi / 72.0
    doc = pdfium.PdfDocument(path)
    page_count = len(doc)
    try:
        if page_count < 1:
            return False

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        hdc.SetMapMode(1)  # MM_TEXT — pixel coordinates match device resolution
        printable_w = hdc.GetDeviceCaps(8)   # HORZRES
        printable_h = hdc.GetDeviceCaps(10)  # VERTRES
        first = doc[0]
        pdf_landscape = float(first.get_width()) >= float(first.get_height())
        rotation = _warehouse_label_print_rotation(
            pdf_landscape, printable_w, printable_h
        )

        hdc.StartDoc("FDC warehouse labels")
        try:
            for index in range(len(doc)):
                page = doc[index]
                bitmap = page.render(scale=scale, rotation=rotation)
                try:
                    pil_image = bitmap.to_pil()
                finally:
                    bitmap.close()

                img_w, img_h = pil_image.size
                fit = min(printable_w / img_w, printable_h / img_h)
                dest_w = max(1, int(img_w * fit))
                dest_h = max(1, int(img_h * fit))
                x0 = (printable_w - dest_w) // 2
                y0 = (printable_h - dest_h) // 2

                hdc.StartPage()
                dib = ImageWin.Dib(pil_image)
                dib.draw(hdc.GetHandleOutput(), (x0, y0, x0 + dest_w, y0 + dest_h))
                hdc.EndPage()
        finally:
            hdc.EndDoc()
            hdc.DeleteDC()
    finally:
        doc.close()

    logger.info(
        "Printed label PDF via Win32 GDI to %s (%d page(s) at %d DPI, rotation=%s)",
        printer_name,
        page_count,
        dpi,
        rotation,
    )
    return True


def _print_pdf_to_windows_printer(pdf_bytes, printer_name, logger):
    """Send a 6×4 in label PDF to a Windows printer queue (warehouse Zebra, etc.)."""
    if sys.platform != "win32":
        raise RuntimeError("Direct printer output is only supported on Windows.")
    try:
        import win32print
    except ImportError as exc:
        raise RuntimeError("pywin32 is required for direct warehouse printing.") from exc

    printer_name = (printer_name or "").strip()
    if not printer_name:
        raise RuntimeError("Warehouse printer name is not configured.")

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    installed = {entry[2] for entry in win32print.EnumPrinters(flags)}
    if printer_name not in installed:
        sample = ", ".join(sorted(installed)[:6])
        raise RuntimeError(
            f'Printer "{printer_name}" was not found. Installed printers include: {sample}'
        )

    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="fdc_label_")
    os.close(fd)
    abs_path = os.path.abspath(path)
    try:
        with open(path, "wb") as handle:
            handle.write(pdf_bytes)

        failures = []
        for label, attempt in (
            ("Win32 GDI", lambda: _print_pdf_via_win32_gdi(abs_path, printer_name, logger)),
            ("SumatraPDF", lambda: _print_pdf_via_sumatra(abs_path, printer_name, logger)),
            ("ShellExecute", lambda: _print_pdf_via_shell_execute(abs_path, printer_name, logger)),
            ("Adobe Reader", lambda: _print_pdf_via_adobe(abs_path, printer_name, logger)),
        ):
            try:
                if attempt():
                    break
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                logger.warning("%s warehouse print attempt failed: %s", label, exc)
        else:
            hint = (
                "Confirm the printer is online on this PC (Flask host, not your browser). "
                "Install pypdfium2 and pywin32, or SumatraPDF with SUMATRA_PDF_PATH, "
                "or set a default app for .pdf files."
            )
            detail = "; ".join(failures) if failures else "no print method succeeded"
            raise RuntimeError(
                f'Could not print 4×6 labels to "{printer_name}". {hint} ({detail})'
            )

        def _cleanup():
            try:
                os.remove(path)
            except OSError:
                pass

        threading.Timer(120.0, _cleanup).start()
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def get_order_ship_to_lines(order_number):
    """Return ship-to lines for ez stickers (same fields as order lookup)."""
    return fetch_order_ship_to_lines(order_number)


def _ez_logo_path():
    try:
        root = Path(current_app.root_path)
    except RuntimeError:
        root = Path(__file__).resolve().parents[2]
    branded = root / "static" / "images" / "ez_sticker_fdc_logo.png"
    if branded.is_file():
        return branded
    return root / "static" / "icons" / "favicon-fdc.png"


def _ez_ship_to_display_lines(ship_to_lines):
    """Split TO: prefix from first line; return address lines for centered block."""
    raw = [str(line).strip() for line in (ship_to_lines or []) if str(line).strip()]
    if not raw:
        return []
    first = raw[0]
    if first.upper().startswith("TO:"):
        name = first[3:].strip()
        return ([name] if name else []) + raw[1:]
    return raw


def _ez_font_metrics(font_name, font_size):
    try:
        ascent = pdfmetrics.getAscent(font_name) / 1000.0 * font_size
        descent = abs(pdfmetrics.getDescent(font_name) / 1000.0 * font_size)
    except Exception:
        ascent = font_size * 0.72
        descent = font_size * 0.22
    return ascent, descent


def _ez_baseline_below(y_bottom, font_name, font_size, gap_pt=10):
    """Next baseline when stacking downward (PDF y decreases)."""
    ascent, _ = _ez_font_metrics(font_name, font_size)
    return y_bottom - gap_pt - ascent


def _ez_bottom_below_baseline(y_baseline, font_name, font_size):
    _, descent = _ez_font_metrics(font_name, font_size)
    return y_baseline - descent


def _ez_draw_centered(c, y, text, font_name, font_size, color=None):
    if color:
        c.setFillColor(color)
    else:
        c.setFillColor(rl_colors.black)
    c.setFont(font_name, font_size)
    c.drawCentredString(EZ_PAGE_W / 2, y, text)
    if color:
        c.setFillColor(rl_colors.black)


def _ez_parse_service_input(service):
    """Up to EZ_SERVICE_MAX_LINES non-empty lines from string or list."""
    if isinstance(service, (list, tuple)):
        raw_parts = [str(x) for x in service]
    else:
        text = str(service or "").replace("\r\n", "\n").replace("\r", "\n")
        raw_parts = text.split("\n")
    lines = [part.strip() for part in raw_parts if part.strip()]
    return lines[:EZ_SERVICE_MAX_LINES]


def _ez_wrap_words(text, font_name, font_size, max_width):
    words = str(text).split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if stringWidth(trial, font_name, font_size) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _ez_service_display_lines(service, font_name, font_size, max_width, max_lines=EZ_SERVICE_MAX_LINES):
    """User lines plus word-wrap; capped at max_lines."""
    user_lines = _ez_parse_service_input(service)
    if not user_lines:
        return []
    wrapped = []
    for line in user_lines:
        if stringWidth(line, font_name, font_size) <= max_width:
            wrapped.append(line)
        else:
            wrapped.extend(_ez_wrap_words(line, font_name, font_size, max_width))
        if len(wrapped) >= max_lines:
            break
    return wrapped[:max_lines]


def _ez_service_block_height(lines, font_name, font_size, line_gap):
    if not lines:
        return 0.0
    ascent, descent = _ez_font_metrics(font_name, font_size)
    per_line = ascent + descent
    return len(lines) * per_line + max(0, len(lines) - 1) * line_gap


def _ez_fit_service_layout(service, max_width, max_height):
    """Pick Times-Bold size and wrapped lines; prefer fewer lines, then larger type."""
    font_name = "Times-Bold"
    best = None
    for size in range(46, 9, -2):
        lines = _ez_service_display_lines(service, font_name, size, max_width)
        if not lines:
            continue
        line_gap = max(3, int(size * 0.10))
        block_h = _ez_service_block_height(lines, font_name, size, line_gap)
        if block_h > max_height:
            continue
        score = (len(lines), -size)
        if best is None or score < best[0]:
            best = (score, size, line_gap, lines)
    if best:
        _, size, line_gap, lines = best
        return font_name, size, line_gap, lines
    lines = _ez_service_display_lines(service, font_name, 10, max_width)
    return font_name, 10, 3, lines[:EZ_SERVICE_MAX_LINES]


def _ez_footer_zone_top():
    """Highest y reserved for grey box number at bottom."""
    ascent, _ = _ez_font_metrics("Times-Roman", 28)
    return EZ_BOX_NUM_BASELINE + ascent + EZ_MIDDLE_GAP_ABOVE_BOX


def _ez_measure_to_block(ship_to_lines, total_boxes):
    """Return height and draw specs for TO / ship-to / TOTAL (no service)."""
    to_lines = _ez_ship_to_display_lines(ship_to_lines)
    to_line_size = 12 if len(to_lines) > 3 else 14
    specs = []
    h = 0.0

    to_label_font, to_label_size = "Times-Bold", 16
    gap = 14
    h += gap + _ez_font_metrics(to_label_font, to_label_size)[0] + _ez_font_metrics(to_label_font, to_label_size)[1]
    specs.append(("to_label", "TO:", to_label_font, to_label_size, gap))

    for line in to_lines:
        font = _label_font_name(line, bold=True)
        if not font.startswith("LabelCJK"):
            font = "Times-Bold"
        gap = 6
        h += gap + _ez_font_metrics(font, to_line_size)[0] + _ez_font_metrics(font, to_line_size)[1]
        specs.append(("ship", line, font, to_line_size, gap))

    box_word = "BOX" if int(total_boxes) == 1 else "BOXES"
    total_font, total_size = "Times-Bold", 14
    gap = 14
    h += gap + _ez_font_metrics(total_font, total_size)[0] + _ez_font_metrics(total_font, total_size)[1]
    specs.append(
        ("total", f"TOTAL {total_boxes} {box_word}", total_font, total_size, gap)
    )
    return h, specs


def _ez_plan_middle_block(service, ship_to_lines, total_boxes, zone_height):
    """Fit service and measure full centered block (service + TO + ship + TOTAL)."""
    max_width = EZ_PAGE_W - 2 * EZ_SERVICE_SIDE_MARGIN
    to_h, to_specs = _ez_measure_to_block(ship_to_lines, total_boxes)
    service_max_h = max(0.35 * inch, zone_height - to_h)
    svc_font, svc_size, svc_gap, svc_lines = _ez_fit_service_layout(
        service, max_width, service_max_h
    )
    svc_h = _ez_service_block_height(svc_lines, svc_font, svc_size, svc_gap) if svc_lines else 0.0
    if svc_lines:
        svc_gap_before = 12
        svc_h += svc_gap_before
    else:
        svc_gap_before = 0
    return {
        "total_height": svc_h + to_h,
        "service": (svc_font, svc_size, svc_gap, svc_lines, svc_gap_before),
        "to_specs": to_specs,
    }


def _ez_draw_middle_block(c, block_top_y, layout):
    """Draw service + TO block top-down; block_top_y is top edge of centered zone."""
    y_bottom = block_top_y
    svc_font, svc_size, svc_gap, svc_lines, svc_gap_before = layout["service"]
    for idx, line in enumerate(svc_lines):
        gap = svc_gap_before if idx == 0 else svc_gap
        y = _ez_baseline_below(y_bottom, svc_font, svc_size, gap_pt=gap)
        _ez_draw_centered(c, y, line.upper(), svc_font, svc_size)
        y_bottom = _ez_bottom_below_baseline(y, svc_font, svc_size)

    for _kind, text, font, size, gap in layout["to_specs"]:
        y = _ez_baseline_below(y_bottom, font, size, gap_pt=gap)
        _ez_draw_centered(c, y, text, font, size)
        y_bottom = _ez_bottom_below_baseline(y, font, size)


def _ez_draw_label_page(c, logo_path, service, ship_to_lines, box_num, total_boxes):
    """Match Savvy/UCL 4x6 reference: logo banner, from line, service, TO block, totals."""
    cx = EZ_PAGE_W / 2
    top_y = EZ_PAGE_H - 0.10 * inch
    y_bottom = top_y

    if logo_path and os.path.isfile(logo_path):
        logo_w = EZ_PAGE_W - 0.36 * inch
        logo_h = logo_w * (95 / 272)
        c.drawImage(
            str(logo_path),
            (EZ_PAGE_W - logo_w) / 2,
            top_y - logo_h,
            width=logo_w,
            height=logo_h,
            preserveAspectRatio=True,
            mask="auto",
        )
        y_bottom = top_y - logo_h
    else:
        c.setFont("Times-Bold", 28)
        c.drawCentredString(cx, top_y - 20, "F.D.C")
        y_bottom = top_y - 0.55 * inch

    from_font, from_size = "Times-Bold", 12
    y = _ez_baseline_below(y_bottom, from_font, from_size, gap_pt=14)
    _ez_draw_centered(c, y, EZ_FROM_LINE, from_font, from_size)
    from_bottom = _ez_bottom_below_baseline(y, from_font, from_size)

    zone_top = from_bottom - EZ_MIDDLE_GAP_AFTER_FROM
    zone_bottom = _ez_footer_zone_top()
    zone_height = max(0.5 * inch, zone_top - zone_bottom)

    layout = _ez_plan_middle_block(service, ship_to_lines, total_boxes, zone_height)
    block_h = layout["total_height"]
    block_bottom = zone_bottom + (zone_height - block_h) / 2.0
    block_top = block_bottom + block_h
    _ez_draw_middle_block(c, block_top, layout)

    _ez_draw_centered(c, EZ_BOX_NUM_BASELINE, f"Box {box_num}", "Times-Roman", 28, color=EZ_GREY)
    _ez_draw_centered(
        c, EZ_BOX_OF_BASELINE, f"Box {box_num} of {total_boxes}", "Times-Roman", 15, color=EZ_GREY
    )


def build_ez_sticker_pdf(ship_to_lines, service, total_boxes, order_number=None):
    """One portrait 4×6 page per box."""
    total = int(total_boxes)
    if total < 1:
        raise ValueError("Total boxes must be at least 1.")
    service = str(service or "").strip()
    if not service:
        raise ValueError("Shipping service is required.")

    logo_path = str(_ez_logo_path())
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(EZ_PAGE_W, EZ_PAGE_H))
    for box_num in range(1, total + 1):
        if box_num > 1:
            pdf.showPage()
        _ez_draw_label_page(pdf, logo_path, service, ship_to_lines, box_num, total)
    pdf.save()
    buf.seek(0)
    return buf.getvalue()


def register_label_maker_routes(app):
    """Register label maker routes with the Flask app."""

    @app.route('/download_label_excel')
    @login_required
    def download_label_excel():
        current_user = session['username']
        order_number = session.get('label_order_number')

        if not order_number:
            flash('No label order available for download. Please generate labels first.', 'error')
            return redirect(url_for('order_lookup'))

        try:
            # Regenerate the Excel file
            app.logger.info("***%s***__@@@downloading labels@@@__ %s",current_user, order_number)
            excel_data, error_message, _, _, _, _, _ = process_orders(order_number)

            if error_message or excel_data is None:
                flash(f'Error regenerating Excel file: {error_message}', 'error')
                return redirect(url_for('order_lookup'))

            filename = f"{order_number}_label.xlsx"
            _record_jennifer_label_download(order_number, current_user, app.logger)

            return send_file(
                io.BytesIO(excel_data),
                download_name=filename,
                as_attachment=False,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
            return redirect(url_for('order_lookup'))

    @app.route('/download_label_pdf')
    @login_required
    def download_label_pdf():
        current_user = session['username']
        order_number = session.get('label_order_number')

        if not order_number:
            flash('No label order available for download. Please generate labels first.', 'error')
            return redirect(url_for('order_lookup'))

        try:
            app.logger.info("***%s***__@@@downloading label PDF@@@__ %s", current_user, order_number)
            excel_data, error_message, _, _, _, _, _ = process_orders(order_number)

            if error_message or excel_data is None:
                flash(f'Error regenerating labels for PDF: {error_message}', 'error')
                return redirect(url_for('order_lookup'))

            pdf_data = excel_labels_to_pdf(excel_data)
            filename = f"{order_number}_label.pdf"
            _record_jennifer_label_download(order_number, current_user, app.logger)

            return send_file(
                io.BytesIO(pdf_data),
                download_name=filename,
                as_attachment=False,
                mimetype='application/pdf',
            )
        except Exception as e:
            app.logger.exception("Label PDF download failed")
            flash(f'Error generating PDF: {str(e)}', 'error')
            return redirect(url_for('order_lookup'))

    @app.route('/download_label_pdf_4perpage')
    @login_required
    def download_label_pdf_4perpage():
        current_user = session['username']
        order_number = session.get('label_order_number')

        if not order_number:
            flash('No label order available for download. Please generate labels first.', 'error')
            return redirect(url_for('order_lookup'))

        try:
            app.logger.info(
                "***%s***__@@@downloading 4-per-page label PDF@@@__ %s",
                current_user,
                order_number,
            )
            excel_data, error_message, _, _, _, _, _ = process_orders(order_number)

            if error_message or excel_data is None:
                flash(f'Error regenerating labels for PDF: {error_message}', 'error')
                return redirect(url_for('order_lookup'))

            pdf_data = excel_labels_to_pdf_4perpage(excel_data)
            filename = f"{order_number}_label_4perpage.pdf"
            _record_jennifer_label_download(order_number, current_user, app.logger)

            return send_file(
                io.BytesIO(pdf_data),
                download_name=filename,
                as_attachment=False,
                mimetype='application/pdf',
            )
        except Exception as e:
            app.logger.exception("4-per-page label PDF download failed")
            flash(f'Error generating 4-per-page PDF: {str(e)}', 'error')
            return redirect(url_for('order_lookup'))

    @app.route('/print_label_pdf_warehouse', methods=['POST'])
    @login_required
    def print_label_pdf_warehouse():
        current_user = session['username']
        order_number = session.get('label_order_number')

        if not order_number:
            return jsonify({
                "ok": False,
                "error": "No label order in session. Generate labels first.",
            }), 400

        try:
            app.logger.info(
                "***%s***__@@@printing label PDF to warehouse@@@__ %s printer=%s",
                current_user,
                order_number,
                WAREHOUSE_LABEL_PRINTER,
            )
            excel_data, error_message, _, _, _, _, _ = process_orders(order_number)
            if error_message or excel_data is None:
                return jsonify({"ok": False, "error": error_message or "Failed to build labels."}), 400

            pdf_data = excel_labels_to_pdf(excel_data)
            _print_pdf_to_windows_printer(pdf_data, WAREHOUSE_LABEL_PRINTER, app.logger)
            _record_jennifer_label_download(order_number, current_user, app.logger)

            return jsonify({
                "ok": True,
                "printer": WAREHOUSE_LABEL_PRINTER,
                "order_number": order_number,
            })
        except Exception as e:
            app.logger.exception("Warehouse label print failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route('/ez_label_from_order_lookup', methods=['POST'])
    @login_required
    def ez_label_from_order_lookup():
        current_user = session['username']
        payload = request.get_json(silent=True) or {}
        order_number = str(
            payload.get("order_number")
            or session.get("order_number")
            or session.get("label_order_number")
            or ""
        ).strip()
        if not order_number:
            return jsonify({"ok": False, "error": "No order in session. Look up an order first."}), 400

        service = str(payload.get("service") or "").strip()
        try:
            total_boxes = int(payload.get("total_boxes"))
        except (TypeError, ValueError):
            total_boxes = 0

        if not service:
            return jsonify({"ok": False, "error": "Shipping service is required (e.g. UCL)."}), 400
        if total_boxes < 1:
            return jsonify({"ok": False, "error": "Total boxes must be a positive number."}), 400

        try:
            from py.other.eta_fashion_label_pdf import (
                ETA_FASHION_CUSTOMER_ID,
                fetch_order_customer_id,
            )

            customer_id, _cust_err = fetch_order_customer_id(order_number)
            is_eta_fashion = customer_id == ETA_FASHION_CUSTOMER_ID

            if is_eta_fashion:
                ship_to_lines = [EZ_ETA_SHIP_TO_NAME]
            else:
                ship_to_lines, addr_error = get_order_ship_to_lines(order_number)
                if addr_error:
                    return jsonify({"ok": False, "error": addr_error}), 400

            if is_eta_fashion and not service:
                service = EZ_ETA_DEFAULT_SERVICE

            app.logger.info(
                "***%s***__@@@ez label@@@__ %s service=%s boxes=%s",
                current_user,
                order_number,
                service,
                total_boxes,
            )
            pdf_data = build_ez_sticker_pdf(ship_to_lines, service, total_boxes, order_number)
            try:
                from py.other.label_status import mark_ez_label
                mark_ez_label(
                    order_number,
                    current_user,
                    service=service,
                    total_boxes=total_boxes,
                )
            except Exception:
                app.logger.exception("Failed to record ez label status")

            filename = f"{order_number}_4x6_{total_boxes}boxes.pdf"
            return send_file(
                io.BytesIO(pdf_data),
                download_name=filename,
                as_attachment=False,
                mimetype="application/pdf",
            )
        except Exception as e:
            app.logger.exception("Ez label PDF failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route('/label_maker_from_order_lookup', methods=['GET', 'POST'])
    @login_required
    def label_maker_from_order_lookup():
        current_user = session['username']
        try:
            order_number = (
                (request.form.get('order_number') or session.get('order_number') or '')
                .strip()
            )
            if order_number:
                session['order_number'] = order_number
            if not order_number:
                flash('Please lookup an order first before generating labels.', 'error')
                return redirect(url_for('order_lookup'))

            # Validate order number format
            if not (len(order_number) == 4 or len(order_number) == 5) or not order_number.isdigit():
                flash('Invalid order number format. Please enter a 4 or 5-digit number.', 'error')
                return redirect(url_for('order_lookup'))

            app.logger.info("***%s***__@@@made labels@@@__ %s",current_user, order_number)
            excel_data, error_message, number_of_labels, duplicate_numbers, info_list, addresslist, warnings = process_orders(order_number)
            recent_fdc_orders = get_recent_fdc_orders(100)
            ez_default_boxes = parse_boxes_from_notes(fetch_order_notes(order_number))

            if error_message:
                return render_template(
                    'order_lookup.html',
                    excel_html=None,
                    filename=None,
                    number_of_labels=None,
                    duplicate_numbers=None,
                    info_list=None,
                    error_message=error_message,
                    order_number=order_number,
                    current_user=current_user,
                    recent_fdc_orders=recent_fdc_orders,
                    ez_default_boxes=ez_default_boxes,
                )

            # Store only the order_number in session for download (not the large excel data)
            session['label_order_number'] = order_number

            # Show the results page with label information and download option
            return render_template(
                'order_lookup.html',
                excel_html=None,
                filename="Labels Generated Successfully",
                number_of_labels=number_of_labels,
                duplicate_numbers=duplicate_numbers,
                info_list=info_list,
                addresslist=addresslist,
                order_number=order_number,
                warnings=warnings,
                current_user=current_user,
                labels_generated=True,  # Flag to show the results section
                recent_fdc_orders=recent_fdc_orders,
                ez_default_boxes=ez_default_boxes,
            )
        except Exception as e:
            app.logger.error("An error occurred: %s", str(e))
            recent_fdc_orders = get_recent_fdc_orders(100)
            ez_default_boxes = parse_boxes_from_notes(fetch_order_notes(order_number))
            return render_template(
                'order_lookup.html',
                excel_html=None,
                filename=None,
                number_of_labels=None,
                duplicate_numbers=None,
                info_list=None,
                error_message=f"An error occurred: {str(e)}",
                order_number=order_number,
                current_user=current_user,
                recent_fdc_orders=recent_fdc_orders,
                ez_default_boxes=ez_default_boxes,
            )


if __name__ == "__main__":
    order_number = "33339"  # Example order number
    print("\n=== Starting Label Processing ===")
    file_path, error, number_of_labels, duplicate_numbers, label_reference, addresslist = process_orders(order_number)
    if error:
        print(f"\nError: {error}")
    else:
        print(f"\nFile created at: {file_path}")
        print(f"Total number of labels: {number_of_labels}")
        print(f"Duplicate label numbers: {duplicate_numbers}")
        print(f"Label Reference Chart:")
        reference_df = pd.DataFrame(label_reference)
        print(reference_df)
        print(f"Reference chart saved within the same Excel workbook.")
    print("=== Label Processing Completed ===\n")
