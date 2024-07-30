import openpyxl
from openpyxl import Workbook, load_workbook
import pyodbc
import pandas as pd
import datetime
import os
from PIL import Image as PILImage
from openpyxl.styles import Alignment, Border, Side, PatternFill
import io

source_folder = r"D:\Catalog_pictures\catalog_resized\\"
user_input = "N1151,N1223,n1324,t2874,n3007,s1408,et2949,n1782,s1426,n1835,l1494,l1513"
formatted_date = ""

def main():
    global user_input, formatted_date
    if user_input.endswith(","):
        user_input = user_input[:-1].strip()
    user_input_list = [item.strip() for item in user_input.replace(" ", "").split(',')]
    print("useinpu " + str(user_input))
    print("uselist " + str(user_input_list))
    server = "192.168.254.204"
    database_fdc = "omsdata_FDC"
    database_oms_ef = "oms_EF"
    username = "tristen"
    password = 'Qwer1234##'
    driver = 'ODBC Driver 17 for SQL Server'
    today = datetime.datetime.now()
    formatted_date = today.strftime("List-%Y%m%d-%H%M%S")

    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
        )
        conn_oms_ef = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}'
        )
        cursor_fdc = conn_fdc.cursor()
        cursor_oms_ef = conn_oms_ef.cursor()

        queries = []
        for conn, cursor, database_name in [(conn_fdc, cursor_fdc, 'omsdata_FDC'), (conn_oms_ef, cursor_oms_ef, 'oms_EF')]:

            if user_input.endswith(","):
                user_input = user_input[:-1]

            mylist = "'," + str(user_input).replace(" ","")
            query = """\
            DECLARE @AltCDList NVARCHAR(MAX) = ?;
            DECLARE @ProductList NVARCHAR(MAX);
            SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';
            WITH SplitAltCD AS (
                SELECT value
                FROM dbo.SplitString(@AltCDList, ',')
            )
            SELECT @ProductList = COALESCE(@ProductList + ',', '') + PROD_CD
            FROM dbo.inv
            WHERE EXISTS (SELECT 1 FROM SplitAltCD WHERE alt_CD LIKE value);
            WITH SplitValues AS (
                SELECT value
                FROM dbo.SplitString(@ProductList, ',')
            )
            SELECT 
                i.PROD_CD AS SKU,
                i.ALT_CD AS SKU_no_color,
                id.in_stock AS in_stock,
                id.ORDER_QTY AS some_one_ordered,
                (id.in_stock - id.order_qty) AS Avaliable_now,
                id.on_order_qty AS incoming_quantity,
                i.RETAIL_PRS AS selling_price,
                i.PC_CASE AS QTY_in_box,
                i.UT_WT AS unit_weight,
                i.UNIT_COLOR AS color,
                idt.ut_len AS item_length,
                idt.ut_wi AS item_width,
                idt.UT_hi AS item_height
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            WHERE i.PROD_CD IN (SELECT value FROM SplitValues);
            """
            cursor.execute(query, mylist)
            rows = cursor.fetchall()
            rows_with_database_name = [(database_name,) + tuple(row) for row in rows]
            queries.append(rows_with_database_name)

        combined_results = queries[0] + queries[1]

        workbook = Workbook()
        main_sheet = workbook.active
        main_sheet.title = 'main'

        column_headers = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered', 'Avaliable_now','incoming_quantity',
                          'selling_price', 'QTY_in_box', 'unit_weight',
                          'color', 'item_length', 'item_width', 'item_height','dimensions']
        for col_index, header in enumerate(column_headers, start=1):
            main_sheet.cell(row=1, column=col_index, value=header)
        
        for row_index, row in enumerate(combined_results, start=2):
            for col_index, value in enumerate(row, start=1):
                if col_index == 2:
                    value = value.rstrip()
                main_sheet.cell(row=row_index, column=col_index, value=value)

        excel_file_path = os.path.join("output", "list_output", formatted_date + ".xlsx")
        workbook.save(excel_file_path)

        cursor_fdc.close()
        cursor_oms_ef.close()
        conn_fdc.close()
        conn_oms_ef.close()
        
        create_and_save_pivot(excel_file_path)
        generate_image_sheet(combined_results, user_input_list)

    except pyodbc.Error as e:
        print("Error connecting to SQL Server:", e)
    except Exception as e:
        print("Unexpected error:", e)

def create_and_save_pivot(file_path):
    try:
        sheet_name = "main"
        output_sheet_name = "sorted"
        data = pd.read_excel(file_path, sheet_name=sheet_name)
        data = data[(data['Avaliable_now'] != 0) | (data['incoming_quantity'] != 0)]
        pivot_table = pd.pivot_table(data, index=["SKU_no_color", "SKU", "color"], values="Avaliable_now", aggfunc="sum")
        pivot_df = pivot_table.reset_index()

        output_dir = os.path.join("output", "list_output")
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, f"{formatted_date}.xlsx")
        
        # Save the pivot table to a new sheet in the Excel file
        with pd.ExcelWriter(file_path, mode='a', if_sheet_exists='replace') as writer:
            pivot_df.to_excel(writer, sheet_name=output_sheet_name, index=False)

        print("Pivot table created and saved in the same Excel file.")
        return file_path
    except Exception as e:
        print("Error creating and saving pivot table:", e)


