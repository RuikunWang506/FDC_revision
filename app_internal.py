from flask import Flask, render_template, request, send_from_directory, jsonify, redirect, flash, url_for
from googleapiclient.discovery import build
from google.oauth2 import service_account
from flask_basicauth import BasicAuth
import V4_Listcreator, Label_Maker, order_search, SQL_connector
from loris_sticker import main as process_loris_stickers
from macy_special import process_order
import pandas as pd
import logging
import os
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
app.config['PICTURES'] = r"D:\localresized_catelog"


# Define multiple usernames and passwords
USERS = {
    'fdc': '827',  # Lowest privilege, cannot see cost 看不见价格
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

# Create a BasicAuth instance with multiple credentials
class MultiBasicAuth(BasicAuth):
    def check_credentials(self, username, password):
        return USERS.get(username) == password

# Initialize MultiBasicAuth
basic_auth = MultiBasicAuth(app)

# Function to handle application exit
def handle_exit():
    logger.info("Application is exiting")

# Function to handle unhandled exceptions
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        # Call the default excepthook saved earlier if it's a KeyboardInterrupt
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

# Register the exit handler
atexit.register(handle_exit)

# Set the exception hook to handle unhandled exceptions
sys.excepthook = handle_exception

# Home route
@app.route("/", methods=["GET", "POST"])
@basic_auth.required
def home():
    current_user = request.authorization.username
    return render_template("index.html", current_user=current_user)

# Item lookup route
@app.route("/Item_lookup", methods=["GET", "POST"])
@basic_auth.required
def item_lookup():
    excel_html = None
    current_user = request.authorization.username
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
        headers = values[1]  # Assuming the first row contains headers
        input_values = [v.strip().lower() for v in input_value.split(',')]
        matching_rows = []

        # Determine if input values are purely numeric
        is_numeric_input = all(val.isdigit() for val in input_values)

        for row in values[1:]:
            if len(row) >= 2:  # Ensure there are at least 6 elements in the row
                if is_numeric_input:
                    # Match only against the 5th column (index 4) if input is purely numeric
                    if any(val in row[5].lower() for val in input_values):
                        matching_rows.append(row)
                else:
                    # Match against the 1st, 2nd, 6th, and 11th columns if they exist, otherwise skip
                    cells_to_check = [row[0], row[1], row[5]]  # Ensure row[5] exists
                    if len(row) > 10:
                        cells_to_check.append(row[10])  # Add row[10] only if it exists

                    if any(any(val in cell.lower() for val in input_values) for cell in cells_to_check):
                        matching_rows.append(row)

        if not matching_rows:
            return [headers]

        logging.info("Found %d matching rows for input: %s", len(matching_rows), input_value)
        return [headers] + matching_rows
    
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

@app.route("/look_up", methods=["POST"])
@basic_auth.required
def download_excel():
    current_user = request.authorization.username
    user_input = request.form.get("user_input")
    input_value = user_input
    sheet_data = get_sheet_data(input_value)
    e1_value = get_e1_value()  # Add this line to get the E1 value

    if user_input is None:
        return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value)

    if user_input.endswith(","):
        user_input = user_input[:-1]
    excel_html, image_urls = SQL_connector.generate_excel(user_input)

    if excel_html is not None:
        df = pd.read_html(excel_html)[0]
        max_length = max(len(row) for row in sheet_data)
        sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
        df2 = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])

        if current_user == 'fdc':
            df.drop(columns=['cost', 'Container', 'create_dt', 'update_dt'], inplace=True)

        excel_html = df.to_html(classes='table', escape=False)
        sheet_data = df2.to_html(classes='table', escape=False)
        logging.info("Excel HTML and sheet data %s generated for %s", user_input, current_user)
        return render_template("Item_lookup.html", excel_html=excel_html, image_urls=image_urls, sheet_data=sheet_data, current_user=current_user, e1_value=e1_value)
    else:
        return render_template("Item_lookup.html", excel_html=None, sheet_data=None, e1_value=e1_value, current_user=current_user)

# Order lookup route
@app.route("/Order_lookup", methods=["GET", "POST"])
@basic_auth.required
def sql_connector():
    html_table = None
    image_urls = None
    current_user = request.authorization.username
    if request.method == "POST":
        user_input = request.form.get("user_input")
    return render_template("order_lookup.html", html_table=html_table, image_urls=image_urls, current_user=current_user)

@app.route("/submit_order", methods=["POST"])
@basic_auth.required
def submit_order():
    current_user = request.authorization.username
    logging.info("%s submitted an order lookup", current_user)
    if current_user == 'fdc':
        logging.info("User %s does not have permission to view order details", current_user)
        return render_template("order_lookup.html", excel_html=None)

    order_number = request.form["order_number"]
    logging.info("%s is looking up order number: %s", current_user, order_number)
    excel_html = order_search.generate_order(order_number)
    logging.info("%s searched for order number: %s", current_user, order_number)
    if excel_html:
        return render_template("order_lookup.html", excel_html=excel_html)
    else:
        logging.info("No data found for order number: %s", order_number)
        return render_template("order_lookup.html", excel_html=None)

