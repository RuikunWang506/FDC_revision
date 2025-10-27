import pyodbc
import pandas as pd
import os
from py.utilities.setting import Config
from flask import Blueprint, request, render_template, session, current_app
from py.utilities.setting import login_required

order_lookup_bp = Blueprint('order_lookup', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

def get_latest_order_ids():
    """Get the latest order IDs from both FDC and E-Fashion databases based on order date"""
    latest_orders = {'fdc': None, 'ef': None}
    
    databases = {
        'fdc': 'omsdata_FDC',
        'ef': 'oms_EF'
    }
    
    for db_key, database_name in databases.items():
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            
            # Get the most recent order based on order date (ORD_DT) and digit constraints
            if db_key == 'fdc':
                # For FDC, get the most recent 5-digit order (exclude internal system IDs > 99999)
                cursor.execute("""
                    SELECT ord_num FROM orders 
                    WHERE ord_num >= 10000 AND ord_num <= 99999 AND ORD_DT IS NOT NULL
                    ORDER BY ORD_DT DESC, ord_num DESC
                """)
            else:
                # For E-Fashion, get the most recent 4-digit order
                cursor.execute("""
                    SELECT ord_num FROM orders 
                    WHERE ord_num >= 1000 AND ord_num <= 9999 AND ORD_DT IS NOT NULL
                    ORDER BY ORD_DT DESC, ord_num DESC
                """)
            
            result = cursor.fetchone()
            
            if result and result[0]:
                latest_orders[db_key] = result[0]
            
            cursor.close()
            conn.close()
            
        except Exception as e:
            current_app.logger.error(f"Error getting latest order ID from {database_name}: {str(e)}")
            latest_orders[db_key] = "Error"
    
    return latest_orders

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
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
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
                    PO_NUM,
                    ORD_AMT,
                    PAID_AMT,
                    TAXABLE_AMT,
                    INVS_TAX,
                    HANDL_FEE,
                    SHIP_CHG,
                    DISCOUNT,
                    MISC_CHG,
                    SALES_NUM,
                    SALES_NUM2,
                    TERM_DESC,
                    CLOSE_CD,
                    ORD_TYPE,
                    CUS_SHIP_CHG,
                    DISC_AMT,
                    STATUS,
                    SHIP_DESC,
                    TRK_NUM,
                    SP_ADR,
                    SP_ADR_2,
                    SP_ADR_3,
                    SP_ADR_CT,
                    SP_ADR_ST,
                    SP_ADR_ZP,
                    SP_ADR_CN,
                    SP_CUS_NM,
                    ATTN,
                    EMAIL_ADR,
                    PCK_AMT
                FROM dbo.orders
                WHERE ord_num = (SELECT ord_num FROM OrderNum)
            ),
            CustomerInfo AS (
                SELECT 
                    CUS_ID,
                    PRIORITY,
                    PHONE,
                    SHP_CITY,
                    SHP_STATE,
                    SHP_ZIP
                FROM dbo.CUSTOMER
                WHERE CUS_ID IN (SELECT DISTINCT CUS_ID FROM OrderInfo)
            )
            SELECT DISTINCT
                O.ORD_NUM AS order#, 
                O.CUS_NM AS Customer, 
                O.CUS_ID AS CUS#, 
                OD.PO_NUM AS PO#, 
                OD.ORD_AMT AS order_amount,
                OD.PAID_AMT AS paid_amount,
                OD.TAXABLE_AMT AS taxable_amount,
                OD.INVS_TAX AS tax_amount,
                OD.HANDL_FEE AS handling_fee,
                OD.SHIP_CHG AS shipping_charge,
                OD.DISCOUNT AS discount,
                OD.MISC_CHG AS misc_charge,
                OD.CUS_SHIP_CHG AS customer_ship_charge,
                OD.DISC_AMT AS discount_amount,
                OD.PCK_AMT AS package_amount,
                OD.SALES_NUM AS salesman,
                OD.SALES_NUM2 AS salesman2,
                OD.TERM_DESC AS terms,
                OD.CLOSE_CD AS close_code,
                OD.ORD_TYPE AS order_type,
                OD.STATUS AS order_status,
                CI.PRIORITY AS priority,
                OD.SHIP_DESC AS ship_method,
                OD.TRK_NUM AS tracking_number,
                OD.SP_ADR AS ship_address_1,
                OD.SP_ADR_2 AS ship_address_2,
                OD.SP_ADR_3 AS ship_address_3,
                OD.SP_ADR_CT AS ship_to_city,
                OD.SP_ADR_ST AS ship_to_state,
                OD.SP_ADR_ZP AS ship_to_zip,
                OD.SP_ADR_CN AS ship_to_country,
                OD.SP_CUS_NM AS ship_customer_name,
                CI.PHONE AS phone,
                OD.ATTN AS attention,
                OD.EMAIL_ADR AS email,
                O.COMM_LN AS line, 
                O.PROD_CD AS SKU, 
                O.UT_DESC AS Descrip, 
                CAST(O.ORDER_QTY AS DECIMAL(10,0)) AS Qty, 
                CAST(O.UNIT_PRS AS DECIMAL (10,2)) AS price, 
                CAST(O.PC_UNIT AS DECIMAL(10,0)) AS per_box,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.order_date) - 81088, '20230101')) AS VARCHAR) AS order_date,

                            RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                            CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.ship_date) - 81088, '20230101')) AS VARCHAR) AS ship_date,

                            CASE 
                                WHEN OD.cancel_date = 0 THEN ''
                                ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                                     RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                                     CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', OD.cancel_date) - 81088, '20230101')) AS VARCHAR)
                            END AS cancel_date
            FROM OrderInfo O
            INNER JOIN OrderDates OD ON O.ORD_NUM = OD.ord_num
            LEFT JOIN CustomerInfo CI ON O.CUS_ID = CI.CUS_ID;
            """
        
        # Create a Pandas DataFrame from the query results
        df = pd.read_sql_query(query, conn, params=(order_number,))

        if df.empty:
            cursor.close()
            conn.close()
            return None, None

        # Extract header information (same for all rows)
        # Calculate discount as percentage of package amount
        package_amount = df.iloc[0]['package_amount'] if pd.notna(df.iloc[0]['package_amount']) else 0
        discount_pct = df.iloc[0]['discount'] if pd.notna(df.iloc[0]['discount']) else 0
        calculated_discount = (discount_pct / 100) * package_amount if package_amount > 0 else 0
        
        # Calculate order amount as ord_amt + handl_fee
        base_order_amount = df.iloc[0]['order_amount'] if pd.notna(df.iloc[0]['order_amount']) else 0
        handling_fee = df.iloc[0]['handling_fee'] if pd.notna(df.iloc[0]['handling_fee']) else 0
        total_order_amount = base_order_amount + handling_fee
        
        header_info = {
            'order_number': str(df.iloc[0]['order#']),
            'customer': df.iloc[0]['Customer'],
            'customer_id': df.iloc[0]['CUS#'],
            'po_number': df.iloc[0]['PO#'],
            'package_amount': f"${package_amount:.2f}" if package_amount > 0 else 'N/A',
            'discount': f"${calculated_discount:.2f}" if calculated_discount > 0 else 'N/A',
            'shipping_charge': f"${df.iloc[0]['shipping_charge']:.2f}" if pd.notna(df.iloc[0]['shipping_charge']) else 'N/A',
            'handling_fee': f"${df.iloc[0]['handling_fee']:.2f}" if pd.notna(df.iloc[0]['handling_fee']) else 'N/A',
            'tax_amount': f"${df.iloc[0]['tax_amount']:.2f}" if pd.notna(df.iloc[0]['tax_amount']) else 'N/A',
            'order_amount': f"${total_order_amount:.2f}" if total_order_amount > 0 else 'N/A',
            'paid_amount': f"${df.iloc[0]['paid_amount']:.2f}" if pd.notna(df.iloc[0]['paid_amount']) else 'N/A',
            'taxable_amount': f"${df.iloc[0]['taxable_amount']:.2f}" if pd.notna(df.iloc[0]['taxable_amount']) else 'N/A',
            'misc_charge': f"${df.iloc[0]['misc_charge']:.2f}" if pd.notna(df.iloc[0]['misc_charge']) else 'N/A',
            'customer_ship_charge': f"${df.iloc[0]['customer_ship_charge']:.2f}" if pd.notna(df.iloc[0]['customer_ship_charge']) else 'N/A',
            'discount_amount': f"${df.iloc[0]['discount_amount']:.2f}" if pd.notna(df.iloc[0]['discount_amount']) else 'N/A',
            'salesman': df.iloc[0]['salesman'] or 'N/A',
            'salesman2': df.iloc[0]['salesman2'] or 'N/A',
            'terms': df.iloc[0]['terms'] or 'N/A',
            'close_code': str(df.iloc[0]['close_code']) if pd.notna(df.iloc[0]['close_code']) else 'N/A',
            'order_type': df.iloc[0]['order_type'] or 'N/A',
            'order_date': df.iloc[0]['order_date'],
            'ship_date': df.iloc[0]['ship_date'],
            'cancel_date': df.iloc[0]['cancel_date'],
            'order_status': str(df.iloc[0]['order_status']).strip() if pd.notna(df.iloc[0]['order_status']) and str(df.iloc[0]['order_status']).strip() else 'N/A',
            'priority': str(df.iloc[0]['priority']).strip() if pd.notna(df.iloc[0]['priority']) and str(df.iloc[0]['priority']).strip() else 'Normal',
            'ship_method': str(df.iloc[0]['ship_method']).strip() if pd.notna(df.iloc[0]['ship_method']) and str(df.iloc[0]['ship_method']).strip() else 'N/A',
            'tracking_number': str(df.iloc[0]['tracking_number']).strip() if pd.notna(df.iloc[0]['tracking_number']) and str(df.iloc[0]['tracking_number']).strip() else 'N/A',
            'ship_address_1': str(df.iloc[0]['ship_address_1']).strip() if pd.notna(df.iloc[0]['ship_address_1']) and str(df.iloc[0]['ship_address_1']).strip() else '',
            'ship_address_2': str(df.iloc[0]['ship_address_2']).strip() if pd.notna(df.iloc[0]['ship_address_2']) and str(df.iloc[0]['ship_address_2']).strip() else '',
            'ship_address_3': str(df.iloc[0]['ship_address_3']).strip() if pd.notna(df.iloc[0]['ship_address_3']) and str(df.iloc[0]['ship_address_3']).strip() else '',
            'ship_to_city': str(df.iloc[0]['ship_to_city']).strip() if pd.notna(df.iloc[0]['ship_to_city']) and str(df.iloc[0]['ship_to_city']).strip() else 'N/A',
            'ship_to_state': str(df.iloc[0]['ship_to_state']).strip() if pd.notna(df.iloc[0]['ship_to_state']) and str(df.iloc[0]['ship_to_state']).strip() else 'N/A',
            'ship_to_zip': str(df.iloc[0]['ship_to_zip']).strip() if pd.notna(df.iloc[0]['ship_to_zip']) and str(df.iloc[0]['ship_to_zip']).strip() else 'N/A',
            'ship_to_country': str(df.iloc[0]['ship_to_country']).strip() if pd.notna(df.iloc[0]['ship_to_country']) and str(df.iloc[0]['ship_to_country']).strip() else '',
            'ship_customer_name': str(df.iloc[0]['ship_customer_name']).strip() if pd.notna(df.iloc[0]['ship_customer_name']) and str(df.iloc[0]['ship_customer_name']).strip() else '',
            'phone': str(df.iloc[0]['phone']).strip() if pd.notna(df.iloc[0]['phone']) and str(df.iloc[0]['phone']).strip() else 'N/A',
            'attention': str(df.iloc[0]['attention']).strip() if pd.notna(df.iloc[0]['attention']) and str(df.iloc[0]['attention']).strip() else 'N/A',
            'email': str(df.iloc[0]['email']).strip() if pd.notna(df.iloc[0]['email']) and str(df.iloc[0]['email']).strip() else 'N/A'
        }

        # Extract unique locations from ntfile table
        location_query = """
            SELECT CASE 
                WHEN NOTES IS NOT NULL THEN SUBSTRING(NOTES, CHARINDEX('\r\n', NOTES) + 2, LEN(NOTES))
                ELSE ''
            END AS Location
            FROM dbo.ntfile 
            WHERE nt_num = ?
        """
        
        cursor.execute(location_query, (order_number,))
        location_results = cursor.fetchall()
        
        unique_locations = []
        for row in location_results:
            loc = row[0].strip() if row[0] else ''
            if loc and loc not in unique_locations:
                unique_locations.append(loc)
        
        header_info['locations'] = unique_locations

        # Close the cursor and connection
        cursor.close()
        conn.close()

        # Create items DataFrame with only item-specific columns (excluding Location)
        items_df = df[['line', 'SKU', 'Descrip', 'Qty', 'price', 'per_box']].copy()
        
        # Format Qty and per_box as integers (removing decimals)
        items_df['Qty'] = items_df['Qty'].fillna(0).astype(int)
        items_df['per_box'] = items_df['per_box'].fillna(0).astype(int)
        
        # Add hyperlinks to SKU column
        items_df['SKU'] = items_df['SKU'].apply(
            lambda sku: f'<a href="https://www.fdcef.com/item_lookup/{sku}" target="_blank" style="color: #4a90e2; text-decoration: none; font-weight: 600;">{sku}</a>'
        )
        
        # Prepare the HTML content for items table
        full_table_html = items_df.to_html(classes='table table-bordered table-striped', escape=False, index=False, header=False)
        
        # Extract only the tbody content from the generated HTML
        import re
        tbody_match = re.search(r'<tbody[^>]*>(.*?)</tbody>', full_table_html, re.DOTALL)
        if tbody_match:
            items_html = tbody_match.group(1)
        else:
            items_html = full_table_html
        
        return header_info, items_html

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return None, None


@order_lookup_bp.route("/order_lookup", methods=["GET", "POST"])
@order_lookup_bp.route("/order_lookup/<string:order_number>", methods=["GET"])
@login_required
def order_lookup(order_number=None):
    current_user = session['username']
    header_info = None
    items_html = None
    
    # Get latest order IDs from both databases
    latest_orders = get_latest_order_ids()

    if request.method == "POST":
        # Handle the order lookup submission
        order_number = request.form.get("order_number", "").strip()
        
        if not order_number:
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders)

        session['order_number'] = order_number

        if current_user == 'fdc':
            current_app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders)

        header_info, items_html = generate_order(order_number)
        
        if items_html:
            current_app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
        else:
            header_info = None
            items_html = None

    elif order_number:
        # Handle GET request with order_number in the URL
        
        if current_user == 'fdc':
            current_app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders)
        
        header_info, items_html = generate_order(order_number)
        
        if items_html:
            current_app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
        else:
            current_app.logger.info("No data found for order number: %s via URL", order_number)
            header_info = None
            items_html = None

    return render_template(
        "order_lookup.html",
        header_info=header_info,
        items_html=items_html,
        order_number=order_number,
        current_user=current_user,
        latest_orders=latest_orders
    )
