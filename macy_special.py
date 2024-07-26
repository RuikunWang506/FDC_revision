# macy_special.py
import pyodbc
import pandas as pd
import os
import re
from openpyxl import Workbook
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# SQL related settings
server = "192.168.254.204"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

output_directory = "output/macy_label/"
ftpl = Font(name="Arial", size="12")
ft22 = Font(name="Arial", size="14")
ft33 = Font(name="Arial", size="16", bold=True)
Yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')

# Fonts and borders for part 2
ftlb = Font(name="Arial", size="12")
ft3 = Font(name="Arial", size="8")
ft6 = Font(name="Arial", size="7")
ftb = Font(name="Arial", size="12", bold=True)
ftbigger = Font(name="Arial", size="16", bold=True)


def get_data_from_sql(bol):
    if len(bol) == 5:
        database_name = 'omsdata_FDC'
    elif len(bol) == 4:
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
                ord.ord_num = {bol}
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

def process_item(output_line_number, row, from_address, to_address, ws, number_of_labels, po_number, sl_num):
    sku_line = row['PROD_CD']
    sku = sku_line.split()[0]
    color = sku_line.split()[1]
    num_labels =  1 + sum(int(part.split('-')[1]) - int(part.split('-')[0]) for part in row['UT_DESC'].split(',') if '-' in part)
    quantity = row['PC_UNIT']

    for _ in range(num_labels):
        output_line_number = process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, sl_num)
        sl_num += 1

    return output_line_number, sl_num

def process_label(sku, color, from_address, to_address, output_line_number, ws, quantity, number_of_labels, po_number, sl_num):
    output_line_number += 1
    ws["A" + str(output_line_number)] = "Packing Slip"
    ws["A" + str(output_line_number)].font = ft33
    ws.merge_cells("A" + str(output_line_number) + ":" + "C" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="center", vertical="center")

    output_line_number += 3

    for line_num in range(len(from_address)):
        ws["A" + str(output_line_number)] = from_address[line_num]
        ws["A" + str(output_line_number)].font = ftpl
        output_line_number += 1
    output_line_number += 1
    for line_num in range(len(to_address)):
        ws["A" + str(output_line_number)] = to_address[line_num]
        ws["A" + str(output_line_number)].font = ftpl
        output_line_number += 1
    output_line_number += 1
    
    ws["A" + str(output_line_number)] = "P.O. # " + po_number.strip()
    ws["A" + str(output_line_number)].font = ftpl
    ws.merge_cells("A" + str(output_line_number) + ":" + "C" + str(output_line_number))
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    output_line_number += 1
    output_line_number += 1
    
    ws["A" + str(output_line_number)] = "STYLE#"
    ws["A" + str(output_line_number)].font = ftpl
    ws["B" + str(output_line_number)] = sku
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].font = ftpl
    ws["D" + str(output_line_number)] = "BOX#: " + str(sl_num)
    ws["D" + str(output_line_number)].font = ftpl
    output_line_number += 1
    
    ws["A" + str(output_line_number)].alignment = Alignment(horizontal="left", vertical="center")
    ws["A" + str(output_line_number)] = "COLOR:"
    ws["A" + str(output_line_number)].font = ftpl
    ws["B" + str(output_line_number)] = color
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].font = ftpl
    output_line_number += 1
    
    ws["A" + str(output_line_number)] = "QUANTITY"
    ws["A" + str(output_line_number)].font = ftpl
    ws["B" + str(output_line_number)] = quantity
    ws["B" + str(output_line_number)].fill = Yellow_fill
    ws["B" + str(output_line_number)].font = ftpl
    ws["C" + str(output_line_number)] = "PCS"
    ws["C" + str(output_line_number)].font = ftpl
    output_line_number += 2

    return output_line_number

