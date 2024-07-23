import json
import os
import hmac
import hashlib
import time
from urllib.parse import urlencode
import requests
import pandas as pd
import asyncio
import aiohttp
import async_timeout
from google.oauth2 import service_account
import gspread
import logging
import datetime

# Load credentials from TK_credential.json
with open('TK_credential.json') as f:
    credentials = json.load(f)

app_key = credentials['app_key']
app_secret = credentials['app_secret']
access_token = credentials['access_token']
refresh_token = credentials['refresh_token']
shop_cipher = credentials['shop_cipher']
shop_id = credentials['shop_id']

# Define Google Sheets credentials and scope
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=scope)
client = gspread.authorize(creds)

# Ensure the output directory exists
output_dir = 'output/tkpull'
os.makedirs(output_dir, exist_ok=True)

# Function to clear the output directory
def clear_output_directory():
    for file_name in os.listdir(output_dir):
        file_path = os.path.join(output_dir, file_name)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
        except Exception as e:
            print(f'Failed to delete {file_path}. Reason: {e}')

# Clear the output directory before starting
clear_output_directory()

# Logging configuration
logging.basicConfig(filename=os.path.join(output_dir, 'script.log'), level=logging.INFO)

def log_message(message):
    logging.info(message)

def generate_sha256_signature(secret, message):
    """Generate SHA256 HMAC signature."""
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def get_tiktok_api_signature(app_secret, url_path, query_params, body=''):
    """Generate the signature for TikTok API request."""
    query_params = {k: v for k, v in query_params.items() if k not in ['sign', 'access_token']}
    sorted_keys = sorted(query_params.keys())
    concatenated_params = ''.join(f'{key}{query_params[key]}' for key in sorted_keys)
    input_string = url_path + concatenated_params
    if body:
        input_string += body
    input_string = app_secret + input_string + app_secret
    return generate_sha256_signature(app_secret, input_string)

def load_ignore_list(filename='ignore.txt'):
    """Load the list of product IDs to ignore from a text file."""
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def make_tiktok_request(ignore_list):
    page_no = 1
    all_products_collected = False
    next_page_token = None
    processed_product_ids = set()

    while not all_products_collected:
        url_path = '/product/202312/products/search'
        query_params = {
            'app_key': app_key,
            'shop_cipher': shop_cipher,
            'shop_id': shop_id,
            'timestamp': str(int(time.time())),
            'version': '202312',
            'page_size': 50,
            'page_no': page_no  # Add page_no to query_params
        }
        
        print(f"Fetching page {page_no}")

        if next_page_token:
            query_params['page_token'] = next_page_token
        
        body = {}
        body_str = json.dumps(body)
        signature = get_tiktok_api_signature(app_secret, url_path, query_params, body_str)
        query_params['sign'] = signature

        base_url = 'https://open-api.tiktokglobalshop.com'
        full_url = f"{base_url}{url_path}?{urlencode(query_params)}"
        
        headers = {
            'Content-Type': 'application/json',
            'x-tts-access-token': access_token
        }

        response = requests.post(full_url, headers=headers, data=body_str)
        
        if response.status_code == 200:
            response_data = response.json()
            with open(os.path.join(output_dir, 'tiktok_response.txt'), 'a') as file:
                file.write(json.dumps(response_data, indent=4) + '\n')

            save_response_to_excel(response_data, processed_product_ids, ignore_list)
            page_no += 1  # Increment page number for next loop

            # Check if there's more data
            next_page_token = response_data.get('data', {}).get('next_page_token', None)
            if not next_page_token:
                all_products_collected = True
        else:
            response_data = {
                'status_code': response.status_code,
                'response_text': response.text
            }
            all_products_collected = True  # Exit loop on error

        with open(os.path.join(output_dir, 'tiktok_response.txt'), 'a') as file:
            file.write(json.dumps(response_data, indent=4) + '\n')

