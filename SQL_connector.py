import pyodbc
import openpyxl
import os
import pandas as pd
from flask import current_app as app
import time

# SQL Server connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Fdc2022!!'
driver = 'ODBC Driver 17 for SQL Server'

def sort_filenames(file_names):
    """Sort filenames alphabetically. Modify if custom sorting is needed."""
    return sorted(file_names)

def generate_excel(user_input):
    global file_path
    try:
        # Normalize and split the user input
        user_input = ' '.join(user_input.lower().strip().split())
        input_parts = [part.strip() for part in user_input.split(",")]

        # Initialize SQL conditions for inventory query
        prod_cd_conditions = []
        alt_cd_conditions = []
        descrip_conditions = []

        # Build dynamic conditions based on input for inventory
        for part in input_parts:
            if "%" in part:
                prod_cd_conditions.append("i.PROD_CD LIKE ?")
                alt_cd_conditions.append("i.ALT_CD LIKE ?")
                descrip_conditions.append("i.DESCRIP LIKE ?")
            elif " " in part:
                prod_cd_conditions.append("i.PROD_CD = ?")
            else:
                prod_cd_conditions.append("i.PROD_CD LIKE ?")

        combined_condition_inventory = " OR ".join(prod_cd_conditions + alt_cd_conditions + descrip_conditions)

        # Prepare SQL query parameters for inventory
        inventory_query_params = []
        for part in input_parts:
            if "%" in part:
                inventory_query_params.extend([part, part, part])
            elif " " in part:
                inventory_query_params.append(part)
            else:
                inventory_query_params.append(f"{part}%")

        # Initialize SQL conditions for history query
        history_conditions = []
        history_query_params = []

        # Build dynamic conditions based on input for history
        for part in input_parts:
            if "%" in part:
                history_conditions.append("ol.prod_cd LIKE ?")
                history_query_params.append(part)
            else:
                history_conditions.append("ol.prod_cd LIKE ?")
                history_query_params.append(f"{part}%")

        combined_condition_history = " OR ".join(history_conditions) if history_conditions else "1=1"

        # Establish database connections
        conn_fdc = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
        )
        conn_oms_ef = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}'
        )

        # Query inventory data
        queries = []
        
        for conn, database_name in [(conn_fdc, 'omsdata_FDC'), (conn_oms_ef, 'oms_EF')]:
            cursor = conn.cursor()
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
                WHERE {combined_condition_inventory};
            """
            app.logger.debug(f"Executing inventory query on {database_name}: {query}")
            app.logger.debug(f"With parameters: {inventory_query_params}")
            cursor.execute(query, inventory_query_params)
            rows = cursor.fetchall()
            rows_with_database_name = [(database_name,) + tuple(row) for row in rows]
            queries.append(rows_with_database_name)

        combined_results = queries[0] + queries[1]

        # Process results to Excel
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        column_headers = ['DB', 'SKU', 'Style', 'Descrip', 'Stock', 'Sold', 'Avail',
                          '🚢', 'cost', 'MSRP', 'CaseQty', 'box_wt',
                          'pc_wt', 'color', 'len', 'wid', 'hei', 'update dt', 'create dt']
        for col_index, header in enumerate(column_headers, start=1):
            sheet.cell(row=1, column=col_index, value=header)

        for row_index, row in enumerate(combined_results, start=2):
            for col_index, value in enumerate(row, start=1):
                sheet.cell(row=row_index, column=col_index, value=value)

        # Save the Excel file
        output_directory = r"output\itemsearch_output"
        os.makedirs(output_directory, exist_ok=True)
        first4user = user_input.split(',')[0].strip()
        file_path = os.path.join(output_directory, f"{first4user}.xlsx")
        workbook.save(file_path)

        # Create HTML table
        df = pd.read_excel(file_path)
        html_table = df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="excel_table",
            escape=False
        )

        # History query
        history_query = f"""
            SELECT 
                ol.prod_cd,
                ol.ord_num,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_olg_dt,
                c.cus_nm AS cus_id,
                ol.order_qty,
                ol.unit_prs
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE {combined_condition_history}

        """
        app.logger.debug(f"Executing history query: {history_query}")
        app.logger.debug(f"With parameters: {history_query_params}")

        all_history_rows = []
        for conn, database_name in [(conn_fdc, 'omsdata_FDC'), (conn_oms_ef, 'oms_EF')]:
            cursor = conn.cursor()
            cursor.execute(history_query, history_query_params)
            rows = cursor.fetchall()
            updated_db_name = database_name.replace('omsdata_FDC', 'FDC').replace('oms_EF', 'E_FAS')
            rows_with_db = [(updated_db_name,) + tuple(row) for row in rows]
            all_history_rows.extend(rows_with_db)

        if all_history_rows:
            history_columns = ['Database'] + [desc[0] for desc in cursor.description]
            history_df = pd.DataFrame.from_records(all_history_rows, columns=history_columns)
            # Format numeric columns
            history_df['order_qty'] = pd.to_numeric(history_df['order_qty'], errors='coerce').fillna(0).astype(int)
            history_df['unit_prs'] = pd.to_numeric(history_df['unit_prs'], errors='coerce').round(2)

            # Optionally, rename columns for better display
            history_df.rename(columns={
                'prod_cd': 'Product Code',
                'formatted_olg_dt': 'Order Date',
                'ord_num': 'Order Number',
                'cus_id': 'Customer ID',
                'order_qty': 'Order Quantity',
                'unit_prs': 'Unit Price'
            }, inplace=True)

            # Create HTML table
            history_html_table = history_df.to_html(
                index=False,
                classes="table table-bordered table-striped",
                table_id="history_table",
                float_format="{:.2f}".format,
                escape=False
            )
        else:
            history_html_table = "<p>No item history found for the given input.</p>"
        purchase_html = generate_purchase_history(input_parts)


        return html_table, history_html_table,purchase_html, [], user_input  # image_urls is handled in Flask route

    except pyodbc.Error as e:
        app.logger.error(f"Error connecting to SQL Server: {e}")
        return None, None, [], user_input
    finally:
        if 'conn_fdc' in locals() and conn_fdc:
            conn_fdc.close()
        if 'conn_oms_ef' in locals() and conn_oms_ef:
            conn_oms_ef.close()

def get_file_path():
    return file_path

def generate_purchase_history(input_parts):
    purchase_conditions = []
    purchase_params = []
    
    for part in input_parts:
        if "%" in part:
            purchase_conditions.append("pl.prod_cd LIKE ?")
            purchase_params.append(part)
        else:
            purchase_conditions.append("pl.prod_cd LIKE ?")
            purchase_params.append(f"{part}%")

    combined_condition_purchase = " OR ".join(purchase_conditions) if purchase_conditions else "1=1"

    purchase_query = f"""
        SELECT 
            pl.prod_cd,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', pl.log_date) - 81088, '20230101')) AS VARCHAR) AS log_date,
            pl.pur_num,
            pl.hdl_chg
        FROM [omsdata_FDC].[dbo].[plog] pl
        WHERE {combined_condition_purchase}
        AND pl.hdl_chg > 0
        ORDER BY pl.log_date DESC
    """

    purchase_rows = []
    conn_fdc = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}')
    cursor = conn_fdc.cursor()
    cursor.execute(purchase_query, purchase_params)
    rows = cursor.fetchall()
    
    if rows:
        purchase_df = pd.DataFrame.from_records(rows, columns=['SKU', 'PO DATE', 'PO/Container', 'Quantity'])
        # Convert Quantity column to integer
        purchase_df['Quantity'] = pd.to_numeric(purchase_df['Quantity'], errors='coerce').fillna(0).astype(int)
        purchase_html = purchase_df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="purchase_table"  # Removed float_format since we're using integers
        )
    else:
        purchase_html = "<p>No purchase history found.</p>"
        
    return purchase_html