from flask import request, render_template, jsonify, redirect, url_for, session, flash,send_from_directory
from googleapiclient.discovery import build
from google.oauth2 import service_account
import logging
import os,re
import time
import pandas as pd
from werkzeug.utils import secure_filename
import requests
import datetime
import subprocess
import sys
from io import StringIO
from user_agents import parse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import V4_Listcreator, Label_Maker, order_search, SQL_connector
from loris_sticker import main as process_loris_stickers
from macy_special import process_order
from config import Config

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
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")
            if check_credentials(username, password):
                session['username'] = username
                flash(f"Logged in as {username}")
                log_user_activity(username)
                return redirect(url_for("home"))
            else:
                flash("Invalid credentials")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop('username', None)
        flash("Logged out")
        return redirect(url_for("login"))

    @app.route("/", methods=["GET", "POST"])
    def home():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("index.html", current_user=current_user)

    @app.route("/Item_lookup", methods=["GET", "POST"])
    def item_lookup():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        excel_html = None
        if request.method == "POST":
            item_number = request.form.get("item_number")
            app.logger.info("%s searched for: %s", current_user, item_number)
        return render_template("Item_lookup.html", excel_html=excel_html, current_user=current_user)

    def get_sheet_data(input_value):
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=Config.SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=Config.SPREADSHEET_ID, range='ONLINE_CATALOG').execute()
        values = result.get('values', [])

        if not values:
            return "No data found in the sheet."
        else:
            headers = values[1]
            sku_index, style_index, tik_index = headers.index('SKU'), headers.index('Style'), headers.index('TiK 1-999 #')
            display_headers = values[1]
            input_values = [v.strip().lower() for v in input_value.split(',')]
            matching_rows = []
            is_numeric_input = all(val.isdigit() for val in input_values)

            for row in values[2:]:
                if len(row) > max(sku_index, style_index, tik_index):
                    if is_numeric_input:
                        if any(val in row[tik_index].lower() for val in input_values):
                            matching_rows.append(row)
                    else:
                        cells_to_check = [row[sku_index], row[style_index], row[tik_index]]
                        if any(any(val in cell.lower() for val in input_values) for cell in cells_to_check):
                            matching_rows.append(row)

            return [display_headers] + matching_rows if matching_rows else [display_headers]

    def get_e1_value():
        creds = service_account.Credentials.from_service_account_file(Config.SERVICE_ACCOUNT_FILE, scopes=Config.SCOPES)
        service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=Config.SPREADSHEET_ID, range='ONLINE_CATALOG!E1').execute()
        values = result.get('values', [])
        return values[0][0] if values else None

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
        e1_value = get_e1_value()

        if not user_input:
            return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value)

        if user_input.endswith(","):
            user_input = user_input[:-1]

        excel_html, image_urls = SQL_connector.generate_excel(user_input)
        search_terms = [term.strip() for term in user_input.split(",")]

        file_names = []
        for term in search_terms:
            for filename in os.listdir(Config.CATALOG_ORIGINAL):
                if filename.lower().startswith(term):
                    file_names.append(filename)

        file_info = []
        for filename in file_names:
            resized_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
            if os.path.exists(resized_path):
                size = os.path.getsize(resized_path)
                modified_time = os.path.getmtime(resized_path)
                modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                file_info.append((filename, size, modified_date))

        if excel_html:
            df = pd.read_html(StringIO(excel_html))[0]
            sheet_data = get_sheet_data(user_input)
            max_length = max(len(row) for row in sheet_data)
            sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
            df2 = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])

            if current_user == 'fdc':
                df.drop(columns=['cost', 'Container', 'create_dt', 'update_dt'], inplace=True)

            sorted_file_names = sort_filenames(file_names)
            sorted_image_urls = [os.path.join('/ftp', fn) for fn in sorted_file_names]

            excel_html = df.to_html(classes='table', escape=False)
            sheet_data = df2.to_html(classes='table', escape=False)
            app.logger.info("Excel HTML and sheet data %s generated for %s", user_input, current_user)
            return render_template("Item_lookup.html", excel_html=excel_html, image_urls=sorted_image_urls, file_names=sorted_file_names, file_info=file_info, sheet_data=sheet_data, current_user=current_user, e1_value=e1_value)
        else:
            return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value, current_user=current_user)

    @app.route("/Order_lookup", methods=["GET", "POST"])
    def sql_connector():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        html_table = None
        image_urls = None
        if request.method == "POST":
            user_input = request.form.get("user_input")
        return render_template("order_lookup.html", html_table=html_table, image_urls=image_urls, current_user=current_user)

    @app.route('/label_maker_from_order_lookup', methods=['POST'])
    def label_maker_from_order_lookup():
        order_number = session.get('order_number')
        if not order_number:
            return "Order number is missing.", 400

        app.logger.info("Received order_number: %s", order_number)
        filename, error_message, number_of_labels, duplicate_numbers, info_list = Label_Maker.process_orders(order_number)

        if error_message:
            return error_message, 400

        base_filename = os.path.basename(filename)
        download_url = url_for('download_file2', filename=base_filename)

        return render_template(
            'order_lookup.html',
            filename=base_filename,
            download_url=download_url,
            number_of_labels=number_of_labels,
            duplicate_numbers=duplicate_numbers,
            info_list=info_list
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

    @app.route("/v4_listcreator", methods=["GET", "POST"])
    def v4_listcreator():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        filename = None
        if request.method == "POST":
            user_input = request.form.get("user_input")
            app.logger.info("%s submitted input for v4_listcreator: %s", current_user, user_input)
            V4_Listcreator.user_input = user_input
            V4_Listcreator.main()
            filename = f"{V4_Listcreator.formatted_date}.xlsx"
            app.logger.info("%s downloaded the file: %s", current_user, filename)
        return render_template("v4_listcreator.html", filename=filename, current_user=current_user)

    @app.route("/googlesheet", methods=["GET", "POST"])
    def googlesheet():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']
        sheet_data_html = None
        e1_value = get_e1_value()
        if request.method == "POST":
            user_input = request.form.get("user_input")
            app.logger.info("%s is looking up in googlesheet with input: %s", current_user, user_input)
            sheet_data = get_sheet_data(user_input)
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

    def sort_filenames(filenames):
        base_files = {}
        version_files = []
        pattern = re.compile(r"^(.*?)(v\d+)?\.jpg$")

        for filename in filenames:
            match = pattern.match(filename)
            if match:
                base = match.group(1)
                version = match.group(2) or ""
                if version:
                    version_files.append((base, version, filename))
                else:
                    base_files[base] = filename

        version_files.sort(key=lambda x: (x[0], int(x[1][1:]) if x[1] else 0))

        sorted_filenames = []
        for base, base_file in base_files.items():
            sorted_filenames.append(base_file)
            sorted_filenames.extend([v[2] for v in version_files if v[0] == base])

        return sorted_filenames
