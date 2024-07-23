import pyodbc
import pandas as pd
import os

# Replace these values with your actual SQL Server connection details for omsdata_FDC and oms_EF databases
server = "192.168.254.204"
username = "tristen"
password = 'Qwer1234##'

driver = 'ODBC Driver 17 for SQL Server'  # Replace 'xx' with the appropriate version number

def generate_order(order_number):
    global file_path
    try:
        # Determine the database and cursor based on the length of order_number
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            print("Invalid input length. Please provide a 4 or 5-digit input.")
            return None, None  # Return None for both values in case of an error

        conn = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_name};UID={username};PWD={password}'
        )

        # Create a cursor object to execute the SQL query
        cursor = conn.cursor()

        # Define the SQL query (same as in your original code)
        query = """\
           WITH OrderNum AS (
                SELECT ? AS ord_num
            ),
            OrderInfo AS (
                SELECT O.ORD_NUM, O.CUS_ID, O.COMM_LN, O.UT_DESC, O.PROD_CD, O.ORDER_QTY, O.UNIT_PRS, O.PC_UNIT, C.CUS_NM
                FROM DBO.ORD_LOG O
                INNER JOIN DBO.CUSTOMER C ON O.CUS_ID = C.CUS_ID
                WHERE O.ORD_NUM = (SELECT ord_num FROM OrderNum)
            ),
            OrderDates AS (
                SELECT 
                    ord_num,
                    ord_dt AS order_date,
                    ship_dt AS ship_date,
                    can_dt AS cancel_date,
                    PO_NUM
                FROM dbo.orders
                WHERE ord_num = (SELECT ord_num FROM OrderNum)
            )
            SELECT DISTINCT
                O.ORD_NUM AS order#, 
                O.CUS_NM AS Customer, 
                O.CUS_ID AS CUS#, 
                OD.PO_NUM AS PO#, 
                O.COMM_LN AS line, 
                O.PROD_CD AS SKU, 
                O.UT_DESC AS Descrip, 
                CAST(O.ORDER_QTY AS DECIMAL(10,0)) AS Qty, 
                CAST(O.UNIT_PRS AS DECIMAL (10,2)) AS price, 
                CAST(O.PC_UNIT AS DECIMAL(10,0)) AS per_box,
                CASE 
                    WHEN N.NOTES IS NOT NULL THEN SUBSTRING(N.NOTES, CHARINDEX('\r\n', N.NOTES) + 2, LEN(N.NOTES))
                    ELSE ''
                END AS Location,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR) AS order_date,

                            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR) AS ship_date,

                            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR) AS cancel_date
            FROM OrderInfo O
            LEFT JOIN dbo.ntfile N ON O.ORD_NUM = N.nt_num
            INNER JOIN OrderDates OD ON O.ORD_NUM = OD.ord_num;




            """
        
        # Create a Pandas DataFrame from the query results
        df = pd.read_sql_query(query, conn, params=(order_number,))

        # Close the cursor and connection
        cursor.close()
        conn.close()

        # Save the DataFrame to an Excel file
        output_directory = "output\ordersearch_output"
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)

        file_path = os.path.join(output_directory, f"{order_number}.xlsx")
        df.to_excel(file_path, index=False)

        # Prepare the HTML content to display the Excel
        excel_html = df.to_html(classes='table table-bordered table-striped', escape=False)
        return excel_html

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return None