# V4 List Creator route
@app.route("/v4_listcreator", methods=["GET", "POST"])
@basic_auth.required
def v4_listcreator():
    current_user = request.authorization.username
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
@basic_auth.required
def googlesheet():
    current_user = request.authorization.username
    sheet_data_html = None
    e1_value = get_e1_value()
    if request.method == "POST":
        user_input = request.form.get("user_input")
        logging.info("%s is looking up in googlesheet with input: %s", request.authorization.username, user_input)
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

# Label Maker route
@app.route("/label_maker", methods=["GET", "POST"])
@basic_auth.required
def label_maker():
    current_user = request.authorization.username
    filename = None
    error_message = None
    if request.method == "POST":
        order_number = request.form.get("order_number")
        logging.info("%s is processing order number for label maker: %s", request.authorization.username, order_number)
        Label_Maker.order_number = order_number
        filename, error_message = Label_Maker.process_orders()
        if filename is None and error_message is None:
            error_message = f"Order : {order_number} not found, maybe wrong?"
            logging.warning("Order number %s not found for %s", order_number, request.authorization.username)
        elif filename:
            filename = os.path.basename(filename)
    return render_template("label_maker.html", filename=filename, error_message=error_message, current_user=current_user)

@app.route("/macy", methods=["GET", "POST"])
@basic_auth.required
def macy_maker():
    current_user = request.authorization.username
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

# Siri tester route
@app.route("/siri", methods=["GET", "POST"])
@basic_auth.required
def siri():
    excel_html = None
    image_urls = None
    sheet_data = None
    current_user = request.authorization.username
    if request.method == "POST":
        item_number = request.form.get("item_number")
        logging.info("%s searched for: %s", current_user, item_number)
        logging.info("Request Headers: %s", request.headers)
        logging.info("Request Form Data: %s", request.form)
        if request.is_json:
            logging.info("Request JSON: %s", request.get_json())
        excel_html, image_urls = SQL_connector.generate_excel(item_number)
        sheet_data = get_sheet_data(item_number)
        if excel_html and sheet_data:
            max_length = max(len(row) for row in sheet_data)
            sheet_data = [row + [''] * (max_length - len(row)) for row in sheet_data]
            df_sheet = pd.DataFrame(sheet_data[1:], columns=sheet_data[0])
            sheet_data_html = df_sheet.to_html(classes='table table-bordered table-striped', escape=False)
            return render_template("siri.html", excel_html=excel_html, image_urls=image_urls, sheet_data=sheet_data_html)
        else:
            return render_template("siri.html", excel_html=None, image_urls=None, sheet_data=None)
    return render_template("siri.html", excel_html=None, image_urls=None, sheet_data=None)

# Loris tool kit route
@app.route("/loris", methods=["GET", "POST"])
@basic_auth.required
def loris():
    current_user = request.authorization.username
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
                logging.info("Processing data in Loris tool kit for %s", request.authorization.username)
                filename = process_loris_stickers(input_data)
                if filename is not None:
                    filename = os.path.basename(filename)
                else:
                    error_message = "An error occurred while processing the data."
                    logging.error("Error processing data in Loris tool kit for %s", request.authorization.username)
            except Exception as e:
                error_message = f"An error occurred: {e}"
                logging.error("Exception in Loris tool kit: %s", e)
        elif "show_table" in request.form:
            try:
                table_data = parse_input_data(input_data)
            except Exception as e:
                error_message = f"An error occurred while parsing the data: %e"
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
@basic_auth.required
def display():
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
    return send_from_directory(app.config["PICTURES"], path, as_attachment=False)

@app.route('/download4/<filename>')
@basic_auth.required
def download_file4(filename):
    logging.info("File %s downloaded from label output", filename)
    return send_from_directory("output/macy_label/", filename, as_attachment=True)

# Bug report route
@app.route("/report_bug", methods=["GET"])
@basic_auth.required
def report_bug():
    return render_template("report_bug.html")

# Add the route to handle form submission
@app.route("/submit_bug", methods=["POST"])
@basic_auth.required
def submit_bug():
    current_user = request.authorization.username
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

    logging.info(f"Bug report {bug_id} submitted by {request.authorization.username}")
    
    # Send Slack notification
    slack_message = {
        "text": f"A new bug report has been submitted.\n\n*Type:* {bug_type}\n*Description:* {bug_description}\n*Submitted by:* {request.authorization.username}\n*Bug ID:* {bug_id}"
    }
    response = requests.post(SLACK_WEBHOOK_URL, json=slack_message)

    if response.status_code != 200:
        logging.error(f"Request to Slack returned an error {response.status_code}, the response is:\n{response.text}")

    return f"Bug report {bug_id} submitted successfully!"

@app.route('/shipping_receipt', methods=['GET'])
@basic_auth.required
def shipping_receipt():
    return render_template('upload_file.html')

@app.route('/send_shipping_receipt', methods=['POST'])
@basic_auth.required
def send_shipping_receipt():
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
            "text": f"*Submitted by:* {request.authorization.username}\n*Memo:* {memo}"
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
    return render_template("calculator2.html")

@app.route("/rentalagreement", methods=["GET", "POST"])
def rental_agreement():
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
    logger.info("Starting Flask application")
    try:
        app.run(host='0.0.0.0', port=5555, debug=True)
    except Exception as e:
        logger.error("Error starting Flask application: %s", e)
