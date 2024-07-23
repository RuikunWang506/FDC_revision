import pyodbc
import gspread
from google.oauth2 import service_account
import time
import datetime
import logging
from googleapiclient.errors import HttpError

logging.basicConfig(
    filename='log.txt',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
# Define your SQL Server connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
database_oms_ef = "oms_EF"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

# Define Google Sheets credentials and scope
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=scope)
client = gspread.authorize(creds)

def fetch_inventory(skus):
    conn_fdc = pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={database_fdc};UID={username};PWD={password}'
    )
    conn_oms_ef = pyodbc.connect(
        f'DRIVER={driver};SERVER={server};DATABASE={database_oms_ef};UID={username};PWD={password}'
    )

    cursor_fdc = conn_fdc.cursor()
    cursor_oms_ef = conn_oms_ef.cursor()

    inventory_data = []

    for sku in skus:
        fdc_in_stock = 0
        ef_in_stock = 0
        fdc_order_qty = 0
        ef_order_qty = 0

        cursor_fdc.execute("SELECT in_stock, ORDER_QTY FROM dbo.inv_data WHERE PROD_CD = ?", sku)
        fdc_data = cursor_fdc.fetchone()
        if fdc_data:
            fdc_in_stock = fdc_data[0]
            fdc_order_qty = fdc_data[1]

        cursor_oms_ef.execute("SELECT in_stock, ORDER_QTY FROM dbo.inv_data WHERE PROD_CD = ?", sku)
        oms_ef_data = cursor_oms_ef.fetchone()
        if oms_ef_data:
            ef_in_stock = oms_ef_data[0]
            ef_order_qty = oms_ef_data[1]

        # If both fdc_data and oms_ef_data are None, SKU not found in both databases, skip
        if fdc_data is None and oms_ef_data is None:
            continue

        inventory_data.append((sku, fdc_in_stock, fdc_order_qty, ef_in_stock, ef_order_qty))

    conn_fdc.close()
    conn_oms_ef.close()

    print(inventory_data)
    return inventory_data

def update_google_sheet(sheet, inventory_data):
    batch_updates = []

    # Fetch all values in the sheet
    try:
        values = sheet.get_all_values()
    except gspread.exceptions.APIError as e:
        print("API Error:", e)
        return

    for data in inventory_data:
        sku, fdc_in_stock, fdc_order_qty, ef_in_stock, ef_order_qty = data

        combined_in_stock = float(fdc_in_stock) + float(ef_in_stock)
        combined_order_qty = float(fdc_order_qty) + float(ef_order_qty)
        available_now = float(combined_in_stock) - float(combined_order_qty)

        index = next((i + 1 for i, row in enumerate(values) if row and row[0] == sku), None)
        if index is None or index == 1:
            continue

        # Update values if they are zero
        combined_order_qty_val = '' if combined_order_qty <= 0 else combined_order_qty
        available_now_val = '' if available_now <= 0 else available_now

        values_to_update = [[combined_order_qty_val, available_now_val]]
        range_to_update = f'D{index}:E{index}'
        print("Range to update:", range_to_update)  # Print range to be updated
        batch_updates.append({
            'range': range_to_update,
            'values': values_to_update
        })

    if batch_updates:
        try:
            sheet.batch_update(batch_updates)
        except gspread.exceptions.APIError as e:
            print("API Error:", e)
            retries = 3
            for attempt in range(retries):
                delay = (2 ** attempt) + random.random()
                print(f"Retry attempt {attempt + 1} in {delay} seconds...")
                time.sleep(delay)
                try:
                    sheet.batch_update(batch_updates)
                    break
                except gspread.exceptions.APIError as e:
                    print("API Error:", e)
                    if attempt == retries - 1:
                        print("Maximum retries exceeded. Aborting.")

def main():
    # Update the spreadsheet details
    spreadsheet_url = 'https://docs.google.com/spreadsheets/d/1JvogexnVvPEPTxeuXhqt5zvYh7vMzOqhxCe6yxBWqaI/edit?pli=1#gid=138971742'
    sheet_name = 'ONLINE_CATALOG'

    # Open the spreadsheet by URL
    spreadsheet = client.open_by_url(spreadsheet_url)
    # Access the specified sheet by name
    sheet = spreadsheet.worksheet(sheet_name)

    # Read SKUs from the specified column
    sku_column = sheet.col_values(1)  # Assuming H column is the 8th column
    print(sku_column)
    skus = [sku for sku in sku_column if sku]  # Filter out empty cells
    print(skus)
    
    update_timestamp(sheet)
    # Fetch inventory data
    inventory_data = fetch_inventory(skus)
    log_inventory_data(inventory_data)
    # Update Google Sheet with inventory data
    update_google_sheet(sheet, inventory_data)

def log_inventory_data(inventory_data):
    # Log inventory data along with timestamp
    timestamp = datetime.datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
    logging.info(f"Inventory data at {timestamp}:")
    for data in inventory_data:
        logging.info(data)

def update_timestamp(sheet):
    # Get current timestamp
    timestamp = datetime.datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
    
    try:
        # Update cell L1 with the timestamp
        sheet.update_cell(1, 5, "updt_" + timestamp)  # Cell L1 (row=1, col=12)

        # Log success
        logging.info(f"Timestamp {timestamp} updated successfully.")
    except Exception as e:
        # Log error
        logging.error(f"Failed to update timestamp: {e}")


if __name__ == "__main__":
    main()