import pyodbc
import openpyxl
import os
import datetime
import pandas as pd
from PIL import Image

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
        user_input = user_input.lower().strip()

        # Split user_input into parts, trimming spaces around each part
        input_parts = [part.strip() for part in user_input.split(",")]

        # Generate the conditions for PROD_CD with and without colors
        prod_cd_conditions = []
        for part in input_parts:
            if " " in part:  # Handle cases like 'en2209 bk'
                prod_cd_conditions.append(f"i.PROD_CD = '{part}'")
            else:  # Handle cases like 'en2208' without color
                prod_cd_conditions.append(f"i.PROD_CD LIKE '{part}%'")

        combined_condition = " OR ".join(prod_cd_conditions)

        # Debug: Print the generated SQL WHERE condition
        print(f"Generated SQL WHERE condition: {combined_condition}")

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
        column_headers = ['Database', 'SKU', 'Style', 'Container', 'Stock', 'Reserve', 'Available',
                          'Ocean', 'cost', 'MSRP', 'QTY_in_box', 'box_wt(lb)',
                          'pc_wt(lb)', 'color', 'length', 'width', 'height', "update_dt", "create_dt"]

        # Write the column headers to the Excel sheet
        for col_index, header in enumerate(column_headers, start=1):
            sheet.cell(row=1, column=col_index, value=header)

        # Write the data to the Excel sheet
        for row_index, row in enumerate(combined_results, start=2):
            for col_index, value in enumerate(row, start=1):
                if col_index == 3:  # Check if it's the column with SKU_no_color
                    hyperlink_value = value.strip()  # Remove trailing spaces

                    # Remove the color part from the PROD_CD for image URL generation
                    base_sku = hyperlink_value.split()[0]

                    local_resized_image_url = f"ftp/{base_sku}.jpg"
                    backup_original_image_url = f"/ftp2/{base_sku}.jpg"

                    # Add original image URL if not already added
                    if local_resized_image_url not in image_urls:
                        image_urls.append(local_resized_image_url)

                    # Check if the image exists in ftp directory
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
        html_table = df.to_html(index=False, classes="table table-bordered table-striped", table_id="excel_table")

        return html_table, image_urls, user_input

    except pyodbc.Error as e:
        print("Error connecting to SQL Server:", e)

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
