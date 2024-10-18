import pyodbc
import openpyxl
import os
import datetime
import pandas as pd
from PIL import Image
from flask import current_app as app  # Import current_app to use Flask's logger

# SQL Server connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

def generate_excel(user_input):
    global file_path
    image_urls = []
    try:
        # Normalize the input to lowercase and strip extra spaces
        user_input = ' '.join(user_input.lower().strip().split())

        # Split the input by commas
        input_parts = [part.strip() for part in user_input.split(",")]

        # Initialize conditions for each situation
        prod_cd_conditions = []
        alt_cd_conditions = []
        descrip_conditions = []

        
        for part in input_parts:
            if "%" in part:  # Handle Situation 3: Wildcards or specific query command
                prod_cd_conditions.append(f"i.PROD_CD LIKE '{part}'")
                alt_cd_conditions.append(f"i.ALT_CD LIKE '{part}'")
                descrip_conditions.append(f"i.DESCRIP LIKE '{part}'")

            elif " " in part:  # Handle Situation 2: Full prod_cd with color code
                prod_cd_conditions.append(f"i.PROD_CD = '{part}'")
            else:  # Handle Situation 1: alt_cd, so match prod_cd using LIKE
                prod_cd_conditions.append(f"i.PROD_CD LIKE '{part}%'")

        # Combine all conditions
        combined_condition = " OR ".join(prod_cd_conditions + alt_cd_conditions + descrip_conditions)

        # Debug: Log the generated SQL WHERE condition
        #app.logger.info(f"Generated SQL WHERE condition: {combined_condition}")

        # Establish connections to the SQL Server databases
        conn_fdc = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
        )

        conn_oms_ef = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}'
        )

        # Execute the SQL query with the combined conditions
        queries = []
        for conn, cursor, database_name in [
            (conn_fdc, conn_fdc.cursor(), 'omsdata_FDC'), 
            (conn_oms_ef, conn_oms_ef.cursor(), 'oms_EF')
        ]:
            query = f"""
                SELECT 
                    i.PROD_CD AS SKU,
                    i.ALT_CD AS SKU_no_color,
                    i.DESCRIP AS Container,
                    id.in_stock AS in_stock,
                    id.ORDER_QTY AS some_one_ordered,
                    (id.in_stock - id.order_qty) AS Available_now,
                    id.on_order_qty AS incoming_quantity,
                    i.SALES_COST AS bag_cost,
                    i.RETAIL_PRS AS selling_price,
                    i.PC_CASE AS QTY_in_box,
                    i.CASE_WT AS box_weight,
                    i.UT_WT AS unit_weight,
                    i.UNIT_COLOR AS color,
                    idt.ut_len AS item_length,
                    idt.ut_wi AS item_width,
                    idt.UT_hi AS item_height,
                    CASE 
                        WHEN LT_SL_DT = 0 THEN 'NEVER'
                        ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
                    END AS last_activity,
                    fcd.create_dt
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
                LEFT JOIN (
                    SELECT 
                        prod_cd,
                        RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
                    FROM wm_skufile
                ) AS fcd ON i.PROD_CD = fcd.prod_cd
                WHERE {combined_condition};
            """
            
            cursor.execute(query)
            rows = cursor.fetchall()
            rows_with_database_name = [(database_name,) + tuple(row) for row in rows]
            queries.append(rows_with_database_name)

        # Combine the results from both databases
        combined_results = queries[0] + queries[1]


        # Process the combined result
        workbook = openpyxl.Workbook()
        sheet = workbook.active

        # Column headers for the Excel sheet
        column_headers = ['Database', 'SKU', 'Style', 'Descrip', 'Stock', 'Sold', 'Avail',
                          'Ocean', 'cost', 'MSRP', 'Case<br>Qty', 'box_wt<br>(lb)',
                          'pc_wt<br>(lb)', 'color', 'length', 'width', 'height', 'update<br>dt', 'create<br>dt']

        # Write the column headers to the Excel sheet
        for col_index, header in enumerate(column_headers, start=1):
            sheet.cell(row=1, column=col_index, value=header)

        # Write the data to the Excel sheet
        for row_index, row in enumerate(combined_results, start=2):
            for col_index, value in enumerate(row, start=1):
                if col_index == 3:  # Check if it's the column with SKU_no_color
                    hyperlink_value = value.strip()  # Remove trailing spaces

                    # Split the hyperlink_value and get the base_sku, or use the full value if there's no space
                    split_value = hyperlink_value.split()
                    base_sku = split_value[0] if split_value else hyperlink_value

                    local_resized_image_url = f"ftp/{base_sku}.jpg"
                    backup_original_image_url = f"/ftp2/{base_sku}.jpg"

                    # Add the image URL to the list if it's not already there
                    if local_resized_image_url not in image_urls:
                        image_urls.append(local_resized_image_url)

                    # Check if the image exists in the ftp directory
                    if os.path.exists(os.path.join('ftp', f'{base_sku}.jpg')):
                        value = f'<a href="{local_resized_image_url}">{value}</a>'
                    else:
                        value = f'<a href="{backup_original_image_url}">{value}</a>'

                # Write data to the Excel sheet
                sheet.cell(row=row_index, column=col_index, value=value)

        # Save the Excel file
        output_directory = r"output\itemsearch_output"
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)

        first4user = user_input.split(',')[0].strip()
        file_path = os.path.join(output_directory, f"{first4user}.xlsx")
        workbook.save(file_path)

        df = pd.read_excel(file_path)
        html_table = df.to_html(
        index=False,
        classes="table table-bordered table-striped",
        table_id="excel_table",
        escape=False  # Allow HTML in headers
    )
        return html_table, image_urls, user_input
    
    except pyodbc.Error as e:
        app.logger.error(f"Error connecting to SQL Server: {e}")

    finally:
        # Ensure that the connections are closed
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()

def get_file_path():
    return file_path

if __name__ == "__main__":
    user_input = input("Enter User Input: ")
    generate_excel(user_input)
