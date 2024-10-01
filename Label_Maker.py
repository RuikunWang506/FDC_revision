import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
import shutil
from collections import Counter

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
ft = Font(name="Arial", size=12)  # Changed size to integer
ft2 = Font(name="Arial", size=14)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

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
                ord.order_qty ,          -- Added ord_qty to the CTE
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
            order_qty % PC_UNIT AS Remainder  -- Calculating the remainder
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
        return None, error_message, None, None, None, None
    
    print("process_orders: Creating Excel workbook.")
    wb = Workbook()
    ws = wb.active
    ws.title = "Labels"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = "1"
    ws.column_dimensions['A'].width = 11.71
    ws.column_dimensions['B'].width = 12.29
    ws.column_dimensions['C'].width = 12.43
    ws.column_dimensions['D'].width = 6.57
    ws.column_dimensions['E'].width = 8.43
    ws.page_margins.left = 1
    ws.page_margins.right = 0
    ws.page_margins.top = 0
    ws.page_margins.bottom = 0
    ws.page_margins.header = 0
    ws.page_margins.footer = 0

    print("process_orders: Setting up addresses.")
    from_address = (
        ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 5 else
        ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 4 else
        ["FROM: N/A", "Address Line 2", "Address Line 3", "Address Line 4"]
    )
    to_address = ["TO: "+ df.iloc[0]['SP_ADR'], df.iloc[0]['SP_ADR_2'], df.iloc[0]['SP_ADR_22'], df.iloc[0]['SP_ADR_3']]
    addresslist=to_address
    po_number = str(df.iloc[0]['PO_NUM'])
    notes = str(df.iloc[0]['NOTES'])
    text_after_boxes_match = re.search(r'BOXES\s+(.*)', notes)
    text_after_boxes = text_after_boxes_match.group(1) if text_after_boxes_match else "N/A"

    print("process_orders: Extracting number of labels and duplicates.")
    number_of_labels = 0
    all_desc_parts = []
    max_label_overall = 0
    for idx, row in df.iterrows():
        desc_parts = re.split(r'[-,]', row['UT_DESC'].strip())
        desc_parts = [part.strip() for part in desc_parts]
        all_desc_parts.extend(desc_parts)
        numeric_parts = list(map(int, filter(lambda x: x.isdigit(), desc_parts)))
        if numeric_parts:
            max_label = max(numeric_parts)
            number_of_labels = max(number_of_labels, max_label)
            max_label_overall = max(max_label_overall, max_label)
            print(f"process_orders: Row {idx+1}: max_label = {max_label}")
        else:
            print(f"process_orders: Row {idx+1}: No numeric labels found.")

    print(f"process_orders: Total number of labels determined: {number_of_labels}")

    desc_counts = Counter(all_desc_parts)
    # Corrected line:
    duplicate_numbers = [item for item, count in desc_counts.items() if item.isdigit() and count > 1]
    print(f"process_orders: Duplicate numbers identified: {duplicate_numbers}")

    # Compute the sum of remainders for each duplicate number
    duplicate_remainders = {}

    for number in duplicate_numbers:
        # Create a regex pattern to match the exact number as a whole word
        pattern = rf'\b{re.escape(number)}\b'
        
        # Find all rows where 'UT_DESC' contains the duplicate number
        rows_with_number = df[df['UT_DESC'].str.contains(pattern, regex=True, na=False)]
        
        # Sum the 'Remainder' for these rows
        duplicate_remainders[number] = rows_with_number['Remainder'].sum()

    print(f"process_orders: Duplicate remainders: {duplicate_remainders}")

    output_line_number = 1
    sl_num = 1
    info_list = []
    for idx, row in df.iterrows():
        print(f"\nprocess_orders: Processing row {idx+1}")
        output_line_number, sl_num, info = process_item(
            output_line_number, row, from_address, to_address, ws,
            str(number_of_labels), po_number, sl_num, duplicate_numbers, duplicate_remainders
        )
        info_list.append(info)
        print(f"process_orders: After row {idx+1}: output_line_number = {output_line_number}, sl_num = {sl_num}, info = {info}")

    print("\nprocess_orders: Generating additional labels if necessary.")
    while sl_num <= number_of_labels:
        print(f"process_orders: Generating label number {sl_num}")
        output_line_number = generate_additional_label(
            output_line_number, from_address, to_address, ws,
            str(number_of_labels), po_number, sl_num
        )
        sl_num += 1
        print(f"process_orders: After generating label {sl_num -1}: output_line_number = {output_line_number}, sl_num = {sl_num}")

    # Calculate summary_start_row and summary_end_row
    summary_start_row = number_of_labels * 20 + 7
    summary_end_row = (number_of_labels + 1) * 20

    print(f"process_orders: Summary will start at row {summary_start_row}")
    print(f"process_orders: Summary will end at row {summary_end_row}")

    print("process_orders: Adding summary information.")
    ws["B" + str(summary_start_row)] = "FDC"
    ws["C" + str(summary_start_row)].font = ft2
    ws["B" + str(summary_start_row + 1)] = "内部单号:"
    ws["C" + str(summary_start_row + 1)] = str(order_number)
    ws["C" + str(summary_start_row + 1)].font = ft2
    ws["B" + str(summary_start_row + 3)] = "Boxes #:"
    ws["C" + str(summary_start_row + 3)] = str(number_of_labels)
    ws["C" + str(summary_start_row + 3)].font = ft2
    ws["B" + str(summary_start_row + 4)] = "PO#:"
    ws["C" + str(summary_start_row + 4)].font = ft2
    ws["C" + str(summary_start_row + 4)] = po_number
    ws["B" + str(summary_start_row + 5)] = "LOCATION: "
    ws["C" + str(summary_start_row + 5)] = text_after_boxes  # Adjust as needed
    ws["C" + str(summary_start_row + 5)].font = ft2

    total_rows = ws.max_row
    print(f"process_orders: Total rows in Excel file before deletion: {total_rows}")

    # Define the summary_end_row based on number_of_labels
    summary_end_row = (number_of_labels + 1) * 20
    print(f"process_orders: Calculated summary_end_row: {summary_end_row}")

    # Delete all rows after summary_end_row
    if summary_end_row < total_rows:
        rows_to_delete = total_rows - summary_end_row
        print(f"process_orders: Deleting {rows_to_delete} rows after row {summary_end_row}")
        ws.delete_rows(summary_end_row + 1, rows_to_delete)
    else:
        print("process_orders: No rows to delete after summary_end_row.")

    # Optionally, set the print area to limit the printed content
    ws.print_area = f"A1:E{summary_end_row-1}"

    target_filename = f"{order_number}_label.xlsx"
    file_path = os.path.join(output_directory, target_filename)
    wb.save(file_path)
    print(f"process_orders: Excel file saved at {file_path}")

    print(f"process_orders: Final output_line_number = {output_line_number}, sl_num = {sl_num}")
    return file_path, None, number_of_labels, duplicate_numbers, info_list,addresslist

