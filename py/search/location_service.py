from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file, jsonify
import mysql.connector
from mysql.connector import Error
import pandas as pd
import os
from datetime import datetime
from io import BytesIO
import json
import pyodbc
from flask import current_app
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection, get_sql_server_connection_fdc, get_sql_server_connection_oms_ef

 

# Create a Blueprint for location_service
location_service_bp = Blueprint('location_service', __name__, template_folder='../templates')

# Database configurations from settings
db_config = Config.DB_CONFIG
sql_server_config = Config.SQL_SERVER_CONFIG

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def allowed_file(filename):
    """Check if the uploaded file is an allowed Excel file."""
    allowed_extensions = {'xlsx', 'xls'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

@location_service_bp.route('/location_service', methods=['GET', 'POST'])
def location_service():
    current_app.logger.info(f"***{session.get('username', 'Unknown')}***___@@@Accessed@@@__Location Service")

    allowed_ip = '173.198.50.202'
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    if 'username' in session:
        # User is logged in; use the session username
        current_user = session['username']
    else:
        if user_ip == allowed_ip:
            # User IP is allowed; set username to 'local_user'
            current_user = 'local_user'
            session['username'] = current_user
        else:
            # User is not logged in and IP is not allowed; redirect to login
            session['next'] = request.url
            return redirect(url_for("auth.login"))

    search_results = []
    all_products = []
    search_inputs = []

    action = 'search'  # Default action

    try:
        connection = get_db_connection()
        if connection is None:
            flash("Failed to connect to the database.", 'error')
            return redirect(url_for('location_service.location_service'))

        cursor = connection.cursor(dictionary=True)
        

        if request.method == 'POST':
            action = request.form.get('action', 'search')

            if action == 'search':
                # Deserialize the JSON string into a Python list
                search_inputs = request.form.get('search_input[]', '[]')

                if action == 'search':
                    # Deserialize the JSON string into a Python list
                    search_inputs = request.form.get('search_input[]', '[]')

                    if isinstance(search_inputs, str):
                        try:
                            search_inputs = json.loads(search_inputs)
                        except json.JSONDecodeError:
                            search_inputs = [search_inputs]  # Treat as a single search term if not valid JSON

                    # Ensure search_inputs is always a list
                    if not isinstance(search_inputs, list):
                        search_inputs = [search_inputs]

                    # Clean up inputs
                    search_inputs = [input.strip() for input in search_inputs if input.strip()]

                    # Debug: Log the search inputs
                    print(f"Search inputs: {search_inputs}")

                    # Ensure at least one valid input is provided
                    if not search_inputs:
                        flash('Please enter at least one SKU or Style to search.', 'error')
                        return redirect(url_for('location_service.location_service'))

                    # Prepare placeholders for query based on the number of search inputs
                    placeholders = ' OR '.join([f"SKU LIKE %s OR Style LIKE %s"] * len(search_inputs))

                    # Final query string
                    query = f"SELECT * FROM products WHERE {placeholders}"

                    # Add wildcards to each search input
                    wildcard_search_inputs = [f"%{input}%" for input in search_inputs]

                    # Debug: Log the query and parameters
                    print(f"Executing query: {query} with params: {wildcard_search_inputs * 2}")

                    # Execute query, passing each input for both SKU and Style
                    cursor.execute(query, wildcard_search_inputs * 2)
                    search_results = cursor.fetchall()

                    # Debug: Log the search results
                    print(f"Search results: {search_results}")

                    if not search_results:
                        flash(f"No matching products found for: {', '.join(search_inputs)}", 'error')
                    else:
                        flash('Products found. You can update their locations below.', 'message')
                        session['last_search'] = search_inputs  # Store search inputs in session

            elif action == 'update_multiple':
                # Determine the number of rows by counting 'original_sku_' fields
                num_rows = len([key for key in request.form.keys() if key.startswith('original_sku_')])
                updated_count = 0

                # Collect all original SKUs
                original_skus = []

                # Loop over the number of rows and process each one
                for i in range(1, num_rows + 1):
                    original_sku = request.form.get(f'original_sku_{i}', '').strip()
                    original_style = request.form.get(f'original_style_{i}', '').strip()
                    new_location = (request.form.get(f'new_location_{i}') or '').strip()

                    # Add original SKU to the list
                    if original_sku:
                        original_skus.append(original_sku)

                    if new_location:  # Only update if new location is provided
                        update_query = """
                            UPDATE products 
                            SET Location = %s, Update_Date = NOW()
                            WHERE SKU = %s AND Style = %s
                        """
                        cursor.execute(update_query, (new_location, original_sku, original_style))
                        if cursor.rowcount > 0:
                            updated_count += 1

                connection.commit()

                if updated_count > 0:
                    flash(f'Updated {updated_count} product(s) successfully.', 'message')
                else:
                    flash('No locations were updated. Please check your inputs.', 'error')

                # Re-fetch the updated products using the original SKU list
                if original_skus:
                    # Prepare placeholders for query based on the number of original SKUs
                    placeholders = ', '.join(['%s'] * len(original_skus))

                    # Query to fetch updated products using the original SKUs
                    query = f"SELECT * FROM products WHERE SKU IN ({placeholders})"
                    
                    # Execute query with the original SKUs
                    cursor.execute(query, original_skus)
                    search_results = cursor.fetchall()

                    if not search_results:
                        flash('No matching products found after update.', 'error')
                    else:
                        flash('Products updated successfully.', 'message')
                else:
                    search_results = None
                    flash('Please enter a search query.', 'warning')

            elif action == 'viewall':
                # Fetch all products without pagination
                select_query = "SELECT * FROM products ORDER BY Update_Date DESC"
                cursor.execute(select_query)
                all_products = cursor.fetchall()
                if not all_products:
                    flash('No products available to display.', 'error')
                else:
                    flash('Displaying all products.', 'message')

            elif action == 'add':
                # Handle adding new products
                new_skus = request.form.getlist('new_sku')
                new_styles = request.form.getlist('new_style')
                new_locations = request.form.getlist('new_location')

                # Validate that all lists have the same length
                if not (len(new_skus) == len(new_styles) == len(new_locations)):
                    flash('Mismatch in the number of SKUs, Styles, and Locations provided.', 'error')
                    return redirect(url_for('location_service.location_service', action='add'))

                # Initialize counters and lists
                added_count = 0
                duplicate_count = 0
                invalid_entries = []
                added_skus = []  # List to store SKUs of newly added products

                insert_query = """
                    INSERT INTO products (SKU, Style, Location, Update_Date)
                    VALUES (%s, %s, %s, NOW())
                """

                check_query = "SELECT * FROM products WHERE SKU = %s AND Style = %s"

                for sku, style, location in zip(new_skus, new_styles, new_locations):
                    sku = sku.strip()
                    style = style.strip()
                    location = location.strip()

                    # Validate input fields
                    if not sku or not style or not location:
                        invalid_entries.append((sku, style, location))
                        continue

                    # Check for duplicates
                    cursor.execute(check_query, (sku, style))
                    existing_product = cursor.fetchone()
                    if existing_product:
                        duplicate_count += 1
                        continue

                    # Insert new product
                    cursor.execute(insert_query, (sku, style, location))
                    added_count += 1
                    added_skus.append(sku)  # Add SKU to the list of added SKUs

                connection.commit()

                # Prepare flash messages
                if added_count > 0:
                    flash(f'Added {added_count} new product(s) successfully.', 'message')
                if duplicate_count > 0:
                    flash(f'Skipped {duplicate_count} duplicate product(s).', 'warning')
                if invalid_entries:
                    flash(f'Skipped {len(invalid_entries)} invalid product(s) due to missing fields.', 'error')

                # Fetch details of newly added products
                if added_skus:
                    placeholders = ', '.join(['%s'] * len(added_skus))
                    query = f"SELECT * FROM products WHERE SKU IN ({placeholders})"
                    cursor.execute(query, added_skus)
                    new_product_details = cursor.fetchall()
                else:
                    new_product_details = []

                # Pass new product details to the template
                return render_template('location_service.html',
                                    search_results=new_product_details,
                                    all_products=all_products,
                                    action='search',
                                    current_user=current_user)

            elif action == 'import_export':
                if 'import_export_action' not in request.form:
                    flash('No import/export action specified.', 'error')
                else:
                    import_export_action = request.form.get('import_export_action', '').lower()

                    if import_export_action == 'export':
                        # Export the entire table to Excel
                        try:
                            select_query = "SELECT * FROM products"
                            cursor.execute(select_query)
                            products = cursor.fetchall()
                            if not products:
                                flash('No products available to export.', 'error')
                            else:
                                df = pd.DataFrame(products)
                                # Convert datetime objects to string to prevent serialization issues
                                if 'Update_Date' in df.columns:
                                    df['Update_Date'] = df['Update_Date'].apply(
                                        lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if x else ''
                                    )
                                # Generate filename with current datetime
                                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                                filename = f'products_backup_{timestamp}.xlsx'
                                
                                # Create Excel file in memory
                                from io import BytesIO
                                bio = BytesIO()
                                df.to_excel(bio, index=False)
                                bio.seek(0)

                                flash('Products exported successfully.', 'message')
                                # Send the file for download directly from memory
                                return send_file(
                                    bio, 
                                    as_attachment=True, 
                                    download_name=filename,
                                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                                )
                        except Exception as e:
                            flash(f'An error occurred during export: {e}', 'error')

                    elif import_export_action == 'import':
                        # Import the uploaded Excel file
                        try:
                            # Fetch current products for backup
                            cursor.execute("SELECT * FROM products")
                            products = cursor.fetchall()  # Ensure 'products' is assigned here

                            if 'import_file' not in request.files:
                                flash('No file part in the request.', 'error')
                                return redirect(url_for('location_service.location_service', action='import_export'))

                            file = request.files['import_file']
                            if file.filename == '':
                                flash('No file selected for uploading.', 'error')
                                return redirect(url_for('location_service.location_service', action='import_export'))

                            if file and allowed_file(file.filename):
                                # Read the Excel file into a DataFrame
                                df = pd.read_excel(file, engine='openpyxl')

                                # Validate required columns
                                required_columns = {'SKU', 'Style', 'Location'}
                                if not required_columns.issubset(set(df.columns)):
                                    flash('Uploaded Excel file is missing required columns: SKU, Style, Location.', 'error')
                                    return redirect(url_for('location_service.location_service', action='import_export'))

                                # Optional: Additional data validation can be added here

                                # Backup current table before wiping (in memory, not saved to disk)
                                backup_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                                if products:
                                    backup_df = pd.DataFrame(products)
                                    # Convert datetime objects to string
                                    if 'Update_Date' in backup_df.columns:
                                        backup_df['Update_Date'] = backup_df['Update_Date'].apply(
                                            lambda x: x.strftime('%Y-%m-%d %H:%M:%S') if x else ''
                                        )
                                    # Store backup in session for potential download if import fails
                                    session['backup_data'] = backup_df.to_dict('records')
                                    session['backup_timestamp'] = backup_timestamp
                                else:
                                    flash('No existing products to backup before import.', 'warning')

                                # Wipe the current table
                                cursor.execute("DELETE FROM products")
                                connection.commit()

                                # Insert new data
                                insert_query = """
                                    INSERT INTO products (SKU, Style, Location, Update_Date, TMHK_SKUID, TMUS_SKUID, TKUS_SKUID, TKUS_PICKID, TKUS_PNUM)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """

                                insert_data = []
                                for _, row in df.iterrows():
                                    sku = str(row['SKU']).strip()
                                    style = str(row['Style']).strip()
                                    location = str(row['Location']).strip()
                                    update_date = str(row['Update_Date']).strip()
                                    tmhk_skuid = str(row.get('TMHK_SKUID', '')).strip()
                                    tmus_skuid = str(row.get('TMUS_SKUID', '')).strip()
                                    tkus_skuid = str(row.get('TKUS_SKUID', '')).strip()
                                    tkus_pickid = str(row.get('TKUS_PICKID', '')).strip()
                                    tkus_pnum = str(row.get('TKUS_PNUM', '')).strip()

                                    insert_data.append((sku, style, location, update_date, tmhk_skuid, tmus_skuid, tkus_skuid, tkus_pickid, tkus_pnum))
                                
                                cursor.executemany(insert_query, insert_data)
                                connection.commit()

                                flash('Products imported and database updated successfully.', 'message')
                                return redirect(url_for('location_service.location_service', action='viewall'))
                            else:
                                flash('Allowed file types are Excel files (.xlsx, .xls).', 'error')
                                return redirect(url_for('location_service.location_service', action='import_export'))
                        except Exception as e:
                            flash(f'An error occurred while importing the file: {e}', 'error')
                            return redirect(url_for('location_service.location_service', action='import_export'))
                    else:
                        flash('Unknown import/export action.', 'error')
            else:
                flash('Unknown action.', 'error')

        elif request.method == 'GET':
            action = request.args.get('action', 'search')

            if action == 'search':
                # Retrieve the last search query from the session if available
                search_inputs = []

                if search_inputs:
                    # Clean up inputs in case they have extra spaces
                    search_inputs = [input.strip() for input in search_inputs if input.strip()]

                    if search_inputs:
                        # Prepare placeholders for query based on the number of search inputs
                        placeholders = ', '.join(['%s'] * len(search_inputs))

                        # Modify the query to search for multiple SKUs or Styles using the IN clause
                        query = f"SELECT * FROM products WHERE SKU IN ({placeholders}) OR Style IN ({placeholders})"
                        cursor.execute(query, search_inputs + search_inputs)  # Pass inputs for both SKU and Style
                        search_results = cursor.fetchall()  # Fetch results

                        if not search_results:
                            flash('No matching products found.', 'error')
                        else:
                            flash('Products found. You can update their locations below.', 'message')
                    else:
                        # Clear the session if no search input is found
                        session.pop('last_search', None)
                        search_results = None
                        flash('Please enter a search query.', 'warning')
                else:
                # If no search inputs are available, do not perform a search
                    search_results = None


            elif action == 'viewall':
                # Fetch all products without pagination
                select_query = "SELECT * FROM products ORDER BY Update_Date DESC"
                cursor.execute(select_query)
                all_products = cursor.fetchall()
                if not all_products:
                    flash('No products available to display.', 'error')
                else:
                    flash('Displaying all products.', 'message')

            # Handle other GET actions if necessary

    except Error as e:
        # Log the error in production instead of printing
        print(f"Database error: {e}")
        flash(f"Database error: {e}", 'error')

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection and connection.is_connected():
            connection.close()

    # If 'search' is in the query parameters, pre-fill the search input
    search_query = request.args.get('search', '') or session.get('last_search', '')

    return render_template('location_service.html', 
                           search_results=search_results, 
                           all_products=all_products,
                           action=action,
                           current_user=current_user,
                           search_query=search_query)

@location_service_bp.route('/delete_product', methods=['POST'])
def delete_product():
    current_app.logger.info("Delete product request received")

    """
    Handles AJAX requests to delete a product based on SKU and Style.
    Expects JSON payload with 'sku' and 'style'.
    Returns JSON response indicating success or failure.
    """
    if not request.is_json:
        return jsonify({'success': False, 'message': 'Invalid request format. JSON expected.'}), 400
    
    data = request.get_json()
    delete_sku = data.get('sku', '').strip()
    delete_style = data.get('style', '').strip()

    if not delete_sku or not delete_style:
        return jsonify({'success': False, 'message': 'Missing SKU or Style for deletion.'}), 400

    try:
        connection = get_db_connection()
        if connection is None:
            return jsonify({'success': False, 'message': 'Failed to connect to the database.'}), 500

        cursor = connection.cursor()
        delete_query = """
            DELETE FROM products
            WHERE SKU = %s AND Style = %s
        """
        cursor.execute(delete_query, (delete_sku, delete_style))
        connection.commit()

        if cursor.rowcount > 0:
            return jsonify({'success': True, 'message': f'Product with SKU "{delete_sku}" and Style "{delete_style}" has been deleted successfully.'})
        else:
            return jsonify({'success': False, 'message': f'No product found with SKU "{delete_sku}" and Style "{delete_style}".'}), 404

    except Error as e:
        print(f"Database error during deletion: {e}")
        return jsonify({'success': False, 'message': 'An error occurred while deleting the product.'}), 500

    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection and connection.is_connected():
            connection.close()

@location_service_bp.route('/sync-to-oms', methods=['POST'])
def sync_to_oms():
    try:
        current_app.logger.info("Starting sync to OMS")
        mysql_connection = mysql.connector.connect(**db_config)
        mysql_cursor = mysql_connection.cursor(dictionary=True)

        # Use GROUP_CONCAT to aggregate all locations for each SKU
        mysql_cursor.execute("""
            SELECT sku, 
                   GROUP_CONCAT(location ORDER BY location SEPARATOR ', ') as location
            FROM products
            GROUP BY sku
        """)
        products = mysql_cursor.fetchall()

        if not products:
            return jsonify({'message': 'No products found to sync.'}), 200

        fdc_conn = get_sql_server_connection_fdc()
        fdc_cursor = fdc_conn.cursor()

        oms_ef_conn = get_sql_server_connection_oms_ef()
        oms_ef_cursor = oms_ef_conn.cursor()

        # Prepare bulk update data with abbreviations if needed
        update_data = []
        truncated_skus = []  # Collect SKUs that were truncated
        long_sku_list = []  # Collect SKUs that are too long
        
        # SQL Server inv_loc column is char(20) - CONFIRMED from table structure query
        MAX_LOCATION_LENGTH = 20
        
        for product in products:
            sku = (product['sku'] if product['sku'] else '').strip()
            location = (product['location'] if product['location'] else '').strip()
            original_location = location
            original_sku = sku
            
            # Check SKU length (SQL Server prod_cd column limit - typically 20-30 chars)
            if len(sku) > 20:
                long_sku_list.append({'sku': sku, 'length': len(sku)})
                sku = sku[:20].strip()  # Truncate SKU to 20 characters
            
            # Always apply abbreviations first to save space
            location = location.replace(' MID', ' M').replace(' TOP', ' T').replace(' BOT', ' B')
            
            # Clean up location format: remove "L", remove "-", remove space after comma
            # Example: "L1-A B, L2-C M" becomes "1AB,2CM"
            if location:
                parts = location.split(', ')
                cleaned_parts = []
                for part in parts:
                    if part.startswith('L'):
                        part = part[1:]  # Remove leading 'L'
                    part = part.replace('-', '')  # Remove all dashes
                    part = part.replace(' ', '')  # Remove all spaces
                    cleaned_parts.append(part)
                location = ','.join(cleaned_parts)  # Join with comma (no space)
            
            # Store if truncation will be needed
            needs_truncation = len(location) > MAX_LOCATION_LENGTH
            
            # Ensure location is within 40 character limit - FORCE truncation
            if len(location) > MAX_LOCATION_LENGTH:
                # Truncate at last comma before position 40
                truncated = location[:MAX_LOCATION_LENGTH]
                last_comma = truncated.rfind(', ')
                if last_comma > 0:
                    location = truncated[:last_comma]
                else:
                    # Hard truncate at 40 if no comma found
                    location = location[:MAX_LOCATION_LENGTH]
            
            # CRITICAL: ABSOLUTE FINAL CHECK - force limit to prevent ANY string over 40
            if len(location) > MAX_LOCATION_LENGTH:
                location = location[:MAX_LOCATION_LENGTH]
            
            # Log truncation after all processing
            if needs_truncation:
                truncated_skus.append({
                    'sku': original_sku,
                    'original': original_location,
                    'truncated': location,
                    'original_length': len(original_location),
                    'final_length': len(location)
                })
            
            update_data.append((location, sku))

        
        # Validate all fields are within limits and find any violators
        max_loc_length = 0
        max_sku_length = 0
        violators = []
        
        for loc, sku in update_data:
            loc_len = len(loc)
            sku_len = len(sku)
            
            if loc_len > max_loc_length:
                max_loc_length = loc_len
            if sku_len > max_sku_length:
                max_sku_length = sku_len
            
            # Find any that exceed limits
            if loc_len > MAX_LOCATION_LENGTH:
                violators.append({'sku': sku, 'location': loc, 'length': loc_len})
        

        # Check if records exist and get current values to skip unchanged records
        sku_list = [sku for _, sku in update_data]
        
        # Fetch existing records with current locations in batches
        check_batch_size = 100
        fdc_sku_map = {}  # Maps trimmed SKU to actual prod_cd
        fdc_current_locations = {}  # Maps trimmed SKU to current inv_loc value
        
        for i in range(0, len(sku_list), check_batch_size):
            batch_skus = sku_list[i:i+check_batch_size]
            placeholders = ','.join(['?' for _ in batch_skus])
            fdc_cursor.execute(f"SELECT prod_cd, inv_loc FROM inv_data WHERE prod_cd IN ({placeholders})", batch_skus)
            for row in fdc_cursor.fetchall():
                actual_prod_cd = row[0]
                current_inv_loc = (row[1] or '').strip()  # Current location in database
                trimmed_sku = actual_prod_cd.strip() if actual_prod_cd else actual_prod_cd
                fdc_sku_map[trimmed_sku] = actual_prod_cd
                fdc_current_locations[trimmed_sku] = current_inv_loc
        
        
        # Filter update_data: only include records where location has actually changed
        fdc_update_data = []
        fdc_skipped_unchanged = 0
        for loc, sku in update_data:
            if sku in fdc_sku_map:
                current_loc = fdc_current_locations.get(sku, '')
                if current_loc != loc:  # Only update if changed
                    fdc_update_data.append((loc, fdc_sku_map[sku]))
                else:
                    fdc_skipped_unchanged += 1
        
        
        if not fdc_update_data:
            fdc_updated = 0
        else:
            update_query = "UPDATE inv_data SET inv_loc = ? WHERE prod_cd = ?"
            
            fdc_updated = 0
            batch_size = 500
            
            for i in range(0, len(fdc_update_data), batch_size):
                batch = fdc_update_data[i:i+batch_size]
                try:
                    fdc_cursor.executemany(update_query, batch)
                    # executemany with pyodbc returns -1 for rowcount, so count the batch size
                    batch_updated = len(batch)
                    fdc_updated += batch_updated
                except Exception as e:
                    current_app.logger.error(f"FDC batch {i//batch_size + 1} FAILED: {str(e)}")
                    # Skip batch
                    continue
            

        # Check if records exist in OMS_EF and get current values to skip unchanged records
        oms_sku_map = {}  # Maps trimmed SKU to actual prod_cd
        oms_current_locations = {}  # Maps trimmed SKU to current inv_loc value
        
        for i in range(0, len(sku_list), check_batch_size):
            batch_skus = sku_list[i:i+check_batch_size]
            placeholders = ','.join(['?' for _ in batch_skus])
            oms_ef_cursor.execute(f"SELECT prod_cd, inv_loc FROM inv_data WHERE prod_cd IN ({placeholders})", batch_skus)
            for row in oms_ef_cursor.fetchall():
                actual_prod_cd = row[0]
                current_inv_loc = (row[1] or '').strip()  # Current location in database
                trimmed_sku = actual_prod_cd.strip() if actual_prod_cd else actual_prod_cd
                oms_sku_map[trimmed_sku] = actual_prod_cd
                oms_current_locations[trimmed_sku] = current_inv_loc
        
        
        # Filter update_data: only include records where location has actually changed
        oms_update_data = []
        oms_skipped_unchanged = 0
        for loc, sku in update_data:
            if sku in oms_sku_map:
                current_loc = oms_current_locations.get(sku, '')
                if current_loc != loc:  # Only update if changed
                    oms_update_data.append((loc, oms_sku_map[sku]))
                else:
                    oms_skipped_unchanged += 1
        
        
        if not oms_update_data:
            current_app.logger.warning("No matching SKUs found in OMS_EF database")
            oms_updated = 0
        else:
            oms_update_query = "UPDATE inv_data SET inv_loc = ? WHERE prod_cd = ?"
            
            oms_updated = 0
            
            for i in range(0, len(oms_update_data), batch_size):
                batch = oms_update_data[i:i+batch_size]
                try:
                    oms_ef_cursor.executemany(oms_update_query, batch)
                    # executemany with pyodbc returns -1 for rowcount, so count the batch size
                    batch_updated = len(batch)
                    oms_updated += batch_updated
                except Exception as e:
                    current_app.logger.error(f"OMS_EF batch {i//batch_size + 1} FAILED: {str(e)}")
                    # Skip batch
                    continue
            

        fdc_conn.commit()
        oms_ef_conn.commit()

        updated_count = fdc_updated + oms_updated
        total_skipped_unchanged = fdc_skipped_unchanged + oms_skipped_unchanged
        total_existing = len(fdc_sku_map) + len(oms_sku_map)
        skipped_not_exists = len(update_data) - total_existing
        
        current_app.logger.info(f"Skipped {skipped_not_exists} SKUs that don't exist in either database")
        current_app.logger.info(f"Skipped {total_skipped_unchanged} SKUs that already have the correct location")
        current_app.logger.info(f"Sync completed successfully. Updated {updated_count} records total.")
        return jsonify({'message': f'Sync completed. Updated {updated_count} records, skipped {total_skipped_unchanged} unchanged.'})

    except Exception as e:
        current_app.logger.error(f"An error occurred during sync: {str(e)}")
        return jsonify({'message': f'An error occurred: {str(e)}'}), 500

    finally:
        if 'mysql_cursor' in locals() and mysql_cursor:
            mysql_cursor.close()
        if 'mysql_connection' in locals() and mysql_connection.is_connected():
            mysql_connection.close()
        if 'fdc_cursor' in locals() and fdc_cursor:
            fdc_cursor.close()
        if 'fdc_conn' in locals() and fdc_conn:
            fdc_conn.close()
        if 'oms_ef_cursor' in locals() and oms_ef_cursor:
            oms_ef_cursor.close()
        if 'oms_ef_conn' in locals() and oms_ef_conn:
            oms_ef_conn.close()


@location_service_bp.route('/suggestions', methods=['GET'])
def suggestions():
    current_app.logger.info("Suggestions request received")

    query = request.args.get('q', '').strip()
    suggestions = []

    if not query:
        return jsonify({'suggestions': suggestions})

    try:
        connection = get_db_connection()
        if connection is None:
            return jsonify({'suggestions': suggestions})

        cursor = connection.cursor()
        # Use LIKE for partial matching and limit the number of results
        like_query = f"%{query}%"
        sql_query = """
            SELECT DISTINCT SKU FROM products WHERE SKU LIKE %s
            UNION
            SELECT DISTINCT Style FROM products WHERE Style LIKE %s
            LIMIT 10
        """
        cursor.execute(sql_query, (like_query, like_query))
        results = cursor.fetchall()
        # Flatten the list of tuples
        suggestions = [item[0] for item in results]
    except Error as e:
        print(f"Error fetching suggestions: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection and connection.is_connected():
            connection.close()

    return jsonify({'suggestions': suggestions})
