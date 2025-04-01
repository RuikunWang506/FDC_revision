from flask import Blueprint, request, jsonify, redirect, session, render_template, url_for, current_app, send_file
import pyodbc
import pandas as pd
import os
import io
import csv
import openpyxl

# Define the blueprint
order_processor_bp = Blueprint('order_processor', __name__)

# Database connection details
server = "192.168.254.204"
username = "tristen"
password = "Fdc2022!!"
database = "oms_EF"
driver = 'ODBC Driver 17 for SQL Server'
table_name = 'dbo.ord_log'
zhuankuan_database = "omsdata_FDC"

def get_db_connection():
    return pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={database};UID={username};PWD={password}'
    )

def get_zhuankuan_db_connection():
    return pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={zhuankuan_database};UID={username};PWD={password}'
    )

@order_processor_bp.route('/order_processor', methods=['GET'])
def order_processor():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("login"))
    current_user = session['username']
    return render_template('order_processor.html', current_user=current_user)


@order_processor_bp.route('/verify_order', methods=['POST'])
def verify_order():
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    # Use force=True to parse JSON regardless of Content-Type header
    data = request.get_json(force=True)
    order_number = data.get('order_number')
    items = data.get('items', [])

    if not order_number:
        return jsonify({"success": False, "message": "Order number is required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE ord_num = ?", order_number)
        count = cursor.fetchone()[0]
        if count == 0:
            return jsonify({"success": False, "message": f"Order {order_number} not found"}), 404

        cursor.execute(f"""
            SELECT PROD_CD, ORDER_QTY, UNIT_PRS
            FROM {table_name}
            WHERE ord_num = ?
        """, order_number)

        all_order_items = []
        for row in cursor.fetchall():
            all_order_items.append({
                'product_code': row[0].strip(),
                'quantity': row[1],
                'price': float(row[2])
            })

        conn.close()
        return jsonify({
            "success": True,
            "data": {
                "order_number": order_number,
                "items": all_order_items
            }
        })

    except Exception as e:
        current_app.logger.error(f"Error verifying order: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

@order_processor_bp.route('/update_order', methods=['POST'])
def update_order():
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json(force=True)
    order_number = data.get('order_number')
    items = data.get('items', [])
    current_user = session['username']

    if not order_number or not items:
        return jsonify({"success": False, "message": "Order number and items are required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        updated_items = []
        not_found_items = []

        for item in items:
            product_code = item['product_code']
            quantity = item['quantity']
            price = item['price']

            cursor.execute(f"""
                SELECT COUNT(*) FROM {table_name} 
                WHERE ord_num = ? AND PROD_CD = ?
            """, order_number, product_code)
            count = cursor.fetchone()[0]

            if count > 0:
                query = f"""
                    UPDATE {table_name}
                    SET ORDER_QTY = ?, UNIT_PRS = ?
                    WHERE ord_num = ? AND PROD_CD = ?
                """
                cursor.execute(query, quantity, price, order_number, product_code)
                current_app.logger.info(f"User {current_user} updated item {product_code} in order {order_number}: qty={quantity}, price={price}")
                updated_items.append(product_code)
            else:
                current_app.logger.warning(f"Item {product_code} not found in order {order_number}")
                not_found_items.append(product_code)

        conn.commit()
        placeholders = ','.join(['?'] * len(updated_items))
        cursor.execute(f"""
            SELECT PROD_CD, ORDER_QTY, UNIT_PRS 
            FROM {table_name} 
            WHERE ord_num = ? AND PROD_CD IN ({placeholders})
        """, [order_number] + updated_items)

        updated_data = []
        for row in cursor.fetchall():
            updated_data.append({
                'product_code': row[0].strip(),
                'quantity': row[1],
                'price': float(row[2])
            })

        conn.close()
        result_message = f"Successfully updated {len(updated_items)} items in order {order_number}"
        if not_found_items:
            result_message += f". {len(not_found_items)} items were not found in the order: {', '.join(not_found_items)}"

        current_app.logger.info(f"User {current_user} {result_message}")
        return jsonify({
            "success": True,
            "message": result_message,
            "updated_items": updated_items,
            "not_found_items": not_found_items,
            "updated_data": updated_data
        })

    except Exception as e:
        current_app.logger.error(f"Error updating order: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

@order_processor_bp.route('/generate_excel', methods=['POST'])
def generate_excel():
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json(force=True)
    items = data.get('items', [])

    if not items:
        return jsonify({"success": False, "message": "No items provided"}), 400

    try:
        df = pd.DataFrame(items)
        df = df.rename(columns={
            'product_code': 'PROD_CD',
            'quantity': 'ORDER_QTY',
            'price': 'UNIT_PRS'
        })
        output = io.BytesIO()
        writer = pd.ExcelWriter(output, engine='xlsxwriter')
        df.to_excel(writer, sheet_name='Sheet1', index=False)
        writer.close()
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name='order_data.xlsx',
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except Exception as e:
        current_app.logger.error(f"Error generating Excel: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500

@order_processor_bp.route('/zhuankuan', methods=['POST'])
def zhuankuan():
    """Process item codes and generate product information as CSV"""
    if 'username' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401
    
    # Get item codes from request (either JSON or form data)
    if request.is_json:
        data = request.get_json()
        user_input = data.get('item_codes', '')
    else:
        user_input = request.form.get('item_codes', '')
    
    current_user = session['username']
    
    if not user_input:
        return jsonify({"success": False, "message": "Item codes are required"}), 400
    
    try:
        # Connect to the SQL Server database
        conn = get_zhuankuan_db_connection()
        cursor = conn.cursor()
        
        # Process the input
        mylist = "'," + str(user_input) + ",'"
        query = """\
            DECLARE @AltCDList NVARCHAR(MAX) = ?;
                DECLARE @ProductList NVARCHAR(MAX);
                SET @AltCDList = REPLACE(@AltCDList, ',', '%,') + '%';

                -- Create a CTE to split the comma-separated values in @AltCDList
                WITH SplitAltCD AS (
                    SELECT value
                    FROM dbo.SplitString(@AltCDList, ',')
                )
                -- Run the first query and concatenate the result into @ProductList
                SELECT @ProductList = COALESCE(@ProductList + ',', '') + PROD_CD
                FROM dbo.inv
                WHERE EXISTS (SELECT 1 FROM SplitAltCD WHERE alt_CD LIKE value);

                -- Create a CTE to split the comma-separated values in @ProductList
                WITH SplitValues AS (
                    SELECT value
                    FROM dbo.SplitString(@ProductList, ',')
                )
                -- Query using the CTE to get the desired results
                SELECT i.PROD_CD AS Item#,
                    '01' AS WHS#,
                    i.DESCRIP AS Descrip,
                    '' AS Unit,
                    '' AS OrdSize,
                    '' AS MinStk,
                    '' AS [Price Type],
                    i.RETAIL_PRS AS Price1,
                    '' AS Price2,
                    '' AS Price3,
                    '' AS Price4,
                    '' AS Price5,
                    '' AS QtyPrice1,
                    '' AS QtyPrice2,
                    '' AS QtyPrice3,
                    '' AS QtyPrice4,
                    '' AS QtyPrice5,
                    '' AS QtyPrice6,
                    '' AS Range1,
                    '' AS Range2,
                    '' AS Range3,
                    '' AS Range4,
                    '' AS Range5,
                    '' AS Range6,
                    '' AS Range7,
                    '' AS CreateDate,
                    '' AS Note1,
                    '' AS Note2,
                    '' AS "Item_Def._Bin_Location",
                    '' AS Warehouse_Bin_Location,
                    '' AS Product_Year,
                    '' AS Comp,
                    i.Class_CD AS Class#,
                    '' AS Vend#,
                    '' AS Vend_Item#,
                    '' AS Image_Name,
                    i.PC_CASE AS "Unit/Case",
                    i.BOX_Case AS "Box/Case",
                    '' AS Case_Vol,
                    '' AS Unit_Vol,
                    i.Sales_Cost AS Sales_Cost,
                    '' AS Discount,
                    '' AS CommRt,
                    '' AS Taxable,
                    '' AS Stkable,
                    '' AS UPC_Code,
                    '' AS Updt_Date,
                    '' AS Updt_By,
                    '' AS Dept#,
                    i.CASE_WT AS Case_Wt,
                    i.UT_WT AS Unit_Wt,
                    '' AS OEM#,
                    i.ALT_CD AS "Alt.Item#",
                    '' AS Lead_Date,
                    i.UNIT_COLOR AS Color,
                    '' AS Size,
                    '' AS In_Stock,
                    '' AS Last_Rcv_Q,
                    '' AS Last_Rcv_Dat,
                    i.Sales_Cost AS Base_Cost,
                    '' AS Duty,
                    '' AS Handling_fee,
                    '' AS Ship_Fee,
                    i.Sales_Cost AS "Avg.Cost",
                    '' AS Last_Sale_Da,
                    '' AS Last_Order,
                    '' AS Order_Date,
                    '' AS So_Qty,
                    '' AS PO_Qty,
                    '' AS l2yrsqty,
                    '' AS lyrsqty,
                    '' AS ThisYrSQty,
                    '' AS Descrip2,
                    '' AS Descrip3,
                    '' AS Notes_Line_1,
                    '' AS Notes_Line_2,
                    '' AS Notes_Line_3,
                    '' AS Notes_Line_4,
                    '' AS Notes_Line_5,
                    '' AS Notes_Line_6,
                    '' AS Notes_Line_7,
                    '' AS Notes_Line_8,
                    '' AS Case_Length,
                    '' AS Case_Height,
                    '' AS Case_Width,
                    idt.ut_len AS Unit_Length,
                    idt.ut_wi AS Unit_Height,
                    idt.UT_hi AS Unit_Width,
                    '' AS Division,
                    '' AS "Case/Pallet",
                    '' AS Catalog_Page,
                    '' AS Status,
                    '' AS Default_Unit,
                    '' AS Release,
                    '' AS Last_Sale_Qty,
                    '' AS Allocate_Qty,
                    '' AS More_Notes_1,
                    '' AS More_Notes_2,
                    '' AS More_Notes_3,
                    '' AS More_Notes_4,
                    '' AS More_Notes_5,
                    '' AS More_Notes_6,
                    '' AS More_Notes_7,
                    '' AS More_Notes_8
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
                WHERE i.PROD_CD IN (SELECT value FROM SplitValues);
        """
        
        cursor.execute(query, mylist)
        rows = cursor.fetchall()
        
        # Get column headers
        column_headers = [column[0] for column in cursor.description]
        
        # Create CSV in memory
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write headers
        writer.writerow(column_headers)
        
        # Write data rows
        for row in rows:
            writer.writerow(row)
        
        # Convert to bytes
        mem = io.BytesIO()
        mem.write(output.getvalue().encode('utf-8'))
        mem.seek(0)
        output.close()
        
        # Close the cursor and connection
        cursor.close()
        conn.close()
        
        current_app.logger.info(f"User {current_user} generated zhuankuan data for: {user_input}")
        
        # Return the CSV file
        return send_file(
            mem,
            as_attachment=True,
            download_name="zhuankuan_output.csv",
            mimetype="text/csv"
        )
        
    except Exception as e:
        current_app.logger.error(f"Error in zhuankuan process: {str(e)}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500
