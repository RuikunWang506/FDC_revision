import pyodbc
import openpyxl
import os
import pandas as pd
from flask import current_app as app
import time
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

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
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        conn_oms_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
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
                    CAST(ISNULL(id.in_stock, 0) AS INT) AS in_stock,
                    CAST(ISNULL(id.ORDER_QTY, 0) AS INT) AS some_one_ordered,
                    CAST(ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0) AS INT) AS Available_now,
                    CAST(ISNULL(id.on_order_qty, 0) AS INT) AS incoming_quantity,
                    i.SALES_COST AS bag_cost,
                    i.RETAIL_PRS AS selling_price,
                    CAST(ISNULL(i.PC_CASE, 0) AS INT) AS QTY_in_box,
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
                    FROM wm_skufile WHERE wm_skufile.WHS_NUM = '01'
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

        # Return the Excel file as bytes instead of saving to disk
        from io import BytesIO
        bio = BytesIO()
        workbook.save(bio)
        bio.seek(0)
        excel_data = bio.getvalue()

        # Create HTML table from the combined results
        column_headers = ['DB', 'SKU', 'Style', 'Descrip', 'Stock', 'Sold', 'Avail',
                          '🚢', 'cost', 'MSRP', 'CaseQty', 'box_wt',
                          'pc_wt', 'color', 'len', 'wid', 'hei', 'update dt', 'create dt']
        df = pd.DataFrame(combined_results, columns=column_headers)
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
            
        # Purchase history
        purchase_html = generate_purchase_history(input_parts)
        
        # NEW: Add inventory adjustments
        adjustment_html = generate_inventory_adjustments(input_parts)

        # Return all HTML tables and Excel data, including the new adjustment_html
        return html_table, history_html_table, purchase_html, adjustment_html, excel_data, user_input


    except pyodbc.Error as e:
        app.logger.error(f"Error connecting to SQL Server: {e}")
        return None, None, None, None, [], user_input
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
        FROM [dbo].[plog] pl
        WHERE {combined_condition_purchase}
        AND pl.hdl_chg > 0
        ORDER BY pl.log_date DESC
    """

    all_purchase_rows = []
    
    # Connect to both databases and fetch data
    for db_name, db in [(sql_server_config["database_fdc"], 'omsdata_FDC'), (sql_server_config["database_oms_ef"], 'oms_EF')]:
        try:
            conn = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={db_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
            cursor = conn.cursor()
            cursor.execute(purchase_query, purchase_params)
            rows = cursor.fetchall()
            
            # Add database source column
            display_db_name = 'FDC' if db == 'omsdata_FDC' else 'E_FAS'
            rows_with_db = [(display_db_name,) + tuple(row) for row in rows]
            all_purchase_rows.extend(rows_with_db)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error connecting to {db}: {e}")
    
    if all_purchase_rows:
        purchase_df = pd.DataFrame.from_records(all_purchase_rows, columns=['Database', 'SKU', 'PO DATE', 'PO/Container', 'Quantity'])
        # Convert Quantity column to integer
        purchase_df['Quantity'] = pd.to_numeric(purchase_df['Quantity'], errors='coerce').fillna(0).astype(int)
        purchase_html = purchase_df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="purchase_table"
        )
    else:
        purchase_html = "<p>No purchase history found.</p>"
        
    return purchase_html

def generate_inventory_adjustments(input_parts):
    adjustment_conditions = []
    adjustment_params = []
    
    for part in input_parts:
        if "%" in part:
            adjustment_conditions.append("prod_cd LIKE ?")
            adjustment_params.append(part)
        else:
            adjustment_conditions.append("prod_cd LIKE ?")
            adjustment_params.append(f"{part}%")

    combined_condition_adjustment = " OR ".join(adjustment_conditions) if adjustment_conditions else "1=1"

    adjustment_query = f"""
        SELECT 
            prod_cd,
            log_num,
            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', log_dt) - 81088, '20230101')) AS VARCHAR) AS formatted_log_dt,
            log_by,
            log_qty
        FROM [dbo].[wm_log]
        WHERE ({combined_condition_adjustment})
        AND type_cd = 'adj'
        ORDER BY log_dt DESC
    """

    all_adjustment_rows = []
    
    # Connect to both databases and fetch data
    for db_name, db in [(sql_server_config["database_fdc"], 'omsdata_FDC'), (sql_server_config["database_oms_ef"], 'oms_EF')]:
        try:
            conn = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={db_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
            cursor = conn.cursor()
            cursor.execute(adjustment_query, adjustment_params)
            rows = cursor.fetchall()
            
            # Add database source column
            display_db_name = 'FDC' if db == 'omsdata_FDC' else 'E_FAS'
            rows_with_db = [(display_db_name,) + tuple(row) for row in rows]
            all_adjustment_rows.extend(rows_with_db)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error connecting to {db}: {e}")
    
    if all_adjustment_rows:
        adjustment_df = pd.DataFrame.from_records(all_adjustment_rows, 
                                                 columns=['Database', 'SKU', 'Log Number', 'Date', 'Adjusted By', 'Quantity'])
        
        # Convert Quantity column to integer
        adjustment_df['Quantity'] = pd.to_numeric(adjustment_df['Quantity'], errors='coerce').fillna(0).astype(int)
        
        adjustment_html = adjustment_df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="adjustment_table"
        )
    else:
        adjustment_html = "<p>No inventory adjustments found.</p>"
        
    return adjustment_html

def get_last_20_orders():
    """Get the last 20 order numbers from both FDC and E-Fashion databases"""
    return get_orders_by_criteria(num_orders=20, database_filter='both')

def debug_date_format(days_back=1):
    """Debug function to understand the date format in your database"""
    try:
        driver = sql_server_config["driver"]
        server = sql_server_config["server"]
        username = sql_server_config["username"]
        password = sql_server_config["password"]
        conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_fdc"]};UID={username};PWD={password}')
        cursor = conn.cursor()
        
        # Get some recent orders with their raw ord_dt values and converted dates
        debug_query = """
            SELECT TOP 5
                o.ord_num,
                o.ord_dt as raw_ord_dt,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_date,
                GETDATE() as current_datetime,
                DATEDIFF(day, '19000101', GETDATE()) + 81088 as today_in_db_format,
                DATEDIFF(day, '19000101', DATEADD(day, -1, GETDATE())) + 81088 as yesterday_in_db_format
            FROM orders o
            ORDER BY o.ord_dt DESC
        """
        
        cursor.execute(debug_query)
        rows = cursor.fetchall()
        
        app.logger.debug("Date format debug results:")
        for row in rows:
            app.logger.debug(f"Order: {row[0]}, Raw ord_dt: {row[1]}, Formatted: {row[2]}")
        
        if rows:
            app.logger.debug(f"Today in DB format: {rows[0][4]}")
            app.logger.debug(f"Yesterday in DB format: {rows[0][5]}")
        
        conn.close()
        return rows
    except Exception as e:
        app.logger.error(f"Debug query failed: {e}")
        return []

def get_orders_by_criteria(num_orders=20, database_filter='both', days_back=None, since_date=None, user=None):
    """Get orders based on various criteria with proper date filtering"""
    all_orders = []
    
    # Database connection variables
    driver = sql_server_config["driver"]
    server = sql_server_config["server"]
    username = sql_server_config["username"]
    password = sql_server_config["password"]
    
    # Determine which databases to query
    databases_to_query = []
    if database_filter in ['both', 'fdc']:
        databases_to_query.append((sql_server_config["database_fdc"], 'FDC'))
    if database_filter in ['both', 'efashion']:
        databases_to_query.append((sql_server_config["database_oms_ef"], 'E_FAS'))
    
    # Build the WHERE clause based on criteria
    where_clause = ""
    top_clause = ""
    
    if days_back is not None:
        if days_back == 0:
            # Today only - get orders from start of today to end of today
            where_clause = f"""
            WHERE o.ord_dt >= (
                DATEDIFF(day, '20230101', CAST(GETDATE() AS DATE)) + 81088
            )
            AND o.ord_dt < (
                DATEDIFF(day, '20230101', DATEADD(day, 1, CAST(GETDATE() AS DATE))) + 81088
            )
            """
        else:
            # Last X days: days_back=1 means today + yesterday, days_back=2 means today + yesterday + day before, etc.
            where_clause = f"""
            WHERE o.ord_dt >= (
                DATEDIFF(day, '20230101', DATEADD(day, -{days_back}, CAST(GETDATE() AS DATE))) + 81088
            )
            """

    elif since_date:
        try:
            from datetime import datetime
            parsed_date = datetime.strptime(since_date, '%m-%d-%Y')
            sql_date = parsed_date.strftime('%Y-%m-%d')
            
            where_clause = f"""
            WHERE o.ord_dt >= (
                DATEDIFF(day, '20230101', '{sql_date}') + 81088
            )
            """
        except ValueError as e:
            top_clause = f"TOP {num_orders}"
            app.logger.info(f"Invalid date format: {since_date}, error: {e}, falling back to TOP {num_orders}")
    else:
        top_clause = f"TOP {num_orders}"
    
    # Connect to databases and fetch data
    for db_name, db_display in databases_to_query:
        try:
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={db_name};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            # Debug the new date calculation
            if days_back or since_date:
                debug_query = f"""
                    SELECT 
                        GETDATE() as current_datetime,
                        DATEDIFF(day, '20230101', GETDATE()) + 81088 as today_in_new_format,
                        DATEDIFF(day, '20230101', DATEADD(day, -{days_back if days_back else 0}, GETDATE())) + 81088 as cutoff_in_new_format,
                        MAX(o.ord_dt) as latest_order_date,
                        COUNT(*) as total_orders_in_db
                    FROM orders o
                """
                cursor.execute(debug_query)
                debug_result = cursor.fetchone()
                
                # Check how many orders match our criteria
                count_query = f"""
                    SELECT COUNT(*) as matching_orders
                    FROM orders o
                    {where_clause}
                """
                cursor.execute(count_query)
                count_result = cursor.fetchone()
            
            # Main query to get orders
            query = f"""
                SELECT {top_clause}
                    o.ord_num,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_ord_dt,
                    c.cus_nm AS customer_name,
                    o.ord_amt AS order_amount,
                    o.handl_fee AS handling_fee,
                    o.misc_chg AS misc_charge,
                    (o.ord_amt + o.handl_fee + o.misc_chg) AS order_total,
                    COUNT(ol.prod_cd) AS total_items,
                    SUM(ISNULL(ol.order_qty, 0)) AS total_quantity
                FROM orders o
                JOIN customer c ON o.cus_id = c.cus_id
                LEFT JOIN ord_log ol ON o.ord_num = ol.ord_num
                {where_clause}
                GROUP BY o.ord_num, o.ord_dt, c.cus_nm, o.ord_amt, o.handl_fee, o.misc_chg
                ORDER BY o.ord_dt DESC
            """
            
            cursor.execute(query)
            rows = cursor.fetchall()
            
            # Add database source column
            rows_with_db = [(db_display,) + tuple(row) for row in rows]
            all_orders.extend(rows_with_db)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.info(f"SQL Error in {db_name}: {e}")
        except Exception as e:
            app.logger.info(f"General error in {db_name}: {e}")
    
    return all_orders


def get_order_skus_for_orders(orders_data):
    """Get distinct (database, order number, SKU) rows for the selected orders."""
    sku_rows = []

    if not orders_data:
        return sku_rows

    fdc_orders = [order[1] for order in orders_data if order[0] == 'FDC']
    efas_orders = [order[1] for order in orders_data if order[0] == 'E_FAS']

    driver = sql_server_config["driver"]
    server = sql_server_config["server"]
    username = sql_server_config["username"]
    password = sql_server_config["password"]

    if fdc_orders:
        try:
            conn = pyodbc.connect(
                f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_fdc"]};UID={username};PWD={password}'
            )
            cursor = conn.cursor()

            placeholders = ','.join(['?' for _ in fdc_orders])
            query = f"""
                SELECT DISTINCT ord_num, prod_cd
                FROM ord_log
                WHERE ord_num IN ({placeholders})
                  AND prod_cd IS NOT NULL
            """
            cursor.execute(query, fdc_orders)
            rows = cursor.fetchall()
            sku_rows.extend([('FDC', row[0], row[1]) for row in rows])

            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order SKUs from FDC: {e}")

    if efas_orders:
        try:
            conn = pyodbc.connect(
                f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_oms_ef"]};UID={username};PWD={password}'
            )
            cursor = conn.cursor()

            placeholders = ','.join(['?' for _ in efas_orders])
            query = f"""
                SELECT DISTINCT ord_num, prod_cd
                FROM ord_log
                WHERE ord_num IN ({placeholders})
                  AND prod_cd IS NOT NULL
            """
            cursor.execute(query, efas_orders)
            rows = cursor.fetchall()
            sku_rows.extend([('E_FAS', row[0], row[1]) for row in rows])

            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order SKUs from E-Fashion: {e}")

    return sku_rows


def get_order_sku_quantities_for_orders(orders_data):
    """Get summed quantity and revenue by (database, order number, SKU) for selected orders."""
    sku_qty_rows = []

    if not orders_data:
        return sku_qty_rows

    fdc_orders = [order[1] for order in orders_data if order[0] == 'FDC']
    efas_orders = [order[1] for order in orders_data if order[0] == 'E_FAS']

    driver = sql_server_config["driver"]
    server = sql_server_config["server"]
    username = sql_server_config["username"]
    password = sql_server_config["password"]

    if fdc_orders:
        try:
            conn = pyodbc.connect(
                f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_fdc"]};UID={username};PWD={password}'
            )
            cursor = conn.cursor()

            placeholders = ','.join(['?' for _ in fdc_orders])
            query = f"""
                SELECT
                    ord_num,
                    prod_cd,
                    SUM(ISNULL(order_qty, 0)) AS total_qty,
                    SUM(ISNULL(order_qty, 0) * ISNULL(unit_prs, 0)) AS total_revenue
                FROM ord_log
                WHERE ord_num IN ({placeholders})
                  AND prod_cd IS NOT NULL
                GROUP BY ord_num, prod_cd
            """
            cursor.execute(query, fdc_orders)
            rows = cursor.fetchall()
            sku_qty_rows.extend([('FDC', row[0], row[1], row[2], row[3]) for row in rows])

            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order SKU quantities from FDC: {e}")

    if efas_orders:
        try:
            conn = pyodbc.connect(
                f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_oms_ef"]};UID={username};PWD={password}'
            )
            cursor = conn.cursor()

            placeholders = ','.join(['?' for _ in efas_orders])
            query = f"""
                SELECT
                    ord_num,
                    prod_cd,
                    SUM(ISNULL(order_qty, 0)) AS total_qty,
                    SUM(ISNULL(order_qty, 0) * ISNULL(unit_prs, 0)) AS total_revenue
                FROM ord_log
                WHERE ord_num IN ({placeholders})
                  AND prod_cd IS NOT NULL
                GROUP BY ord_num, prod_cd
            """
            cursor.execute(query, efas_orders)
            rows = cursor.fetchall()
            sku_qty_rows.extend([('E_FAS', row[0], row[1], row[2], row[3]) for row in rows])

            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order SKU quantities from E-Fashion: {e}")

    return sku_qty_rows


def get_sold_out_skus_from_orders(orders_data):
    """Get SKUs from the provided orders that are sold out (combined available quantity is 0 or negative)"""
    if not orders_data:
        return []
    
    # Extract order numbers from the orders data
    order_numbers = []
    order_db_map = {}  # Map order numbers to their database
    
    for order_row in orders_data:
        db_name = order_row[0]  # Database name (FDC/E_FAS)
        order_num = order_row[1]  # Order number
        order_numbers.append(order_num)
        order_db_map[order_num] = db_name
    
    all_sku_data = []
    
    # Connect to both databases and fetch data
    for db_name, db_display in [(sql_server_config["database_fdc"], 'FDC'), (sql_server_config["database_oms_ef"], 'E_FAS')]:
        try:
            driver = sql_server_config["driver"]
            server = sql_server_config["server"]
            username = sql_server_config["username"]
            password = sql_server_config["password"]
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={db_name};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            # Get order numbers that belong to this database
            db_order_numbers = [ord_num for ord_num, db in order_db_map.items() if db == db_display]
            
            if not db_order_numbers:
                conn.close()
                continue
            
            # Create placeholders for the IN clause
            placeholders = ','.join(['?' for _ in db_order_numbers])
            
            # Query to get SKU data from the specific orders (don't filter by availability yet)
            query = f"""
                SELECT DISTINCT
                    ol.ord_num,
                    ol.prod_cd AS SKU,
                    i.ALT_CD AS Style,
                    i.DESCRIP AS Description,
                    ISNULL(id.in_stock, 0) AS Stock,
                    ISNULL(id.ORDER_QTY, 0) AS Ordered,
                    ISNULL((id.in_stock - id.order_qty), 0) AS Available,
                    ISNULL(id.on_order_qty, 0) AS Incoming,
                    i.RETAIL_PRS AS Price,
                    ol.order_qty AS Qty_In_Order
                FROM ord_log ol
                JOIN dbo.inv i ON ol.prod_cd = i.PROD_CD
                LEFT JOIN dbo.inv_data id ON i.PROD_CD = id.Prod_cd
                WHERE ol.ord_num IN ({placeholders})
                ORDER BY ol.ord_num, ol.prod_cd
            """
            
            cursor.execute(query, db_order_numbers)
            rows = cursor.fetchall()
            
            # Add database source column
            rows_with_db = [(db_display,) + tuple(row) for row in rows]
            all_sku_data.extend(rows_with_db)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error connecting to {db_name}: {e}")
    
    # Now combine data by SKU and filter for sold out items
    if not all_sku_data:
        return []
    
    # Convert to DataFrame for easier processing
    df = pd.DataFrame.from_records(all_sku_data,
                                 columns=['Database', 'Order Number', 'SKU', 'Style', 'Description', 'Stock', 'Ordered', 'Available', 'Incoming', 'Price', 'Qty in Order'])
    
    # Convert numeric columns
    numeric_cols = ['Stock', 'Ordered', 'Available', 'Incoming', 'Price', 'Qty in Order']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    # Group by SKU and sum the availability across databases
    sku_summary = df.groupby('SKU').agg({
        'Available': 'sum',
        'Stock': 'sum',
        'Ordered': 'sum',
        'Incoming': 'sum'
    }).reset_index()
    
    # Get SKUs that are actually sold out (combined available <= 0)
    sold_out_skus = sku_summary[sku_summary['Available'] <= 0]['SKU'].tolist()
    
    # Filter original data to only include sold out SKUs
    sold_out_data = df[df['SKU'].isin(sold_out_skus)]
    
    # Convert back to list of tuples for consistency
    return sold_out_data.values.tolist()

def get_all_skus_from_orders(orders_data):
    """Get all unique SKUs from the given orders across both databases"""
    all_skus = set()
    
    if not orders_data:
        return list(all_skus)
    
    # Extract order numbers by database
    fdc_orders = [order[1] for order in orders_data if order[0] == 'FDC']
    efas_orders = [order[1] for order in orders_data if order[0] == 'E_FAS']
    
    driver = sql_server_config["driver"]
    server = sql_server_config["server"]
    username = sql_server_config["username"]
    password = sql_server_config["password"]
    
    # Get SKUs from FDC orders
    if fdc_orders:
        try:
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_fdc"]};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            placeholders = ','.join(['?' for _ in fdc_orders])
            query = f"SELECT DISTINCT prod_cd FROM ord_log WHERE ord_num IN ({placeholders})"
            cursor.execute(query, fdc_orders)
            
            fdc_skus = [row[0] for row in cursor.fetchall()]
            all_skus.update(fdc_skus)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting SKUs from FDC orders: {e}")
    
    # Get SKUs from E-Fashion orders
    if efas_orders:
        try:
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_oms_ef"]};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            placeholders = ','.join(['?' for _ in efas_orders])
            query = f"SELECT DISTINCT prod_cd FROM ord_log WHERE ord_num IN ({placeholders})"
            cursor.execute(query, efas_orders)
            
            efas_skus = [row[0] for row in cursor.fetchall()]
            all_skus.update(efas_skus)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting SKUs from E-Fashion orders: {e}")
    
    return list(all_skus)

def get_sku_inventory_from_both_databases(sku_list, user=None):
    """Get inventory information for a list of SKUs from both FDC and E-Fashion databases"""
    all_inventory = []
    
    if not sku_list:
        return all_inventory
    
    # Filter out None values and convert to strings
    sku_list = [str(sku) for sku in sku_list if sku is not None]
    
    if not sku_list:
        return all_inventory
    
    # Log the method requested from audit.html
    import flask
    search_type = flask.request.form.get('search_type', 'orders') if flask.has_request_context() else 'orders'
    database_filter = flask.request.form.get('database_filter', 'both') if flask.has_request_context() else 'both'
    num_orders = flask.request.form.get('num_orders') if flask.has_request_context() else None
    days_back = flask.request.form.get('days_back') if flask.has_request_context() else None
    since_date = flask.request.form.get('since_date') if flask.has_request_context() else None
    username = user if user else "anonymous"
    # Build user input summary
    user_input_summary = ""
    if search_type == "days" and days_back:
        user_input_summary = f"{database_filter} database ***{days_back}*** days "
    elif search_type == "since" and since_date:
        user_input_summary = f"{database_filter} database since ***{since_date}***  "
    elif search_type == "orders" and num_orders:
        user_input_summary = f"orders {database_filter} database last ***{num_orders}*** "
    else:
        user_input_summary = f"{database_filter} database***{search_type}*** "
    app.logger.info(f"***{username.upper()}***__@@@Requested Audit Info for@@@:__ {user_input_summary}")
    
    # Process SKUs in batches to avoid parameter limit (SQL Server limit is ~2100 parameters)
    batch_size = 1000  # Conservative batch size for inventory queries
    
    # Connect to both databases and fetch inventory data
    for db_name, db_display in [(sql_server_config["database_fdc"], 'FDC'), (sql_server_config["database_oms_ef"], 'E_FAS')]:
        try:
            driver = sql_server_config["driver"]
            server = sql_server_config["server"]
            username = sql_server_config["username"]
            password = sql_server_config["password"]
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={db_name};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            for i in range(0, len(sku_list), batch_size):
                sku_batch = sku_list[i:i + batch_size]
                placeholders = ','.join(['?' for _ in sku_batch])
                
                # Query to get inventory information for SKU batch
                query = f"""
                    SELECT 
                        i.PROD_CD AS SKU,
                        i.ALT_CD AS Style,
                        i.DESCRIP AS Description,
                        ISNULL(id.in_stock, 0) AS Stock,
                        ISNULL(id.ORDER_QTY, 0) AS Ordered,
                        ISNULL((id.in_stock - id.order_qty), 0) AS Available,
                        ISNULL(id.on_order_qty, 0) AS Incoming,
                        ISNULL(i.RETAIL_PRS, 0) AS Price
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE i.PROD_CD IN ({placeholders})
                """
                
                app.logger.debug(f"Executing inventory query on {db_display} batch {i//batch_size + 1} with {len(sku_batch)} SKUs")
                cursor.execute(query, sku_batch)
                rows = cursor.fetchall()
                
                # Add database source column
                rows_with_db = [(db_display,) + tuple(row) for row in rows]
                all_inventory.extend(rows_with_db)
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error connecting to {db_name}: {e}")
    
    return all_inventory

def get_sku_order_quantities(orders_data, sku_list, user=None):
    """Get the quantities ordered for each SKU in the given orders"""
    sku_order_data = []
    
    if not orders_data or not sku_list:
        return sku_order_data
    
    # Database connection variables
    driver = sql_server_config["driver"]
    server = sql_server_config["server"]
    username = sql_server_config["username"]
    password = sql_server_config["password"]
    
    # Filter out None values and convert to strings
    sku_list = [str(sku) for sku in sku_list if sku is not None]
    
    if not sku_list:
        return sku_order_data
    
    # Extract order numbers by database
    fdc_orders = [order[1] for order in orders_data if order[0] == 'FDC']
    efas_orders = [order[1] for order in orders_data if order[0] == 'E_FAS']
        
    # Process SKUs in batches to avoid parameter limit (SQL Server limit is ~2100 parameters)
    batch_size = 500  # Safe batch size
    
    # Get order quantities from FDC
    if fdc_orders:
        try:
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_fdc"]};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            order_placeholders = ','.join(['?' for _ in fdc_orders])
            
            for i in range(0, len(sku_list), batch_size):
                sku_batch = sku_list[i:i + batch_size]
                sku_placeholders = ','.join(['?' for _ in sku_batch])
                
                query = f"""
                    SELECT ord_num, prod_cd, order_qty 
                    FROM ord_log 
                    WHERE ord_num IN ({order_placeholders}) 
                    AND prod_cd IN ({sku_placeholders})
                """
                
                app.logger.debug(f"Executing FDC order quantities query batch {i//batch_size + 1} with {len(fdc_orders)} orders and {len(sku_batch)} SKUs")
                cursor.execute(query, fdc_orders + sku_batch)
                rows = cursor.fetchall()
                
                for row in rows:
                    sku_order_data.append(('FDC', row[0], row[1], row[2]))
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order quantities from FDC: {e}")
    
    # Get order quantities from E-Fashion
    if efas_orders:
        try:
            conn = pyodbc.connect(f'DRIVER={driver};SERVER={server};DATABASE={sql_server_config["database_oms_ef"]};UID={username};PWD={password}')
            cursor = conn.cursor()
            
            order_placeholders = ','.join(['?' for _ in efas_orders])
            
            for i in range(0, len(sku_list), batch_size):
                sku_batch = sku_list[i:i + batch_size]
                sku_placeholders = ','.join(['?' for _ in sku_batch])
                
                query = f"""
                    SELECT ord_num, prod_cd, order_qty 
                    FROM ord_log 
                    WHERE ord_num IN ({order_placeholders}) 
                    AND prod_cd IN ({sku_placeholders})
                """
                
                app.logger.debug(f"Executing E-FAS order quantities query batch {i//batch_size + 1} with {len(efas_orders)} orders and {len(sku_batch)} SKUs")
                cursor.execute(query, efas_orders + sku_batch)
                rows = cursor.fetchall()
                
                for row in rows:
                    sku_order_data.append(('E_FAS', row[0], row[1], row[2]))
            
            conn.close()
        except pyodbc.Error as e:
            app.logger.error(f"Error getting order quantities from E-Fashion: {e}")
    
    return sku_order_data