import pyodbc
import pandas as pd

from datetime import datetime, timedelta
from flask import Blueprint, request, render_template, session, current_app, jsonify
import re

# Import helper to fetch locations for SKUs so we can populate Location server-side
from py.search.item_lookup import fetch_locations
from py.utilities.setting import Config, login_required

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
                    INVS_NUM,
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
                CASE 
                    WHEN OD.INVS_NUM IS NULL OR OD.INVS_NUM = 0 THEN 'not invoiced'
                    ELSE CAST(OD.INVS_NUM AS VARCHAR)
                END AS invoice_num,
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
            return None, None, False

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
            'customer_id': str(df.iloc[0]['CUS#']).strip(),
            'invoice_number': df.iloc[0]['invoice_num'] if (pd.notna(df.iloc[0]['invoice_num']) and str(df.iloc[0]['invoice_num']).strip() not in ['0', 'not invoiced']) else 'not invoiced',
            'po_number': df.iloc[0]['PO#'],
            'package_amount': f"${package_amount:,.2f}" if package_amount > 0 else 'N/A',
            'discount': f"${calculated_discount:,.2f}" if calculated_discount > 0 else 'N/A',
            'shipping_charge': f"${df.iloc[0]['shipping_charge']:,.2f}" if pd.notna(df.iloc[0]['shipping_charge']) else 'N/A',
            'handling_fee': f"${df.iloc[0]['handling_fee']:,.2f}" if pd.notna(df.iloc[0]['handling_fee']) else 'N/A',
            'tax_amount': f"${df.iloc[0]['tax_amount']:,.2f}" if pd.notna(df.iloc[0]['tax_amount']) else 'N/A',
            'order_amount': f"${total_order_amount:,.2f}" if total_order_amount > 0 else 'N/A',
            'paid_amount': f"${df.iloc[0]['paid_amount']:,.2f}" if pd.notna(df.iloc[0]['paid_amount']) else 'N/A',
            'taxable_amount': f"${df.iloc[0]['taxable_amount']:,.2f}" if pd.notna(df.iloc[0]['taxable_amount']) else 'N/A',
            'misc_charge': f"${df.iloc[0]['misc_charge']:,.2f}" if pd.notna(df.iloc[0]['misc_charge']) else 'N/A',
            'customer_ship_charge': f"${df.iloc[0]['customer_ship_charge']:,.2f}" if pd.notna(df.iloc[0]['customer_ship_charge']) else 'N/A',
            'discount_amount': f"${df.iloc[0]['discount_amount']:,.2f}" if pd.notna(df.iloc[0]['discount_amount']) else 'N/A',
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

        # Attempt to fetch order notes from dbo.ntfile
        try:
            conn_notes = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cur2 = conn_notes.cursor()
            # Some orders have multiple ntfile rows; pick the first non-empty note, favoring the longest
            cur2.execute(
                """
                SELECT TOP 1 NOTES
                FROM dbo.ntfile
                WHERE NT_NUM = ?
                  AND NOTES IS NOT NULL
                  AND LTRIM(RTRIM(NOTES)) <> ''
                ORDER BY LEN(NOTES) DESC
                """,
                (order_number,)
            )
            note_row = cur2.fetchone()
            header_info['notes'] = str(note_row[0]).strip() if note_row and note_row[0] else ''
            try:
                preview = header_info['notes'][:200] if header_info['notes'] else ''
            except Exception:
                pass
            cur2.close()
            conn_notes.close()
        except Exception as e:
            current_app.logger.error(f"Error fetching order notes for {order_number}: {e}")
            header_info['notes'] = ''

        # Close the cursor and connection
        cursor.close()
        conn.close()        # Create items DataFrame with item-specific columns including Location
        items_df = df[['line', 'SKU', 'Descrip', 'Qty', 'price', 'per_box']].copy()

        # Clean SKU values: remove trailing/leading whitespace and non-breaking spaces
        try:
            items_df['SKU'] = items_df['SKU'].astype(str).str.replace('\u00A0', ' ').str.strip()
        except Exception:
            # fallback simple strip
            items_df['SKU'] = items_df['SKU'].astype(str).str.strip()
        
        # Adjust price to list price by adding discount per unit
        discount_amount = df.iloc[0]['discount_amount'] if pd.notna(df.iloc[0]['discount_amount']) else 0
        items_df['price'] = items_df['price'] + (discount_amount / items_df['Qty'])
        
        # Use the same lookup pattern as in Item Lookup: pass lowercased SKUs to fetch_locations
        # Try several SKU variants to maximize matching with products.SKU values in MySQL
        try:
            skus_original = items_df['SKU'].astype(str).tolist()
            skus_lower = [s.lower() for s in skus_original]
            skus_stripped = [s.replace(' ', '') for s in skus_lower]
            skus_upper = [s.upper() for s in skus_original]

            locations = {}
            # Try variants in order: lower, original, stripped, upper
            variants = [skus_lower, skus_original, skus_stripped, skus_upper]
            for variant in variants:
                try:
                    if not variant:
                        continue
                    found = fetch_locations(variant)
                    # merge keys (fetch_locations lowercases keys internally)
                    for k, v in found.items():
                        if k not in locations or not locations[k]:
                            locations[k] = v
                except Exception as _e:
                    current_app.logger.debug(f"fetch_locations variant failed: {_e}")
                    continue
        except Exception as e:
            current_app.logger.error(f"Error preparing SKU variants for location lookup for order {order_number}: {e}")
            locations = {}

        # Log what we found for easier debugging
        try:
            if locations:
                sample = dict(list(locations.items())[:20])
        except Exception:
            pass

        # Map locations back to rows using lowercased SKU key (fallback to stripped/alnum handled in fetch_locations)
        try:
            items_df['Location'] = items_df['SKU'].astype(str).str.lower().map(locations).fillna('N/A')
        except Exception as e:
            current_app.logger.error(f"Error mapping locations into items_df for order {order_number}: {e}")
            items_df['Location'] = 'N/A'
        
        # Format Qty and per_box as integers (removing decimals)
        items_df['Qty'] = items_df['Qty'].fillna(0).astype(int)
        items_df['per_box'] = items_df['per_box'].fillna(0).astype(int)
        
        # Check if ALL descriptions contain a dash "-" for label generation
        all_descriptions_have_dash = items_df['Descrip'].str.contains('-', na=False).all()
        
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
        
        return header_info, items_html, all_descriptions_have_dash

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return None, None, False


