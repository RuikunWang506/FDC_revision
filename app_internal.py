from flask import Flask, render_template, request, send_from_directory, jsonify, redirect, flash, url_for, session
from googleapiclient.discovery import build
from google.oauth2 import service_account
from flask_basicauth import BasicAuth
import V4_Listcreator, Label_Maker, order_search, SQL_connector
from loris_sticker import main as process_loris_stickers
from macy_special import process_order
import pandas as pd
import logging
import os, re,time
import requests
import datetime
import sys
import subprocess
from werkzeug.utils import secure_filename
import atexit
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Configure logging
log_file = "Log_VJune_2024.txt"
log_level = logging.INFO

# Create a custom logger
logger = logging.getLogger()
logger.setLevel(log_level)

# Create handlers
file_handler = logging.FileHandler(log_file)
console_handler = logging.StreamHandler()

# Set level for handlers
file_handler.setLevel(log_level)
console_handler.setLevel(log_level)

# Create formatters and add it to handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Custom filter to ignore HTTP requests
class IgnoreHTTPRequestFilter(logging.Filter):
    def filter(self, record):
        return not ('GET /' in record.getMessage() or 'POST /' in record.getMessage())

# Add the custom filter to handlers
file_handler.addFilter(IgnoreHTTPRequestFilter())
console_handler.addFilter(IgnoreHTTPRequestFilter())

# Add handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Disable Flask default HTTP request logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
if not os.path.exists(log_file):
    with open(log_file, 'w') as file:
        file.write("Log file created.\n")

# Google Sheets API credentials
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SERVICE_ACCOUNT_FILE = 'credentials.json'  # Path to your service account JSON file
creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1JvogexnVvPEPTxeuXhqt5zvYh7vMzOqhxCe6yxBWqaI/edit'
SPREADSHEET_ID = SPREADSHEET_URL.split('/d/')[1].split('/edit')[0]

SLACK_WEBHOOK_URL = 'https://hooks.slack.com/services/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'
SLACK_WEBHOOK_URL_SHIPPING_RECEIPT = 'https://hooks.slack.com/services/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'
SLACK_TOKEN = 'xoxb-4986748541874-7439245251395-YMVIoCeqbhWRp4rAFx66JGMK'  # Replace with your actual Slack bot token
SLACK_CHANNEL = 'C07CGNKSPF1'  # Replace with your actual Slack channel
client = WebClient(token=SLACK_TOKEN)


# Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Set the secret key for session management
app.config['catalog_resized'] = r"D:\Catalog_pictures\catalog_resized"
app.config['catalog_original'] = r"D:\Catalog_pictures\catalog_original"
@app.template_filter('zip')
def zip_filter(a, b):
    return zip(a, b)

app.jinja_env.filters['zip'] = zip_filter

# Define multiple usernames and passwords
USERS = {
    'fdc': '827',  # Lowest privilege, cannot see cost 看不见价格
    'yetimaster':'whitebox',
    'yeti': '5788',  # Full visibility
    'ken': '2843', # Full visibility
    'ahua':'5788', # Full visibility
    'derrick': '0914',  # Full visibility
    'nicye':'dick', # Full visibility
    'ting':'827', # Full visibility
    'helen':'827', # Full visibility
    'kx':'200', #full visibility ( jarven)
    'dennis':'dennis',
    'baiqiang':'1234' # Full visibility
}

def check_credentials(username, password):
    return USERS.get(username) == password

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        if check_credentials(username, password):
            session['username'] = username
            flash(f"Logged in as {username}")
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


# Item lookup route
@app.route("/Item_lookup", methods=["GET", "POST"])
def item_lookup():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    excel_html = None
    if request.method == "POST":
        item_number = request.form.get("item_number")
        logging.info("%s searched for: %s", current_user, item_number)
    return render_template("Item_lookup.html", excel_html=excel_html, current_user=current_user)

def get_sheet_data(input_value):
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range='ONLINE_CATALOG').execute()
    values = result.get('values', [])

    if not values:
        return "No data found in the sheet."
    else:
        # Headers are in the second row
        headers = values[1]

        # Identify the indices of the desired columns
        sku_index = headers.index('SKU')
        style_index = headers.index('Style')
        tik_index = headers.index('TiK 1-999 #')

        # Updated to match the actual header names and the desired columns
        display_headers = values[1]
        input_values = [v.strip().lower() for v in input_value.split(',')]
        if 'all' in input_values:
            return values[1:]  # Return all data including headers
        matching_rows = []

        # Determine if input values are purely numeric
        is_numeric_input = all(val.isdigit() for val in input_values)

        for row in values[2:]:  # Skip the first two rows (headers and actual data starts from row 3)
            if len(row) > max(sku_index, style_index, tik_index):  # Ensure the required columns exist in the row
                if is_numeric_input:
                    # Match only against the 'TiK 1-999 #' column if input is purely numeric
                    if any(val in row[tik_index].lower() for val in input_values):
                        matching_rows.append(row)
                else:
                    # Match against the 'SKU', 'Style', and 'TiK 1-999 #' columns
                    cells_to_check = [row[sku_index], row[style_index], row[tik_index]]

                    if any(any(val in cell.lower() for val in input_values) for cell in cells_to_check):
                        matching_rows.append(row)

        if not matching_rows:
            return [display_headers]

        logging.info("Found %d matching rows for input: %s", len(matching_rows), input_value)
        return [display_headers] + matching_rows

    
