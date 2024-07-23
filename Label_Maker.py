import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
import shutil

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

order_number = ""

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
    print(df)
    conn.close()

    return df

def process_orders():
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

    local_max = -9999
    for _, row in df.iterrows():
        try:
            length_of_range = max(map(int, filter(lambda x: x.isdigit(), re.split(r'[-,\.]', row['UT_DESC']))))
            local_max = max(local_max, length_of_range)
        except ValueError:
            error_message = "Invalid description value: you should change your description in OMS, 单子数字还没改哦"
            print(error_message)
            return None, error_message

    output_line_number = 1
    sl_num = 1
    for _, row in df.iterrows():
        output_line_number, sl_num = process_item(output_line_number, row, from_address, to_address, ws, str(local_max), po_number, sl_num)

    while sl_num <= local_max:
        output_line_number = generate_additional_label(output_line_number, from_address, to_address, ws, str(local_max), po_number, sl_num)
        sl_num += 1

    ws["B" + str(local_max * 20 + 7)] = "FDC"
    ws["C" + str(local_max * 20 + 7)].font = ft2
    ws["B" + str(local_max * 20 + 8)] = "内部单号:"
    ws["C" + str(local_max * 20 + 8)] = str(order_number)
    ws["C" + str(local_max * 20 + 8)].font = ft2
    ws["B" + str(local_max * 20 + 10)] = "Boxes #:"
    ws["C" + str(local_max * 20 + 10)] = str(local_max)
    ws["C" + str(local_max * 20 + 10)].font = ft2
    ws["B" + str(local_max * 20 + 11)] = "PO#:"
    ws["C" + str(local_max * 20 + 11)] = str(po_number)
    ws["C" + str(local_max * 20 + 11)].font = ft2
    ws["B" + str(local_max * 20 + 12)] = "LOCATION: "
    ws["C" + str(local_max * 20 + 12)] = text_after_boxes   # Adjust as needed
    ws["C" + str(local_max * 20 + 12)].font = ft2

    target_filename = str(order_number) + "_label"
    wb.save(output_directory + target_filename + ".xlsx")
    print(sl_num)

    return output_directory + target_filename + ".xlsx", error_message

def process_item(output_line_number, row, from_address, to_address, ws, number_of_labels, po_number, sl_num):
    sku_line = row['PROD_CD']
    sku = sku_line.split()[0]
    color = sku_line.split()[1]
    num_labels =  1 + sum(int(part.split('-')[1]) - int(part.split('-')[0]) for part in row['UT_DESC'].split(',') if '-' in part)

    print( str(sku) + "  " + str(color) + ":"  +str(num_labels) + "  " + row['UT_DESC'])
    quantity = row['PC_UNIT']

    for _ in range(num_labels):
        output_line_number = process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, sl_num)
        sl_num += 1

    return output_line_number, sl_num

def process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, sl_num):
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
    ws["E" + str(output_line_number)] = "PCS"
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
    ws["d" + str(output_line_number)].fill = Yellow_fill
    ws["E" + str(output_line_number)] = "PCS"
    ws["E" + str(output_line_number)].font = ft
    output_line_number += 1

    ws["A" + str(output_line_number)] = "SIZE:N-A"
    ws["A" + str(output_line_number)].font = ft
    ws.merge_cells("A" + str(output_line_number) + ":" + "B" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["C" + str(output_line_number)] = "COLOR:"
    ws["C" + str(output_line_number)].font = ft
    ws["d" + str(output_line_number)].fill = Yellow_fill
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
    ws["E" + str(output_line_number)] = "PCS"
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
    process_orders()