@order_lookup_bp.route("/order_lookup", methods=["GET", "POST"])
@order_lookup_bp.route("/order_lookup/<string:order_number>", methods=["GET"])
@login_required
def order_lookup(order_number=None):
    current_user = session['username']
    header_info = None
    items_html = None
    all_descriptions_have_dash = False
    
    # Get latest order IDs from both databases
    latest_orders = get_latest_order_ids()


    error_message = None
    po_number = None
    if request.method == "POST":
        # If searching by PO
        po_number = request.form.get("search_by_po", "").strip()
        if po_number:
            # Try to find order number by PO in both databases
            order_number = find_order_number_by_po(po_number)
            if not order_number:
                error_message = f"No order found for PO: {po_number}"
                return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders, all_descriptions_have_dash=False, error_message=error_message, po_number=po_number)
            # else, continue as if searching by order number
        else:
            order_number = request.form.get("order_number", "").strip()

        if not order_number:
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders, all_descriptions_have_dash=False, po_number=po_number)

        session['order_number'] = order_number

        if current_user == 'fdc':
            current_app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders, all_descriptions_have_dash=False, po_number=po_number)

        header_info, items_html, all_descriptions_have_dash = generate_order(order_number)

        if items_html:
            current_app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
        else:
            header_info = None
            items_html = None
            all_descriptions_have_dash = False

    elif order_number:
        # Handle GET request with order_number in the URL
        
        if current_user == 'fdc':
            current_app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", header_info=None, items_html=None, current_user=current_user, latest_orders=latest_orders, all_descriptions_have_dash=False)
        
        header_info, items_html, all_descriptions_have_dash = generate_order(order_number)
        
        if items_html:
            current_app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
        else:
            current_app.logger.info("No data found for order number: %s via URL", order_number)
            header_info = None
            items_html = None
            all_descriptions_have_dash = False

    return render_template(
        "order_lookup.html",
        header_info=header_info,
        items_html=items_html,
        order_number=order_number,
        current_user=current_user,
        latest_orders=latest_orders,
        all_descriptions_have_dash=all_descriptions_have_dash,
        error_message=error_message,
        po_number=po_number
    )

