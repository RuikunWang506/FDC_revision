from flask import request, render_template, jsonify, redirect, url_for, session, flash, send_from_directory, send_file
from werkzeug.utils import secure_filename
import os
import pandas as pd
from custom_query import execute_custom_query, allowed_file
from indexing_app import generate_index
from config import Config
from logging_setup import configure_logging
from googleapiclient.discovery import build
from google.oauth2 import service_account
import logging
import re
import urllib.parse
import time
import requests
import datetime
import subprocess
import sys
import json
from io import StringIO
from user_agents import parse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import openai
from openai import OpenAIError, RateLimitError
import V5_Listcreator, Label_Maker, order_search, SQL_connector
from loris_sticker import main as process_loris_stickers
from macy_special import process_order

# New imports
import base64
from PIL import Image
import io
import zipfile

client = WebClient(token=Config.SLACK_TOKEN)

# Configure logging


def register_routes(app):
    

    def check_credentials(username, password):
        return Config.USERS.get(username) == password

    def log_user_activity(username):
        client_ip = request.remote_addr
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

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if 'username' in session:
            return redirect(url_for("home"))
        
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
        return render_template("login.html")

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

    @app.route("/Item_lookup", methods=["GET", "POST"])
    def item_lookup():
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
        current_user = session['username']
        excel_html = None
        if request.method == "POST":
            item_number = request.form.get("item_number")
            app.logger.info("%s searched for: %s", current_user, item_number)
        return render_template("Item_lookup.html", excel_html=excel_html, current_user=current_user)


    def get_sheet_data_and_e1_value(input_value):
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=Config.SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        sheet = service.spreadsheets()
        
        # Fetch both the sheet data and E1 value in one request
        ranges = ['ONLINE_CATALOG', 'ONLINE_CATALOG!E1']
        result = sheet.values().batchGet(spreadsheetId=Config.SPREADSHEET_ID, ranges=ranges).execute()
        
        sheet_data = result['valueRanges'][0].get('values', [])
        e1_value = result['valueRanges'][1].get('values', [[None]])[0][0]
        
        if not sheet_data:
            return "No data found in the sheet.", e1_value
        else:
            headers = sheet_data[1]
            sku_index, style_index, tik_index = headers.index('SKU'), headers.index('Style'), headers.index('TiK 1-999 #')
            display_headers = sheet_data[1]
            input_values = [v.strip().lower() for v in input_value.split(',')]
            matching_rows = []
            is_numeric_input = all(val.isdigit() for val in input_values)

            for row in sheet_data[2:]:
                if len(row) > max(sku_index, style_index, tik_index):
                    if is_numeric_input:
                        if any(val in row[tik_index].lower() for val in input_values):
                            matching_rows.append(row)
                    else:
                        cells_to_check = [row[sku_index], row[style_index], row[tik_index]]
                        if any(any(val in cell.lower() for val in input_values) for cell in cells_to_check):
                            matching_rows.append(row)

            return [display_headers] + matching_rows if matching_rows else [display_headers], e1_value


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

    @app.route("/look_up", methods=["POST"])
    def download_excel():
        if 'username' not in session:
            return redirect(url_for("login"))

        current_user = session['username']
        user_input = request.form.get("user_input", "").lower().strip()

        if not user_input:
            _, e1_value = get_sheet_data_and_e1_value('')
            return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value, current_user=current_user, user_input=user_input)

        if user_input.endswith(","):
            user_input = user_input[:-1]

        # Generate Excel and image URLs
        excel_html, image_urls, user_input = SQL_connector.generate_excel(user_input)

        if not excel_html:  # If the excel_html is None or empty, return the template without rendering data
            app.logger.info("No valid data to display for input: %s", user_input)
            return render_template("Item_lookup.html", excel_html=None, current_user=current_user)

        # Normalize input parts by stripping color suffixes for image lookup
        search_terms = [term.split()[0].strip() for term in user_input.split(",")]

        # Get filenames matching the search terms
        file_names = []
        for term in search_terms:
            for filename in os.listdir(Config.CATALOG_ORIGINAL):
                if filename.lower().startswith(term):
                    file_names.append(filename)

            # Check in catalog_resized if not found in catalog_original
            for filename in os.listdir(Config.CATALOG_RESIZED):
                if filename.lower().startswith(term) and filename not in file_names:
                    file_names.append(filename)

        # Get file information
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
            df = pd.read_html(StringIO(excel_html))[0]
            sheet_data, e1_value = get_sheet_data_and_e1_value(user_input)

            # Normalize sheet data to ensure all rows have the same number of columns
            max_length = max(len(row) for row in sheet_data)
            sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
            df2 = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])

            if current_user == 'fdc':
                df.drop(columns=['cost', 'Container', 'create_dt', 'update_dt'], inplace=True)

            sorted_file_names = sort_filenames(file_names)
            sorted_image_urls = [os.path.join('/ftp', fn) for fn in sorted_file_names]

            # Convert DataFrames back to HTML
            excel_html = df.to_html(classes='table text-center', escape=False, table_id="excel_table_1")
            sheet_data_html = df2.to_html(classes='table', escape=False, table_id="excel_table_2")

            app.logger.info("Excel HTML and sheet data generated for %s", current_user)
            return render_template(
                "Item_lookup.html",
                excel_html=excel_html,
                image_urls=sorted_image_urls,
                file_names=sorted_file_names,
                file_info=file_info,
                sheet_data=sheet_data_html,
                current_user=current_user,
                e1_value=e1_value,
                user_input=user_input.capitalize()
            )
        else:
            _, e1_value = get_sheet_data_and_e1_value('')
            return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value, current_user=current_user, user_input=user_input.capitalize())


                                            

    @app.route("/Order_lookup", methods=["GET", "POST"])
    def sql_connector():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        html_table = None
        if request.method == "POST":
            user_input = request.form.get("user_input")
        return render_template("order_lookup.html", html_table=html_table,  current_user=current_user)

    @app.route('/label_maker_from_order_lookup', methods=['POST'])
    def label_maker_from_order_lookup():
        try:
            order_number = session.get('order_number')
            if not order_number:
                return "Order number is missing.", 400

            app.logger.info("Received order_number: %s", order_number)
            filename, error_message, number_of_labels, duplicate_numbers, info_list = Label_Maker.process_orders(order_number)

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
                order_number=order_number
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

    @app.route("/submit_order", methods=["POST"])
    def submit_order():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        app.logger.info("%s submitted an order lookup", current_user)
        if current_user == 'fdc':
            app.logger.info("User %s does not have permission to view order details", current_user)
            return render_template("order_lookup.html", excel_html=None)

        order_number = request.form["order_number"]
        session['order_number'] = order_number
        app.logger.info("%s is looking up order number: %s", current_user, order_number)
        excel_html = order_search.generate_order(order_number)
        app.logger.info("%s searched for order number: %s", current_user, order_number)
        if excel_html:
            return render_template("order_lookup.html", excel_html=excel_html, order_number=order_number)
        else:
            app.logger.info("No data found for order number: %s", order_number)
            return render_template("order_lookup.html", excel_html=None)

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
                    app.logger.info("%s downloaded the file: %s", current_user, filename)
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
    @app.route("/googlesheet", methods=["GET", "POST"])
    def googlesheet():
        if 'username' not in session:
            return redirect(url_for("login"))
        
        current_user = session['username']
        sheet_data_html = None
        e1_value = None

        if request.method == "POST":
            user_input = request.form.get("user_input")
            app.logger.info("%s is looking up in googlesheet with input: %s", current_user, user_input)
            
            # Fetch both sheet data and e1_value
            sheet_data, e1_value = get_sheet_data_and_e1_value(user_input)
            
            if sheet_data:
                max_length = max(len(row) for row in sheet_data)
                sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
                df_sheet = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])
                sheet_data_html = df_sheet.to_html(classes='table table-bordered table-striped', escape=False)

        return render_template("googlesheet.html", sheet_data=sheet_data_html, e1_value=e1_value, current_user=current_user)


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
        filename = None
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
                    filename = process_loris_stickers(input_data)
                    if filename is not None:
                        filename = os.path.basename(filename)
                    else:
                        error_message = "An error occurred while processing the data."
                        app.logger.error("Error processing data in Loris tool kit for %s", current_user)
                except Exception as e:
                    error_message = f"An error occurred: {e}"
                    app.logger.error("Exception in Loris tool kit: %s", e)
            elif "show_table" in request.form:
                try:
                    table_data = parse_input_data(input_data)
                except Exception as e:
                    error_message = f"An error occurred while parsing the data: {e}"
                    app.logger.error("Error parsing data in Loris tool kit: %s", e)
        return render_template("loris.html", filename=filename, error_message=error_message, table_data=table_data, input_data=input_data, sku=sku, barcode=barcode, quantity=quantity, current_user=current_user)

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
                    quantity = ' '.join(parts[2:])
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
            return render_template('log_viewer.html', log_lines=reversed_lines), 200
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
        app.logger.info("File %s downloaded", filename)
        return send_from_directory("output/list_output", filename, as_attachment=False)

    @app.route('/download2/<filename>')
    def download_file2(filename):
        app.logger.info("File %s downloaded from label output", filename)
        return send_from_directory("output/label_output/", filename, as_attachment=False)

    @app.route('/download3/<filename>')
    def download_file3(filename):
        app.logger.info("File %s downloaded from barcode output", filename)
        return send_from_directory("output/barcode_output/", filename, as_attachment=False)

    @app.route('/ftp/<path:path>', methods=['GET'])
    def serve_file(path):
        app.logger.info("Serving file %s", path)
        return send_from_directory(Config.CATALOG_RESIZED, path, as_attachment=False)

    @app.route('/ftp2/<path:path>', methods=['GET'])
    def serve_file2(path):
        app.logger.info("Serving file %s", path)
        return send_from_directory(Config.CATALOG_ORIGINAL, path, as_attachment=False)
    
    @app.route('/public/<path:path>', methods=['GET'])
    def serve_file_pub(path):
        app.logger.info("Serving file %s", path)
        return send_from_directory(Config.FTP, path, as_attachment=True)

    @app.route('/download4/<filename>')
    def download_file4(filename):
        if 'username' not in session:
            return redirect(url_for("login"))
        app.logger.info("File %s downloaded from label output", filename)
        return send_from_directory("output/macy_label/", filename, as_attachment=True)

    @app.route("/report_bug", methods=["GET"])
    def report_bug():
        if 'username' not in session:
            return redirect(url_for("login"))
        return render_template("report_bug.html")

    @app.route("/submit_bug", methods=["POST"])
    def submit_bug():
        if 'username' not in session:
            return redirect(url_for("login"))
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

    @app.route('/shipping_receipt', methods=['GET'])
    def shipping_receipt():
        if 'username' not in session:
            return redirect(url_for("login"))
        return render_template('upload_file.html')


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
        

    def resize_image(image_path, size=(300, 300)):
        try:
            with Image.open(image_path) as img:
                img = img.convert("RGB")
                img = img.resize(size, Image.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG")
                buffer.seek(0)
                return buffer
        except Exception as e:
            app.logger.error(f"Error resizing image {image_path}: {e}")
            return None

    def b64encode_filter(data):
        data.seek(0)
        return base64.b64encode(data.read()).decode('utf-8')

    app.jinja_env.filters['b64encode'] = b64encode_filter

    @app.route('/download_all_images')
    def download_all_images():
        current_user = session['username']
        search_str = request.args.get('search_str', '').lower()
        index_file = os.path.join(app.config['BIG_PICTURE_DIR'], 'index.json')

        if not os.path.exists(index_file):
            return "Index file not found. Please generate the index first.", 500

        with open(index_file, 'r') as f:
            index = json.load(f)

        matching_files = [file_path for file_path in index if os.path.basename(file_path).lower().startswith(search_str)]

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            for file_path in matching_files:
                zf.write(file_path, os.path.basename(file_path))
        
        memory_file.seek(0)
        app.logger.info("%s downloaded all images of %s", current_user, search_str)
        
        # Change the download_name to use search_str.zip
        zip_filename = f"{search_str}.zip"
        return send_file(memory_file, download_name=zip_filename, as_attachment=True)
    @app.route('/download_selected_images', methods=['POST'])
    def download_selected_images():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401

        selected_images = request.form.getlist('selected_images')
        if not selected_images:
            flash("No images selected.")
            return redirect(url_for('big_img'))

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            for filename in selected_images:
                file_path = os.path.join(app.config['BIG_PICTURE_DIR'], filename)
                zf.write(file_path, os.path.basename(file_path))

        memory_file.seek(0)
        zip_filename = "selected_images.zip"
        return send_file(memory_file, download_name=zip_filename, as_attachment=True)
    @app.template_filter('basename')
    def basename_filter(path):
        return os.path.basename(path)

    # Register the filter
    app.jinja_env.filters['basename'] = basename_filter 

    def format_file_size(size):
        if size < 1000:
            return f"{size} KB"
        else:
            size_mb = size / 1000
            return f"{size_mb:.2f} MB"

    @app.route('/big_img', methods=['GET', 'POST'])
    def big_img():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']

        if request.method == 'POST':
            search_str = request.form.get('search_str', '').lower()
            index_file = os.path.join(app.config['BIG_PICTURE_DIR'], 'index.json')
            
            if not os.path.exists(index_file):
                return "Index file not found. Please generate the index first.", 500

            with open(index_file, 'r') as f:
                index = json.load(f)

            matching_files = [file_path for file_path in index if os.path.basename(file_path).lower().startswith(search_str)]
            
            image_data = []
            for file_path in matching_files:
                image_path = os.path.join(app.config['BIG_PICTURE_DIR'], file_path)
                resized_image = resize_image(image_path)
                if resized_image:
                    modified_time = os.path.getmtime(image_path)
                    modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                    file_size = format_file_size(os.path.getsize(image_path) // 1024)  # Convert bytes to KB
                    filename = os.path.basename(file_path)
                    image_data.append((file_path, resized_image, modified_date, file_size))
                    
            return render_template('big_img.html', image_data=image_data, quote=urllib.parse.quote)
        return render_template('big_img.html', quote=urllib.parse.quote)

    @app.route('/ftp3/<path:encoded_path>', methods=['GET'])
    def serve_file3(encoded_path):
        decoded_path = urllib.parse.unquote(encoded_path)
        app.logger.info("Serving file %s", decoded_path)
        full_path = os.path.join(app.config['BIG_PICTURE_DIR'], decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=False)

    @app.route('/ftp3/download/<path:encoded_path>', methods=['GET'])
    def download_file5(encoded_path):
        decoded_path = urllib.parse.unquote(encoded_path)
        app.logger.info("Serving file %s", decoded_path)
        full_path = os.path.join(app.config['BIG_PICTURE_DIR'], decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=True)  
    


    def load_credentials():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        credentials_path = os.path.join(base_dir, 'credentials.json')
        with open(credentials_path) as file:
            credentials = json.load(file)
        return credentials

    def setup_openai():
        credentials = load_credentials()
        openai.api_key = credentials['openai_api_key']
        return openai

    # Set up OpenAI API key
    setup_openai()

    def get_gpt_response(message, retries=5):
        try:
            completion = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "user",
                        "content": message,
                    },
                ],
            )
            return completion.choices[0].message['content']
        except RateLimitError:
            if retries > 0:
                delay = (6 - retries) ** 2  # Exponential backoff
                time.sleep(delay)
                return get_gpt_response(message, retries - 1)
            else:
                return "Error: Rate limit exceeded. Please try again later."
        except OpenAIError as e:
            return f"An OpenAI error occurred: {str(e)}"
        except Exception as e:
            return f"An error occurred: {str(e)}"

    @app.route('/chat', methods=['POST'])
    def chat():
        data = request.get_json()
        user_message = data.get('message', '')
        gpt_response = get_gpt_response(user_message)
        return jsonify({'response': gpt_response})
    
    '''
    client = OpenAI(
    # defaults to os.environ.get("OPENAI_API_KEY")
    api_key="private",
)

def chat_gpt(prompt):
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()'''