####this can probably be combined with get_sheet_data    
def get_e1_value():
    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=SPREADSHEET_ID, range='ONLINE_CATALOG!E1').execute()
    values = result.get('values', [])
    if not values:
        return None
    else:
        return values[0][0]

def sort_filenames(filenames):
    base_files = {}
    version_files = []

    # Regex to capture the base name and version number
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

    # Sort version files by base name and version number
    version_files.sort(key=lambda x: (x[0], int(x[1][1:]) if x[1] else 0))

    sorted_filenames = []
    for base, base_file in base_files.items():
        sorted_filenames.append(base_file)
        sorted_filenames.extend([v[2] for v in version_files if v[0] == base])

    return sorted_filenames

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
            # Paths to the files
            resized_path = os.path.join(app.config['catalog_resized'], file_name)
            original_path = os.path.join(app.config['catalog_original'], file_name)

            # Delete the files if they exist
            if os.path.exists(resized_path):
                os.remove(resized_path)
                logging.info(f"{current_user} deleted {resized_path}")

            if os.path.exists(original_path):
                os.remove(original_path)
                logging.info(f"{current_user} deleted {original_path}")

            return jsonify({"message": "File deleted successfully"}), 200
        except Exception as e:
            logging.error(f"Error deleting file {file_name}: {e}")
            return jsonify({"error": "Error deleting file"}), 500
    else:
        return jsonify({"error": "No file name provided"}), 400

@app.route("/look_up", methods=["POST"])
def download_excel():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    user_input = request.form.get("user_input").lower()  # Normalize to lowercase
    input_value = user_input
    sheet_data = get_sheet_data(input_value)
    e1_value = get_e1_value()  # Add this line to get the E1 value

    if user_input is None:
        return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value)

    if user_input.endswith(","):
        user_input = user_input[:-1]

    excel_html, image_urls = SQL_connector.generate_excel(user_input)
    
    # Split user input into individual items
    search_terms = [term.strip() for term in user_input.split(",")]

    # Fetch images from the catalog_resized directory based on user input
    image_urls = []
    file_names = []
    for term in search_terms:
        for filename in os.listdir(app.config['catalog_original']):
            if filename.lower().startswith(term):
                image_urls.append(os.path.join('/ftp', filename))
                file_names.append(filename)

    file_info = []
    for filename in file_names:
        resized_path = os.path.join(app.config['catalog_original'], filename)
        if os.path.exists(resized_path):
            size = os.path.getsize(resized_path)
            modified_time = os.path.getmtime(resized_path)
            modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
            file_info.append((filename, size, modified_date))

    if excel_html is not None:
        df = pd.read_html(excel_html)[0]
        max_length = max(len(row) for row in sheet_data)
        sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
        df2 = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])

        if current_user == 'fdc':
            df.drop(columns=['cost', 'Container', 'create_dt', 'update_dt'], inplace=True)

        # Sort filenames
        sorted_file_names = sort_filenames(file_names)
        sorted_image_urls = [os.path.join('/ftp', fn) for fn in sorted_file_names]

        excel_html = df.to_html(classes='table', escape=False)
        sheet_data = df2.to_html(classes='table', escape=False)
        logging.info("Excel HTML and sheet data %s generated for %s", user_input, current_user)
        return render_template("Item_lookup.html", excel_html=excel_html, image_urls=sorted_image_urls, file_names=sorted_file_names, file_info=file_info, sheet_data=sheet_data, current_user=current_user, e1_value=e1_value)
    else:
        return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value, current_user=current_user)

    
# Order lookup route
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

    print(f"Received order_number: {order_number}")  # Debug print

    filename, error_message, number_of_labels, duplicate_numbers = Label_Maker.process_orders(order_number)
    
    if error_message:
        return error_message, 400

    # Extract just the filename
    base_filename = os.path.basename(filename)
    # Generate the correct download URL
    download_url = url_for('download_file2', filename=base_filename)

    return render_template(
        'order_lookup.html',
        filename=base_filename,
        download_url=download_url,
        number_of_labels=number_of_labels,
        duplicate_numbers=duplicate_numbers
    )