def process_item(output_line_number, row, from_address, to_address, ws, number_of_labels, po_number, sl_num, duplicate_numbers, duplicate_remainders):
    print("----------------------------process_item--------------------------------------")
    print(f"process_item: Starting processing for PROD_CD = {row['PROD_CD']}")

    sku_line = row['PROD_CD']
    sku_parts = sku_line.split()
    if len(sku_parts) < 2:
        print("process_item: Unexpected PROD_CD format:", sku_line)
        sku = sku_line
        color = "Unknown"
    else:
        sku, color = sku_parts[0], sku_parts[1]

    # Split on both space and comma, and filter to only keep parts with '-'
    parts_with_dash = [part for part in re.split(r'[\s,]+', row['UT_DESC'].strip()) if '-' in part]
    num_labels = sum([int(part.split('-')[1]) - int(part.split('-')[0]) + 1 for part in parts_with_dash if part.split('-')[1].isdigit() and part.split('-')[0].isdigit()])
    quantity = row['PC_UNIT']
    info = f"生成======>   {sku}{color}  生成 {num_labels}张贴纸，对应行数为 {row['UT_DESC']}, 每箱是不是 {int(quantity)} 个包？ {duplicate_numbers},这些MIX了"
    print(f"process_item: SKU = {sku}, Color = {color}, Number of Labels = {num_labels}, Quantity = {quantity}")

    desc_parts = re.split(r'[-\s,]+', row['UT_DESC'].strip())
    desc_parts = [part.strip() for part in desc_parts]

    for i in range(num_labels):
        current_label_number = sl_num + i
        if str(current_label_number) in duplicate_numbers:
            print(f"process_item: Label {current_label_number} is a duplicate. Marking as 'MIX'.")
            # Set quantity to the sum of remainders for this duplicate number
            quantity = duplicate_remainders.get(str(current_label_number), 0)
            output_line_number = process_label(
                sku, "MIX", from_address, to_address, output_line_number, ws,
                quantity, number_of_labels, po_number, current_label_number, info
            )
        else:
            print(f"process_item: Label {current_label_number} is unique. Using color '{color}'.")
            output_line_number = process_label(
                sku, color, from_address, to_address, output_line_number, ws,
                quantity, number_of_labels, po_number, current_label_number, info
            )
    sl_num += num_labels

    print(f"process_item: Finished processing item. output_line_number = {output_line_number}, sl_num = {sl_num}, info = {info}")
    return output_line_number, sl_num, info

