from flask import Blueprint, request, render_template, redirect, url_for, flash, session, send_file, jsonify
import mysql.connector
from mysql.connector import Error
import pandas as pd
import os
from datetime import datetime
from io import BytesIO

# Create a Blueprint for location_service
location_service_bp = Blueprint('location_service', __name__, template_folder='../templates')

# Database configuration
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'  # Replace with your actual database name
}

# Ensure the output directory exists
OUTPUT_DIR = os.path.join(os.getcwd(), 'output', 'location_DB')
os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def allowed_file(filename):
    """Check if the uploaded file is an allowed Excel file."""
    allowed_extensions = {'xlsx', 'xls'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

@location_service_bp.route('/location_service', methods=['GET', 'POST'])
def location_service():
    allowed_ip = '173.198.50.202'
    user_ip = request.remote_addr

    if 'username' in session:
        # User is logged in; use the session username
        current_user = session['username']
    else:
        if user_ip == allowed_ip:
            # User IP is allowed; set username to 'fdc'
            current_user = 'fdc'
            # Optionally, you might want to store this in the session
            session['username'] = current_user
        else:
            # User is not logged in and IP is not allowed; redirect to login
            session['next'] = request.url
            return redirect(url_for("login"))

    search_results = []
    all_products = []
    action = 'search'  # Default action

    # Determine the action based on GET or POST
    if request.method == 'POST':
        action = request.form.get('action', 'search')  # Default to 'search' if 'action' not provided
    else:
        action = request.args.get('action', 'search')  # Default to 'search' if 'action' not provided

    try:
        connection = get_db_connection()
        if connection is None:
            flash("Failed to connect to the database.", 'error')
            return redirect(url_for('location_service.location_service'))

        cursor = connection.cursor(dictionary=True)

        if action == 'search':
            if request.method == 'POST':
                search_input = request.form.get('search_input', '').strip()
                if not search_input:
                    flash('Please enter a SKU or Style to search.', 'error')
                else:
                    query = "SELECT * FROM products WHERE SKU = %s OR Style = %s"
                    cursor.execute(query, (search_input, search_input))
                    search_results = cursor.fetchall()
                    if not search_results:
                        flash('No matching products found.', 'error')
                    else:
                        flash('Products found. You can update their locations below.', 'message')
                        # Store the search input in session to pre-fill the search box
                        session['last_search'] = search_input
            else:
                # For GET requests, retrieve the last search query if available
                search_input = request.args.get('search', '') or session.get('last_search', '')
                if search_input:
                    query = "SELECT * FROM products WHERE SKU = %s OR Style = %s"
                    cursor.execute(query, (search_input, search_input))
                    search_results = cursor.fetchall()
                    if not search_results:
                        flash('No matching products found.', 'error')
                    else:
                        flash('Products found. You can update their locations below.', 'message')

        elif action == 'update_multiple' and request.method == 'POST':
            # Retrieve lists of data
            original_skus = request.form.getlist('original_sku')
            original_styles = request.form.getlist('original_style')
            new_locations = request.form.getlist('new_location')

            update_query = """
                UPDATE products 
                SET Location = %s, Update_Date = NOW()
                WHERE SKU = %s AND Style = %s
            """
            updated_count = 0
            for sku, style, new_loc in zip(original_skus, original_styles, new_locations):
                if new_loc.strip():  # Only update if new location is provided
                    cursor.execute(update_query, (new_loc.strip(), sku, style))
                    if cursor.rowcount > 0:
                        updated_count += 1
            connection.commit()
            if updated_count > 0:
                flash(f'Updated {updated_count} product(s) successfully.', 'message')
            else:
                flash('No locations were updated. Please check your inputs.', 'error')

            # Re-fetch the search results if a search was performed
            search_input = session.get('last_search', '')
            if search_input:
                query = "SELECT * FROM products WHERE SKU = %s OR Style = %s"
                cursor.execute(query, (search_input, search_input))
                search_results = cursor.fetchall()
                if not search_results:
                    flash('No matching products found after update.', 'error')
                else:
                    flash('Products updated successfully.', 'message')

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
            if request.method == 'POST':
                new_skus = request.form.getlist('new_sku')
                new_styles = request.form.getlist('new_style')
                new_locations = request.form.getlist('new_location')

                # Validate that all lists have the same length
                if not (len(new_skus) == len(new_styles) == len(new_locations)):
                    flash('Mismatch in the number of SKUs, Styles, and Locations provided.', 'error')
                    return redirect(url_for('location_service.location_service', action='add'))

                # Initialize counters
                added_count = 0
                duplicate_count = 0
                invalid_entries = []

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

                    # Prepare data for insertion
                    cursor.execute(insert_query, (sku, style, location))
                    added_count += 1

                connection.commit()

                # Prepare flash messages
                if added_count > 0:
                    flash(f'Added {added_count} new product(s) successfully.', 'message')
                if duplicate_count > 0:
                    flash(f'Skipped {duplicate_count} duplicate product(s).', 'warning')
                if invalid_entries:
                    flash(f'Skipped {len(invalid_entries)} invalid product(s) due to missing fields.', 'error')

                # Redirect to view all products or back to add form
                return redirect(url_for('location_service.location_service', action='viewall'))
            else:
                # For GET requests, simply render the add product form without performing any action
                pass

        elif action == 'import_export':
            if request.method == 'POST':
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
                            filepath = os.path.join(OUTPUT_DIR, filename)
                            # Save to Excel
                            df.to_excel(filepath, index=False)

                            flash('Products exported successfully.', 'message')
                            # Send the file for download
                            return send_file(
                                filepath, 
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

                            # Backup current table before wiping
                            backup_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                            backup_filename = f'products_backup_before_import_{backup_timestamp}.xlsx'
                            backup_filepath = os.path.join(OUTPUT_DIR, backup_filename)
                            if products:
                                backup_df = pd.DataFrame(products)
                                backup_df.to_excel(backup_filepath, index=False)
                            else:
                                flash('No existing products to backup before import.', 'warning')

                            # Wipe the current table
                            cursor.execute("DELETE FROM products")
                            connection.commit()

                            # Insert new data
                            insert_query = """
                                INSERT INTO products (SKU, Style, Location, Update_Date)
                                VALUES (%s, %s, %s, NOW())
                            """
                            insert_data = []
                            for _, row in df.iterrows():
                                sku = str(row['SKU']).strip()
                                style = str(row['Style']).strip()
                                location = str(row['Location']).strip()
                                insert_data.append((sku, style, location))
                            
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
                # For GET requests, simply render the import/export section
                pass

        else:
            flash('Unknown action.', 'error')

    except Error as e:
        # Log the error in production instead of printing
        print(f"Database error: {e}")
        flash(f"Database error: {e}", 'error')

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

    # Handle GET requests and prepare data for rendering
    if action == 'viewall' and request.method == 'GET':
        # Data has already been fetched in the 'viewall' action above
        pass
    elif action == 'search' and not search_results:
        # If there's a search query but no results, handle accordingly
        pass
    elif action == 'add':
        pass  # Flash message already set
    elif action == 'update_multiple':
        pass  # Flash message already set
    elif action == 'import_export':
        pass  # Flash message already set

    # If 'search' is in the query parameters, pre-fill the search input
    search_query = request.args.get('search', '') or session.get('last_search', '')

    return render_template('location_service.html', 
                           search_results=search_results, 
                           all_products=all_products,
                           action=action,
                           current_user=current_user,
                           search_query=search_query)

@location_service_bp.route('/suggestions', methods=['GET'])
def suggestions():
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
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

    return jsonify({'suggestions': suggestions})