def process_label_2(output_line_number, row, to_address, ws_labels, number_of_labels, po_number, carrier, project,bol):
    sku_line = row['PROD_CD']
    sku = sku_line.split()[0]
    color = sku_line.split()[1]
    quantity = row['PC_UNIT']
    from_address = (
        ["SHIP FROM:", "FASHION DISTRIBUTION", "CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(bol) == 5 else
        ["SHIP FROM:", "E-Fashion Inc","E-Fashion INC", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
        if len(bol) == 4 else
        "N/A"
    )

    for sl_num in range(number_of_labels):
        temp_output_line_number = output_line_number
        temp_output_line_number += 5	

        for line_num in range(len(from_address)):
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
        ws_labels["B" + str(temp_output_line_number)] = "BOL#: " + str(bol)
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

        output_line_number = temp_output_line_number

    return output_line_number

def create_pallet_labels_sheet(wb, po_num, bol, apt):
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

    # Sample data to fill in the sheet
    data = [
        ["ROUTING DESTINATION"],
        ["SANTA FE SPRINGS 1:1 MERGE CENTER"],
        ["c/o DYNAMIC"],
        ["14141 ALONDRA BOULEVARD"],
        ["SANTA FE SPRINGS CA  90670"],
        [""],
        ["VENDOR NAME"],
        ["E-Fashion Inc"],
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
    ws['A11']= bol
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

def process_order(po_num, bol, apt, carrier, project):
    try:
        df = get_data_from_sql(bol)
        
        if df is None or df.empty:
            print(f"No data found for order number: {bol}")
            return None, f"No data found for order number: {bol}"
        
        target_filename = f"macyset_{bol}.xlsx"
        target_filepath = os.path.join(output_directory, target_filename)

        wb = Workbook()
        ws = wb.create_sheet(title="packing_list")
        ws_labels = wb.create_sheet(title="label_4x6")
        default_sheet = wb["Sheet"]
        wb.remove(default_sheet)

        ws.page_setup.paperSize = "1"
        ws.page_setup.orientation = "landscape"
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

        ws_labels.page_setup.orientation = "portrait"
        ws_labels.page_setup.paperSize = "1"
        ws_labels.column_dimensions['A'].width = 5.29
        ws_labels.column_dimensions['B'].width = 8.14
        ws_labels.column_dimensions['C'].width = 6.57
        ws_labels.column_dimensions['D'].width = 2.43
        ws_labels.column_dimensions['E'].width = 5.29
        ws_labels.column_dimensions['F'].width = 5.29
        ws_labels.page_margins.left = 0.2
        ws_labels.page_margins.right = 0
        ws_labels.page_margins.top = 0 
        ws_labels.page_margins.bottom = 0 
        ws_labels.page_margins.header = 0 
        ws_labels.page_margins.footer = 0

        from_address = (
            ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
            if len(bol) == 5 else
            ["FROM: E-Fashion Inc", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
            if len(bol) == 4 else
            "N/A"
        )
        
        to_address = ["TO: " + df.iloc[0]['SP_ADR'].strip(), df.iloc[0]['SP_ADR_2'].strip(), df.iloc[0]['SP_ADR_22'].strip(), df.iloc[0]['SP_ADR_3'].strip()]
        to_address_4x6 = ["SHIP TP:",  df.iloc[0]['SP_ADR'].strip(), df.iloc[0]['SP_ADR_2'].strip(), df.iloc[0]['SP_ADR_22'].strip(), df.iloc[0]['SP_ADR_3'].strip()]
        po_number = str(df.iloc[0]['PO_NUM'])
        
        local_max = -9999
        for _, row in df.iterrows():
            try:
                length_of_range = max(map(int, filter(lambda x: x.isdigit(), re.split(r'[-,\.]', row['UT_DESC']))))
                local_max = max(local_max, length_of_range)
            except ValueError:
                error_message = "Invalid description value: you should change your description in OMS 单子数据还没改哦"
                print(error_message)
                return None, error_message

        output_line_number_packing = 1
        output_line_number_labels = 1
        sl_num = 1
        for _, row in df.iterrows():
            output_line_number_packing, sl_num = process_item(output_line_number_packing, row, from_address, to_address, ws, local_max, po_number, sl_num)
        output_line_number_labels = process_label_2(output_line_number_labels, row, to_address_4x6, ws_labels, local_max, po_number, carrier, project, bol)

        create_pallet_labels_sheet(wb, po_num, bol, apt)
        wb.save(target_filepath)
        return target_filepath, None

    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        print(error_message)
        return None, error_message

# Remove this block to prevent automatic execution
# if __name__ == "__main__":
#     df = get_data_from_sql(bol)
#     from_address = ["FROM: FASHION DISTRIBUTION CENTER", "827 LAWSON STREET", "CITY OF INDUSTRY, CA 91748"]
#     to_address = ["TO: " + df.iloc[0]['SP_ADR'], df.iloc[0]['SP_ADR_2'], df.iloc[0]['SP_ADR_22'], df.iloc[0]['SP_ADR_3']]
#     po_number = df.iloc[0]['PO_NUM']
#     process_order(po_num, bol, apt, carrier, project)
