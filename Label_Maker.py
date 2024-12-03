import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from collections import defaultdict

# SQL related settings
server = "192.168.254.204"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

# Directories
output_directory = "output/label_output/"

# Ensure output directory exists
os.makedirs(output_directory, exist_ok=True)

# Fonts and styles
ft = Font(name="Arial", size=12)
ft_bold = Font(name="Arial", size=12, bold=True)
ft_summary = Font(name="Arial", size=14, bold=True)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

def extract_label_numbers(ut_desc):
    """
    Extracts individual label numbers from the UT_DESC string.
    Handles ranges (e.g., '1-30') and individual numbers separated by commas.
    """
    parts = ut_desc.split(',')
    label_numbers = []
    for part in parts:
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
            f'DRIVER={{{driver}}};'
            f'SERVER={server};'
            f'DATABASE={database_name};'
            f'UID={username};'
            f'PWD={password}'
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

    # Save the Excel workbook
    target_filename = f"{order_number}_label.xlsx"
    file_path = os.path.join(output_directory, target_filename)
    wb.save(file_path)
    print(f"process_orders: Excel file saved at {file_path}")

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

    return file_path, None, total_unique_labels, duplicate_label_numbers, label_reference, to_address, warnings

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