def save_response_to_excel(response_data, processed_product_ids, ignore_list):
    if response_data.get('code') == 0 and 'data' in response_data:
        products = response_data['data'].get('products', [])
        
        data = []
        for product in products:
            product_id = str(product.get('id', ''))
            if product_id in processed_product_ids or product_id in ignore_list:
                continue  # Skip duplicate or ignored product IDs
            processed_product_ids.add(product_id)
            
            for sku in product.get('skus', []):
                sales_attr = sku.get('sales_attributes', [])
                value_name = ', '.join([attr.get('value_name', '') for attr in sales_attr if 'value_name' in attr])
                value_id = ', '.join([attr.get('value_id', '') for attr in sales_attr if 'value_id' in attr])
                
                data.append({
                    'product_id': product_id,
                    'sku_id': str(sku.get('id', '')),
                    'inventory_quantity': sku['inventory'][0].get('quantity', ''),
                    'warehouse_id': sku['inventory'][0].get('warehouse_id', ''),
                    'tax_exclusive_price': sku.get('price', {}).get('tax_exclusive_price', ''),
                    'seller_sku': sku.get('seller_sku', ''),
                    'value_id': value_id,
                    'value_name': value_name
                })
        
        df = pd.DataFrame(data)
        # Check if file exists and append data
        file_path = os.path.join(output_dir, 'tiktok_products.xlsx')
        if os.path.exists(file_path):
            existing_df = pd.read_excel(file_path, dtype={'product_id': str, 'sku_id': str, 'warehouse_id': str})
            df = pd.concat([existing_df, df], ignore_index=True)
        
        df.to_excel(file_path, index=False)
        log_message("Response data saved to 'tiktok_products.xlsx'")
    else:
        log_message("No valid data to save.")

async def fetch_product_details(session, product_id, df):
    print("fetchingID:" + str(product_id))
    url_path = f'/product/202309/products/{product_id}'
    query_params = {
        'app_key': app_key,
        'shop_cipher': shop_cipher,
        'timestamp': str(int(time.time())),
        'version': '202309'
    }
    
    body = ''

    signature = get_tiktok_api_signature(app_secret, url_path, query_params, body)
    query_params['sign'] = signature

    base_url = 'https://open-api.tiktokglobalshop.com'
    full_url = f"{base_url}{url_path}?{urlencode(query_params)}"
    
    headers = {
        'Content-Type': 'application/json',
        'x-tts-access-token': access_token
    }

    delay = 1
    async with async_timeout.timeout(30):
        while True:
            async with session.get(full_url, headers=headers) as response:
                status = response.status
                response_data = await response.json()
                
                if status == 200 and response_data.get('code') == 0:
                    product_data = response_data.get('data', {})
                    
                    for sku in product_data.get('skus', []):
                        sku_id = sku.get('id', '')
                        sales_attributes = sku.get('sales_attributes', [])
                        value_names = [attr.get('value_name', '') for attr in sales_attributes if 'value_name' in attr]
                        value_name_str = ', '.join(value_names)
                        value_id = [attr.get('value_id', '') for attr in sales_attributes if 'value_id' in attr]
                        value_id_str = ', '.join(value_id)

                        main_images = product_data.get('main_images', [])
                        main_image_url = main_images[0].get('urls', [''])[0] if main_images else ''

                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'seller_sku'] = sku.get('seller_sku', '')
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'value_name'] = value_name_str
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'value_id'] = value_id_str
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'status'] = product_data.get('status', '')
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'title'] = product_data.get('title', '')
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'category'] = ', '.join([category.get('local_name', '') for category in product_data.get('category_chains', [])])
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'main_image'] = main_image_url
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'package_dimensions'] = f"{product_data.get('package_dimensions', {}).get('length', '')}x{product_data.get('package_dimensions', {}).get('width', '')}x{product_data.get('package_dimensions', {}).get('height', '')} {product_data.get('package_dimensions', {}).get('unit', '')}"
                        df.loc[(df['product_id'] == product_id) & (df['sku_id'] == sku_id), 'package_weight'] = f"{product_data.get('package_weight', {}).get('value', '')} {product_data.get('package_weight', {}).get('unit', '')}"
                    
                    log_message(f"Excel file updated with details for product ID {product_id}.")
                    break
                elif status == 429:
                    log_message("Rate limit exceeded. Retrying...")
                    await asyncio.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    df.loc[df['product_id'] == product_id, 'status'] = 'Failed'
                    log_message(f"Failed to fetch details for product ID {product_id}.")
                    break