@app.route("/submit_order", methods=["POST"])
def submit_order():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    logging.info("%s submitted an order lookup", current_user)
    if current_user == 'fdc':
        logging.info("User %s does not have permission to view order details", current_user)
        return render_template("order_lookup.html", excel_html=None)

    order_number = request.form["order_number"]
    session['order_number'] = order_number  # Store order number in session
    logging.info("%s is looking up order number: %s", current_user, order_number)
    excel_html = order_search.generate_order(order_number)
    logging.info("%s searched for order number: %s", current_user, order_number)
    if excel_html:
        return render_template("order_lookup.html", excel_html=excel_html, order_number=order_number)
    else:
        logging.info("No data found for order number: %s", order_number)
        return render_template("order_lookup.html", excel_html=None)

# V4 List Creator route
@app.route("/v4_listcreator", methods=["GET", "POST"])
def v4_listcreator():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    filename = None
    if request.method == "POST":
        user_input = request.form.get("user_input")
        logging.info("%s submitted input for v4_listcreator: %s", current_user, user_input)
        V4_Listcreator.user_input = user_input
        V4_Listcreator.main()
        filename = f"{V4_Listcreator.formatted_date}.xlsx"
        logging.info("%s downloaded the file: %s", current_user, filename)
    return render_template("v4_listcreator.html", filename=filename, current_user=current_user)

# Google Sheet route
@app.route("/googlesheet", methods=["GET", "POST"])
def googlesheet():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    sheet_data_html = None
    e1_value = get_e1_value()
    if request.method == "POST":
        user_input = request.form.get("user_input")
        logging.info("%s is looking up in googlesheet with input: %s", current_user, user_input)
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
        logging.info("Running fast script")
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
        logging.error("Error running fast script: %s", e)
        return jsonify(str(e))

@app.route("/run_get_tk_sheet", methods=["POST"])
def run_get_tk_sheet():
    try:
        logging.info("Running get_tk_sheet script")
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
        logging.error("Error running get_tk_sheet script: %s", e)
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
        logging.info("%s is processing order number for label maker: %s", current_user, order_number)
        Label_Maker.order_number = order_number
        filename, error_message, number_of_labels, duplicate_numbers = Label_Maker.process_orders()
        if filename is None and error_message is None:
            error_message = f"Order : {order_number} not found, maybe wrong?"
            logging.warning("Order number %s not found for %s", order_number, current_user)
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
        logging.info("%s is processing Macy Order: %s", current_user, bol)
        
        filename, error_message = process_order(po_num, bol, apt, carrier, project)
        if filename is None and error_message is None:
            error_message = f"Order : {bol} not found, maybe wrong?"
            logging.warning("Order number %s not found for %s", bol, current_user)
        elif filename:
            filename = os.path.basename(filename)

    return render_template("macy.html", filename=filename, error_message=error_message, current_user=current_user, po_num=po_num, bol=bol, apt=apt, carrier=carrier, project=project)

# Loris tool kit route
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
                logging.info("Processing data in Loris tool kit for %s", current_user)
                filename = process_loris_stickers(input_data)
                if filename is not None:
                    filename = os.path.basename(filename)
                else:
                    error_message = "An error occurred while processing the data."
                    logging.error("Error processing data in Loris tool kit for %s", current_user)
            except Exception as e:
                error_message = f"An error occurred: {e}"
                logging.error("Exception in Loris tool kit: %s", e)
        elif "show_table" in request.form:
            try:
                table_data = parse_input_data(input_data)
            except Exception as e:
                error_message = f"An error occurred while parsing the data: {e}"
                logging.error("Error parsing data in Loris tool kit: %s", e)
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
        with open(log_file, 'r') as file:
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

# Additional paths
@app.route('/download/<filename>')
def download_file(filename):
    logging.info("File %s downloaded", filename)
    return send_from_directory("output/list_output", filename, as_attachment=False)

@app.route('/download2/<filename>')
def download_file2(filename):
    logging.info("File %s downloaded from label output", filename)
    return send_from_directory("output/label_output/", filename, as_attachment=False)

@app.route('/download3/<filename>')
def download_file3(filename):
    logging.info("File %s downloaded from barcode output", filename)
    return send_from_directory("output/barcode_output/", filename, as_attachment=False)

@app.route('/ftp/<path:path>', methods=['GET'])
def serve_file(path):
    logging.info("Serving file %s", path)
    return send_from_directory(app.config["catalog_resized"], path, as_attachment=False)

@app.route('/ftp2/<path:path>', methods=['GET'])
def serve_file2(path):
    logging.info("Serving file %s", path)
    return send_from_directory(app.config["catalog_original"], path, as_attachment=False)

