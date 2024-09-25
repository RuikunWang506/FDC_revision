import openpyxl
from openpyxl import Workbook, load_workbook
import pyodbc
import pandas as pd
import datetime
import os
from PIL import Image as PILImage
from openpyxl.styles import Alignment, Border, Side, PatternFill
import io
from config import Config
from openpyxl.utils.datetime import from_excel
from datetime import datetime as dt

# Define global variables
user_input_type = None
image_option = None
user_input = None
formatted_date = ""
source_folder = None  # Correctly define and highlight the source_folder variable
    
def main():
    global user_input, formatted_date, user_input_type, source_folder,today

    today = datetime.datetime.now()
    formatted_date = today.strftime("List-%Y%m%d-%H%M%S")

    if user_input_type == "directory":
        # Map the input directory to the local Dropbox path
        dropbox_index = user_input.lower().find('dropbox')
        if dropbox_index != -1:
            relative_path = user_input[dropbox_index + len('dropbox') + 1:]  # +1 to skip the backslash
            user_input_directory = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
            
            if not os.path.isdir(user_input_directory):
                raise ValueError(f"The directory {user_input_directory} does not exist on the local system.")

            # Get all filenames without the .jpg extension from the directory
            user_input_list = [os.path.splitext(f)[0] for f in os.listdir(user_input_directory) if f.endswith('.jpg')]
            user_input = ','.join(user_input_list)
        else:
            raise ValueError("The provided directory path does not include 'Dropbox'.")

    else:
        # If it's already a list input
        if user_input.endswith(","):
            user_input = user_input[:-1].strip()
        user_input_list = [item.strip() for item in user_input.replace(" ", "").split(',')]

    print("Processed user_input: " + str(user_input))
    print("user_input_list: " + str(user_input_list))

    # Select the correct source folder based on image_option
    if image_option == "dropboxpic":
        source_folder = user_input_directory
    else:
        source_folder = Config.CATALOG_RESIZED

    # Continue with the existing logic...
    # Database connection details
    server = "192.168.254.204"
    database_fdc = "omsdata_FDC"
    database_oms_ef = "oms_EF"
    username = "tristen"
    password = 'Qwer1234##'
    driver = 'ODBC Driver 17 for SQL Server'

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
            mylist = "'," + str(user_input).replace(" ", "")
            query = """\
            DECLARE @AltCDList NVARCHAR(MAX) = ?;
            DECLARE @ProductList NVARCHAR(MAX);

            -- Modify AltCDList and generate the product list
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

            -- Main Query
            SELECT 
                i.PROD_CD AS SKU,
                i.ALT_CD AS SKU_no_color,
                id.in_stock AS in_stock,
                id.ORDER_QTY AS some_one_ordered,
                (id.in_stock - id.order_qty) AS Available_now,
                id.on_order_qty AS incoming_quantity,
                i.RETAIL_PRS AS selling_price,
                i.PC_CASE AS QTY_in_box,
                i.UT_WT AS unit_weight,
                i.UNIT_COLOR AS color,
                idt.ut_len AS item_length,
                idt.ut_wi AS item_width,
                idt.UT_hi AS item_height,
                -- Add last_activity logic
                CASE 
                    WHEN LT_SL_DT = 0 THEN 'NEVER'
                    ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
                END AS last_activity,
                -- Add created_dt logic
                fcd.create_dt AS created_dt

            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
            LEFT JOIN (
                SELECT 
                    prod_cd,
                    -- Add created_dt formatting logic from wm_skufile
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
                FROM wm_skufile
            ) AS fcd ON i.PROD_CD = fcd.prod_cd
            WHERE i.PROD_CD IN (SELECT value FROM SplitValues);
            """
            cursor.execute(query, mylist)
            rows = cursor.fetchall()
            rows_with_database_name = [(database_name,) + tuple(row) for row in rows]
            queries.append(rows_with_database_name)

        combined_results = queries[0] + queries[1]

        # Create Excel workbook and save the results
        workbook = Workbook()
        main_sheet = workbook.active
        main_sheet.title = 'main'

        column_headers = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered', 'Avaliable_now',
                          'incoming_quantity', 'selling_price', 'QTY_in_box', 'unit_weight', 'color', 'item_length',
                          'item_width', 'item_height', 'last_activity','Created_dt']
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
    global source_folder  # Use the source_folder determined in the main function
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
    for xd in range(2, existing_wb["main"].max_row + 1):
        if existing_wb["main"]["A" + str(xd)].value is None:
            break
        if sku_name == str(existing_wb["main"]["C" + str(xd)].value).strip():
            price = existing_wb["main"]["H" + str(xd)].value
            break
    ws['F' + str(row)] = "$" + str(price)

    detail_row_num = 0
    for j in range(2, existing_wb["sorted"].max_row + 1):
        if str(existing_wb["sorted"]["A" + str(j)].value).strip().startswith(sku_name):
            ws["H" + str(row + detail_row_num)] = existing_wb["sorted"]["C" + str(j)].value
            ws["I" + str(row + detail_row_num)] = existing_wb["sorted"]["D" + str(j)].value
            ws["N" + str(row + detail_row_num)] = existing_wb["sorted"]["B" + str(j)].value
            
            last_modified_dates = []  # To store all last_modified dates
            created_dates = []  # To store all created_dt dates

            for xd in range(2, existing_wb["main"].max_row + 1):
                if existing_wb["main"]["A" + str(xd)].value is None:
                    break

                if str(existing_wb["main"]["B" + str(xd)].value).strip() == str(existing_wb["sorted"]["B" + str(j)].value).strip():
                    ws["K" + str(row + detail_row_num)] = existing_wb["main"]["I" + str(xd)].value
                    dim_len = existing_wb["main"]["L" + str(xd)].value
                    dim_wi = existing_wb["main"]["M" + str(xd)].value
                    dim_hi = existing_wb["main"]["N" + str(xd)].value
                    ws["L" + str(row + detail_row_num)] = f"{dim_len} x {dim_wi} x {dim_hi}"

                    if str(existing_wb["main"]["A" + str(xd)].value).strip() == "omsdata_FDC":
                        ws["O" + str(row + detail_row_num)] = existing_wb["main"]["F" + str(xd)].value
                        ws["J" + str(row + detail_row_num)] = existing_wb["main"]["G" + str(xd)].value
                    elif str(existing_wb["main"]["A" + str(xd)].value).strip() == "oms_EF":
                        ws["P" + str(row + detail_row_num)] = existing_wb["main"]["F" + str(xd)].value

                    # Collect last_modified and created_dt dates
                    last_modified = existing_wb["main"]["o" + str(xd)].value
                    created_dt = existing_wb["main"]["p" + str(xd)].value

                    # Collect dates if they are valid
                    if last_modified:
                        last_modified_dates.append(last_modified)

                    if created_dt:
                        created_dates.append(created_dt)

            # Get the most recent last_modified date
            last_modified_dates_filtered = [date for date in last_modified_dates if date != 'NEVER']

            if last_modified_dates_filtered:  # Check if the list is not empty
                # Convert filtered date strings to datetime objects
                last_modified_dates_dt = [dt.strptime(date, '%m/%d/%Y') for date in last_modified_dates_filtered]
                
                # Get the most recent (newest) last modified date
                newest_last_modified = max(last_modified_dates_dt).strftime('%m/%d/%Y')
                ws["Q" + str(row + detail_row_num)] = newest_last_modified
            else:
                ws["Q" + str(row + detail_row_num)] = "No Date"  # or some other default value

            created_dates_filtered = [date for date in created_dates if date != 'NEVER']


            if created_dates_filtered:
                # Convert the filtered date strings to datetime objects
                created_dates_dt = [dt.strptime(date, '%m/%d/%Y') for date in created_dates_filtered]
                
                # Get the oldest date if there are valid dates
                oldest_created_dt = min(created_dates_dt).strftime('%m/%d/%Y')
                ws["R" + str(row + detail_row_num)] = oldest_created_dt
            else:
                # If all values are 'NEVER', return 'NEVER'
                ws["R" + str(row + detail_row_num)] = "NEVER"
            
            detail_row_num += 1
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
    new_ws['A1'] = "Created Time:" + str(today.strftime("%Y/%m/%d"))
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
    new_ws['Q2'] = "Last Modified Date"
    new_ws['R2'] = "Created Date"
    

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
    print(main())
    print("output/list_output/" + formatted_date + ".xlsx")