def set_border(ws, cell_range):
    thin = Side(border_style="thin", color="000000")
    for row in ws[cell_range]:
        for cell in row:
            cell.border = Border(top=thin, left=thin, right=thin, bottom=thin)

def reduce_image_quality(image_path, quality=20):
    image = PILImage.open(image_path)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=quality)
    buffer.seek(0)
    return buffer

def add_data_to_excel_sheet(sku_name, ws, row, existing_wb):
    sorted_sheet = existing_wb["sorted"]
    main_sheet = existing_wb["main"]
    detail_row_num = 0  

    picfile = sku_name + ".jpg"
    image_path = os.path.join(source_folder, picfile)
    
    if not os.path.exists(image_path):
        image_path = os.path.join(source_folder, "nopic.jpg")
    
    buffer = reduce_image_quality(image_path)
    excelImg = openpyxl.drawing.image.Image(buffer)
    excelImg.height = 150
    excelImg.width = 200
    excelImg.anchor = 'A' + str(row)
    ws.add_image(excelImg)

    print(sku_name)
    ws['G' + str(row)] = sku_name
    price = ""
    for xd in range(2, main_sheet.max_row + 1):
        if main_sheet["A" + str(xd)].value is None:
            break
        if sku_name == str(main_sheet["C" + str(xd)].value).strip():
            price = main_sheet["H" + str(xd)].value
            break
    ws['F' + str(row)] = "$" + str(price)

    for j in range(2, sorted_sheet.max_row + 1):
        if str(sorted_sheet["A" + str(j)].value).strip().startswith(sku_name):
            ws["H" + str(row + detail_row_num)] = sorted_sheet["C" + str(j)].value
            ws["I" + str(row + detail_row_num)] = sorted_sheet["D" + str(j)].value
            ws["N" + str(row + detail_row_num)] = sorted_sheet["B" + str(j)].value
            for xd in range(2, main_sheet.max_row + 1):
                if main_sheet["A" + str(xd)].value is None:
                    break
                if str(main_sheet["B" + str(xd)].value).strip() == str(sorted_sheet["B" + str(j)].value).strip():
                    ws["K" + str(row + detail_row_num)] = main_sheet["I" + str(xd)].value
                    dim_len = main_sheet["L" + str(xd)].value
                    dim_wi = main_sheet["M" + str(xd)].value
                    dim_hi = main_sheet["N" + str(xd)].value
                    ws["L" + str(row + detail_row_num)] = f"{dim_len} x {dim_wi} x {dim_hi}"

                    if str(main_sheet["A" + str(xd)].value).strip() == "omsdata_FDC":
                        ws["O" + str(row + detail_row_num)] = main_sheet["F" + str(xd)].value
                        ws["J" + str(row + detail_row_num)] = main_sheet["G" + str(xd)].value
                    elif str(main_sheet["A" + str(xd)].value).strip() == "oms_EF":
                        ws["P" + str(row + detail_row_num)] = main_sheet["F" + str(xd)].value
            detail_row_num += 1

    print(sku_name + " completed")
    ws.merge_cells("A" + str(row) + ":" + "D" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws.merge_cells("E" + str(row) + ":" + "E" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws.merge_cells("F" + str(row) + ":" + "F" + str(row + (7 if detail_row_num < 8 else detail_row_num - 1)))
    ws['F' + str(row)].alignment = Alignment(horizontal='center', vertical='center')
    row += 8 if detail_row_num < 8 else detail_row_num

    return row  

def generate_image_sheet(combined_results, user_input_list):
    existing_wb = load_workbook(os.path.join("output", "list_output", formatted_date + ".xlsx"))
    new_ws = existing_wb.create_sheet(title="list")
    new_ws.freeze_panes = 'A3'
    new_ws['A1'] = "PLEASE FILL HERE ! POWERED BY SID AND TRISTEN"
    new_ws.merge_cells("A1:E1")
    new_ws['A1'].alignment = Alignment(horizontal='center', vertical='center')  
    new_ws['A2'] = "ITEM"
    new_ws.merge_cells("A2:D2")
    new_ws['E2'] = "Description"
    new_ws['F2'] = "Price"
    new_ws['G2'] = "Style"
    new_ws['H2'] = "Color"
    new_ws['I2'] = "Quantity"
    new_ws['J2'] = "Ocean"
    new_ws['K2'] = "PCS/BOX"
    new_ws['L2'] = "Dimension"
    new_ws['N2'] = "SKU"
    new_ws['O2'] = "FDC Quantity"
    new_ws['P2'] = "E fashion quantity"

    row = 3
    for sku in user_input_list:
        sku_capitalize = sku.upper()
        row = add_data_to_excel_sheet(sku_capitalize, new_ws, row, existing_wb)

    highlight_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")
    new_ws['A1'].fill = highlight_fill
    set_border(new_ws, 'A1:L' + str(new_ws.max_row))
    existing_wb.save(os.path.join("output", "list_output", formatted_date + ".xlsx"))
    
if __name__ == "__main__":
    main()
    print("output/list_output/" + formatted_date + ".xlsx")