def process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, current_label_number, info):
    print("process_label: Creating label in Excel.")
    print(f"process_label: SKU = {sku}, Color = {color}, Current Label Number = {current_label_number}")

    output_line_number += 2
    # Add from_address
    for line_num, address_line in enumerate(from_address, start=1):
        cell = f"A{output_line_number}"
        ws[cell] = address_line
        ws[cell].font = ft
        print(f"process_label: Writing from_address line {line_num} to {cell}")
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add to_address
    for line_num, address_line in enumerate(to_address, start=1):
        cell = f"A{output_line_number}"
        ws[cell] = address_line
        ws[cell].font = ft
        print(f"process_label: Writing to_address line {line_num} to {cell}")
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add DEPT# and PO#
    ws[f"A{output_line_number}"] = "DEPT#"
    ws[f"A{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = f"P.O. # {po_number}"
    ws[f"C{output_line_number}"].font = ft
    ws.merge_cells(f"C{output_line_number}:E{output_line_number}")
    ws[f"C{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    print(f"process_label: Writing DEPT# and PO# to row {output_line_number}")
    output_line_number += 1

    # Add STYLE#, QUANTITY, and PCS
    ws[f"A{output_line_number}"] = "STYLE#"
    ws[f"A{output_line_number}"].font = ft
    ws[f"B{output_line_number}"] = sku
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "QUANTITY"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = quantity
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"] = "PCS"
    ws[f"E{output_line_number}"].font = ft
    print(f"process_label: Writing STYLE#, QUANTITY, PCS to row {output_line_number}")
    output_line_number += 1

    # Add SIZE and COLOR
    ws[f"A{output_line_number}"] = "SIZE:N-A"
    ws[f"A{output_line_number}"].font = ft
    ws.merge_cells(f"A{output_line_number}:B{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"C{output_line_number}"] = "COLOR:"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = color
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    print(f"process_label: Writing SIZE and COLOR to row {output_line_number}")
    output_line_number += 1

    # Add COORDINATE and PRE-TICKETED
    ws[f"A{output_line_number}"] = "COORDINATE: NO"
    ws[f"A{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "PRE-TICKETED: NO"
    ws[f"C{output_line_number}"].font = ft
    print(f"process_label: Writing COORDINATE and PRE-TICKETED to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    # Add TOTAL QUANTITY IN CARTON
    ws[f"A{output_line_number}"] = "TOTAL QUANTITY IN CARTON"
    ws.merge_cells(f"A{output_line_number}:C{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"A{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = f"=D{output_line_number - 4}"
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"].font = ft
    print(f"process_label: Writing TOTAL QUANTITY IN CARTON to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    # Add CARTON information
    ws[f"A{output_line_number}"] = "CARTON"
    ws[f"A{output_line_number}"].font = ft
    ws[f"B{output_line_number}"] = current_label_number
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].alignment = Alignment(horizontal="center", vertical="center")
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "OF"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = number_of_labels
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    print(f"process_label: Writing CARTON information to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    print(f"process_label: Finished writing label {current_label_number} at line {output_line_number}")
    return output_line_number

def generate_additional_label(output_line_number, from_address, to_address, ws, number_of_labels, po_number, sl_num):
    print("generate_additional_label: Creating additional label.")
    print(f"generate_additional_label: sl_num = {sl_num}")

    # Since 'info' is not defined in this function, we'll omit it from the print statement
    # Alternatively, you can pass 'info' as a parameter if needed

    output_line_number += 1
    # Add from_address
    for line_num, address_line in enumerate(from_address, start=1):
        cell = f"A{output_line_number}"
        ws[cell] = address_line
        ws[cell].font = ft
        print(f"generate_additional_label: Writing from_address line {line_num} to {cell}")
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add to_address
    for line_num, address_line in enumerate(to_address, start=1):
        cell = f"A{output_line_number}"
        ws[cell] = address_line
        ws[cell].font = ft
        print(f"generate_additional_label: Writing to_address line {line_num} to {cell}")
        output_line_number += 1
    output_line_number += 1  # Blank line

    # Add DEPT# and PO#
    ws[f"A{output_line_number}"] = "DEPT#"
    ws[f"A{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = f"P.O. # {po_number}"
    ws[f"C{output_line_number}"].font = ft
    ws.merge_cells(f"C{output_line_number}:E{output_line_number}")
    ws[f"C{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    print(f"generate_additional_label: Writing DEPT# and PO# to row {output_line_number}")
    output_line_number += 1

    # Add STYLE#, QUANTITY, and PCS
    ws[f"A{output_line_number}"] = "STYLE#"
    ws[f"A{output_line_number}"].font = ft
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "QUANTITY"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"] = "PCS"
    ws[f"E{output_line_number}"].font = ft
    print(f"generate_additional_label: Writing STYLE#, QUANTITY, PCS to row {output_line_number}")
    output_line_number += 1

    # Add SIZE and COLOR (Leaving COLOR blank as info is not available here)
    ws[f"A{output_line_number}"] = "SIZE:N-A"
    ws[f"A{output_line_number}"].font = ft
    ws.merge_cells(f"A{output_line_number}:B{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"C{output_line_number}"] = "COLOR:"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = "N/A"  # Placeholder since color info is not available
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    print(f"generate_additional_label: Writing SIZE and COLOR to row {output_line_number}")
    output_line_number += 1

    # Add COORDINATE and PRE-TICKETED
    ws[f"A{output_line_number}"] = "COORDINATE: NO"
    ws[f"A{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "PRE-TICKETED: NO"
    ws[f"C{output_line_number}"].font = ft
    print(f"generate_additional_label: Writing COORDINATE and PRE-TICKETED to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    # Add TOTAL QUANTITY IN CARTON
    ws[f"A{output_line_number}"] = "TOTAL QUANTITY IN CARTON"
    ws.merge_cells(f"A{output_line_number}:C{output_line_number}")
    ws[f"A{output_line_number}"].alignment = Alignment(horizontal="left", vertical="center")
    ws[f"A{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = f"=D{output_line_number - 4}"
    ws[f"D{output_line_number}"].font = ft
    ws[f"E{output_line_number}"].font = ft
    print(f"generate_additional_label: Writing TOTAL QUANTITY IN CARTON to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    # Add CARTON information
    ws[f"A{output_line_number}"] = "CARTON"
    ws[f"A{output_line_number}"].font = ft
    ws[f"B{output_line_number}"] = sl_num
    ws[f"B{output_line_number}"].fill = Yellow_fill
    ws[f"B{output_line_number}"].alignment = Alignment(horizontal="center", vertical="center")
    ws[f"B{output_line_number}"].font = ft
    ws[f"C{output_line_number}"] = "OF"
    ws[f"C{output_line_number}"].font = ft
    ws[f"D{output_line_number}"] = number_of_labels
    ws[f"D{output_line_number}"].fill = Yellow_fill
    ws[f"D{output_line_number}"].font = ft
    print(f"generate_additional_label: Writing CARTON information to row {output_line_number}")
    output_line_number += 2  # Add a blank line

    print(f"generate_additional_label: Finished creating additional label at line {output_line_number}")
    return output_line_number

if __name__ == "__main__":
    order_number = "33345"  # Example order number
    print("\n=== Starting Label Processing ===")
    file_path, error, number_of_labels, duplicate_numbers, info_list ,addresslist= process_orders(order_number)
    if error:
        print(f"\nError: {error}")
    else:
        print(f"\nFile created at: {file_path}")
        print(f"Total number of labels: {number_of_labels}")
        print(f"Duplicate numbers: {duplicate_numbers}")
        print(f"Info List ({len(info_list)} items):")
        for idx, info in enumerate(info_list, start=1):
            print(f"{idx}: {info}")
        # To see the final row reached, you can add:
        print(f"Final row reached: {file_path}")
    print("=== Label Processing Completed ===\n")