def find_order_number_by_po(po_number):
    # Try both databases for a matching PO
    try:
        for db_name, length in [('omsdata_FDC', 5), ('oms_EF', 4)]:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={db_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            cursor.execute("SELECT TOP 1 ord_num FROM orders WHERE PO_NUM = ? ORDER BY ORD_DT DESC", (po_number,))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row and row[0]:
                return str(row[0]).zfill(length)
    except Exception as e:
        current_app.logger.error(f"Error finding order by PO: {po_number}: {e}")
    return None


@order_lookup_bp.route('/debug_order_locations/<string:order_number>', methods=['GET'])
@login_required
def debug_order_locations(order_number):
    """Debug route: return locations mapping for SKUs in the given order number."""
    try:
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return jsonify({'error': 'Invalid order number format'}), 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT PROD_CD FROM DBO.ORD_LOG WHERE ORD_NUM = ?", (order_number,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        sku_list = [r[0] for r in rows if r and r[0]]
        # If no SKUs found, return empty
        if not sku_list:
            return jsonify({'skus': [], 'locations': {}})

        locations = fetch_locations([str(s) for s in sku_list])
        return jsonify({'skus': sku_list, 'locations': locations})
    except Exception as e:
        current_app.logger.error(f"Error in debug_order_locations for {order_number}: {e}")
        return jsonify({'error': str(e)}), 500

@order_lookup_bp.route("/update_order_description", methods=["POST"])
@login_required
def update_order_description():
    """Update the description (UT_DESC) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_description = data.get('description')
        
        if not all([order_number, line_number, new_description]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the description in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET UT_DESC = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_description, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated description@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Description: {new_description}"
            )
            return {'success': True, 'message': 'Description updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating description: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_qty", methods=["POST"])
@login_required
def update_order_qty():
    """Update the quantity (ORDER_QTY) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_qty = data.get('qty')
        
        if not all([order_number, line_number, new_qty]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the quantity in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET ORDER_QTY = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_qty, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated qty@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Qty: {new_qty}"
            )
            return {'success': True, 'message': 'Quantity updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating quantity: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route("/update_order_price", methods=["POST"])
@login_required
def update_order_price():
    """Update the price (UNIT_PRS) for a specific order line"""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        line_number = data.get('line_number')
        sku = data.get('sku')
        new_price = data.get('price')
        
        if not all([order_number, line_number, new_price]):
            return {'success': False, 'message': 'Missing required fields'}, 400
        
        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400
        
        # Connect to database
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        
        # Update the price in ORD_LOG table
        update_query = """
            UPDATE DBO.ORD_LOG 
            SET UNIT_PRS = ?
            WHERE ORD_NUM = ? AND COMM_LN = ?
        """
        
        cursor.execute(update_query, (new_price, order_number, line_number))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(
                f"***{current_user}***__@@@updated price@@@__ Order: {order_number}, Line: {line_number}, SKU: {sku}, New Price: {new_price}"
            )
            return {'success': True, 'message': 'Price updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
            
    except Exception as e:
        current_app.logger.error(f"Error updating price: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500


@order_lookup_bp.route('/debug_order_notes/<string:order_number>', methods=['GET'])
@login_required
def debug_order_notes(order_number):
    """Debug route to inspect dbo.ntfile NOTES for a given order number."""
    try:
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return jsonify({'error': 'Invalid order number format'}), 400

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute("SELECT NT_NUM, NOTES FROM dbo.ntfile WHERE NT_NUM = ?", (order_number,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        notes_list = []
        for r in rows:
            nt = str(r[0]) if r and len(r) > 0 else ''
            n = str(r[1]).strip() if r and len(r) > 1 and r[1] is not None else ''
            notes_list.append({'nt_num': nt, 'notes': n})

        return jsonify({
            'database': database_name,
            'order_number': order_number,
            'count': len(notes_list),
            'results': notes_list
        })
    except Exception as e:
        current_app.logger.error(f"Error in debug_order_notes for {order_number}: {e}")
        return jsonify({'error': str(e)}), 500


@order_lookup_bp.route("/update_order_notes", methods=["POST"])
@login_required
def update_order_notes():
    """Update the order-level NOTES field in dbo.ntfile for the given order."""
    try:
        data = request.get_json()
        order_number = data.get('order_number')
        notes = data.get('notes', '')

        if not order_number:
            return {'success': False, 'message': 'Missing order number'}, 400

        # Determine database based on order number length
        if len(order_number) == 5:
            database_name = 'omsdata_FDC'
        elif len(order_number) == 4:
            database_name = 'oms_EF'
        else:
            return {'success': False, 'message': 'Invalid order number format'}, 400

        # Log the notes being saved for debugging
        try:
            current_app.logger.info(f"Saving notes for order {order_number}: {repr(notes[:100])} (length: {len(notes)})")
        except:
            pass

        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()

        # First check if a record exists for this order in ntfile
        cursor.execute("SELECT COUNT(*) FROM dbo.ntfile WHERE NT_NUM = ?", (order_number,))
        record_exists = cursor.fetchone()[0] > 0

        if record_exists:
            # Update existing record
            update_query = "UPDATE dbo.ntfile SET NOTES = ? WHERE NT_NUM = ?"
            cursor.execute(update_query, (notes, order_number))
        else:
            # Insert new record if it doesn't exist (NT_CD defaults to 0)
            insert_query = "INSERT INTO dbo.ntfile (NT_NUM, NT_CD, NOTES) VALUES (?, 0, ?)"
            cursor.execute(insert_query, (order_number, notes))
        
        conn.commit()
        rows_affected = cursor.rowcount

        cursor.close()
        conn.close()

        if rows_affected > 0:
            current_user = session.get('username', 'unknown')
            current_app.logger.info(f"***{current_user}***__@@@updated notes@@@__ Order: {order_number}, rows affected: {rows_affected}")
            return {'success': True, 'message': 'Order notes updated successfully'}, 200
        else:
            return {'success': False, 'message': 'No rows were updated'}, 404
    except Exception as e:
        current_app.logger.error(f"Error updating order notes: {str(e)}")
        return {'success': False, 'message': f'Database error: {str(e)}'}, 500

