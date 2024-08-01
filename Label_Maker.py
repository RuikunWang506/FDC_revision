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

# Fonts and styles
ft = Font(name="Arial", size="12")
ft2 = Font(name="Arial", size="14")
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

order_number = "1762"

def get_data_from_sql(order_number):
    if len(order_number) == 5:
        database_name = 'omsdata_FDC'
    elif len(order_number) == 4:
        database_name = 'oms_EF'
    else:
        print("Invalid input length. Please provide a 4 or 5-digit input.")
        return None

    conn = pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={database_name};UID={username};PWD={password}'
    )

    query = f"""
        WITH ExtractedNumbers AS (
            SELECT DISTINCT
                ord.UT_DESC,
                ord.PROD_CD,
                ord.PC_UNIT,
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
            NOTES
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

    df = pd.read_sql_query(query, conn)

    conn.close()

    return df

def process_orders(order_number):
    error_message = None
    df = get_data_from_sql(order_number)
    
    if df is None or df.empty:
        error_message = f"No data found for order number: {order_number}"
        print(error_message)
        return None, error_message
    
    wb = Workbook()
    ws = wb.active
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

    from_address = (
        ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 5 else
        ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(order_number) == 4 else
        "N/A"
    )
    to_address = ["TO: "+ df.iloc[0]['SP_ADR'], df.iloc[0]['SP_ADR_2'], df.iloc[0]['SP_ADR_22'], df.iloc[0]['SP_ADR_3']]
    po_number = str(df.iloc[0]['PO_NUM'])
    text_after_boxes = re.search(r'BOXES\s+(.*)', str(df.iloc[0]['NOTES'])).group(1) if re.search(r'BOXES\s+(.*)', str(df.iloc[0]['NOTES'])) else None

    number_of_labels = 0
    all_desc_parts = []
    for _, row in df.iterrows():
        desc_parts = re.split(r'[-,]', row['UT_DESC'].strip())
        desc_parts = [part.strip() for part in desc_parts]
        all_desc_parts.extend(desc_parts)
        max_label = max(map(int, filter(lambda x: x.isdigit(), desc_parts)))
        number_of_labels = max(number_of_labels, max_label)

    desc_counts = Counter(all_desc_parts)
    duplicate_numbers = [item for item, count in desc_counts.items() if count > 1]  
    print("Number of labels is:", number_of_labels)
    print("Mixed Boxes are:", duplicate_numbers)
        
    output_line_number = 1
    sl_num = 1
    info_list = []
    for _, row in df.iterrows():
        output_line_number, sl_num, info = process_item(output_line_number, row, from_address, to_address, ws, str(number_of_labels), po_number, sl_num, duplicate_numbers)
        info_list.append(info)

    while sl_num <= number_of_labels:
        output_line_number = generate_additional_label(output_line_number, from_address, to_address, ws, str(number_of_labels), po_number, sl_num)
        sl_num += 1

    ws["B" + str(number_of_labels * 20 + 7)] = "FDC"
    ws["C" + str(number_of_labels * 20 + 7)].font = ft2
    ws["B" + str(number_of_labels * 20 + 8)] = "内部单号:"
    ws["C" + str(number_of_labels * 20 + 8)] = str(order_number)
    ws["C" + str(number_of_labels * 20 + 8)].font = ft2
    ws["B" + str(number_of_labels * 20 + 10)] = "Boxes #:"
    ws["C" + str(number_of_labels * 20 + 10)] = str(number_of_labels)
    ws["C" + str(number_of_labels * 20 + 10)].font = ft2
    ws["B" + str(number_of_labels * 20 + 11)] = "PO#:"
    ws["C" + str(number_of_labels * 20 + 11)].font = ft2
    ws["B" + str(number_of_labels * 20 + 12)] = "LOCATION: "
    ws["C" + str(number_of_labels * 20 + 12)] = text_after_boxes   # Adjust as needed
    ws["C" + str(number_of_labels * 20 + 12)].font = ft2

    target_filename = str(order_number) + "_label"
    wb.save(output_directory + target_filename + ".xlsx")

    return output_directory + target_filename + ".xlsx", None, number_of_labels, duplicate_numbers, info_list

def process_item(output_line_number, row, from_address, to_address, ws, number_of_labels, po_number, sl_num, duplicate_numbers):
    sku_line = row['PROD_CD']
    sku = sku_line.split()[0]
    color = sku_line.split()[1]

    # Split on both space and comma, and filter to only keep parts with '-'
    parts_with_dash = [part for part in re.split(r'[\s,]+', row['UT_DESC'].strip()) if '-' in part]
    num_labels = sum([int(part.split('-')[1]) - int(part.split('-')[0]) + 1 for part in parts_with_dash])

    info = (str(sku) + "  " + str(color) + ":"  + str(num_labels) + "  " + row['UT_DESC'])
    print(info)
    quantity = row['PC_UNIT']

    desc_parts = re.split(r'[-\s,]+', row['UT_DESC'].strip())
    desc_parts = [part.strip() for part in desc_parts]

    for i in range(num_labels):
        current_label_number = sl_num + i
        if str(current_label_number) in duplicate_numbers:
            output_line_number = process_label(sku, "MIX", from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, current_label_number,info)
        else:
            output_line_number = process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, current_label_number,info)
    sl_num += num_labels
    return output_line_number, sl_num,info

def process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, current_label_number,info):
    output_line_number += 1
    output_line_number += 1
    for line_num in range(len(from_address)):
        ws["A" + str(output_line_number)] = from_address[line_num]
        ws["A" + str(output_line_number)].font = ft
        output_line_number += 1
    output_line_number += 1
    for line_num in range(len(to_address)):
        ws["A" + str(output_line_number)] = to_address[line_num]
        ws["A" + str(output_line_number)].font = ft
        output_line_number += 1
    output_line_number += 1
    ws["A" + str(output_line_number)] = "DEPT#"
    ws["A" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "P.O. # " + po_number
    ws["C" + str(output_line_number)].font = ft
    ws.merge_cells("C" + str(output_line_number) + ":" + "E" + str(output_line_number))
    ws["C" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    output_line_number += 1

    ws["A" + str(output_line_number)] = "STYLE#"
    ws["A" + str(output_line_number)].font = ft
    ws["B" + str(output_line_number)] = sku
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "QUANTITY"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = quantity
    ws["D" + str(output_line_number)].fill = Yellow_fill
    ws["D" + str(output_line_number)].font = ft
    ws["E" + str(output_line_number)] = "PCS"
    ws["E" + str(output_line_number)].font = ft
    output_line_number += 1

    ws["A" + str(output_line_number)] = "SIZE:N-A"
    ws["A" + str(output_line_number)].font = ft
    ws.merge_cells("A" + str(output_line_number) + ":" + "B" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["C" + str(output_line_number)] = "COLOR:"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = color
    ws["D" + str(output_line_number)].fill = Yellow_fill
    ws["D" + str(output_line_number)].font = ft
    output_line_number += 1

    ws["A" + str(output_line_number)] = "COORDINATE: NO"
    ws["A" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "PRE-TICKETED: NO"
    ws["C" + str(output_line_number)].font = ft
    output_line_number += 2

    ws["A" + str(output_line_number)] = "TOTAL QUANTITY IN CARTON"
    ws.merge_cells("A" + str(output_line_number) + ":" + "C" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["A" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = "=D" + str(output_line_number - 4)
    ws["D" + str(output_line_number)].font = ft
    ws["E" + str(output_line_number)].font = ft
    output_line_number += 2

    ws["A" + str(output_line_number)] = "CARTON"
    ws["A" + str(output_line_number)].font = ft
    ws["B" + str(output_line_number)] = current_label_number
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].alignment = Alignment(horizontal="center", vertical="center")
    ws["B" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "OF"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = number_of_labels
    ws["D" + str(output_line_number)].fill = Yellow_fill
    ws["D" + str(output_line_number)].font = ft
    output_line_number += 1
    output_line_number += 1

    return output_line_number

def generate_additional_label(output_line_number, from_address, to_address, ws, number_of_labels, po_number, sl_num):
    output_line_number += 1
    output_line_number += 1
    for line_num in range(len(from_address)):
        ws["A" + str(output_line_number)] = from_address[line_num]
        ws["A" + str(output_line_number)].font = ft
        output_line_number += 1
    output_line_number += 1
    for line_num in range(len(to_address)):
        ws["A" + str(output_line_number)] = to_address[line_num]
        ws["A" + str(output_line_number)].font = ft
        output_line_number += 1
    output_line_number += 1
    ws["A" + str(output_line_number)] = "DEPT#"
    ws["A" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "P.O. # " + po_number
    ws["C" + str(output_line_number)].font = ft
    ws.merge_cells("C" + str(output_line_number) + ":" + "E" + str(output_line_number))
    ws["C" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    output_line_number += 1

    ws["A" + str(output_line_number)] = "STYLE#"
    ws["A" + str(output_line_number)].font = ft
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["C" + str(output_line_number)] = "QUANTITY"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)].fill = Yellow_fill
    ws["D" + str(output_line_number)].font = ft
    ws["E" + str(output_line_number)].font = ft
    output_line_number += 1

    ws["A" + str(output_line_number)] = "SIZE:N-A"
    ws["A" + str(output_line_number)].font = ft
    ws.merge_cells("A" + str(output_line_number) + ":" + "B" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["C" + str(output_line_number)] = "COLOR:"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)].fill = Yellow_fill
    output_line_number += 1

    ws["A" + str(output_line_number)] = "COORDINATE: NO"
    ws["A" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "PRE-TICKETED: NO"
    ws["C" + str(output_line_number)].font = ft
    output_line_number += 2

    ws["A" + str(output_line_number)] = "TOTAL QUANTITY IN CARTON"
    ws.merge_cells("A" + str(output_line_number) + ":" + "C" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["A" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = "=D" + str(output_line_number - 4)
    ws["D" + str(output_line_number)].font = ft
    ws["E" + str(output_line_number)].font = ft
    output_line_number += 2

    ws["A" + str(output_line_number)] = "CARTON"
    ws["A" + str(output_line_number)].font = ft
    ws["B" + str(output_line_number)] = sl_num
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].alignment = Alignment(horizontal="center", vertical="center")
    ws["B" + str(output_line_number)].font = ft
    ws["C" + str(output_line_number)] = "OF"
    ws["C" + str(output_line_number)].font = ft
    ws["D" + str(output_line_number)] = number_of_labels
    ws["D" + str(output_line_number)].fill = Yellow_fill
    ws["D" + str(output_line_number)].font = ft
    output_line_number += 1
    output_line_number += 1

    return output_line_number

if __name__ == "__main__":
    order_number = "1762"  # Example order number
    file_path, error, number_of_labels, duplicate_numbers, info_list = process_orders(order_number)
    if error:
        print(f"Error: {error}")
    else:
        print(f"File created at: {file_path}")
        for info in info_list:
            print(info)