@app.route('/download4/<filename>')
def download_file4(filename):
    if 'username' not in session:
        return redirect(url_for("login"))
    logging.info("File %s downloaded from label output", filename)
    return send_from_directory("output/macy_label/", filename, as_attachment=True)

# Bug report route
@app.route("/report_bug", methods=["GET"])
def report_bug():
    if 'username' not in session:
        return redirect(url_for("login"))
    return render_template("report_bug.html")

# Add the route to handle form submission
@app.route("/submit_bug", methods=["POST"])
def submit_bug():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    bug_type = request.form["bug_type"]
    bug_description = request.form["bug_description"]
    bug_file = request.files["bug_file"]

    # Create a new folder for each bug report
    bug_id = 10001
    bug_dir = os.path.join("output", "bug", str(bug_id))
    while os.path.exists(bug_dir):
        bug_id += 1
        bug_dir = os.path.join("output", "bug", str(bug_id))
    os.makedirs(bug_dir)

    # Save the bug description
    with open(os.path.join(bug_dir, "description.txt"), "w") as f:
        f.write(f"Type: {bug_type}\nDescription: {bug_description}")

    # Save the uploaded file if there is one
    if bug_file:
        filename = secure_filename(bug_file.filename)
        bug_file.save(os.path.join(bug_dir, filename))

    logging.info(f"Bug report {bug_id} submitted by {current_user}")
    
    # Send Slack notification
    slack_message = {
        "text": f"A new bug report has been submitted.\n\n*Type:* {bug_type}\n*Description:* {bug_description}\n*Submitted by:* {current_user}\n*Bug ID:* {bug_id}"
    }
    response = requests.post(SLACK_WEBHOOK_URL, json=slack_message)

    if response.status_code != 200:
        logging.error(f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}")

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
        # Save the memo with a unique name if provided
        if memo:
            save_memo_with_unique_name(receipt_dir, memo)

        # Save the uploaded file with a unique name
        file_path = save_file_with_unique_name(receipt_dir, file, secure_filename(file.filename))

        try:
            # Upload the file to Slack using files_upload_v2
            response = client.files_upload_v2(
                channel=SLACK_CHANNEL,
                file=file_path,
                initial_comment=f"Shipping receipt uploaded:\n\n*memo:* {memo}\n*Receipt Date:* {receipt_date}\n*shipper:* {store}"
            )

            if response['ok']:
                flash('File successfully uploaded and sent to Slack')
            else:
                logging.error(f"Slack API error: {response['error']}")
                flash('Failed to send file to Slack')

        except SlackApiError as e:
            logging.error(f"Slack API error: {e.response['error']}")
            flash('Failed to send file to Slack')

    elif memo:
        # Save the memo with a unique name
        save_memo_with_unique_name(receipt_dir, memo)

        flash('Memo successfully saved.')

        slack_message = {
            "text": f"*Submitted by:* {current_user}\n*Memo:* {memo}"
        }
        response = requests.post(SLACK_WEBHOOK_URL_SHIPPING_RECEIPT, json=slack_message)

        if response.status_code != 200:
            logging.error(f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}")

    return redirect(url_for('shipping_receipt'))
# Rental agreement route
output_dir = os.path.join('output', 'scooter')
os.makedirs(output_dir, exist_ok=True)

@app.route("/calculator", methods=["GET"])
def calculatorapp():
    if 'username' not in session:
        return redirect(url_for("login"))
    return render_template("calculator2.html")

@app.route("/rentalagreement", methods=["GET", "POST"])
def rental_agreement():
    if 'username' not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        name = request.form["name"]
        phone = request.form["phone"]
        email = request.form["email"]

        # Create the content of the rental agreement
        agreement_content = (
            f"Name: {name}\n"
            f"Phone: {phone}\n"
            f"Email: {email}\n"
            f"Agreement: I agree to the terms and conditions.\n"
            f"Rate: $2.99 per mile + $0.99 start fee\n"
            f"Company: Flamingyeti Express\n"
            f"Address: Tristen Table, City of Industry, CA 91748\n"
            f"Contact Number: 626-278-9330\n"
        )

        # Save the agreement to a text file
        file_path = os.path.join(output_dir, f"{name.replace(' ', '_')}_rental_agreement.txt")
        with open(file_path, 'w') as file:
            file.write(agreement_content)
        slack_message = {
        "text": f"A new agreement from {name} !"
    }
        response = requests.post(SLACK_WEBHOOK_URL, json=slack_message)

        return "Agreement submitted successfully!"
    return render_template("rental_agreement.html")

if __name__ == "__main__":
    logger.info("autobots, roll out!")
    try:
        app.run(host='0.0.0.0', port=5555, debug=True)
    except Exception as e:
        logger.error("Error starting Flask application: %s", e)
