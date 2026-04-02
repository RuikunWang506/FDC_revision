from flask import Flask, render_template, current_app,request, redirect, url_for, flash, session, jsonify, Blueprint
from py.utilities import SQL_connector
import pandas as pd
from urllib.parse import quote
import os
from werkzeug.utils import secure_filename

master_audit_bp = Blueprint('master_audit', __name__, template_folder='templates')

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@master_audit_bp.route("/audit", methods=["GET", "POST"])
def audit():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    # Initialize variables - ADD sold_out_df HERE
    orders_html = "<p>No orders found.</p>"
    customer_pivot_html = "<p>No customer summary available.</p>"
    sold_out_html = "<p>No sold out items found.</p>"
    matched_inventory_html = ""
    orders_data = []
    sold_out_df = pd.DataFrame()  # Initialize as empty DataFrame
    
    # Get form parameters
    search_type = request.form.get('search_type', 'orders')
    database_filter = request.form.get('database_filter', 'both')
    num_orders = int(request.form.get('num_orders', 0))
    days_back_value = request.form.get('days_back', '').strip()
    days_back = int(days_back_value) if days_back_value != '' else None    
    since_date = request.form.get('since_date', '').strip()
    
    uploaded_file = request.files.get('inventory_file')

    # Get orders based on criteria
    try:
        if search_type == 'days' and days_back is not None:

            orders_data = SQL_connector.get_orders_by_criteria(database_filter=database_filter, days_back=days_back, user=current_user)
        elif search_type == 'since' and since_date:
            orders_data = SQL_connector.get_orders_by_criteria(database_filter=database_filter, since_date=since_date, user=current_user)
        else:
            orders_data = SQL_connector.get_orders_by_criteria(num_orders=num_orders, database_filter=database_filter, user=current_user)
        
        if orders_data:
            orders_df = pd.DataFrame.from_records(orders_data, 
                                                columns=['Database', 'Order Number', 'Order Date', 'Customer', 'Order Amount', 'Handling Fee', 'Misc Charge', 'Order Total', 'Total Items', 'Total Quantity'])
            # Convert numeric columns
            numeric_cols = ['Order Amount', 'Handling Fee', 'Misc Charge', 'Order Total']
            for col in numeric_cols:
                orders_df[col] = pd.to_numeric(orders_df[col], errors='coerce').fillna(0).round(2)
            
            orders_df['Total Items'] = pd.to_numeric(orders_df['Total Items'], errors='coerce').fillna(0).astype(int)
            orders_df['Total Quantity'] = pd.to_numeric(orders_df['Total Quantity'], errors='coerce').fillna(0).astype(int)

            # Build customer-level pivot summary from the same source data as the top orders table.
            pivot_df = orders_df.copy()

            def normalize_customer_name(name):
                customer_name = str(name).strip() if pd.notna(name) else ''
                lowered = customer_name.lower()
                if 'sam moon' in lowered:
                    return 'Sam Moon (All Locations)'
                if 'fashion q' in lowered:
                    return 'Fashion Q (All Locations)'
                return customer_name

            pivot_df['Customer'] = pivot_df['Customer'].apply(normalize_customer_name)
            customer_pivot_df = pivot_df.groupby('Customer', dropna=False).agg({
                'Order Amount': 'sum',
                'Total Items': 'sum',
                'Total Quantity': 'sum',
                'Order Number': 'nunique'
            }).reset_index().rename(columns={'Order Number': 'Number of Orders'})

            # Build true unique styles per customer from distinct order+SKU rows.
            customer_key_df = pivot_df[['Database', 'Order Number', 'Customer']].copy()
            customer_key_df['Order Number'] = customer_key_df['Order Number'].astype(str)
            customer_key_df = customer_key_df.drop_duplicates(subset=['Database', 'Order Number'])

            order_sku_rows = SQL_connector.get_order_skus_for_orders(orders_data)
            order_sku_qty_rows = SQL_connector.get_order_sku_quantities_for_orders(orders_data)
            unique_styles_df = pd.DataFrame(columns=['Customer', 'Unique Styles'])
            prefix_columns = ['S(ES)', 'N(EN)', 'BN', 'BT', 'GN', 'M(EM)', 'L', 'GT', 'T(ET)', 'TJ', 'Other']
            prefix_qty_df = pd.DataFrame(columns=['Customer'] + prefix_columns)
            prefix_revenue_df = pd.DataFrame(columns=['Customer'] + prefix_columns)
            asp_column_map = {col: f'{col}均价' for col in prefix_columns}

            if order_sku_rows:
                sku_df = pd.DataFrame.from_records(order_sku_rows, columns=['Database', 'Order Number', 'SKU'])
                sku_df['Order Number'] = sku_df['Order Number'].astype(str)
                sku_df['SKU'] = sku_df['SKU'].astype(str).str.strip()

                sku_with_customer_df = sku_df.merge(
                    customer_key_df,
                    on=['Database', 'Order Number'],
                    how='left'
                )
                sku_with_customer_df = sku_with_customer_df.dropna(subset=['Customer', 'SKU'])

                unique_styles_df = sku_with_customer_df.groupby('Customer').agg({
                    'SKU': 'nunique'
                }).reset_index().rename(columns={'SKU': 'Unique Styles'})

            if order_sku_qty_rows:
                sku_qty_df = pd.DataFrame.from_records(
                    order_sku_qty_rows,
                    columns=['Database', 'Order Number', 'SKU', 'Qty', 'Revenue']
                )
                sku_qty_df['Order Number'] = sku_qty_df['Order Number'].astype(str)
                sku_qty_df['SKU'] = sku_qty_df['SKU'].astype(str).str.strip().str.upper()
                sku_qty_df['Qty'] = pd.to_numeric(sku_qty_df['Qty'], errors='coerce').fillna(0)
                sku_qty_df['Revenue'] = pd.to_numeric(sku_qty_df['Revenue'], errors='coerce').fillna(0)

                sku_qty_with_customer_df = sku_qty_df.merge(
                    customer_key_df,
                    on=['Database', 'Order Number'],
                    how='left'
                )
                sku_qty_with_customer_df = sku_qty_with_customer_df.dropna(subset=['Customer'])

                def get_prefix_bucket(sku):
                    s = str(sku).strip().upper()
                    if s.startswith('ES') or s.startswith('S'):
                        return 'S(ES)'
                    if s.startswith('EN') or s.startswith('N'):
                        return 'N(EN)'
                    if s.startswith('BN'):
                        return 'BN'
                    if s.startswith('BT'):
                        return 'BT'
                    if s.startswith('GN'):
                        return 'GN'
                    if s.startswith('EM') or s.startswith('M'):
                        return 'M(EM)'
                    if s.startswith('L'):
                        return 'L'
                    if s.startswith('GT'):
                        return 'GT'
                    if s.startswith('TJ'):
                        return 'TJ'
                    if s.startswith('ET') or s.startswith('T'):
                        return 'T(ET)'
                    return 'Other'

                sku_qty_with_customer_df['Prefix Bucket'] = sku_qty_with_customer_df['SKU'].apply(get_prefix_bucket)
                bucketed_df = sku_qty_with_customer_df

                if not bucketed_df.empty:
                    prefix_qty_df = bucketed_df.groupby(['Customer', 'Prefix Bucket'])['Qty'].sum().unstack(fill_value=0).reset_index()
                    prefix_revenue_df = bucketed_df.groupby(['Customer', 'Prefix Bucket'])['Revenue'].sum().unstack(fill_value=0).reset_index()
                    for col in prefix_columns:
                        if col not in prefix_qty_df.columns:
                            prefix_qty_df[col] = 0
                        if col not in prefix_revenue_df.columns:
                            prefix_revenue_df[col] = 0
                    prefix_qty_df = prefix_qty_df[['Customer'] + prefix_columns]
                    prefix_revenue_df = prefix_revenue_df[['Customer'] + prefix_columns]

            customer_pivot_df = customer_pivot_df.merge(unique_styles_df, on='Customer', how='left')
            customer_pivot_df['Unique Styles'] = customer_pivot_df['Unique Styles'].fillna(0).astype(int)
            customer_pivot_df = customer_pivot_df.merge(prefix_qty_df, on='Customer', how='left')
            customer_pivot_df = customer_pivot_df.merge(prefix_revenue_df, on='Customer', how='left', suffixes=('', ' Revenue'))

            for col in prefix_columns:
                if col not in customer_pivot_df.columns:
                    customer_pivot_df[col] = 0
                customer_pivot_df[col] = pd.to_numeric(customer_pivot_df[col], errors='coerce').fillna(0).astype(int)

                revenue_col = f'{col} Revenue'
                if revenue_col not in customer_pivot_df.columns:
                    customer_pivot_df[revenue_col] = 0
                customer_pivot_df[revenue_col] = pd.to_numeric(customer_pivot_df[revenue_col], errors='coerce').fillna(0)

                asp_col = asp_column_map[col]
                customer_pivot_df[asp_col] = customer_pivot_df.apply(
                    lambda row: (row[revenue_col] / row[col]) if row[col] > 0 else 0,
                    axis=1
                )

            ordered_prefix_cols = []
            for col in prefix_columns:
                ordered_prefix_cols.append(col)
                ordered_prefix_cols.append(asp_column_map[col])

            prefix_qty_totals = {col: float(customer_pivot_df[col].sum()) for col in prefix_columns}
            prefix_revenue_totals = {
                col: float(customer_pivot_df[f'{col} Revenue'].sum()) if f'{col} Revenue' in customer_pivot_df.columns else 0.0
                for col in prefix_columns
            }

            def format_prefix_avg(prefix_name):
                qty_total = prefix_qty_totals.get(prefix_name, 0.0)
                revenue_total = prefix_revenue_totals.get(prefix_name, 0.0)
                if qty_total > 0:
                    return f"<strong>${(revenue_total / qty_total):,.2f}</strong>"
                return '<strong>$0.00</strong>'

            customer_pivot_df = customer_pivot_df[[
                'Customer', 'Order Amount', 'Number of Orders', 'Total Items', 'Unique Styles', 'Total Quantity',
                *ordered_prefix_cols
            ]]

            customer_pivot_df = customer_pivot_df.sort_values('Order Amount', ascending=False)

            pivot_total_row = pd.DataFrame([{
                'Customer': '<strong>TOTALS</strong>',
                'Order Amount': f"<strong>${customer_pivot_df['Order Amount'].sum():,.2f}</strong>",
                'Number of Orders': f"<strong>{int(customer_pivot_df['Number of Orders'].sum()):,}</strong>",
                'Total Items': f"<strong>{int(customer_pivot_df['Total Items'].sum()):,}</strong>",
                'Unique Styles': f"<strong>{int(customer_pivot_df['Unique Styles'].sum()):,}</strong>",
                'Total Quantity': f"<strong>{int(customer_pivot_df['Total Quantity'].sum()):,}</strong>",
                'S(ES)': f"<strong>{int(customer_pivot_df['S(ES)'].sum()):,}</strong>",
                'S(ES)均价': format_prefix_avg('S(ES)'),
                'N(EN)': f"<strong>{int(customer_pivot_df['N(EN)'].sum()):,}</strong>",
                'N(EN)均价': format_prefix_avg('N(EN)'),
                'BN': f"<strong>{int(customer_pivot_df['BN'].sum()):,}</strong>",
                'BN均价': format_prefix_avg('BN'),
                'BT': f"<strong>{int(customer_pivot_df['BT'].sum()):,}</strong>",
                'BT均价': format_prefix_avg('BT'),
                'GN': f"<strong>{int(customer_pivot_df['GN'].sum()):,}</strong>",
                'GN均价': format_prefix_avg('GN'),
                'M(EM)': f"<strong>{int(customer_pivot_df['M(EM)'].sum()):,}</strong>",
                'M(EM)均价': format_prefix_avg('M(EM)'),
                'L': f"<strong>{int(customer_pivot_df['L'].sum()):,}</strong>",
                'L均价': format_prefix_avg('L'),
                'GT': f"<strong>{int(customer_pivot_df['GT'].sum()):,}</strong>",
                'GT均价': format_prefix_avg('GT'),
                'T(ET)': f"<strong>{int(customer_pivot_df['T(ET)'].sum()):,}</strong>",
                'T(ET)均价': format_prefix_avg('T(ET)'),
                'TJ': f"<strong>{int(customer_pivot_df['TJ'].sum()):,}</strong>",
                'TJ均价': format_prefix_avg('TJ'),
                'Other': f"<strong>{int(customer_pivot_df['Other'].sum()):,}</strong>",
                'Other均价': format_prefix_avg('Other'),
            }])

            customer_pivot_df['Order Amount'] = customer_pivot_df['Order Amount'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['Number of Orders'] = customer_pivot_df['Number of Orders'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['Total Items'] = customer_pivot_df['Total Items'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['Unique Styles'] = customer_pivot_df['Unique Styles'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['Total Quantity'] = customer_pivot_df['Total Quantity'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['S(ES)'] = customer_pivot_df['S(ES)'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['N(EN)'] = customer_pivot_df['N(EN)'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['BN'] = customer_pivot_df['BN'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['BT'] = customer_pivot_df['BT'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['GN'] = customer_pivot_df['GN'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['M(EM)'] = customer_pivot_df['M(EM)'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['L'] = customer_pivot_df['L'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['GT'] = customer_pivot_df['GT'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['GT均价'] = customer_pivot_df['GT均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['T(ET)'] = customer_pivot_df['T(ET)'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['T(ET)均价'] = customer_pivot_df['T(ET)均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['TJ'] = customer_pivot_df['TJ'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['TJ均价'] = customer_pivot_df['TJ均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['Other'] = customer_pivot_df['Other'].map(lambda x: f"{int(x):,}")
            customer_pivot_df['Other均价'] = customer_pivot_df['Other均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['S(ES)均价'] = customer_pivot_df['S(ES)均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['N(EN)均价'] = customer_pivot_df['N(EN)均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['BN均价'] = customer_pivot_df['BN均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['BT均价'] = customer_pivot_df['BT均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['GN均价'] = customer_pivot_df['GN均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['M(EM)均价'] = customer_pivot_df['M(EM)均价'].map(lambda x: f"${x:,.2f}")
            customer_pivot_df['L均价'] = customer_pivot_df['L均价'].map(lambda x: f"${x:,.2f}")

            customer_pivot_with_totals = pd.concat([customer_pivot_df, pivot_total_row], ignore_index=True)
            customer_pivot_with_totals = customer_pivot_with_totals.rename(columns={
                'Customer': 'customer',
                'Order Amount': '销售额',
                'Number of Orders': '总单数',
                'Total Items': '款次',
                'Unique Styles': '款数',
                'Total Quantity': '共计（只包）'
            })
            customer_pivot_html = customer_pivot_with_totals.to_html(
                index=False,
                classes="table table-bordered table-striped table-sm",
                table_id="customer_pivot_table",
                escape=False
            )
            
            # Calculate totals for summary row
            total_order_amount = orders_df['Order Amount'].sum()
            total_order_total = orders_df['Order Total'].sum()
            total_items_sum = orders_df['Total Items'].sum()
            total_quantity_sum = orders_df['Total Quantity'].sum()
            order_count = len(orders_df)
            
            # Add hyperlinks to order numbers
            orders_df['Order Number'] = orders_df['Order Number'].apply(
                lambda x: f'<a href="https://www.fdcef.com/order_lookup/{x}" target="_blank">{x}</a>'
            )
            
            # Create summary row
            summary_row = pd.DataFrame([{
                'Database': '<strong>TOTALS</strong>',
                'Order Number': f'<strong>{order_count} orders</strong>',
                'Order Date': '',
                'Customer': '',
                'Order Amount': f'<strong>${total_order_amount:,.2f}</strong>',
                'Handling Fee': '',
                'Misc Charge': '',
                'Order Total': f'<strong>${total_order_total:,.2f}</strong>',
                'Total Items': f'<strong>{total_items_sum:,}</strong>',
                'Total Quantity': f'<strong>{total_quantity_sum:,}</strong>'
            }])
            
            # Append summary row to the dataframe
            orders_df_with_totals = pd.concat([orders_df, summary_row], ignore_index=True)
            
            orders_html = orders_df_with_totals.to_html(
                index=False,
                classes="table table-bordered table-striped table-sm",
                table_id="orders_table",
                escape=False  # Allow HTML in the table
            )
        else:
            orders_html = "<p>No orders found for the selected criteria.</p>"
            customer_pivot_html = "<p>No customer summary available for the selected criteria.</p>"
            orders_data = []
    except Exception as e:
        orders_html = f"<p>Error loading orders: {str(e)}</p>"
        customer_pivot_html = f"<p>Error loading customer summary: {str(e)}</p>"
        orders_data = []
    
    # Get sold out SKUs using the existing logic
    try:
        if orders_data:
            # Step 1: Get all unique SKUs from all orders
            all_skus = SQL_connector.get_all_skus_from_orders(orders_data)
            
            if all_skus:
                # Step 2: Check inventory for all SKUs in both databases
                all_inventory = SQL_connector.get_sku_inventory_from_both_databases(all_skus, current_user)
                
                # Step 3: Get order quantities for all SKUs
                order_quantities = SQL_connector.get_sku_order_quantities(orders_data, all_skus, current_user)
                
                if all_inventory:
                    # Create DataFrame from inventory data
                    inventory_df = pd.DataFrame.from_records(all_inventory,
                                                           columns=['Database', 'SKU', 'Style', 'Description', 'Stock', 'Ordered', 'Available', 'Incoming', 'Price'])
                    
                    # Create DataFrame from order quantities
                    order_df = pd.DataFrame.from_records(order_quantities,
                                                       columns=['Order_Database', 'Order Number', 'SKU', 'Qty in Order'])
                    
                    # Group order quantities by SKU and collect order numbers
                    order_summary = order_df.groupby('SKU').agg({
                        'Qty in Order': 'sum',
                        'Order Number': lambda x: sorted(set(x))
                    }).reset_index()
                    
                    # Merge inventory with order data
                    merged_df = inventory_df.merge(order_summary, on='SKU', how='left')
                    merged_df['Qty in Order'] = merged_df['Qty in Order'].fillna(0)
                    merged_df['Order Number'] = merged_df['Order Number'].fillna('').apply(lambda x: x if isinstance(x, list) else [])
                    
                    # Convert numeric columns
                    numeric_cols = ['Stock', 'Ordered', 'Available', 'Incoming', 'Price', 'Qty in Order']
                    for col in numeric_cols:
                        merged_df[col] = pd.to_numeric(merged_df[col], errors='coerce').fillna(0)
                    
                    # Create pivot table by Style and SKU, combining data from both databases
                    pivot_df = merged_df.groupby(['Style', 'SKU']).agg({
                        'Stock': 'sum',
                        'Ordered': 'sum', 
                        'Available': 'sum',
                        'Incoming': 'sum',
                        'Qty in Order': 'sum',
                        'Order Number': lambda x: sorted(set([item for sublist in x for item in sublist if sublist]))  # Flatten and deduplicate
                    }).reset_index()
                    
                    # Filter for sold out items (Available <= 0)
                    sold_out_df = pivot_df[pivot_df['Available'] <= 0].copy()
                    
                    if not sold_out_df.empty:
                        # Create hyperlinks for order numbers
                        def create_order_links(order_list):
                            if not order_list:
                                return ''
                            links = []
                            for order_num in order_list:
                                links.append(f'<a href="https://www.fdcef.com/order_lookup/{order_num}" target="_blank">{order_num}</a>')
                            return ', '.join(links)
                        
                        sold_out_df['Order Numbers'] = sold_out_df['Order Number'].apply(create_order_links)
                        
                        # Add hyperlinks for Style and SKU columns
                        sold_out_df['Style'] = sold_out_df['Style'].apply(
                            lambda x: f'<a href="/item_lookup/{quote(str(x).strip())}">{x}</a>' if pd.notna(x) else x
                        )
                        
                        sold_out_df['SKU'] = sold_out_df['SKU'].apply(
                            lambda x: f'<a href="/item_lookup/{quote(str(x).strip())}">{x}</a>' if pd.notna(x) else x
                        )
                        
                        # Drop the original Order Number column and reorder
                        sold_out_df = sold_out_df.drop('Order Number', axis=1)
                        
                        # Rename columns for better display
                        sold_out_df.columns = ['Style', 'SKU', 'Total Stock', 'Total Ordered', 'Total Available', 'Total Incoming', 'Total Qty in Orders', 'Order Numbers']
                        
                        # Convert to integers where appropriate
                        int_cols = ['Total Stock', 'Total Ordered', 'Total Available', 'Total Incoming', 'Total Qty in Orders']
                        for col in int_cols:
                            sold_out_df[col] = sold_out_df[col].astype(int)
                        # Calculate totals for summary row
                        total_stock_sum = sold_out_df['Total Stock'].sum()
                        total_ordered_sum = sold_out_df['Total Ordered'].sum()
                        total_available_sum = sold_out_df['Total Available'].sum()
                        total_incoming_sum = sold_out_df['Total Incoming'].sum()
                        total_qty_in_orders_sum = sold_out_df['Total Qty in Orders'].sum()
                        sold_out_count = len(sold_out_df)

                        # Create summary row for sold out items
                        sold_out_summary_row = pd.DataFrame([{
                            'Style': '<strong>TOTALS</strong>',
                            'SKU': f'<strong>{sold_out_count} items</strong>',
                            'Total Stock': f'<strong>{total_stock_sum:,}</strong>',
                            'Total Ordered': f'<strong>{total_ordered_sum:,}</strong>',
                            'Total Available': f'<strong>{total_available_sum:,}</strong>',
                            'Total Incoming': f'<strong>{total_incoming_sum:,}</strong>',
                            'Total Qty in Orders': f'<strong>{total_qty_in_orders_sum:,}</strong>',
                            'Order Numbers': ''
                        }])
                        sold_out_df_with_totals = pd.concat([sold_out_df, sold_out_summary_row], ignore_index=True)


                        sold_out_html = sold_out_df_with_totals.to_html(

                            index=False,
                            classes="table table-bordered table-striped table-sm",
                            table_id="sold_out_table",
                            escape=False  # Allow HTML in the table
                        )
                    else:
                        sold_out_html = "<p>No sold out SKUs found in the selected orders.</p>"
                else:
                    sold_out_html = "<p>No inventory data found for SKUs in the selected orders.</p>"
            else:
                sold_out_html = "<p>No SKUs found in the selected orders.</p>"
        else:
            sold_out_html = "<p>No orders to analyze.</p>"
    except Exception as e:
        sold_out_html = f"<p>Error loading sold out items: {str(e)}</p>"
    # Process uploaded Excel file if provided
    matched_inventory_html = ""
    if uploaded_file and allowed_file(uploaded_file.filename) and not sold_out_df.empty:
        try:
            # Create uploads directory if it doesn't exist
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            
            # Save uploaded file
            filename = secure_filename(uploaded_file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            uploaded_file.save(filepath)
            
            # Read Excel file
            excel_df = pd.read_excel(filepath)
            
            # Clean up the file after reading
            os.remove(filepath)
            
            # Check if required columns exist
            required_columns = ['Stock SKU', 'On Hand', 'Available', 'Reserved']
            missing_columns = [col for col in required_columns if col not in excel_df.columns]
            
            if missing_columns:
                matched_inventory_html = f"<p>Error: Missing required columns in Excel file: {', '.join(missing_columns)}</p>"
            else:
                # Clean and prepare data
                excel_df['Stock SKU'] = excel_df['Stock SKU'].astype(str).str.strip().str.upper()
                
                # Convert numeric columns
                for col in ['On Hand', 'Available', 'Reserved']:
                    excel_df[col] = pd.to_numeric(excel_df[col], errors='coerce').fillna(0)
                
                # Get SKUs from sold out table (remove HTML links for matching)
                sold_out_skus = sold_out_df['SKU'].apply(
                    lambda x: x.split('>')[-2].split('<')[0] if '<a href=' in str(x) else str(x)
                ).str.strip().str.upper()
                
                # Find matches between sold out SKUs and Excel SKUs
                matched_skus = []
                for i, sku in enumerate(sold_out_skus):
                    if sku in excel_df['Stock SKU'].values:
                        excel_row = excel_df[excel_df['Stock SKU'] == sku].iloc[0]
                        
                        # Get original sold out row data
                        sold_out_row = sold_out_df.iloc[i].copy()
                        
                        matched_skus.append({
                            'Style': sold_out_row['Style'],
                            'SKU': sold_out_row['SKU'],
                            'DB Total Stock': sold_out_row['Total Stock'],
                            'DB Total Ordered': sold_out_row['Total Ordered'],
                            'DB Total Available': sold_out_row['Total Available'],
                            'DB Total Incoming': sold_out_row['Total Incoming'],
                            'Excel On Hand': int(excel_row['On Hand']),
                            'Excel Available': int(excel_row['Available']),
                            'Excel Reserved': int(excel_row['Reserved']),
                            'Combined Available': sold_out_row['Total Available'] + int(excel_row['Available']),
                            'Total Qty in Orders': sold_out_row['Total Qty in Orders'],
                            'Order Numbers': sold_out_row['Order Numbers']
                        })
                
                if matched_skus:
                    matched_df = pd.DataFrame(matched_skus)
                    
                    # Calculate totals for matched inventory summary
                    matched_count = len(matched_df)
                    db_stock_sum = matched_df['DB Total Stock'].sum()
                    db_ordered_sum = matched_df['DB Total Ordered'].sum()
                    db_available_sum = matched_df['DB Total Available'].sum()
                    db_incoming_sum = matched_df['DB Total Incoming'].sum()
                    excel_on_hand_sum = matched_df['Excel On Hand'].sum()
                    excel_available_sum = matched_df['Excel Available'].sum()
                    excel_reserved_sum = matched_df['Excel Reserved'].sum()
                    combined_available_sum = matched_df['Combined Available'].sum()
                    total_qty_orders_sum = matched_df['Total Qty in Orders'].sum()
                    
                    # Create summary row for matched inventory
                    matched_summary_row = pd.DataFrame([{
                        'Style': '<strong>TOTALS</strong>',
                        'SKU': f'<strong>{matched_count} matched items</strong>',
                        'DB Total Stock': f'<strong>{db_stock_sum:,}</strong>',
                        'DB Total Ordered': f'<strong>{db_ordered_sum:,}</strong>',
                        'DB Total Available': f'<strong>{db_available_sum:,}</strong>',
                        'DB Total Incoming': f'<strong>{db_incoming_sum:,}</strong>',
                        'Excel On Hand': f'<strong>{excel_on_hand_sum:,}</strong>',
                        'Excel Available': f'<strong>{excel_available_sum:,}</strong>',
                        'Excel Reserved': f'<strong>{excel_reserved_sum:,}</strong>',
                        'Combined Available': f'<strong>{combined_available_sum:,}</strong>',
                        'Total Qty in Orders': f'<strong>{total_qty_orders_sum:,}</strong>',
                        'Order Numbers': ''
                    }])
                    
                    matched_df_with_totals = pd.concat([matched_df, matched_summary_row], ignore_index=True)
                    
                    matched_inventory_html = matched_df_with_totals.to_html(
                        index=False,
                        classes="table table-bordered table-striped table-sm",
                        table_id="matched_inventory_table",
                        escape=False
                    )
                else:
                    matched_inventory_html = "<p>No matching SKUs found between sold out items and uploaded Excel file.</p>"
                    
        except Exception as e:
            matched_inventory_html = f"<p>Error processing Excel file: {str(e)}</p>"
    return render_template("audit.html", 
                         current_user=current_user,
                         orders_html=orders_html,
                         sold_out_html=sold_out_html,
                         search_type=search_type,
                         database_filter=database_filter,
                         num_orders=num_orders,
                         days_back=days_back,
                         since_date=since_date,
                         customer_pivot_html=customer_pivot_html,
                         matched_inventory_html=matched_inventory_html,
                         show_upload_section=not sold_out_df.empty)
    