from flask import Flask, render_template, current_app,request, redirect, url_for, flash, session, jsonify, Blueprint
import SQL_connector
import pandas as pd
from urllib.parse import quote

master_audit_bp = Blueprint('master_audit', __name__, template_folder='templates')

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Blueprint
import SQL_connector
import pandas as pd
from urllib.parse import quote

master_audit_bp = Blueprint('master_audit', __name__, template_folder='templates')

@master_audit_bp.route("/audit", methods=["GET", "POST"])
def audit():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    # Get form parameters
    search_type = request.form.get('search_type', 'orders')
    database_filter = request.form.get('database_filter', 'both')
    num_orders = int(request.form.get('num_orders', 20))
    days_back_value = request.form.get('days_back', '').strip()
    days_back = int(days_back_value) if days_back_value != '' else None    
    since_date = request.form.get('since_date', '').strip()
    
    # Get orders based on criteria
    try:
        if search_type == 'days' and days_back is not None:

            orders_data = SQL_connector.get_orders_by_criteria(database_filter=database_filter, days_back=days_back)
        elif search_type == 'since' and since_date:
            orders_data = SQL_connector.get_orders_by_criteria(database_filter=database_filter, since_date=since_date)
        else:
            orders_data = SQL_connector.get_orders_by_criteria(num_orders=num_orders, database_filter=database_filter)
        
        if orders_data:
            orders_df = pd.DataFrame.from_records(orders_data, 
                                                columns=['Database', 'Order Number', 'Order Date', 'Customer', 'Order Amount', 'Handling Fee', 'Misc Charge', 'Order Total', 'Total Items'])
            # Convert numeric columns
            numeric_cols = ['Order Amount', 'Handling Fee', 'Misc Charge', 'Order Total']
            for col in numeric_cols:
                orders_df[col] = pd.to_numeric(orders_df[col], errors='coerce').fillna(0).round(2)
            
            orders_df['Total Items'] = pd.to_numeric(orders_df['Total Items'], errors='coerce').fillna(0).astype(int)
            
            # Calculate totals for summary row
            total_order_amount = orders_df['Order Amount'].sum()
            total_order_total = orders_df['Order Total'].sum()
            total_items_sum = orders_df['Total Items'].sum()
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
                'Total Items': f'<strong>{total_items_sum:,}</strong>'
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
            orders_data = []
    except Exception as e:
        orders_html = f"<p>Error loading orders: {str(e)}</p>"
        orders_data = []
    
    # Get sold out SKUs using the existing logic
    try:
        if orders_data:
            # Step 1: Get all unique SKUs from all orders
            all_skus = SQL_connector.get_all_skus_from_orders(orders_data)
            
            if all_skus:
                # Step 2: Check inventory for all SKUs in both databases
                all_inventory = SQL_connector.get_sku_inventory_from_both_databases(all_skus)
                
                # Step 3: Get order quantities for all SKUs
                order_quantities = SQL_connector.get_sku_order_quantities(orders_data, all_skus)
                
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
    
    return render_template("audit.html", 
                         current_user=current_user,
                         orders_html=orders_html,
                         sold_out_html=sold_out_html,
                         search_type=search_type,
                         database_filter=database_filter,
                         num_orders=num_orders,
                         days_back=days_back,
                         since_date=since_date)
    