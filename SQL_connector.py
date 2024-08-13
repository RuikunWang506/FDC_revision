import pyodbc
import openpyxl
import sys
import datetime
import os
import pandas as pd
from PIL import Image

# Replace these values with your actual SQL Server connection details for omsdata_FDC database
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Qwer1234##'

driver = 'ODBC Driver 17 for SQL Server'  # Replace 'xx' with the appropriate version number

def generate_excel(user_input):
    global file_path
    image_urls = []
    try:
        # Connect to the SQL Server databases
        conn_fdc = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
        )

        conn_oms_ef = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}'
        )

        # Create cursor objects to execute SQL queries in both databases
        cursor_fdc = conn_fdc.cursor()
        cursor_oms_ef = conn_oms_ef.cursor()

        # Normalize the input to lowercase
        user_input = user_input.lower()

        # Execute the same query in both databases and fetch the results
        queries = []
        for conn, cursor, database_name in [(conn_fdc, cursor_fdc, 'omsdata_FDC'), (conn_oms_ef, cursor_oms_ef, 'oms_EF')]:
            if user_input.endswith(","):
                user_input = user_input[:-1]

            mylist = "'," + str(user_input) + ",'"

            query = """\
                DECLARE @AltCDList NVARCHAR(MAX) = ?;
                DECLARE @ProductList NVARCHAR(MAX);

                -- Add a wildcard to the AltCDList
                SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';

                -- Create a CTE to split the comma-separated values in @AltCDList
                WITH SplitAltCD AS (
                    SELECT value
                    FROM dbo.SplitString(@AltCDList, ',')
                ),
                -- Create a CTE to generate the list of product codes based on AltCDList
                ProductListCTE AS (
                    SELECT PROD_CD
                    FROM dbo.inv
                    WHERE EXISTS (SELECT 1 FROM SplitAltCD WHERE alt_CD LIKE value)
                ),
                -- Create a CTE to split the comma-separated values in @ProductList
                SplitValues AS (
                    SELECT value
                    FROM dbo.SplitString(@ProductList, ',')
                ),
                -- CTE to get formatted create date from wm_skufile
                FormattedCreateDateCTE AS (
                    SELECT 
                        prod_cd,
                        RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                        CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
                    FROM wm_skufile
                )

                -- Use the CTEs to perform the desired query
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
                LEFT JOIN FormattedCreateDateCTE AS fcd ON i.PROD_CD = fcd.prod_cd
                WHERE i.PROD_CD IN (SELECT PROD_CD FROM ProductListCTE);

            """

            cursor.execute(query, mylist)
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
                          'Ocean', 'cost', 'MSRP', 'QTY_box', 'box_wt(lb)',
                          'pc_wt(lb)', 'color', 'length', 'width', 'height', "update_dt", "create_dt"]

        # Write the column headers to the Excel sheet
        for col_index, header in enumerate(column_headers, start=1):
            cell = sheet.cell(row=1, column=col_index, value=header)

        # Write the data to the Excel sheet
        for row_index, row in enumerate(combined_results, start=2):
            for col_index, value in enumerate(row, start=1):
                if col_index == 3:  # Check if it's the column with SKU_no_color
                    hyperlink_value = value.strip()  # Remove trailing spaces
                    local_resized_image_url = f"ftp/{hyperlink_value}.jpg"
                    backup_original_image_url = f"/ftp2/{hyperlink_value}.jpg"

                    # Add original image URL if not already added
                    if local_resized_image_url not in image_urls:
                        image_urls.append(local_resized_image_url)

                    # Check if the image exists in ftp directory
                    if os.path.exists(os.path.join('ftp', f'{hyperlink_value}.jpg')):
                        value = f'<a href="{local_resized_image_url}">{value}</a>'
                    else:
                        value = f'<a href="{backup_original_image_url}">{value}</a>'

                # Write data to the Excel sheet
                cell = sheet.cell(row=row_index, column=col_index, value=value)

        # Save the Excel file
        output_directory = r"output\itemsearch_output"
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)

        current_time = datetime.datetime.now()
        first4user = user_input.split(',')[0].strip()
        file_path = os.path.join(output_directory, f"{first4user}.xlsx")
        workbook.save(file_path)

        df = pd.read_excel(file_path)
        html_table = df.to_html(index=False, classes="table table-bordered table-striped", table_id="excel_table")

        return html_table, image_urls,user_input

    except pyodbc.Error as e:
        print("Error connecting to SQL Server:", e)

def get_file_path():
    return file_path

if __name__ == "__main__":
    user_input = input("Enter User Input: ")
    generate_excel(user_input)
