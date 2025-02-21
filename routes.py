from flask import (request, render_template, jsonify, redirect, url_for, 
                   session, flash, send_from_directory,send_file, Response, make_response)
from werkzeug.utils import secure_filename

# Standard library imports
import os
import re
import sys
import time
import json
import datetime
import subprocess
import mysql.connector
from mysql.connector import Error

# External library imports
import pandas as pd
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from user_agents import parse
from googleapiclient.discovery import build
from google.oauth2 import service_account


# Application-specific imports
from custom_query import execute_custom_query
from config import Config
from upload_processor import process_upload
from big_img_routes import register_big_img_routes
from location_service import location_service_bp
from container import container_bp
from boat import boat_service_bp
from chatgpt import chatgpt_bp
from mobile_lite import mobile_lite_bp
import Label_Maker, order_search, SQL_connector
from macy_special import process_orders
from loris_sticker import main, process_input_data
from products import products_bp
from datetime import timedelta,datetime
from dailytodo import dailytodo
from gpgtmx import gpgtmx_bp

import io
import zipfile
from util import resize_image  # Import the master resizing function
from PIL import Image

from V6listcreator import v6_listcreator_bp



 


client = WebClient(token=Config.SLACK_TOKEN)

# Configure logging

def register_routes(app):

    register_big_img_routes(app)
    app.register_blueprint(location_service_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(boat_service_bp)
    app.register_blueprint(chatgpt_bp)
    app.register_blueprint(mobile_lite_bp)
    app.register_blueprint(dailytodo)
    app.register_blueprint(v6_listcreator_bp)
    app.register_blueprint(gpgtmx_bp)

    app.permanent_session_lifetime = timedelta(days=30)



    def check_credentials(username, password):
        return Config.USERS.get(username) == password

    def log_user_activity(username):
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = parse(request.headers.get('User-Agent'))
        referrer = request.referrer
        query_params = request.args

        app.logger.info(
            "***%s*** has logged in. IP: ***%s***, Browser: %s %s, OS: %s %s, Device: ***%s***, "
            "Method: %s, URL: %s, Referrer: %s",
            username, client_ip, user_agent.browser.family, user_agent.browser.version_string,
            user_agent.os.family, user_agent.os.version_string, user_agent.device.family,
            request.method, request.url, referrer
        )

    def get_location(ip):
        try:
            response = requests.get(f'http://ip-api.com/json/{ip}')
            if response.status_code == 200:
                return response.json()
            else:
                app.logger.warning(f"Failed to get location for IP: {ip}")
                return {}
        except Exception as e:
            app.logger.error(f"Error fetching location data: {e}")
            return {}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        # If user is already logged in, redirect to home
        if 'username' in session:
            return redirect(url_for("home"))
        
        # Get the next page from query parameters or referrer
        next_page = request.args.get('next') or request.referrer
        
        # Store the next page in session
        if next_page and not next_page.endswith('/login'):
            session['next'] = next_page
        
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        location_data = get_location(client_ip)
        
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            remember = request.form.get("remember")
            
            if check_credentials(username, password):
                session['username'] = username
                if remember:
                    session.permanent = True
                else:
                    session.permanent = False
                
                flash(f"Logged in as {username}")
                log_user_activity(username)
                
                # Redirect TMX users to gpginvoice page
                if username == 'tmx':
                    return redirect(url_for("gpginvoice"))
                    
                # For other users, redirect to stored next page or home
                redirect_url = session.pop('next', url_for("home"))
                return redirect(redirect_url)
            else:
                flash("Invalid credentials")
        
        return render_template("login.html", client_ip=client_ip, location=location_data)

    @app.route("/logout")
    def logout():
        session.pop('username', None)
        flash("Logged out")
        return redirect(url_for("login"))


    @app.route("/", methods=["GET", "POST"])
    def home():
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("index.html", current_user=current_user)


    
    @app.route("/delete_image", methods=["POST"])
    def delete_image():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        current_user = session['username']
        if current_user != 'yetimaster':
            return jsonify({"error": "Permission denied"}), 403

        file_name = request.form.get("file_name")
        if file_name:
            try:
                original_path = os.path.join(Config.CATALOG_ORIGINAL, file_name)
                if os.path.exists(original_path):
                    os.remove(original_path)
                    app.logger.info(f"{current_user} deleted {original_path}")
                return jsonify({"message": "File deleted successfully"}), 200
            except Exception as e:
                app.logger.error(f"Error deleting file {file_name}: {e}")
                return jsonify({"error": "Error deleting file"}), 500
        else:
            return jsonify({"error": "No file name provided"}), 400
    db_config = {
        'host': '192.168.254.30',
        'port': 3306,
        'user': 'yeti',
        'password': 'qwe123',
        'database': 'onlinedb'  # Replace with your actual database name
    }
    def get_db_connection():
        """Establishes a connection to the MySQL database."""
        try:
            connection = mysql.connector.connect(**db_config)
            if connection.is_connected():
                return connection
        except Error as e:
            app.logger.error(f"Error connecting to MySQL: {e}")
        return None

    def fetch_locations(skus):
        location_dict = {}
        
        if not skus:
            app.logger.info("No SKUs provided to fetch_locations")
            return location_dict
        
        try:
            connection = get_db_connection()
            if connection is None:
                app.logger.error("Failed to connect to the database for fetching locations.")
                return location_dict
            
            cursor = connection.cursor()
            placeholders = ', '.join(['%s'] * len(skus))
            query = f"SELECT SKU, Location FROM products WHERE SKU IN ({placeholders})"
            
            
            cursor.execute(query, skus)
            results = cursor.fetchall()
            
            for sku, location in results:
                location_dict[sku.lower()] = location
            
            for sku in skus:
                if sku.lower() not in location_dict or not location_dict[sku.lower()]:
                    stripped_sku = sku.split(' ')[0]
                    if stripped_sku != sku:
                        cursor.execute(
                            "SELECT SKU, Location, Update_Date FROM products WHERE SKU = %s",
                            (stripped_sku,)
                        )
                        result = cursor.fetchone()
                        if result:
                            location_dict[sku.lower()] = result[1]
        
        except Error as e:
            app.logger.error(f"Error fetching locations: {e}")
        finally:
            if 'cursor' in locals() and cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
        
        return location_dict

    # Ensure the output directory exists
    OUTPUT_DIR = os.path.join(os.getcwd(), 'output', 'location_DB')
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    @app.route("/item_lookup", methods=["GET", "POST"])
    @app.route("/item_lookup/<string:user_input>", methods=["GET"])
    def item_lookup(user_input=None):
        if 'username' not in session or not session['username']:
            session['next'] = request.url
            return redirect(url_for("login"))

        current_user = session['username']
        excel_html = None
        history_html = None  # Initialize history_html

        if request.method == "POST":
            # Handle the case where user input is submitted via the form
            user_input = request.form.get("user_input", "").lower().strip()

        elif request.method == "GET" and user_input:
            # Handle the case where the input is provided via the URL
            user_input = user_input.lower().strip()
        else:
            # Handle the case where user_input is not provided (i.e., user visits the page without input)
            user_input = ""

        # Handle cases where user_input is empty
        if not user_input:
            return render_template("Item_lookup.html", excel_html=None, history_html=None, current_user=current_user, user_input=user_input)

        # Continue with the rest of the logic if user_input is valid
        if user_input.endswith(","):
            user_input = user_input[:-1]

        # Generate Excel and history HTML
        excel_html, history_html, purchase_html, _, user_input = SQL_connector.generate_excel(user_input)

        if not excel_html and not history_html:  # If no valid data is found
            return render_template("Item_lookup.html", excel_html=None, history_html=None, current_user=current_user, user_input=user_input)

        # Process excel_html to add Location and Update_Date columns
        if excel_html:
            from io import StringIO
            df = pd.read_html(StringIO(excel_html))[0]  # Convert HTML to DataFrame

            # Add a row number column
            df.insert(0, '#', range(1, len(df) + 1))

            # Fetch SKUs and update location and date columns
            if 'SKU' in df.columns:
                skus = df['SKU'].astype(str).str.lower().tolist()
            else:
                skus = []

            # Fetch locations and update dates using the helper function
            location_dict = fetch_locations(skus)

            # Add the 'Location' and 'Update_Date' columns
            df['Location'] = df['SKU'].astype(str).str.lower().map(location_dict).fillna('')
            if 'DB' in df.columns:
                replacement_dict = {
                    'oms_ef': 'E_FAS',
                    'omsdata_fdc': 'FDC'
                }
                # Perform case-insensitive replacement
                df['DB'] = df['DB'].str.lower().replace(replacement_dict)
            else:
                app.logger.warning("Database column not found in the DataFrame.")
                
            if 'Location' in df.columns:
                df['Location'] = df.apply(
                    lambda row: (
                        f'<span class="editable">'
                        f'{row["Location"] if pd.notnull(row["Location"]) and row["Location"] != "" else "*"}</span>'
                    ),
                    axis=1
                )

            # Adjust column order to place 'Location' after the third column
            if 'Location' in df.columns:
                cols = list(df.columns)
                loc_index = 3  # After the third column
                cols.insert(loc_index, cols.pop(cols.index('Location')))
                df = df[cols]

            # Replace '<br>' in column headers with spaces
            df.columns = df.columns.str.replace('<br>', ' ')
            df.columns = df.columns.str.replace('\n', '<br>')

            # Convert DataFrame back to HTML for rendering
            excel_html = df.to_html(
                index=False,
                classes="table table-bordered table-striped",
                table_id="excel_table",
                escape=False  # Allow HTML in headers and cells
            )

        # Prepare image and file information
        search_terms = [term.split()[0].strip() for term in user_input.split(",")]
        file_names = []
        for term in search_terms:
            # Check in CATALOG_ORIGINAL
            try:
                for filename in os.listdir(Config.CATALOG_ORIGINAL):
                    if filename.lower().startswith(term):
                        file_names.append(filename)
            except FileNotFoundError:
                app.logger.error("Directory not found: %s", Config.CATALOG_ORIGINAL)

        # Remove duplicates from file_names
        file_names = list(dict.fromkeys(file_names))

        # Get file information (size and modification date)
        file_info = []
        for filename in file_names:
            original_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
            file_path = original_path 

            if os.path.exists(file_path):
                size = os.path.getsize(file_path)
                modified_time = os.path.getmtime(file_path)
                modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                file_info.append((filename, size, modified_date))
            else:
                app.logger.warning("File not found: %s", file_path)

        # Prepare image URLs based on file_info
        sorted_file_names = sort_filenames(file_names)
        sorted_image_urls = []
        for filename in sorted_file_names:
            original_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
            if os.path.exists(original_path):
                image_url = os.path.join('/image', filename)
            else:
                image_url = '/static/no_image_available.jpg'  # Placeholder image

            sorted_image_urls.append(image_url)

        app.logger.info("***%s***__@@@search SKU@@@__ %s",current_user, user_input)
        return render_template(
            "Item_lookup.html",
            excel_html=excel_html,
            history_html=history_html,  # Pass the history_html to the template
            purchase_html=purchase_html,  # Add this
            image_urls=sorted_image_urls,
            file_names=sorted_file_names,
            file_info=file_info,
            current_user=current_user,
            user_input=user_input.capitalize()  # Pass user_input back to the template to avoid "None"
        )

    @app.route('/image/<path:filename>')
    def serve_image(filename):
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        image_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
        
        # Check if image exists
        if not os.path.exists(image_path):
            return send_from_directory('static', 'no_image_available.jpg')
        
        # Get size parameter for optional resizing
        size = request.args.get('size', 'full')
        
        if size == 'full':
            return send_from_directory(Config.CATALOG_ORIGINAL, filename)
        
        try:
            # Resize image on demand
            buffer = resize_image(image_path, int(size) if size.isdigit() else 300)
            return send_file(buffer, mimetype='image/jpeg')
        except Exception as e:
            app.logger.error(f"Error serving image {filename}: {e}")
            return send_from_directory('static', 'no_image_available.jpg')

    @app.route('/update_location', methods=['POST'])
    def update_location():
        data = request.get_json()
        sku = data.get('sku')
        style = data.get('style')
        location = data.get('location')

        if not sku or not style:
            return jsonify({'success': False, 'message': 'SKU or Style is missing.'}), 400

        def connect_to_db():
            """Helper to establish a database connection."""
            try:
                return get_db_connection()
            except Error as e:
                return None

        def check_product_exists(cursor, sku, style):
            """Check if the product exists in the database."""
            select_query = "SELECT * FROM products WHERE SKU = %s AND Style = %s"
            cursor.execute(select_query, (sku, style))
            return cursor.fetchone()

        def update_product_location(cursor, location, sku, style):
            """Update the location for an existing product."""
            update_query = "UPDATE products SET Location = %s, Update_Date = NOW() WHERE SKU = %s AND Style = %s"
            cursor.execute(update_query, (location, sku, style))

        def insert_new_product(cursor, sku, style, location):
            """Insert a new product into the database."""
            insert_query = "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())"
            cursor.execute(insert_query, (sku, style, location))

        connection = connect_to_db()
        if connection is None:
            return jsonify({'success': False, 'message': 'Failed to connect to the database.'}), 500

        try:
            cursor = connection.cursor(dictionary=True)

            # Check if the product exists
            product = check_product_exists(cursor, sku, style)

            if product:
                old_location = product.get('Location', 'N/A')  # Default to 'N/A' if Location is None
                update_product_location(cursor, location, sku, style)
                connection.commit()
            else:
                insert_new_product(cursor, sku, style, location)
                connection.commit()
                app.logger.info(f"New product inserted with SKU={sku}, Style={style}, Location='{location}'.")

            return jsonify({'success': True, 'message': 'Location updated successfully.'})

        except Error as e:
            app.logger.error(f"Database error during location update: {e}")
            return jsonify({'success': False, 'message': 'An error occurred while updating the location.'}), 500

        finally:
            if 'cursor' in locals() and cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
                                            

    @app.route("/order_lookup", methods=["GET", "POST"])
    @app.route("/order_lookup/<string:order_number>", methods=["GET"])
    def order_lookup(order_number=None):
        if 'username' not in session:
            return redirect(url_for("login"))
        
        current_user = session['username']
        excel_html = None

        if request.method == "POST":
            # Handle the order lookup submission
            order_number = request.form.get("order_number", "").strip()
            
            if not order_number:
                return render_template("order_lookup.html", excel_html=None, current_user=current_user)

            session['order_number'] = order_number

            if current_user == 'fdc':
                app.logger.info("User %s does not have permission to view order details", current_user)
                return render_template("order_lookup.html", excel_html=None, current_user=current_user)

            excel_html = order_search.generate_order(order_number)
            
            if excel_html:
                app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
            else:
                excel_html = None

        elif order_number:
            # Handle GET request with order_number in the URL
            
            if current_user == 'fdc':
                app.logger.info("User %s does not have permission to view order details", current_user)
                return render_template("order_lookup.html", excel_html=None, current_user=current_user)
            
            excel_html = order_search.generate_order(order_number)
            
            if excel_html:
                app.logger.info("***%s***__@@@search order@@@__ %s",current_user, order_number)
            else:
                app.logger.info("No data found for order number: %s via URL", order_number)
                excel_html = None

        return render_template(
            "order_lookup.html",
            excel_html=excel_html,
            order_number=order_number,
            current_user=current_user
        )
    @app.route('/label_maker_from_order_lookup', methods=['POST'])
    def label_maker_from_order_lookup():
        current_user = session['username']
        try:
            order_number = session.get('order_number')
            if not order_number:
                return "Order number is missing.", 400

            app.logger.info("***%s***__@@@made labels@@@__ %s",current_user, order_number)
            filename, error_message, number_of_labels, duplicate_numbers, info_list, addresslist, warnings = Label_Maker.process_orders(order_number)

            if error_message:
                return render_template(
                    'order_lookup.html',
                    excel_html=None,
                    filename=None,
                    number_of_labels=None,
                    duplicate_numbers=None,
                    info_list=None,
                    error_message=error_message,
                    order_number=order_number,
                    current_user=current_user
                )

            base_filename = os.path.basename(filename)
            download_url = url_for('download_file2', filename=base_filename)

            return render_template(
                'order_lookup.html',
                filename=base_filename,
                download_url=download_url,
                number_of_labels=number_of_labels,
                duplicate_numbers=duplicate_numbers,
                info_list=info_list,
                addresslist=addresslist,
                order_number=order_number,
                warnings=warnings,
                current_user=current_user  # Pass warnings to the template
            )
        except Exception as e:
            app.logger.error("An error occurred: %s", str(e))
            return render_template(
                'order_lookup.html',
                excel_html=None,
                filename=None,
                number_of_labels=None,
                duplicate_numbers=None,
                info_list=None,
                error_message=f"An error occurred: {str(e)}",
                order_number=order_number,
                current_user=current_user
            )

    
    @app.route("/run_fast_script", methods=["POST"])
    def run_fast_script():
        try:
            app.logger.info("Running fast script")
            process = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'fast.py')],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output_lines = []
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    output_lines.append(output.strip())
            return_code = process.poll()
            if return_code == 0:
                output = "\n".join(output_lines)
            else:
                output = "\n".join(output_lines) + "\nError: " + process.stderr.read().strip()
            return jsonify(output)
        except Exception as e:
            app.logger.error("Error running fast script: %s", e)
            return jsonify(str(e))


    @app.route("/label_maker", methods=["GET", "POST"])
    def label_maker():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        filename = None
        error_message = None
        number_of_labels = None
        duplicate_numbers = None

        if request.method == "POST":
            order_number = request.form.get("order_number")
            app.logger.info("%s is processing order number for label maker: %s", current_user, order_number)
            Label_Maker.order_number = order_number
            filename, error_message, number_of_labels, duplicate_numbers = Label_Maker.process_orders()
            if filename is None and error_message is None:
                error_message = f"Order : {order_number} not found, maybe wrong?"
                app.logger.warning("Order number %s not found for %s", order_number, current_user)
            elif filename:
                filename = os.path.basename(filename)

        return render_template("label_maker.html", filename=filename, error_message=error_message, current_user=current_user, number_of_labels=number_of_labels, duplicate_numbers=duplicate_numbers)

    @app.route("/macy", methods=["GET", "POST"])
    def macy_maker():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        filename = None
        error_message = None
        po_num = request.form.get("po_num", "")
        order_number = request.form.get("order_number", "")
        apt = request.form.get("apt", "")
        carrier = request.form.get("carrier", "")
        project = request.form.get("project", "")

        if request.method == "POST":
            app.logger.info("%s is processing Macy Order: %s", current_user, order_number)
            filename, error_message, total_labels, duplicate_numbers, label_ref, address, warnings = process_orders(po_num, order_number, apt, carrier, project)
            if filename is None and error_message is None:
                error_message = f"Order : {order_number} not found, maybe wrong?"
                app.logger.warning("Order number %s not found for %s", order_number, current_user)
            elif filename:
                filename = os.path.basename(filename)

        return render_template("macy.html", filename=filename, error_message=error_message, current_user=current_user, po_num=po_num, order_number=order_number, apt=apt, carrier=carrier, project=project)

    @app.route("/loris", methods=["GET", "POST"])
    def loris():
        if 'username' not in session:
            return redirect(url_for("login"))

        current_user = session['username']
        filename_30stickers = None
        filename_exact = None
        error_message = None
        table_data = None
        input_data = request.form.get("input_data")
        sku = request.form.get("sku", "")
        barcode = request.form.get("barcode", "")
        quantity = request.form.get("quantity", "")

        if request.method == "POST":
            if "submit_data" in request.form:
                try:
                    app.logger.info("Processing data in Loris tool kit for %s", current_user)
                    
                    # Process input data to get labels
                    labels = process_input_data(input_data)
                    
                    # Generate PDFs for both versions
                    filename_30stickers, filename_exact = main(input_data)
                    
                    if not filename_30stickers or not filename_exact:
                        error_message = "An error occurred while generating the PDFs."
                except Exception as e:
                    error_message = f"An error occurred: {e}"
                    app.logger.error("Exception in Loris tool kit: %s", e)
            elif "show_table" in request.form:
                try:
                    table_data = parse_input_data(input_data)
                except Exception as e:
                    error_message = f"An error occurred while parsing the data: {e}"
                    app.logger.error("Error parsing data in Loris tool kit: %s", e)

        # Ensure table_data is a list
        if table_data is None:
            table_data = []
        return render_template("loris.html", filename_30stickers=filename_30stickers, filename_exact=filename_exact, error_message=error_message, table_data=table_data, input_data=input_data, sku=sku, barcode=barcode, quantity=quantity, current_user=current_user)
        
    def parse_input_data(input_data):
        lines = input_data.strip().split('\n')
        if any(keyword in lines[0] for keyword in ["SKU", "Barcode", "Quantity"]):
            lines = lines[1:]

        table_data = []
        for line in lines:
            if line.strip():
                parts = line.split()
                if len(parts) >= 3:
                    sku = parts[0]
                    barcode = parts[1]
                    try:
                        # Convert quantity to an integer
                        quantity = int(' '.join(parts[2:]))
                    except ValueError:
                        # Handle the case where conversion fails
                        quantity = 0  # or handle it in another way, e.g., log an error

                    table_data.append([sku, barcode, quantity])

        return table_data
        

    @app.route('/log')
    def display():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']

        try:
            with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                lines = file.readlines()
            formatted_lines = format_logs(lines)
            reversed_lines = list(reversed(formatted_lines))

            # Pagination logic
            per_page = 500
            page = request.args.get('page', 1, type=int)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_log_lines = reversed_lines[start:end]

            # Calculate total pages
            total_pages = len(reversed_lines) // per_page + (1 if len(reversed_lines) % per_page > 0 else 0)

            #return render_template('log_viewer.html', log_lines=paginated_log_lines, page=page, total_pages=total_pages,current_user=current_user), 200
            return render_template('log_viewer.html', log_lines=paginated_log_lines, page=page, total_pages=total_pages,current_user=current_user), 200

        except Exception as e:
            app.logger.error('Error accessing log file: %s', e)
            return jsonify(error=str(e)), 500
        
    @app.route('/log/stream', methods=['GET'])
    def stream_log():
       # if 'username' not in session:
       #     return redirect(url_for("login"))
        def generate():
            with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                file.seek(0, 2)  # Move to the end of the file
                while True:
                    line = file.readline()
                    if not line:
                        time.sleep(1)  # Wait for new log lines
                        continue

                    # Format the new log line
                    formatted_line_tuple = format_logs([line.strip()])

                    # format_logs returns a list of tuples, take the first tuple
                    if formatted_line_tuple:
                        log_class, formatted_line = formatted_line_tuple[0]
                    else:
                        log_class, formatted_line = '', line.strip()

                    # Prepare the formatted line as a single string
                    # You can choose to send both log_class and formatted_line or just the formatted_line
                    # For simplicity, we'll send a JSON string with both
                    import json
                    data = json.dumps({
                        'log_class': log_class,
                        'formatted_line': formatted_line
                    })

                    yield f"data: {data}\n\n"  # SSE format: data:<line>
        return Response(generate(), mimetype='text/event-stream')
    
    def format_logs(lines):
        formatted = []
        for line in lines:
            # Determine log class
            if 'ERROR' in line:
                log_class = 'error'
            elif 'INFO' in line:
                log_class = 'info'
            else:
                log_class = ''

            # Strip newline characters
            stripped_line = line.strip()

            # Highlight ***text*** in blue and remove ***
            stripped_line = re.sub(
                r'\*\*\*(.*?)\*\*\*',  # Match text between ***
                lambda m: f'<span class="highlight-blue">{m.group(1).upper()}</span>',  # Apply highlight and exclude ***
                stripped_line
            )

            # Highlight @@@text@@@ in purple and remove @@@
            stripped_line = re.sub(
                r'@@@(.*?)@@@',  # Match text between @@@
                r'<span class="highlight-purple">\1</span>',  # Apply highlight and exclude @@@
                stripped_line
            )

            # Highlight and capitalize the word after __
            stripped_line = re.sub(
                r'__(.*?)__\s*(.*)',  # Match the second occurrence of __ and capture everything after it
                lambda m: f'__{m.group(1)}__ <span class="highlight-red">{m.group(2).upper()}</span>',
                stripped_line
            )

            # Append the formatted line as a two-element tuple
            formatted.append((log_class, stripped_line))
        
        return formatted
         
    @app.route('/download2/<filename>')
    def download_file2(filename):
        current_user = session['username']
        app.logger.info("***%s***__@@@[Download][labels]@@@__ %s", current_user,filename)
        return send_from_directory("output/label_output/", filename, as_attachment=False)

    @app.route('/download3/<filename>')
    def download_file3(filename):
        app.logger.info("[Download][loris_barcode]File %s downloaded from barcode output", filename)
        return send_from_directory("output/barcode_output/", filename, as_attachment=False)


    @app.route('/download4/<filename>')
    def download_file4(filename):
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        app.logger.info("***%s***__@@@[Download][labels]@@@__ %s", current_user,filename)
        return send_from_directory("output/macy_label/", filename, as_attachment=False)

    @app.route("/report_bug", methods=["GET", "POST"])
    def report_bug():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        
        if request.method == "POST":
            current_user = session['username']
            bug_type = request.form["bug_type"]
            bug_description = request.form["bug_description"]
            bug_file = request.files["bug_file"]

            bug_id = 10001
            bug_dir = os.path.join("output", "bug", str(bug_id))
            while os.path.exists(bug_dir):
                bug_id += 1
                bug_dir = os.path.join("output", "bug", str(bug_id))
            os.makedirs(bug_dir)

            with open(os.path.join(bug_dir, "description.txt"), "w") as f:
                f.write(f"Type: {bug_type}\nDescription: {bug_description}")

            if bug_file:
                filename = secure_filename(bug_file.filename)
                bug_file.save(os.path.join(bug_dir, filename))

            app.logger.info(f"Bug report {bug_id} submitted by {current_user}")
            
            slack_message = {
                "text": f"A new bug report has been submitted.\n\n*Type:* {bug_type}\n*Description:* {bug_description}\n*Submitted by:* {current_user}\n*Bug ID:* {bug_id}"
            }
            response = requests.post(Config.SLACK_WEBHOOK_URL, json=slack_message)

            if response.status_code != 200:
                app.logger.error(f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}")

            return f"Bug report {bug_id} submitted successfully!"

        return render_template("report_bug.html",current_user=current_user)

    @app.route('/shipping_receipt', methods=['GET'])
    def shipping_receipt():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template('upload_file.html', current_user=current_user)
    
    @app.route('/help', methods=['GET'])
    def help():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template('help.html', current_user=current_user)


    @app.route('/send_shipping_receipt', methods=['POST'])
    def send_shipping_receipt():
        if 'username' not in session:
            return redirect(url_for("login"))
        file = request.files.get('file')
        memo = request.form.get('memo')
        store = request.form.get('store')
        current_user = session.get('username')

        if not memo and (not file or file.filename == ''):
            flash('Please provide either a memo or a file.')
            return redirect(request.url)

        receipt_date = datetime.now().strftime('%m-%d-%Y')

        receipt_dir = os.path.join('output', 'shipping_receipts', receipt_date)
        os.makedirs(receipt_dir, exist_ok=True)

        def save_file_with_unique_name(directory, file, filename):
            file_path = os.path.join(directory, filename)
            base, extension = os.path.splitext(filename)
            counter = 1

            while os.path.exists(file_path):
                filename = f"{base}_{counter}{extension}"
                file_path = os.path.join(directory, filename)
                counter += 1

            file.save(file_path)
            return file_path

        def save_memo_with_unique_name(directory, memo_text):
            filename = "memo.txt"
            file_path = os.path.join(directory, filename)
            base, extension = os.path.splitext(filename)
            counter = 1

            while os.path.exists(file_path):
                filename = f"{base}_{counter}{extension}"
                file_path = os.path.join(directory, filename)
                counter += 1

            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(memo_text)
            return file_path

        if file and file.filename != '':
            if memo:
                save_memo_with_unique_name(receipt_dir, memo)

            file_path = save_file_with_unique_name(receipt_dir, file, secure_filename(file.filename))

            try:
                response = client.files_upload_v2(
                    channel=Config.SLACK_CHANNEL,
                    file=file_path,
                    initial_comment=f"Shipping receipt uploaded:\n\n*memo:* {memo}\n*Receipt Date:* {receipt_date}\n*shipper:* {store}"
                )

                if response['ok']:
                    flash('File successfully uploaded and sent to Slack')
                else:
                    app.logger.error(f"Slack API error: {response['error']}")
                    flash('Failed to send file to Slack')

            except SlackApiError as e:
                app.logger.error(f"Slack API error: {e.response['error']}")
                flash('Failed to send file to Slack')

        elif memo:
            save_memo_with_unique_name(receipt_dir, memo)

            flash('Memo successfully saved.')

            slack_message = {
                "text": f"*Submitted by:* {current_user}\n*Memo:* {memo}"
            }
            response = requests.post(Config.SLACK_WEBHOOK_URL_SHIPPING_RECEIPT, json=slack_message)

            if response.status_code != 200:
                app.logger.error(f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}")

        return redirect(url_for('shipping_receipt'))

    @app.route("/calculator", methods=["GET"])
    def calculatorapp():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("calculator2.html",  current_user=current_user)
    
    def get_temu_listings_data():
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=Config.SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        sheet = service.spreadsheets()
        
        # Fetch data from the "Temu Listings" sheet
        ranges = ['Temu!A:H']
        result = sheet.values().batchGet(spreadsheetId=Config.SPREADSHEET_ID, ranges=ranges).execute()
        
        temu_listings_data = result['valueRanges'][0].get('values', [])
        
        # Process data to ensure all rows have the same length
        if temu_listings_data:
            max_length = max(len(row) for row in temu_listings_data)
            temu_listings_data = [row + [''] * (max_length - len(row)) for row in temu_listings_data]
            df_sheet = pd.DataFrame(temu_listings_data[1:], columns=temu_listings_data[0])
            
            # Find the index for the column that contains "Product ID"
            product_id_index = next((i for i, header in enumerate(df_sheet.columns) if 'Product ID' in header), -1)

            # Add hyperlinks to product IDs
            if product_id_index != -1:
                df_sheet.iloc[:, product_id_index] = df_sheet.iloc[:, product_id_index].apply(
                    lambda x: f'<a href="https://www.temu.com/goods.html?_bg_fs=1&goods_id={x}">{x}</a>' if pd.notna(x) else x
                )
            
            sheet_data_html = df_sheet.to_html(classes='table table-bordered table-striped', escape=False)
        
        return sheet_data_html

    @app.route("/temu", methods=["GET"])
    def temuroute():
        if 'username' not in session:
            return redirect(url_for("login"))
        sheet_data_html = get_temu_listings_data()
        current_user = session['username']
        return render_template("temu.html", sheet_data_html=sheet_data_html, current_user=current_user)


    @app.route("/geek_tools", methods=["GET"])
    def geek_tools():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("geek_tools.html", current_user=current_user)
    
    @app.route("/shipping_center", methods=["GET"])
    def shipping_center():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("shipping_center.html", current_user=current_user)

    def sort_filenames(filenames):
        base_files = {}
        version_files = []
        pattern = re.compile(r"^(.*?)(_v\d+)?\.jpg$")

        for filename in filenames:
            match = pattern.match(filename)
            if match:
                base = match.group(1)
                version = match.group(2) or ""
                if version:
                    version_files.append((base, version, filename))
                else:
                    base_files[base] = filename

        # Sort versions based on the number after '_v'
        version_files.sort(key=lambda x: (x[0], int(x[1][2:]) if x[1] else 0))

        sorted_filenames = []
        for base, base_file in base_files.items():
            sorted_filenames.append(base_file)
            sorted_filenames.extend([v[2] for v in version_files if v[0] == base])

        return sorted_filenames
    
    @app.route("/geek_tools/custom_query", methods=["GET", "POST"])
    def custom_query():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        excel_html = None
        error_message = None
        log_content = []
        memo_html = None
        sql_query = ""  # Initialize the sql_query variable

        if request.method == "POST":
            sql_query = request.form.get("sql_query")
            database_name = request.form.get("database_name")
            try:
                app.logger.info("%s executed a custom SQL query: %s", current_user, sql_query)
                excel_html, error_message = execute_custom_query(sql_query, database_name)
            except Exception as e:
                error_message = str(e)

        # Read memo from static/query.xlsx
        memo_path = os.path.join(app.root_path, 'static', 'query.xlsx')
        if os.path.exists(memo_path):
            df = pd.read_excel(memo_path, usecols="A,B")
            df = df.dropna(how='all')
            memo_html = df.to_html(classes='table table-bordered table-striped', escape=False, index=False)


        return render_template("custom_query.html", excel_html=excel_html, error_message=error_message, memo_html=memo_html, current_user=current_user, sql_query=sql_query)


    def load_credentials():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        credentials_path = os.path.join(base_dir, 'credentials.json')
        with open(credentials_path) as file:
            credentials = json.load(file)
        return credentials

    @app.route('/upload_center', methods=['GET', 'POST'])
    def upload_center():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']

        if request.method == 'POST':
            files = request.files.getlist('files')
            desired_filename = request.form.get('filename', 'output_image')

            # Use the function from upload_processor.py
            output_urls = process_upload(files, desired_filename)

            return jsonify(output_urls)

        return render_template('upload_center.html', current_user=current_user)
    
    @app.route('/json_data')
    def get_json_data():
        index_file_path = os.path.join(app.config['BIG_PICTURE_DIR'], 'index.json')
        white_bg_file_path = os.path.join(app.config['BIG_PICTURE_DIR'], 'white_background_images.json')

        if os.path.exists(index_file_path) and os.path.exists(white_bg_file_path):
            with open(index_file_path, 'r') as index_file, open(white_bg_file_path, 'r') as white_bg_file:
                index_data = json.load(index_file)
                white_bg_data = json.load(white_bg_file)
            return jsonify({"index_data": index_data, "white_background_data": white_bg_data})
        
        return jsonify({"error": "Files not found"}), 404
    
    @app.route('/download_images', methods=['POST'])
    def download_images():
        user_input = request.form.get('user_input', '')
        image_type = request.form.get('image_type', 'resized')
        selected_images = request.form.get('selected_images', '')
        image_size = request.form.get('image_size')

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            if selected_images:
                selected_files = selected_images.split(',')
                for filename in selected_files:
                    if filename:
                        file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                        if os.path.exists(file_path):
                            # Skip resizing if image_size is 'full' or None
                            if image_size and image_size != 'full':
                                try:
                                    with open(file_path, 'rb') as img_file:
                                        resized_img = resize_image(img_file, int(image_size))
                                        zf.writestr(filename, resized_img.getvalue())
                                except (ValueError, TypeError):
                                    # Fallback to original file if size conversion fails
                                    zf.write(file_path, arcname=filename)
                            else:
                                # Use original file for 'full' size or when size is not specified
                                zf.write(file_path, arcname=filename)
            else:
                # Original logic for all images remains unchanged
                search_terms = [term.split()[0].strip() for term in user_input.split(",")]
                for term in search_terms:
                    try:
                        for filename in os.listdir(Config.CATALOG_ORIGINAL):
                            if filename.lower().startswith(term.lower()):
                                file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                                if os.path.exists(file_path):
                                    if image_type == 'resized':
                                        with Image.open(file_path) as img:
                                            buffer = io.BytesIO()
                                            if img.mode != 'RGB':
                                                img = img.convert('RGB')
                                            img.save(buffer, format='JPEG', quality=20)
                                            buffer.seek(0)
                                            zf.writestr(filename, buffer.getvalue())
                                    else:
                                        zf.write(file_path, arcname=filename)
                    except FileNotFoundError:
                        app.logger.error("Directory not found: %s", Config.CATALOG_ORIGINAL)

        memory_file.seek(0)
        zip_filename = "selected_images.zip" if selected_images else "images.zip"
        return send_file(
            memory_file,
            download_name=zip_filename,
            as_attachment=True
        )
    
    @app.route("/bookmarks", methods=["GET", "POST"])
    def bookmarks_page():  # Renamed function
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))

        current_user = session['username']
        
        if current_user != 'yeti':
            return render_template("index.html", current_user=current_user)

        bookmarks_list = [
            {"title": "Passwords & Accounts", "year":"8888", "category": "Passwords", "url": "https://docs.google.com/spreadsheets/d/1nkle6flS263CJ65b0G8YQf46a3aTUC8vP2r3exfdFms/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TEMU-USA", "year":"2024", "category": "Accounting",  "url": "https://docs.google.com/spreadsheets/d/1-PzqDYddrHTzBZ-f0IdzZFgmQaBK_Hx_Y_IYgHCIGWw/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TEMU-HK", "year":"2024", "category": "Accounting",  "url": "https://docs.google.com/spreadsheets/d/1n_ewjw68TLFIDouP11z-AJlI1PilObQfP3lan7DepUg/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "HR_Ashley Sign-in Sheet", "year":"2024", "category": "Payroll", "url": "https://docs.google.com/spreadsheets/d/1ABTgQ4KmVkBX0KRFV9XuIH94irc3jtBlUtjjpB1HRgE/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TIKTOK_AMBRA",  "year":"2024","category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1gwJVGsJVCpYY-LtimYCwj5_hCUc8xs1zoqzhAqO9XQ8/edit?gid=697444757#gid=697444757","linktype":"Google Sheet"},
            {"title": "Channel_Old Dec-Mar TIKTOK", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/13xcqIjFSOrDBh7xHZFsr_P4LBCKt2uGsrohIMXfeUxg/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_Tiktok Accounting 2024", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1aW4wmxQHtSXZPjgui0PhaKGszkttTKay6JVlNJflJ_M/edit?gid=462431388#gid=462431388","linktype":"Google Sheet"},
            {"title": "HR_1099 Sheet 2024", "year":"2024", "category": "Payroll", "url": "https://docs.google.com/spreadsheets/d/1bgCEWmdWmFTyAkpbOQkxDvEgBFUhPNLVJLRIOsgxWd0/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_ERWAN 2024", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1vIG8VhrZTJmuuEjXFP7ZmdnsyhAbXMWnasIOH_v76Ok/edit?gid=572048210#gid=572048210","linktype":"Google Sheet"},
            {"title": "Tiktok_2025", "year":"2025", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1v9nzwoMk3p9LAcztZOW4et2v51Wfx21VeTlJbF-X64M/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_ERWAN 2025","year": "2025","category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1nU0CIyAtQYGK9AKNBhc-l_Mr-qRaoWLen4g4oZsdaBw/edit?gid=0#gid=0","linktype":"Google Sheet"}
        ]

        return render_template("bookmark.html", current_user=current_user, bookmarks_list=bookmarks_list)

    