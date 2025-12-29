from flask import Blueprint, request, jsonify,redirect,session,render_template,url_for,current_app
import pyodbc
import pandas as pd
from py.utilities.auth import login_required
from py.utilities.setting import Config

# Define the blueprint
customer_bp = Blueprint('customer', __name__)

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

@customer_bp.route('/customer', methods=['GET'])
@login_required
def customer_home():
    current_user = session['username']
    search_id = request.args.get('search_id', '')  # Get search_id from query parameter
    return render_template('customer.html', current_user=current_user, search_id=search_id)


@customer_bp.route('/check_customer', methods=['POST'])
@login_required
def check_customer():
    current_user = session['username']
    data = request.get_json()
    customer_name = data.get('customer_name', '')
    # Get username from request if available, otherwise use session
    username_from_request = data.get('username')
    if username_from_request:
        current_user = username_from_request
        
    current_app.logger.info("***%s***__@@@search customer@@@__ %s", current_user, data)
    results = []
    null_results = []  # List to hold rows with null values
    databases = ['omsdata_FDC', 'oms_EF']

    for database in databases:
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            # Updated query to search by both customer name OR customer ID
            query = f"""
                SELECT
                    c.cus_nm,
                    c.cus_id,
                    RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, CAST(o.ord_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_ord_dt,
                    o.ord_num
                FROM
                    customer c
                LEFT JOIN (
                    SELECT
                        cus_id,
                        MAX(ord_num) AS max_ord_num
                    FROM
                        orders
                    GROUP BY
                        cus_id
                ) AS max_orders ON c.cus_id = max_orders.cus_id
                LEFT JOIN orders o ON max_orders.cus_id = o.cus_id AND max_orders.max_ord_num = o.ord_num
                WHERE
                    c.cus_nm LIKE ? OR c.cus_id LIKE ?
            """
            cursor.execute(query, ('%' + customer_name + '%', '%' + customer_name + '%'))
            rows = cursor.fetchall()

            for row in rows:
                if row[2] is not None and row[3] is not None:  # Check if formatted_ord_dt and max_ord_num are not null
                    results.append({
                        'cus_nm': row[0],
                        'cus_id': row[1],
                        'formatted_ord_dt': row[2],
                        'max_ord_num': row[3],
                        'database_origin': database
                    })
                else:
                    null_results.append({
                        'cus_nm': row[0],
                        'cus_id': row[1],
                        'formatted_ord_dt': row[2],
                        'max_ord_num': row[3],
                        'database_origin': database
                    })

            cursor.close()
            conn.close()
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    return jsonify({
        "success": True,
        "data": results,
        "null_data": null_results  # Return the null results as a separate key
    })

@customer_bp.route('/customer_details/<customer_id>', methods=['GET'])
@login_required
def customer_details(customer_id):
    current_user = session['username']
    # Get username from query parameter if available
    username_from_request = request.args.get('username')
    if username_from_request:
        current_user = username_from_request
        
    database_origin = request.args.get('db')  # Get the database origin from the query parameter
    results = []
    unique_order_numbers = {}
    unique_product_data = {}
    current_app.logger.info("***%s***__@@@search customer id@@@__ %s in database %s", current_user, customer_id, database_origin)
    # Use the selected database
    databases = [database_origin] if database_origin in ['omsdata_FDC', 'oms_EF'] else []

    for database in databases:
        try:
            conn = pyodbc.connect(
                f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
            )
            cursor = conn.cursor()
            query = f"""
                SELECT 
                ol.ord_num,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, CAST(ol.olg_dt AS INT) - 81088, '20230101')) AS VARCHAR) AS formatted_olg_dt,
                ol.prod_cd,
                ol.order_qty,
                ol.unit_prs
                FROM ord_log ol
                JOIN customer c ON ol.cus_id = c.cus_id
                WHERE c.cus_id = ?
            """
            cursor.execute(query, (customer_id,))
            rows = cursor.fetchall()

            for row in rows:
                ord_num = row[0]
                formatted_date = row[1]
                prod_cd = row[2]
                order_qty = int(row[3] or 0)
                unit_prs = round(float(row[4]) if row[4] is not None else 0.0, 2)

                # Append to results for order history
                results.append((ord_num, formatted_date, prod_cd, order_qty, unit_prs))

                # Calculate total for unique order numbers
                if ord_num:
                    if ord_num not in unique_order_numbers:
                        # Query to get ord_amt and handl_fee for the current order number
                        order_query = f"""
                            SELECT ord_amt, handl_fee,misc_chg 
                            FROM orders 
                            WHERE ord_num = ?
                        """
                        cursor.execute(order_query, (ord_num,))
                        order_row = cursor.fetchone()
                        if order_row:
                            ord_amt = order_row[0] or 0
                            handl_fee = order_row[1] or 0
                            misc_chg = order_row[2] or 0
                            total = ord_amt + handl_fee+misc_chg
                            unique_order_numbers[ord_num] = {
                                'date': formatted_date,
                                'ord_amt': ord_amt,
                                'handl_fee': handl_fee,
                                'misc_chg': misc_chg,
                                'total': total
                            }

                if prod_cd:
                    if prod_cd not in unique_product_data:
                        unique_product_data[prod_cd] = {'total_qty': 0, 'prices': set()}
                    unique_product_data[prod_cd]['total_qty'] += order_qty
                    unique_product_data[prod_cd]['prices'].add(unit_prs)

            cursor.close()
            conn.close()
        except Exception as e:
            return jsonify({"success": False, "message": str(e)})

    # Prepare the DataFrame for order history
    history_columns = ['order#', 'Date', 'sku', 'Quantity', 'Price']
    history_df = pd.DataFrame.from_records(results, columns=history_columns)

    # Add hyperlinks
    history_df['order#'] = history_df['order#'].apply(lambda x: f'<a href="{url_for("order_lookup.order_lookup", order_number=x)}" target="_blank">{x}</a>')

    # Modify the sku column to use the new URL format
    history_df['sku'] = history_df['sku'].apply(lambda x: f'<a href="/item_lookup/{x}" target="_blank">{x}</a>')
    history_html_table = history_df.to_html(
        index=False,
        classes="table table-bordered table-striped",
        table_id="customer_details_table",
        float_format="{:.2f}".format,
        escape=False
    )
    product_summary_rows = []
    for prod_cd, data in unique_product_data.items():
        prices = ' ,'.join(map(str, data['prices']))
        product_summary_rows.append(f"<tr><td><a href='/item_lookup/{prod_cd}' target='_blank'>{prod_cd}</a></td><td>{data['total_qty']}</td><td>{prices}</td></tr>")
    
    product_summary_html = f"""
        <h4>Product Summary:</h4>
        <table id='product_summary_table' class='table table-bordered table-striped'>
            <thead>
                <tr>
                    <th>SKU</th>
                    <th>ordered</th>
                    <th>Prices</th>
                </tr>
            </thead>
            <tbody>
                {''.join(product_summary_rows)}
            </tbody>
        </table>
    """

    # Prepare unique order numbers for response
    unique_order_numbers_list = [
    {
        'order_number': ord_num,
        'date': details['date'],
        'ord_amt': details['ord_amt'],
        'handl_fee': details['handl_fee'],
        'total': details['total'],
        'order_lookup_url': url_for('order_lookup.order_lookup', order_number=ord_num)  # Add URL for order lookup
    }
    for ord_num, details in unique_order_numbers.items()
]
    return jsonify({
        "success": True,
        "data": history_html_table,
        "product_summary": product_summary_html,
        "unique_order_numbers": unique_order_numbers_list  # Send the correct structure
    })