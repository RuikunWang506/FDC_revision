# macy_special.py
import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from collections import defaultdict
from py.utilities.setting import Config
from flask import Blueprint, request, render_template, session, send_file, current_app
from py.utilities.setting import login_required
import io

macy_bp = Blueprint('macy', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

ftpl = Font(name="Arial", size="12")
ft22 = Font(name="Arial", size="14")
ft33 = Font(name="Arial", size="16", bold=True)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')
ft = Font(name="Arial", size=12)
ft_bold = Font(name="Arial", size=12, bold=True)
ft_summary = Font(name="Arial", size=14, bold=True)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')
# Fonts and borders for part 2
ftlb = Font(name="Arial", size="12")
ft3 = Font(name="Arial", size="8")
ft6 = Font(name="Arial", size="7")
ftb = Font(name="Arial", size="12", bold=True)
ftbigger = Font(name="Arial", size="16", bold=True)


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
                JOIN
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

def process_orders(po_num, order_number, apt, carrier, project):
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
    ws_labels.title = "packing_list"
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

    ws_labels_4x6 = wb.create_sheet(title="label_4x6")
    ws_labels_4x6.page_setup.orientation = "landscape"
    ws_labels_4x6.page_setup.paperSize = "1"
    ws_labels_4x6.page_setup.orientation = "portrait"
    ws_labels_4x6.page_setup.paperSize = "1"
    ws_labels_4x6.column_dimensions['A'].width = 5.29
    ws_labels_4x6.column_dimensions['B'].width = 8.14
    ws_labels_4x6.column_dimensions['C'].width = 6.57
    ws_labels_4x6.column_dimensions['D'].width = 2.43
    ws_labels_4x6.column_dimensions['E'].width = 5.29
    ws_labels_4x6.column_dimensions['F'].width = 5.29
    ws_labels_4x6.page_margins.left = 0.2
    ws_labels_4x6.page_margins.right = 0
    ws_labels_4x6.page_margins.top = 0 
    ws_labels_4x6.page_margins.bottom = 0 
    ws_labels_4x6.page_margins.header = 0 
    ws_labels_4x6.page_margins.footer = 0
    # Add empty rows at the top

    output_line_number = 1  # Start from row 1

    # Set up addresses
    print("process_orders: Setting up addresses.")
    from_address = (
        ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 4 else
        ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 5 else
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
    text_after_boxes_match = re.search(r'BOXES\s+(.*)', notes)
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
    output_line_number +=1  # Empty row

    # Add this near the start of the function after getting df
    output_line_number_labels = 1  # Initialize the variable
    
        
    # Process second type of labels
    output_line_number_labels = process_label_2(
        output_line_number_labels, 
        row, 
        to_address, 
        ws_labels_4x6, 
        total_unique_labels, 
        po_number, 
        carrier, 
        project,
        order_number
    )
            # Create pallet labels sheet
    create_pallet_labels_sheet(wb, po_num, order_number, apt)



    # Now, handle summary row at the end
    print("\nprocess_orders: Adding summary row.")
    summary_start_row = output_line_number + 5
    summary_end_row = summary_start_row + 5  # Adjust as needed

    print(f"process_orders: Summary will start at row {summary_start_row}")
    print(f"process_orders: Summary will end at row {summary_end_row}")

    print("process_orders: Adding summary information.")
    ws_labels["B" + str(summary_start_row)] = "FDC"
    ws_labels["C" + str(summary_start_row)].font = ft_summary
    ws_labels["B" + str(summary_start_row + 1)] = "内部单号:"
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
    ws[f"A{output_line_number}"] = "Packing Slip"
    ws[f"A{output_line_number}"].font = ft22
    ws.merge_cells(f"A{output_line_number}:C{output_line_number+1}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="center", vertical="center")
    output_line_number += 3  # Blank lines

    # Add from_address
    for address in from_address:
        ws[f"A{output_line_number}"] = address
        ws[f"A{output_line_number}"].font = ft
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add to_address
    for address in to_address:
        ws[f"A{output_line_number}"] = address
        ws[f"A{output_line_number}"].font = ft
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add P.O.#
    ws[f"A{output_line_number}"] = f"P.O. # {po_number.strip()}"
    ws[f"A{output_line_number}"].font = ft_bold
    ws.merge_cells(f"A{output_line_number}:C{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    output_line_number += 2  # Blank line

    # Add STYLE#, QUANTITY, and BOX#
    ws[f"A{output_line_number}"] = "STYLE#"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = sku
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    output_line_number += 1

    # Add COLOR
    ws[f"A{output_line_number}"] = "COLOR:"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = color
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    output_line_number += 1

    # Add QUANTITY
    ws[f"A{output_line_number}"] = "QUANTITY"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = quantity
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "PCS"
    ws[f"C{output_line_number}"].font = ft
    output_line_number += 2  # Blank line

    # Add CARTON information
    ws[f"A{output_line_number}"] = "CARTON"
    ws[f"A{output_line_number}"].font = ft_bold
    ws[f"B{output_line_number}"] = label_number
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].alignment = Alignment(horizontal="center", vertical="center")
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "OF"
    ws[f"C{output_line_number}"].font = ft_bold
    ws[f"D{output_line_number}"] = total_labels
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    output_line_number += 2  # Blank line

    print(f"create_label: Label for label number {label_number} created at line {output_line_number}.")
    return output_line_number


def process_label_2(output_line_number, row, to_address, ws_labels, number_of_labels, po_number, carrier, project,order_number):
    sku_line = row['PROD_CD']
    sku = sku_line.split()[0]
    color = sku_line.split()[1]
    quantity = row['PC_UNIT']
    from_address = (
        ["SHIP FROM:", "E-Fashion Inc","E-Fashion INC", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 4 else
        ["SHIP FROM:", "FASHION DISTRIBUTION", "CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 5 else
        "N/A"
    )
    
    for sl_num in range(number_of_labels):
        temp_output_line_number = output_line_number
        temp_output_line_number += 5	

        max_lines = min(len(from_address), len(to_address))
        for line_num in range(max_lines):
            ws_labels["A" + str(temp_output_line_number)] = from_address[line_num]
            ws_labels["A" + str(temp_output_line_number)].font = ft6	
            ws_labels["E" + str(temp_output_line_number)] = to_address[line_num]
            ws_labels["E" + str(temp_output_line_number)].font = ft6
            ws_labels.merge_cells(("A" + str(temp_output_line_number)) + ":" +("C" + str(temp_output_line_number)))
            ws_labels.merge_cells(("E" + str(temp_output_line_number)) + ":" +("G" + str(temp_output_line_number)))
            temp_output_line_number += 1

        temp_output_line_number += 1
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "Carrier: " + carrier
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "PRO#:" + str(project)
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "BOL#: " + str(order_number)
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "PO#: " + po_number
        ws_labels["B" + str(temp_output_line_number)].font = ftb
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "DEPT: 829 - MBKS HANDBAGS"
        ws_labels["B" + str(temp_output_line_number)].font = ftb
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1
        ws_labels["B" + str(temp_output_line_number)] = "For ST #077 BULK"
        ws_labels["B" + str(temp_output_line_number)].font = ftb
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1

        ws_labels["B" + str(temp_output_line_number)] = "CARTON #"
        ws_labels["B" + str(temp_output_line_number)].font=ft3
        ws_labels["C" + str(temp_output_line_number)] = sl_num + 1
        ws_labels["C" + str(temp_output_line_number)].font = ftbigger
        ws_labels["C" + str(temp_output_line_number)].alignment = Alignment(horizontal="center", vertical="center")
        ws_labels["D" + str(temp_output_line_number)] = "OF"
        ws_labels["D" + str(temp_output_line_number)].font=ft3
        ws_labels.merge_cells(("E" + str(temp_output_line_number)) + ":" +("F" + str(temp_output_line_number)))
        ws_labels["E" + str(temp_output_line_number)] = number_of_labels
        ws_labels["E" + str(temp_output_line_number)].font = ftbigger
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1
        temp_output_line_number += 1

        output_line_number = temp_output_line_number

    return output_line_number

##Pallet labels sheet
def create_pallet_labels_sheet(wb, po_num, order_number, apt):
    ws = wb.create_sheet(title='pallet labels')
    ws.page_margins.left = 0 
    ws.page_margins.right = 0 
    ws.page_margins.top = 0.5 
    ws.page_margins.bottom = 0.5 
    # Define styles
    highlight_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    black_fill = PatternFill(start_color="000001", end_color="000001", fill_type="solid")
    thin_border = Border(
        left=Side(border_style="medium", color="000000"),
        right=Side(border_style="medium", color="000000"),
        top=Side(border_style="medium", color="000000"),
        bottom=Side(border_style="medium", color="000000")
    )
    center_alignment = Alignment(horizontal="center", vertical="center")

    # Determine vendor name based on order number length
    vendor_name = "E-Fashion Inc" if len(order_number) == 4 else "FASHION DISTRIBUTION CENTER" if len(order_number) == 5 else "N/A"

    # Sample data to fill in the sheet
    data = [
        ["ROUTING DESTINATION"],
        ["SANTA FE SPRINGS 1:1 MERGE CENTER"],
        ["c/o DYNAMIC"],
        ["14141 ALONDRA BOULEVARD"],
        ["SANTA FE SPRINGS CA  90670"],
        [""],
        ["VENDOR NAME"],
        [vendor_name],
        [""],
        ["BILL OF LADING NUMBER FOR THIS PALLET"],
        [""],  # Update with BOL from form
        [""],
        ["PURCHASE ORDER NUMBER(S) ON THIS PALLET"],
        [""],  # Update with PO# from form
        [""],
        [""],
        ["FINAL BACKSTAGE DESTINATION DC"],
        ["BACKSTAGE c/o MACY'S - c/o COLUMBUS DC"],
        ["7145 RICKENBACKER PARKWAY WEST"],
        ["COLUMBUS OH  43217"],
        [""],
        ["MACY'S APPOINTMENT NUMBER"],  # Update with APT from form
        ["CARTONS ON THIS PALLET", "15"],
        ["TOTAL CARTONS SHIPPED ON ABOVE APPT NUMBER", "78"],
        ["TOTAL PALLETS SHIPPED ON ABOVE APPT NUMBER", "5"]
    ]

    # Apply data first
    for r, row in enumerate(data, start=1):
        for c, value in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=value)

    # Merge cells as required
    merge_ranges = [
        ('A1:J1'), ('A2:J2'), ('A3:J3'), ('A4:J4'), ('A5:J5'),('A6:J6'), ('A7:J7'), ('A8:J8'),('A9:J9'),
        ('A10:J10'), ('A11:J11'), ('A12:J12'),('A13:J13'), ('A16:J16'),
        ('A17:J17'), ('A18:J18'), ('A19:J19'), ('A20:J20'),('A21:J21'),
        ('A22:H22'), ('I22:J22'), ('A23:H23'), ('I23:J23'), 
        ('A24:H24'), ('I24:J24'), ('A25:H25'), ('I25:J25')
    ]

    # Merging cells in rows 14 and 15 for every two columns
    for col in range(1, 11, 2):
        merge_ranges.append(f'{openpyxl.utils.get_column_letter(col)}14:{openpyxl.utils.get_column_letter(col+1)}14')
        merge_ranges.append(f'{openpyxl.utils.get_column_letter(col)}15:{openpyxl.utils.get_column_letter(col+1)}15')

    for range_str in merge_ranges:
        ws.merge_cells(range_str)

    ws['I22'] = apt
    ws['I23'] = "00"
    ws['I24'] = "00"
    ws['I25'] = "00"
    ws['A11']= order_number
    ws['A14']= po_num

    # Apply formatting, ensuring only top-left cells are assigned values
    for r, row in enumerate(data, start=1):
        for c in range(1, 11):
            cell = ws.cell(row=r, column=c)
            cell.border = thin_border
            cell.alignment = center_alignment

            if r == 1:
                cell.fill = black_fill
                cell.font = Font(color="FFFFFF", bold=True, size=16)
            elif r in [2, 3, 4, 5]:
                cell.font = Font(color="0000FF", bold=True, size=14)
            elif r == 7:
                cell.font = Font(bold=True, size=13)
            elif r == 8:
                cell.font = Font(color="FF0000", bold=True, size=16)
                cell.fill = highlight_fill
            elif r == 10:
                cell.font = Font(bold=True, size=13)
            elif r == 11:
                cell.font = Font(color="FF0000", bold=True, size=13)
                cell.fill = highlight_fill
            elif r == 13:
                cell.font = Font(bold=True, size=13)
            elif r in [14, 15]:
                cell.font = Font(color="FF0000", bold=True, size=12)
                cell.fill = highlight_fill
            elif r == 17:
                cell.fill = black_fill
                cell.font = Font(color="FFFFFF", bold=True, size=14)
            elif r in [18, 19, 20, 21]:
                cell.font = Font(bold=True, size=12)
                if r in [18, 19, 20]:
                    cell.fill = highlight_fill
                    cell.font = Font(color="0000FF", bold=True, size=12)
            elif r in [22, 23, 24, 25]:
                cell.font = Font(bold=True,size=9)
                if r in [22,23,24,25]:
                    if c==9:
                        cell.fill=highlight_fill
                        cell.font = Font(color="FF0000", bold=True, size=11)

    
    # Adjust column widths to 10 characters
    for col in ws.columns:
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = 5

    # Adjust row heights
    for row in range(1, 26):
        ws.row_dimensions[row].height = 15


@macy_bp.route("/macy", methods=["GET", "POST"])
@login_required
def macy_maker():
    current_user = session['username']
    filename = None
    error_message = None
    po_num = request.form.get("po_num", "")
    order_number = request.form.get("order_number", "")
    apt = request.form.get("apt", "")
    carrier = request.form.get("carrier", "")
    project = request.form.get("project", "")

    if request.method == "POST":
        current_app.logger.info("%s is processing Macy Order: %s", current_user, order_number)
        excel_data, error_message, total_labels, duplicate_numbers, label_ref, address, warnings = process_orders(po_num, order_number, apt, carrier, project)
        if excel_data is None and error_message is None:
            error_message = f"Order : {order_number} not found, maybe wrong?"
            current_app.logger.warning("Order number %s not found for %s", order_number, current_user)
        elif excel_data:
            # Return the Excel file directly
            filename = f"macyset_{order_number}_new.xlsx"
            return send_file(
                io.BytesIO(excel_data),
                download_name=filename,
                as_attachment=True,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )

    return render_template("macy.html", filename=filename, error_message=error_message, current_user=current_user, po_num=po_num, order_number=order_number, apt=apt, carrier=carrier, project=project)

