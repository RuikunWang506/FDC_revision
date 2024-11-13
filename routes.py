from flask import (request, render_template, jsonify, redirect, url_for, 
                   session, flash, send_from_directory, send_file)
from werkzeug.utils import secure_filename

# Standard library imports
import os
import io
import re
import sys
import time
import json
import datetime
import subprocess
import urllib.parse
import zipfile
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
from io import StringIO


# Application-specific imports
from custom_query import execute_custom_query, allowed_file
from config import Config
from logging_setup import configure_logging
from upload_processor import process_upload
from big_img_routes import register_big_img_routes
from location_service import location_service_bp
from container import container_bp
from boat import boat_service_bp
from chatgpt import chatgpt_bp
import V5_Listcreator, Label_Maker, order_search, SQL_connector
from macy_special import process_order
from loris_sticker import main, process_input_data
from products import products_bp



client = WebClient(token=Config.SLACK_TOKEN)

# Configure logging

def register_routes(app):
    register_big_img_routes(app)
    app.register_blueprint(location_service_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(boat_service_bp)
    app.register_blueprint(chatgpt_bp)


    def check_credentials(username, password):
        return Config.USERS.get(username) == password

    def log_user_activity(username):
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = parse(request.headers.get('User-Agent'))
        referrer = request.referrer
        query_params = request.args

        app.logger.info(
            "User %s has logged in. IP: %s, Browser: %s %s, OS: %s %s, Device: %s, "
            "Method: %s, URL: %s, Referrer: %s, Query Params: %s",
            username, client_ip, user_agent.browser.family, user_agent.browser.version_string,
            user_agent.os.family, user_agent.os.version_string, user_agent.device.family,
            request.method, request.url, referrer, query_params
        )

    def get_location(ip):
        response = requests.get(f'http://ip-api.com/json/{ip}')
        return response.json()

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if 'username' in session:
            return redirect(url_for("home"))
        
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        location_data = get_location(client_ip)
        
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            if check_credentials(username, password):
                session['username'] = username
                flash(f"Logged in as {username}")
                log_user_activity(username)
                next_page = session.pop('next', url_for("home"))
                return redirect(next_page)
            else:
                flash("Invalid credentials")
        
        session['next'] = request.args.get('next') or request.referrer or url_for("home")
        
        # Pass additional location data to the template if available
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
                resized_path = os.path.join(Config.CATALOG_RESIZED, file_name)
                original_path = os.path.join(Config.CATALOG_ORIGINAL, file_name)
                if os.path.exists(resized_path):
                    os.remove(resized_path)
                    app.logger.info(f"{current_user} deleted {resized_path}")
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
        """
        Fetches the Location for each SKU from the database. If a location is not found,
        it tries to remove the part after the first space in the SKU and retries the query.

        Args:
            skus (list): A list of SKU strings.

        Returns:
            dict: A dictionary mapping SKU to Location.
        """
        location_dict = {}
        updated_date_dict = {}

        if not skus:
            return location_dict, updated_date_dict

        try:
            connection = get_db_connection()
            if connection is None:
                app.logger.error("Failed to connect to the database for fetching locations.")
                return location_dict, updated_date_dict

            cursor = connection.cursor()
            # Prepare the query to fetch Location based on SKUs
            placeholders = ', '.join(['%s'] * len(skus))
            query = f"SELECT SKU, Location, Update_Date FROM products WHERE SKU IN ({placeholders})"
            cursor.execute(query, skus)
            results = cursor.fetchall()

            # Populate the dictionary
            for sku, location, updated_date in results:
                location_dict[sku.lower()] = location  # Assuming SKU matching is case-insensitive
                updated_date_dict[sku.lower()] = updated_date.strftime('%Y-%m-%d') + "<br>location by sku" 

            # Handle SKUs without locations
            for sku in skus:
                if sku.lower() not in location_dict or not location_dict[sku.lower()]:
                    # Modify SKU by removing everything after the first space
                    stripped_sku = sku.split(' ')[0]
                    if stripped_sku != sku:  # Only retry if the SKU was modified
                        cursor.execute(
                            "SELECT SKU, Location, Update_Date FROM products WHERE SKU = %s",
                            (stripped_sku,)
                        )
                        result = cursor.fetchone()
                        if result:
                            location_dict[sku.lower()] = result[1]
                            updated_date_dict[sku.lower()] =  result[2].strftime('%Y-%m-%d') + "<br>accurate location by style" 


        except Error as e:
            app.logger.error(f"Error fetching locations: {e}")
        finally:
            if 'cursor' in locals() and cursor:
                cursor.close()
            if 'connection' in locals() and connection and connection.is_connected():
                connection.close()

        return location_dict, updated_date_dict

    # Ensure the output directory exists
    OUTPUT_DIR = os.path.join(os.getcwd(), 'output', 'location_DB')
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    @app.route("/item_lookup", methods=["GET", "POST"])
    @app.route("/item_lookup/<string:user_input>", methods=["GET"])
    def item_lookup(user_input=None):
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))

        current_user = session['username']
        excel_html = None

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
            return render_template("Item_lookup.html", excel_html=None, current_user=current_user, user_input=user_input)

        # Continue with the rest of the logic if user_input is valid
        if user_input.endswith(","):
            user_input = user_input[:-1]

        # Generate Excel and image URLs
        excel_html, image_urls, user_input = SQL_connector.generate_excel(user_input)

        if not excel_html:  # If no valid data is found
            app.logger.info("No valid data to display for input: %s", user_input)
            return render_template("Item_lookup.html", excel_html=None, current_user=current_user, user_input=user_input)

        # Normalize input parts by stripping color suffixes for image lookup
        search_terms = [term.split()[0].strip() for term in user_input.split(",")]

        # Get filenames matching the search terms
        file_names = []
        for term in search_terms:
            # Check in CATALOG_ORIGINAL
            for filename in os.listdir(Config.CATALOG_ORIGINAL):
                if filename.lower().startswith(term):
                    file_names.append(filename)

            # Check in CATALOG_RESIZED if not found in CATALOG_ORIGINAL
            for filename in os.listdir(Config.CATALOG_RESIZED):
                if filename.lower().startswith(term) and filename not in file_names:
                    file_names.append(filename)

        # Remove duplicates from file_names
        file_names = list(dict.fromkeys(file_names))

        # Get file information (size and modification date)
        file_info = []
        for filename in file_names:
            original_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
            resized_path = os.path.join(Config.CATALOG_RESIZED, filename)
            file_path = original_path if os.path.exists(original_path) else resized_path

            if os.path.exists(file_path):
                size = os.path.getsize(file_path)
                modified_time = os.path.getmtime(file_path)
                modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                file_info.append((filename, size, modified_date))

        if excel_html:
            # Convert the HTML back to a DataFrame
            df = pd.read_html(StringIO(excel_html))[0]

            # Add a row number column to the DataFrame
            df.insert(0, '#', range(1, len(df) + 1))

            # Fetch SKUs from the DataFrame
            if 'SKU' in df.columns:
                skus = df['SKU'].astype(str).str.lower().tolist()
            else:
                app.logger.error("SKU column not found in the DataFrame.")
                skus = []

            # Fetch locations and update dates using the helper function
            location_dict, updated_date_dict = fetch_locations(skus)

            # Add the 'Location' column based on SKU matching
            # Replace 'N/A' with empty string "" for unmatched locations
            df['Location'] = df['SKU'].astype(str).str.lower().map(location_dict).fillna('')

            # Add the 'Update_Date' column based on SKU matching
            df['Update_Date'] = df['SKU'].astype(str).str.lower().map(updated_date_dict).fillna('no data')

            # **Replacement Logic for 'Database' Column**
            if 'Database' in df.columns:
                replacement_dict = {
                    'oms_ef': 'E_FAS',
                    'omsdata_fdc': 'FDC'
                }
                # Perform case-insensitive replacement
                df['Database'] = df['Database'].str.lower().replace(replacement_dict)
            else:
                app.logger.warning("Database column not found in the DataFrame.")

            # Handle case for user 'fdc' and remove sensitive columns
            if current_user == 'fdc':
                sensitive_columns = ['cost', 'Container', 'create<br>dt', 'update<br>dt']
                # Only drop columns that exist in the DataFrame
                existing_sensitive = [col for col in sensitive_columns if col in df.columns]
                if existing_sensitive:
                    df.drop(columns=existing_sensitive, inplace=True)

            # Sort filenames and prepare image URLs
            sorted_file_names = sort_filenames(file_names)
            sorted_image_urls = [os.path.join('/ftp', fn) for fn in sorted_file_names]

            # Convert DataFrame back to HTML for display
            # Specify the desired column order if needed
            # For example, inserting 'Location' after the third column (index 2)
            if 'Location' in df.columns:
                cols = list(df.columns)
                loc_index = 3  # After the third column
                cols.insert(loc_index, cols.pop(cols.index('Location')))
                df = df[cols]

            # Modify 'Location' cells to include 'title' attribute with 'Update_Date'
            if 'Location' in df.columns and 'Update_Date' in df.columns:
                df['Location'] = df.apply(
                    lambda row: (
                        f'<span class="editable" title="Update: {row["Update_Date"]}">'
                        
                        f'{row["Location"] if pd.notnull(row["Location"]) and row["Location"] != "" else "✨ADD✨"}</span>'
                    ),
                    axis=1
                )
            df.columns = df.columns.str.replace('<br>', ' ')  # Remove any unwanted <br> in column headers
            df.columns = df.columns.str.replace('\n', '<br>')  # Replace newline with <br> in column headers



            # Convert DataFrame to HTML without escaping to allow HTML content
            excel_html = df.to_html(
                classes='table text-center',
                escape=False,  # Allow HTML tags like <br> in headers
                table_id="excel_table_1",
                index=False  # Avoid showing the DataFrame index in the table
            )

            app.logger.info("Item lookup for %s with input: %s", current_user, user_input)
            return render_template(
                "Item_lookup.html",
                excel_html=excel_html,
                image_urls=sorted_image_urls,
                file_names=sorted_file_names,
                file_info=file_info,
                current_user=current_user,
                user_input=user_input.capitalize()  # Pass user_input back to the template to avoid "None"
            )

        else:
            # No valid data, return the template without data
            return render_template("Item_lookup.html", excel_html=None, current_user=current_user, user_input=user_input.capitalize())
        
    @app.route('/update_location', methods=['POST'])
    def update_location():
        data = request.get_json()
        sku = data.get('sku')
        style = data.get('style')
        location = data.get('location')

        if not sku or not style:
            app.logger.warning(f"Missing SKU or Style in update_location request: SKU={sku}, Style={style}")
            return jsonify({'success': False, 'message': 'SKU or Style is missing.'}), 400

        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            if connection is None:
                app.logger.error("Failed to connect to the database.")
                return jsonify({'success': False, 'message': 'Failed to connect to the database.'}), 500

            cursor = connection.cursor()

            # Check if the product exists
            select_query = "SELECT * FROM products WHERE SKU = %s AND Style = %s"
            app.logger.info(f"Executing SELECT query: {select_query} with SKU={sku} and Style={style}")
            cursor.execute(select_query, (sku, style))
            product = cursor.fetchone()

            if product:
                # Update existing product
                update_query = "UPDATE products SET Location = %s, Update_Date = NOW() WHERE SKU = %s AND Style = %s"
                app.logger.info(f"Executing UPDATE query: {update_query} for SKU={sku} and Style={style}")
                cursor.execute(update_query, (location, sku, style))
            else:
                # Insert new product
                insert_query = "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())"
                app.logger.info(f"Executing INSERT query: {insert_query} for SKU={sku} and Style={style}")
                cursor.execute(insert_query, (sku, style, location))

            connection.commit()
            app.logger.info(f"Location updated successfully for SKU={sku} and Style={style}")
            return jsonify({'success': True, 'message': 'Location updated successfully.'})

        except Error as e:
            app.logger.error(f"Database error during location update for SKU={sku} and Style={style}: {e}")
            return jsonify({'success': False, 'message': 'An error occurred while updating the location.'}), 500

        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
                                            

    @app.route("/order_lookup", methods=["GET", "POST"])
    def order_lookup():
        if 'username' not in session:
            return redirect(url_for("login"))
        
        current_user = session['username']
        excel_html = None
        order_number = None

        if request.method == "POST":
            # Handle the order lookup submission
            order_number = request.form.get("order_number", "").strip()
            
            if not order_number:
                app.logger.info("No order number provided by %s", current_user)
                return render_template("order_lookup.html", excel_html=None, current_user=current_user)

            session['order_number'] = order_number
            app.logger.info("%s is looking up order number: %s", current_user, order_number)

            if current_user == 'fdc':
                app.logger.info("User %s does not have permission to view order details", current_user)
                return render_template("order_lookup.html", excel_html=None)

            excel_html = order_search.generate_order(order_number)
            
            if excel_html:
                app.logger.info("%s searched for order number: %s", current_user, order_number)
            else:
                app.logger.info("No data found for order number: %s", order_number)
                excel_html = None

        return render_template("order_lookup.html", excel_html=excel_html, order_number=order_number, current_user=current_user)

    @app.route('/label_maker_from_order_lookup', methods=['POST'])
    def label_maker_from_order_lookup():
        try:
            order_number = session.get('order_number')
            if not order_number:
                return "Order number is missing.", 400

            app.logger.info("Received order_number: %s", order_number)
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
                    order_number=order_number
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
                warnings=warnings  # Pass warnings to the template
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
                order_number=order_number
            )

    @app.route("/v5_listcreator", methods=["GET", "POST"])
    def v5_listcreator():
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
        
        current_user = session['username']
        filename = None
        user_input = None  # Initialize user_input to prevent UnboundLocalError
        user_input_list = None  # Initialize user_input_list to store the list of files
        user_input_directory = None  # Initialize user_input_directory to pass it to the template
        file_count = 0  # Initialize file_count

        if request.method == "POST":
            user_input = request.form.get("user_input").strip()
            image_option = request.form.get("image_option")
            app.logger.info(f"{current_user} request to create a list: {user_input}")

            # Check if user_input was provided
            if user_input:
                # Look for the 'Dropbox' in the path and construct the local directory path
                dropbox_index = user_input.lower().find('dropbox')
                if (dropbox_index != -1):
                    relative_path = user_input[dropbox_index + len('dropbox') + 1:].strip()  # +1 to skip the backslash and strip spaces
                    user_input_directory = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path).strip()

                    if os.path.isdir(user_input_directory):
                        user_input_type = "directory"
                        # Create a list of file names without extensions from the directory
                        user_input_list = [os.path.splitext(f)[0] for f in os.listdir(user_input_directory) if f.endswith('.jpg')]
                        # Count the number of files
                        file_count = len(user_input_list)
                        # Join the list into a comma-separated string
                        user_input = ','.join(user_input_list).strip()
                    else:
                        raise ValueError(f"The directory {user_input_directory} does not exist.")
                else:
                    user_input_type = "list"
                    user_input_list = [item.strip() for item in user_input.split(',')]

                # If input is a list, force image option to be 'resizedpic'
                if user_input_type == "list":
                    image_option = "resizedpic"

                # Pass these to the v5_Listcreator.py
                try:
                    V5_Listcreator.user_input = user_input_directory if user_input_type == "directory" else ','.join(user_input_list)
                    V5_Listcreator.image_option = image_option
                    V5_Listcreator.user_input_type = user_input_type
                    V5_Listcreator.main()
                    filename = f"{V5_Listcreator.formatted_date}.xlsx"
                except Exception as e:
                    app.logger.error("An error occurred: %s", str(e))
                    return render_template("v5_listcreator.html", filename=None, user_input=user_input, user_input_directory=user_input_directory, file_count=file_count, error_message=f"An error occurred: {str(e)}", current_user=current_user)
            else:
                # Handle the case where user_input is not provided
                app.logger.warning("No user input provided.")
                return render_template("v5_listcreator.html", filename=None, user_input=user_input, user_input_directory=user_input_directory, file_count=file_count, error_message="Please provide input.", current_user=current_user)

        return render_template("v5_listcreator.html", filename=filename, user_input=user_input, user_input_directory=user_input_directory, file_count=file_count, current_user=current_user)
    
    @app.route('/download_images', methods=['POST'])
    def download_images():
        image_type = request.form.get('image_type')  # Expecting 'resized' or 'original'
        user_input = request.form.get('user_input')
        image_list = [item.strip().upper() + ".jpg" for item in user_input.split(',')]
        
        # Determine the correct folder based on the image_type
        if image_type == 'resized':
            source_folder = Config.CATALOG_RESIZED
            zip_filename = "clean_resized_images.zip"
        elif image_type == 'original':
            source_folder = Config.CATALOG_ORIGINAL
            zip_filename = "clean_original_images.zip"
        else:
            return "Invalid image type specified.", 400
        
        memory_file = io.BytesIO()
        
        with zipfile.ZipFile(memory_file, 'w') as zf:
            for image_name in image_list:
                image_path = os.path.join(source_folder, image_name)
                if os.path.exists(image_path):
                    zf.write(image_path, arcname=image_name)
        
        memory_file.seek(0)
        return send_file(memory_file, download_name=zip_filename, as_attachment=True)
    

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

    @app.route("/run_get_tk_sheet", methods=["POST"])
    def run_get_tk_sheet():
        try:
            app.logger.info("Running get_tk_sheet script")
            process = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(__file__), 'get_tk_sheet.py')],
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
            app.logger.error("Error running get_tk_sheet script: %s", e)
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
        bol = request.form.get("bol", "")
        apt = request.form.get("apt", "")
        carrier = request.form.get("carrier", "")
        project = request.form.get("project", "")

        if request.method == "POST":
            app.logger.info("%s is processing Macy Order: %s", current_user, bol)
            filename, error_message = process_order(po_num, bol, apt, carrier, project)
            if filename is None and error_message is None:
                error_message = f"Order : {bol} not found, maybe wrong?"
                app.logger.warning("Order number %s not found for %s", bol, current_user)
            elif filename:
                filename = os.path.basename(filename)

        return render_template("macy.html", filename=filename, error_message=error_message, current_user=current_user, po_num=po_num, bol=bol, apt=apt, carrier=carrier, project=project)

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

        try:
            with open(Config.LOG_FILE, 'r') as file:
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

            return render_template('log_viewer.html', log_lines=paginated_log_lines, page=page, total_pages=total_pages), 200
        except Exception as e:
            app.logger.error('Error accessing log file: %s', e)
            return jsonify(error=str(e)), 500

    def format_logs(lines):
        formatted = []
        for line in lines:
            if 'INFO' in line:
                formatted.append(('info', line[:23], line[31:].strip()))
            elif 'ERROR' in line:
                formatted.append(('error', line[:23], line[31:].strip()))
            else:
                formatted.append(('', '', line.strip()))
        return formatted

    @app.route('/download/<filename>')
    def download_file(filename):
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        app.logger.info("[Download][List] %s downloaded the file: %s", current_user, filename)
        return send_from_directory("output/list_output", filename, as_attachment=False)

    @app.route('/download2/<filename>')
    def download_file2(filename):
        app.logger.info("[Download][labels]File %s downloaded from label output", filename)
        return send_from_directory("output/label_output/", filename, as_attachment=False)

    @app.route('/download3/<filename>')
    def download_file3(filename):
        app.logger.info("[Download][loris_barcode]File %s downloaded from barcode output", filename)
        return send_from_directory("output/barcode_output/", filename, as_attachment=False)

    @app.route('/ftp/<path:path>', methods=['GET'])
    def serve_file(path):
        return send_from_directory(Config.CATALOG_RESIZED, path, as_attachment=False)

    @app.route('/ftp2/<path:path>', methods=['GET'])
    def serve_file2(path):
        return send_from_directory(Config.CATALOG_ORIGINAL, path, as_attachment=False)
    
    #@app.route('/public/<path:path>', methods=['GET'])
    #def serve_file_pub(path):
    #    app.logger.info("Serving file public path %s", path)
    #    return send_from_directory(Config.FTP, path, as_attachment=True)
    
    #@app.route('/sizepic/<path:path>', methods=['GET'])
    #def serve_temp_size_pic(path):
    #    app.logger.info("Serving file sizepic %s", path)
    #    return send_from_directory(Config.Dimension_DIR, path, as_attachment=True)

    @app.route('/download4/<filename>')
    def download_file4(filename):
        if 'username' not in session:
            return redirect(url_for("login"))
        app.logger.info("[Download][macy_label]File %s downloaded from label output", filename)
        return send_from_directory("output/macy_label/", filename, as_attachment=False)

    @app.route("/report_bug", methods=["GET", "POST"])
    def report_bug():
        if 'username' not in session:
            return redirect(url_for("login"))
        
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

        return render_template("report_bug.html")

    @app.route('/shipping_receipt', methods=['GET'])
    def shipping_receipt():
        if 'username' not in session:
            return redirect(url_for("login"))
        return render_template('upload_file.html')
    
    @app.route('/help', methods=['GET'])
    def help():
        if 'username' not in session:
            return redirect(url_for("login"))
        return render_template('help.html')


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

        receipt_date = datetime.datetime.now().strftime('%m-%d-%Y')
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
        return render_template("calculator2.html")
    
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
        return render_template("temu.html", sheet_data_html=sheet_data_html)


    @app.route("/geek_tools", methods=["GET"])
    def geek_tools():
        if 'username' not in session:
            return redirect(url_for("login"))
        return render_template("geek_tools.html")

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

    @app.route("/log")
    def display_log():
        if 'username' not in session:
            return redirect(url_for("login"))
        try:
            with open(Config.LOG_FILE, 'r') as file:
                lines = file.readlines()
            return jsonify(log_content=lines), 200
        except Exception as e:
            app.logger.error('Error accessing log file: %s', e)
            return jsonify(error=str(e)), 500


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

        return render_template('upload_center.html')
    
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
    