async def update_excel_with_product_details():
    # Load the original and updated Excel files
    original_df = pd.read_excel(os.path.join(output_dir, 'tiktok_products.xlsx'), dtype={'product_id': str, 'sku_id': str, 'warehouse_id': str})
    updated_df = original_df.copy()
    
    # Add the new columns to the updated DataFrame
    for new_col in ['seller_sku', 'value_name', 'value_id', 'status', 'title', 'category', 'main_image', 'package_dimensions', 'package_weight']:
        updated_df[new_col] = ''

    # Get unique product IDs for API requests
    unique_product_ids = original_df['product_id'].unique()

    async with aiohttp.ClientSession() as session:
        tasks = []
        for product_id in unique_product_ids:
            task = asyncio.ensure_future(fetch_product_details(session, product_id, updated_df))
            tasks.append(task)
            await asyncio.sleep(0.1)  # Small delay between starting tasks to avoid rate limit
        await asyncio.gather(*tasks)

    # Function to clean value names
    def clean_value_name(value):
        value = value.lstrip("'")
        if value.isdigit():
            return int(value)
        return value

    # Filtering DataFrame
    filtered_df = updated_df[~updated_df['status'].isin(['DELETED', 'SELLER_DEACTIVATED', 'DRAFT'])].copy()

    # Remove leading apostrophes and ensure numeric values in the 'value_name' column
    filtered_df['value_name'] = filtered_df['value_name'].apply(clean_value_name)

    # Sort the DataFrame by 'value_name' column
    def sort_key(value):
        if isinstance(value, int):
            return (0, value)  # Numbers come first
        return (1, value)  # Strings come after numbers

    filtered_df = filtered_df.sort_values(by='value_name', key=lambda col: col.map(sort_key))

    # Save the updated DataFrame to a new sheet named 'updated'
    file_path = os.path.join(output_dir, 'tiktok_products.xlsx')
    with pd.ExcelWriter(file_path, mode='a', engine='openpyxl') as writer:
        filtered_df.to_excel(writer, sheet_name='updated', index=False)

    # Update the Google Sheet with the new data
    spreadsheet_url = 'https://docs.google.com/spreadsheets/d/1JvogexnVvPEPTxeuXhqt5zvYh7vMzOqhxCe6yxBWqaI/edit?pli=1#gid=138971742'
    sheet_name = 'tk_import'
    spreadsheet = client.open_by_url(spreadsheet_url)
    sheet = spreadsheet.worksheet(sheet_name)
    
    # Clear the existing sheet
    sheet.clear()
    # Update the sheet with new data
    sheet.update([filtered_df.columns.values.tolist()] + filtered_df.values.tolist())
    update_timestamp(sheet)

def update_timestamp(sheet):
    # Get current timestamp
    timestamp = datetime.datetime.now().strftime("%Y/%m/%d - %H:%M:%S")
    
    try:
        # Update cell L1 with the timestamp
        sheet.update_cell(1, 17, "updt_" + timestamp)  # Cell L1 (row=1, col=12)

        # Log success
        logging.info(f"Timestamp {timestamp} updated successfully.")
    except Exception as e:
        # Log error
        logging.error(f"Failed to update timestamp: {e}")

if __name__ == "__main__":
    # Load ignore list
    ignore_list = load_ignore_list()

    # Make the initial request to get product IDs
    make_tiktok_request(ignore_list)

    # Update the Excel file with detailed product information
    loop = asyncio.get_event_loop()
    loop.run_until_complete(update_excel_with_product_details())

    log_message("All product details have been updated and saved to the Excel file.")
