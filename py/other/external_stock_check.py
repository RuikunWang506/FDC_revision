from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash, current_app
from py.utilities.setting import login_required
from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from datetime import datetime, timedelta
import json
import requests
import hashlib
import hmac
import time

external_stock_check_bp = Blueprint('external_stock_check', __name__)

# External user password

sql_server_config = Config.SQL_SERVER_CONFIG


class TikTokAuthError(Exception):
    pass


class TikTokInventoryFetchError(Exception):
    pass


def has_external_stock_access():
    return bool(session.get('username'))


def get_db_connections():
    """Establishes connections to both FDC and E_FAS databases."""
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        conn_oms_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        return conn_fdc, conn_oms_ef
    except Exception as e:
        current_app.logger.error(f"Database connection error: {e}")
        return None, None


def init_external_check_database():
    """Initialize the externalcheck database if it doesn't exist."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return False
        
        cursor = connection.cursor()
        
        # Create database if it doesn't exist
        cursor.execute("CREATE DATABASE IF NOT EXISTS externalcheck")
        connection.database = 'externalcheck'
        
        # Create table if it doesn't exist
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS tiktok_inventory (
            id INT AUTO_INCREMENT PRIMARY KEY,
            date DATETIME DEFAULT CURRENT_TIMESTAMP,
            sku VARCHAR(100) NOT NULL,
            product_name VARCHAR(255),
            quantity INT DEFAULT 0,
            last_updated DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY unique_sku (sku),
            INDEX idx_sku (sku),
            INDEX idx_date (date)
        )
        """
        cursor.execute(create_table_sql)
        connection.commit()
        cursor.close()
        connection.close()
        return True
    except Exception as e:
        current_app.logger.error(f"Database initialization error: {e}")
        return False


def get_tiktok_access_token():
    """Get or refresh TikTok Shop API access token."""
    try:
        api_key = Config.TIKTOK_SHOP_API_KEY
        if not api_key or api_key == 'YOUR_API_KEY_HERE':
            current_app.logger.warning("TikTok Shop API key not configured")
            return None
        return api_key
    except Exception as e:
        current_app.logger.error(f"TikTok token error: {e}")
        return None


def generate_tiktok_signature(path, query_params, body_str, api_secret):
    """Generate HMAC SHA256 signature for TikTok Shop API v202309.
    
    Algorithm:
    1. Extract and sort query params (excluding 'sign' and 'access_token')
    2. Concatenate as {key}{value}
    3. Append to path
    4. Append body (if not multipart/form-data)
    5. Wrap with api_secret: secret + string + secret
    6. HMAC-SHA256 encode
    """
    try:
        # Step 1-2: Extract, sort, and concatenate query parameters
        exclude_keys = {'sign', 'access_token'}
        sorted_params = sorted(
            [(k, v) for k, v in query_params.items() if k not in exclude_keys]
        )
        param_string = ''.join([f"{k}{v}" for k, v in sorted_params])
        
        # Step 3: Append path
        sign_string = f"{path}{param_string}"
        
        # Step 4: Append body (if not multipart/form-data)
        if body_str:
            sign_string += body_str
        
        # Step 5: Wrap with app_secret
        wrapped_string = f"{api_secret}{sign_string}{api_secret}"
        
        # Step 6: Generate HMAC-SHA256 signature
        signature = hmac.new(
            api_secret.encode('utf-8'),
            wrapped_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    except Exception as e:
        current_app.logger.error(f"Signature generation error: {e}")
        return None


def get_inventory_from_tiktok():
    """Fetch inventory data from TikTok Shop API v202309."""
    try:
        api_key = get_tiktok_access_token()
        api_secret = Config.TIKTOK_SHOP_SECRET
        shop_cipher = Config.TIKTOK_SHOP_SHOP_ID
        app_key = Config.TIKTOK_SHOP_APP_KEY  # App key (for signature)
        
        if not api_key or api_key == 'YOUR_API_KEY_HERE':
            current_app.logger.warning("TikTok Shop API not configured. Using sample data.")
            return []
        
        # Build request - V2 API format for PRODUCTS (not orders)
        path = '/product/202309/products/search'
        
        # Query parameters (page_size goes in query params for V2)
        query_params = {
            'app_key': app_key,
            'timestamp': str(int(time.time())),
            'shop_cipher': shop_cipher,
            'page_size': '100'  # V2 requires page_size in query params
        }
        
        # Request body for POST - filter for LIVE products only
        body = {
            'filter': {
                'status': 'LIVE'  # Only fetch live products to reduce API calls
            }
        }
        body_str = json.dumps(body, separators=(',', ':'))
        
        # Generate signature
        signature = generate_tiktok_signature(path, query_params, body_str, api_secret)
        if not signature:
            return []
        
        # Add signature to query params
        query_params['sign'] = signature
        
        # Build full URL
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(query_params.items())])
        products_url = f"{Config.TIKTOK_SHOP_API_URL}{path}?{query_string}"
        
        # Build headers
        headers = {
            'Content-Type': 'application/json',
            'x-tts-access-token': api_key  # V2 uses x-tts-access-token header
        }
        
        current_app.logger.info(f"Fetching TikTok products from {path} (LIVE status filter)")
        current_app.logger.debug(f"Request body: {body_str}")
        
        # Build headers
        headers = {
            'Content-Type': 'application/json',
            'x-tts-access-token': api_key  # V2 uses x-tts-access-token header
        }
        
        inventory_items = []
        next_page_token = None
        page_count = 0
        
        # Pagination with next_page_token
        while True:
            page_count += 1
            current_app.logger.info(f"Fetching page {page_count}...")
            
            # Update query params for this request
            query_params = {
                'app_key': app_key,
                'timestamp': str(int(time.time())),
                'shop_cipher': shop_cipher,
                'page_size': '100'
            }
            
            # Add next_page_token if we have one
            if next_page_token:
                query_params['page_token'] = next_page_token
            
            # Generate signature with current body
            signature = generate_tiktok_signature(path, query_params, body_str, api_secret)
            if not signature:
                return []
            
            query_params['sign'] = signature
            
            # Build full URL
            query_string = '&'.join([f"{k}={v}" for k, v in sorted(query_params.items())])
            products_url = f"{Config.TIKTOK_SHOP_API_URL}{path}?{query_string}"
            
            response = requests.post(
                products_url,
                headers=headers,
                data=body_str,
                timeout=30
            )

            if response.status_code == 401:
                error_payload = {}
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}

                error_code = error_payload.get('code')
                error_message = error_payload.get('message', response.text)
                request_id = error_payload.get('request_id')

                current_app.logger.error(
                    f"TikTok API auth error: {error_message}"
                    + (f" (request_id={request_id})" if request_id else "")
                )

                if error_code == 105002:
                    raise TikTokAuthError(
                        "TikTok access token expired. Update TIKTOK_SHOP_API_KEY with a fresh token."
                    )

                raise TikTokAuthError("TikTok authentication failed.")
            
            if response.status_code != 200:
                current_app.logger.error(f"TikTok API error: {response.status_code} - {response.text}")
                raise TikTokInventoryFetchError(
                    f"TikTok inventory request failed with status {response.status_code}."
                )
            
            data = response.json()
            
            # V2 API response format
            if data.get('code') != 0:
                if data.get('code') == 105002:
                    raise TikTokAuthError(
                        "TikTok access token expired. Update TIKTOK_SHOP_API_KEY with a fresh token."
                    )
                current_app.logger.error(f"TikTok API response error: {data.get('message', 'Unknown error')}")
                raise TikTokInventoryFetchError(
                    data.get('message', 'TikTok API returned an error while fetching inventory.')
                )
            
            product_data = data.get('data', {})
            products = product_data.get('products', [])
            
            if not products:
                current_app.logger.info(f"No more products (empty response on page {page_count})")
                break
            
            current_app.logger.info(f"Page {page_count}: Processing {len(products)} products")
            
            # Parse products and extract SKU, name, inventory info
            for product in products:
                # Filter: only process products with ACTIVATE status
                if product.get('status') != 'ACTIVATE':
                    current_app.logger.debug(f"Skipping product (status not ACTIVATE): {product.get('status')}")
                    continue
                
                product_id = product.get('product_id', '')
                
                # Get SKU from product or product variants
                skus_to_process = []
                
                # Check for SKU directly on product
                if product.get('sku'):
                    skus_to_process.append({
                        'sku': product.get('sku'),
                        'quantity': product.get('quantity', 0)
                    })
                
                # Check for variants/skus
                variants = product.get('variants', []) or product.get('skus', [])
                for variant in variants:
                    sku = variant.get('sku') or variant.get('seller_sku', '')
                    if sku:
                        # Get quantity from inventory array
                        inventory_array = variant.get('inventory', [])
                        quantity = 0
                        if inventory_array:
                            quantity = inventory_array[0].get('quantity', 0)
                        
                        # Get price from variant
                        price = None
                        currency = None
                        price_info = variant.get('price', {})
                        if isinstance(price_info, dict):
                            price = float(price_info.get('tax_exclusive_price', 0) or 0)
                            currency = price_info.get('currency', 'USD')
                        
                        skus_to_process.append({
                            'sku': sku,
                            'quantity': quantity,
                            'price': price,
                            'currency': currency
                        })
                
                # If no SKUs found, use product_id as fallback
                if not skus_to_process:
                    skus_to_process.append({
                        'sku': product_id,
                        'quantity': product.get('quantity', 0)
                    })
                
                # Add each SKU as inventory item
                for sku_info in skus_to_process:
                    sku = sku_info['sku']
                    if sku:
                        sku_str = str(sku).strip()
                        
                        # Filter: only save SKUs that start with a letter
                        if not sku_str or not sku_str[0].isalpha():
                            current_app.logger.debug(f"Skipping SKU (doesn't start with letter): {sku_str}")
                            continue
                        
                        # Filter: exclude SKUs starting with DS-, ava, S#-, s#- patterns
                        sku_upper = sku_str.upper()
                        if sku_upper.startswith('DS-'):
                            current_app.logger.debug(f"Skipping SKU (starts with DS-): {sku_str}")
                            continue
                        if sku_upper.startswith('AVA'):
                            current_app.logger.debug(f"Skipping SKU (starts with AVA): {sku_str}")
                            continue
                        # Check for S#- pattern (S followed by digit, then dash)
                        if len(sku_str) >= 3 and sku_upper[0] == 'S' and sku_upper[1].isdigit() and sku_upper[2] == '-':
                            current_app.logger.debug(f"Skipping SKU (matches S#- pattern): {sku_str}")
                            continue
                        
                        # Extract product name from SKU (everything before first space)
                        sku_name = sku_str.split()[0] if ' ' in sku_str else sku_str
                        
                        inventory_items.append({
                            'sku': sku_str.upper(),
                            'style': sku_name.upper(),  # Product name = SKU before space
                            'buyer': 'TikTok Shop',
                            'quantity': sku_info.get('quantity', 0),
                            'price': sku_info.get('price'),
                            'currency': sku_info.get('currency', 'USD')
                        })
            
            # Get next page token
            next_page_token = product_data.get('next_page_token')
            if not next_page_token:
                current_app.logger.info(f"Pagination complete. Fetched {len(inventory_items)} total items across {page_count} pages")
                break
        
        current_app.logger.info(f"Fetched {len(inventory_items)} items from TikTok Shop (LIVE status)")
        
        # Special handling: Also fetch T3305 by product ID if not already in results
        t3305_found = any(item['sku'].upper().startswith('T3305') for item in inventory_items)
        if not t3305_found:
            current_app.logger.info("T3305 not found in search results, fetching by product ID...")
            try:
                product_id = '1729404716648272513'
                path = f'/product/202309/products/{product_id}'
                
                query_params = {
                    'app_key': app_key,
                    'timestamp': str(int(time.time())),
                    'shop_cipher': shop_cipher
                }
                
                body = {}
                body_str = ''
                
                signature = generate_tiktok_signature(path, query_params, body_str, api_secret)
                if signature:
                    query_params['sign'] = signature
                    query_string = '&'.join([f"{k}={v}" for k, v in sorted(query_params.items())])
                    product_url = f"{Config.TIKTOK_SHOP_API_URL}{path}?{query_string}"
                    
                    headers = {
                        'Content-Type': 'application/json',
                        'x-tts-access-token': api_key
                    }
                    
                    response = requests.get(product_url, headers=headers, timeout=10)
                    data = response.json()
                    
                    if data.get('code') == 0:
                        product = data.get('data', {})
                        if product and product.get('status') == 'ACTIVATE':
                            current_app.logger.info(f"Successfully fetched T3305 by product ID")
                            
                            # Process SKUs from this product
                            for sku_variant in product.get('skus', []):
                                sku = sku_variant.get('seller_sku', '')
                                if sku:
                                    sku_str = str(sku).strip().upper()
                                    inventory_array = sku_variant.get('inventory', [])
                                    quantity = 0
                                    if inventory_array:
                                        quantity = inventory_array[0].get('quantity', 0)
                                    
                                    sku_name = sku_str.split()[0] if ' ' in sku_str else sku_str
                                    
                                    inventory_items.append({
                                        'sku': sku_str,
                                        'style': sku_name,
                                        'buyer': 'TikTok Shop',
                                        'quantity': quantity,
                                        'price': None,
                                        'currency': 'USD'
                                    })
                            
                            current_app.logger.info(f"Added {len(product.get('skus', []))} T3305 variants")
            except Exception as e:
                current_app.logger.warning(f"Failed to fetch T3305 by product ID: {e}")
        
        return inventory_items
        
    except TikTokAuthError:
        raise
    except requests.exceptions.Timeout:
        current_app.logger.error("TikTok API request timeout")
        raise TikTokInventoryFetchError("TikTok API request timed out.")
    except requests.exceptions.ConnectionError as e:
        error_msg = str(e)
        if 'getaddrinfo failed' in error_msg or 'Failed to resolve' in error_msg:
            current_app.logger.error(f"TikTok API DNS resolution failed. Check API endpoint URL and network connectivity.")
            current_app.logger.error(f"Configured endpoint: {Config.TIKTOK_SHOP_API_URL}")
            current_app.logger.error(f"Set TIKTOK_SHOP_API_URL environment variable to correct regional endpoint.")
        else:
            current_app.logger.error(f"TikTok API connection error: {error_msg}")
        raise TikTokInventoryFetchError("Unable to connect to the TikTok API.")
    except Exception as e:
        current_app.logger.error(f"TikTok fetch error: {e}")
        import traceback
        traceback.print_exc()
        if isinstance(e, TikTokInventoryFetchError):
            raise
        raise TikTokInventoryFetchError(str(e))


def fetch_available_quantity(sku, is_manual=False):
    """Fetch available quantity from both FDC and E-Fashion databases.
    Uses the inv and inv_data tables like product_detail.py does.
    """
    conn_fdc, conn_oms_ef = get_db_connections()
    
    if conn_fdc is None or conn_oms_ef is None:
        return {'fdc': None, 'efashion': None, 'total': None}
    
    try:
        # Fetch from FDC database using inv and inv_data tables
        fdc_qty = None
        try:
            cursor_fdc = conn_fdc.cursor()
            cursor_fdc.execute("""
                SELECT CAST(ISNULL(id.in_stock, 0) AS INT) as available
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                WHERE i.PROD_CD = ? OR i.ALT_CD = ?
            """, (sku, sku))
            row = cursor_fdc.fetchone()
            fdc_qty = row[0] if row else 0
            cursor_fdc.close()
        except Exception as e:
            current_app.logger.debug(f"FDC query for SKU {sku}: {e}")
            fdc_qty = 0
        
        # Fetch from E-Fashion database using inv and inv_data tables
        efashion_qty = None
        try:
            cursor_oms_ef = conn_oms_ef.cursor()
            cursor_oms_ef.execute("""
                SELECT CAST(ISNULL(id.in_stock, 0) AS INT) as available
                FROM dbo.inv AS i
                LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                WHERE i.PROD_CD = ? OR i.ALT_CD = ?
            """, (sku, sku))
            row = cursor_oms_ef.fetchone()
            efashion_qty = row[0] if row else 0
            cursor_oms_ef.close()
        except Exception as e:
            current_app.logger.debug(f"E-Fashion query for SKU {sku}: {e}")
            efashion_qty = 0
        
        # Calculate total
        total = fdc_qty + efashion_qty if (fdc_qty is not None and efashion_qty is not None) else None
        
        return {
            'fdc': fdc_qty,
            'efashion': efashion_qty,
            'total': total
        }
    finally:
        if conn_fdc:
            conn_fdc.close()
        if conn_oms_ef:
            conn_oms_ef.close()


def fetch_available_quantities(skus, manual_skus=None):
    """Fetch available quantities for multiple SKUs.
    Uses individual queries per SKU for reliability.
    Returns dict with SKU as key and availability data as value.
    """
    if manual_skus is None:
        manual_skus = set()
    
    conn_fdc, conn_oms_ef = get_db_connections()
    
    if conn_fdc is None or conn_oms_ef is None or not skus:
        return {sku: {'fdc': 0, 'efashion': 0, 'total': 0} for sku in skus}
    
    result = {}
    
    # Initialize all SKUs with 0
    for sku in skus:
        result[sku] = {'fdc': 0, 'efashion': 0, 'total': 0}
    
    try:
        cursor_fdc = conn_fdc.cursor()
        
        # Query all SKUs (exact match)
        if skus:
            union_query_parts = []
            for sku in skus:
                union_query_parts.append(f"""
                    SELECT '{sku}' as sku_lookup, CAST(ISNULL(SUM(id.in_stock), 0) AS INT) as total_stock
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE RTRIM(i.PROD_CD) = '{sku}' OR RTRIM(i.ALT_CD) = '{sku}'
                """)
            
            union_query = " UNION ALL ".join(union_query_parts)
            cursor_fdc.execute(union_query)
            rows = cursor_fdc.fetchall()
            
            for row in rows:
                sku = row[0]
                total_stock = row[1]
                if sku in result:
                    result[sku]['fdc'] = total_stock
        
        cursor_fdc.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
    
    try:
        cursor_oms_ef = conn_oms_ef.cursor()
        
        # Query all SKUs (exact match)
        if skus:
            union_query_parts = []
            for sku in skus:
                union_query_parts.append(f"""
                    SELECT '{sku}' as sku_lookup, CAST(ISNULL(SUM(id.in_stock), 0) AS INT) as total_stock
                    FROM dbo.inv AS i
                    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
                    WHERE RTRIM(i.PROD_CD) = '{sku}' OR RTRIM(i.ALT_CD) = '{sku}'
                """)
            
            union_query = " UNION ALL ".join(union_query_parts)
            cursor_oms_ef.execute(union_query)
            rows = cursor_oms_ef.fetchall()
            
            for row in rows:
                sku = row[0]
                total_stock = row[1]
                if sku in result:
                    result[sku]['efashion'] = total_stock
        
        cursor_oms_ef.close()
    except Exception as e:
        import traceback
        traceback.print_exc()
    
    # Calculate totals
    for sku in result:
        result[sku]['total'] = result[sku]['fdc'] + result[sku]['efashion']
    
    return result


@external_stock_check_bp.route('/external_stock_check/password', methods=['GET', 'POST'])
def external_password():
    session["next"] = url_for("external_stock_check.external_stock_check_page")
    return redirect(url_for("auth.login"))


# Main page
@external_stock_check_bp.route('/external_stock_check', methods=['GET'])
def external_stock_check_page():
    if not has_external_stock_access():
        return render_template('external_stock_check.html', show_password=True)
    
    # Initialize database
    init_external_check_database()
    
    return render_template('external_stock_check.html', show_password=False)


# API: Fetch and save inventory from TikTok
@external_stock_check_bp.route('/api/external_stock_check/fetch_tiktok', methods=['POST'])
def fetch_tiktok_inventory():
    """Fetch inventory from TikTok and save to database."""
    try:
        if not has_external_stock_access():
            return jsonify({'error': 'Unauthorized'}), 401
        
        inventory_data = get_inventory_from_tiktok()

        # Save to MySQL
        connection = get_mysql_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        connection.database = 'externalcheck'
        cursor = connection.cursor()
        
        # Clear old data
        cursor.execute("TRUNCATE TABLE tiktok_inventory")
        
        # Insert new data
        insert_count = 0
        for item in inventory_data:
            try:
                cursor.execute("""
                    INSERT INTO tiktok_inventory (sku, product_name, quantity, price, currency, date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE product_name=VALUES(product_name), quantity=VALUES(quantity), price=VALUES(price), currency=VALUES(currency), last_updated=NOW()
                """, (
                    item.get('sku'),
                    item.get('style'),  # This is the product name (SKU before space)
                    item.get('quantity', 0),
                    item.get('price'),
                    item.get('currency', 'USD'),
                    datetime.now()
                ))
                insert_count += 1
            except Exception as e:
                current_app.logger.warning(f"Failed to insert item {item}: {e}")
                continue
        
        connection.commit()
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': f'Fetched and saved {insert_count} items from TikTok'
        })
    except TikTokAuthError as e:
        return jsonify({
            'error': str(e),
            'needs_reauth': True
        }), 401
    except TikTokInventoryFetchError as e:
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        current_app.logger.error(f"Error fetching TikTok inventory: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get current inventory data
@external_stock_check_bp.route('/api/external_stock_check/get_inventory', methods=['GET'])
def get_inventory():
    """Get current inventory from database with stock levels."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        connection.database = 'externalcheck'
        cursor = connection.cursor(dictionary=True)
        
        # Get all inventory items
        cursor.execute("""
            SELECT sku, product_name, quantity, date, price
            FROM tiktok_inventory
            ORDER BY date DESC, sku
        """)
        
        items = cursor.fetchall()
        
        # Extract all SKUs and fetch availability in bulk
        skus = [item['sku'] for item in items]
        availability_map = fetch_available_quantities(skus) if skus else {}
        
        # Build results with availability data
        results = []
        for item in items:
            avail_data = availability_map.get(item['sku'], {'fdc': 0, 'efashion': 0, 'total': 0})
            results.append({
                'sku': item['sku'],
                'product_name': item['product_name'],
                'tiktok_quantity': item['quantity'],
                'date': item['date'].isoformat() if item['date'] else None,
                'price': item['price'],
                'fdc_available': avail_data['fdc'],
                'efashion_available': avail_data['efashion'],
                'total_available': avail_data['total']
            })
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'data': results
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching inventory: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get basic inventory data (fast - MySQL only, no availability lookup)
@external_stock_check_bp.route('/api/external_stock_check/get_inventory_basic', methods=['GET'])
def get_inventory_basic():
    """Get basic inventory data from MySQL plus manual styles (fast load, no availability lookup)."""
    try:
        connection = get_mysql_connection()
        if connection is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        connection.database = 'externalcheck'
        cursor = connection.cursor(dictionary=True)
        
        # Get all inventory items from TikTok
        cursor.execute("""
            SELECT sku, product_name, quantity, date, price
            FROM tiktok_inventory
            ORDER BY date DESC, sku
        """)
        
        items = cursor.fetchall()
        
        # Build results with basic data only
        results = []
        tk_skus = set()
        tk_base_styles = set()  # Track base styles (style without color) from TikTok
        
        for item in items:
            tk_skus.add(item['sku'])
            # Extract base style (first word, before space) for duplicate detection
            base_style = item['product_name'].split()[0] if ' ' in item['product_name'] else item['product_name']
            tk_base_styles.add(base_style)
            results.append({
                'sku': item['sku'],
                'product_name': item['product_name'],
                'tiktok_quantity': item['quantity'],
                'date': item['date'].isoformat() if item['date'] else None,
                'price': item['price'],
                'is_manual': False
            })
        
        # Load manual styles from JSON
        import os
        manual_styles_path = os.path.join(current_app.root_path, 'data', 'manual_styles.json')
        
        if os.path.exists(manual_styles_path):
            with open(manual_styles_path, 'r', encoding='utf-8') as f:
                manual_data = json.load(f)
            
            # Add manual styles that are NOT in TikTok inventory
            manual_styles = manual_data.get('styles', [])
            for base_sku in manual_styles:
                # Skip if this base style already exists in TikTok
                if base_sku not in tk_base_styles:
                    # Query database for all SKUs matching this pattern
                    conn_fdc, conn_oms_ef = get_db_connections()
                    if conn_fdc and conn_oms_ef:
                        try:
                            cursor_fdc = conn_fdc.cursor()
                            like_sku = f"{base_sku}%"
                            cursor_fdc.execute(
                                """
                                SELECT DISTINCT RTRIM(i.PROD_CD) as prod_cd
                                FROM dbo.inv AS i
                                WHERE i.PROD_CD LIKE ? OR i.ALT_CD LIKE ?
                                ORDER BY RTRIM(i.PROD_CD)
                                """,
                                (like_sku, like_sku),
                            )
                            matched_skus = {row[0] for row in cursor_fdc.fetchall()}
                            cursor_fdc.close()
                            
                            if conn_fdc:
                                conn_fdc.close()
                            if conn_oms_ef:
                                conn_oms_ef.close()
                        except Exception as e:
                            current_app.logger.debug(f"Error fetching matched SKUs for {base_sku}: {e}")
                            matched_skus = set()
                    else:
                        matched_skus = set()
                    
                    # If we found matched SKUs, add them; otherwise add the base style
                    if matched_skus:
                        for sku in sorted(matched_skus):
                            # Extract base style (everything before first space)
                            base_style = sku.split()[0] if ' ' in sku else sku
                            results.append({
                                'sku': sku,
                                'product_name': base_style,  # Style column shows base style only
                                'tiktok_quantity': None,
                                'date': datetime.now().isoformat(),
                                'price': None,
                                'is_manual': True
                            })
                    else:
                        # Fallback: add base style if no matches found
                        results.append({
                            'sku': base_sku,
                            'product_name': base_sku,
                            'tiktok_quantity': None,
                            'date': datetime.now().isoformat(),
                            'price': None,
                            'is_manual': True
                        })
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'data': results
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching basic inventory: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get availability for multiple SKUs
@external_stock_check_bp.route('/api/external_stock_check/get_availability', methods=['POST'])
def get_availability():
    """Get availability data for multiple SKUs from SQL Server databases."""
    try:
        data = request.get_json()
        skus = data.get('skus', [])
        
        if not skus:
            return jsonify({'success': True, 'data': {}})
        
        availability_map = fetch_available_quantities(skus)
        
        return jsonify({
            'success': True,
            'data': availability_map
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching availability: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get single SKU stock levels
@external_stock_check_bp.route('/api/external_stock_check/stock/<sku>', methods=['GET'])
def get_sku_stock(sku):
    """Get stock levels for a specific SKU."""
    try:
        if not has_external_stock_access():
            return jsonify({'error': 'Unauthorized'}), 401
        
        stock_data = fetch_available_quantity(sku)
        return jsonify({
            'success': True,
            'sku': sku,
            'stock': stock_data
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching stock for SKU {sku}: {e}")
        return jsonify({'error': str(e)}), 500


# API: Get SKU availability from SQL Server
@external_stock_check_bp.route('/api/external_stock_check/sku_availability/<sku>', methods=['GET'])
def get_sku_availability(sku):
    """Get availability for a specific SKU from SQL Server databases."""
    try:
        stock_data = fetch_available_quantity(sku)
        
        return jsonify({
            'sku': sku,
            'fdc': stock_data['fdc'],
            'efashion': stock_data['efashion'],
            'total': stock_data['total']
        })
    except Exception as e:
        current_app.logger.error(f"Error getting availability for {sku}: {e}")
        return jsonify({'error': str(e)}), 500


# API: Export complete CSV with all inventory and availability data
@external_stock_check_bp.route('/api/external_stock_check/export_csv', methods=['POST'])
def export_csv_complete():
    """
    Export all inventory data (TikTok + manual) with availability as CSV.
    Fetches fresh data from both MySQL and SQL Server databases.
    """
    from flask import send_file
    import io
    import os
    
    try:
        # Get all inventory from MySQL
        mysql_conn = get_mysql_connection()
        if mysql_conn is None:
            return jsonify({'error': 'Database connection failed'}), 500
        
        mysql_conn.database = 'externalcheck'
        cursor = mysql_conn.cursor(dictionary=True)
        cursor.execute("SELECT sku, product_name, quantity as tiktok_qty, price, last_updated FROM tiktok_inventory ORDER BY sku")
        inventory_data = cursor.fetchall()
        cursor.close()
        
        # Load manual styles from JSON
        manual_styles_data = []
        manual_styles_path = os.path.join(current_app.root_path, 'data', 'manual_styles.json')
        
        if os.path.exists(manual_styles_path):
            with open(manual_styles_path, 'r', encoding='utf-8') as f:
                manual_data = json.load(f)
            
            manual_styles = manual_data.get('styles', [])
            for base_sku in manual_styles:
                # Query database for all SKUs matching this pattern
                conn_fdc, conn_oms_ef = get_db_connections()
                if conn_fdc and conn_oms_ef:
                    try:
                        cursor_fdc = conn_fdc.cursor()
                        like_sku = f"{base_sku}%"
                        cursor_fdc.execute(
                            """
                            SELECT DISTINCT RTRIM(i.PROD_CD) as prod_cd
                            FROM dbo.inv AS i
                            WHERE i.PROD_CD LIKE ? OR i.ALT_CD LIKE ?
                            ORDER BY RTRIM(i.PROD_CD)
                            """,
                            (like_sku, like_sku),
                        )
                        matched_skus = {row[0] for row in cursor_fdc.fetchall()}
                        cursor_fdc.close()
                        
                        if conn_fdc:
                            conn_fdc.close()
                        if conn_oms_ef:
                            conn_oms_ef.close()
                    except Exception as e:
                        current_app.logger.debug(f"Error fetching matched SKUs for {base_sku}: {e}")
                        matched_skus = set()
                else:
                    matched_skus = set()
                
                # Add matched SKUs to manual styles data
                if matched_skus:
                    for sku in sorted(matched_skus):
                        base_style = sku.split()[0] if ' ' in sku else sku
                        manual_styles_data.append({
                            'sku': sku,
                            'product_name': base_style,
                            'tiktok_qty': None,
                            'price': None
                        })
        
        mysql_conn.close()
        
        if not inventory_data and not manual_styles_data:
            return jsonify({'error': 'No inventory data found'}), 404
        
        # Extract all SKUs (both TikTok and manual)
        all_skus = [item['sku'] for item in inventory_data] + [item['sku'] for item in manual_styles_data]
        
        # Fetch availability for all SKUs
        availability_data = fetch_available_quantities(all_skus)
        
        # Build CSV data
        csv_rows = [['Style', 'SKU', 'Source', 'TikTok Qty', 'Available', 'Price']]
        
        # Add TikTok inventory rows
        for item in inventory_data:
            sku = item['sku']
            style = item['product_name'].split()[0] if item['product_name'] else ''
            tiktok_qty = item['tiktok_qty'] or 0
            available = availability_data.get(sku, {}).get('total', 0)
            price = f"${item['price']:.2f}" if item['price'] else 'N/A'
            source = 'TikTok'
            
            csv_rows.append([style, sku, source, tiktok_qty, available, price])
        
        # Add manual styles rows
        for item in manual_styles_data:
            sku = item['sku']
            style = item['product_name']
            available = availability_data.get(sku, {}).get('total', 0)
            source = 'Manual'
            
            csv_rows.append([style, sku, source, 'N/A', available, 'N/A'])
        
        # Write to StringIO
        output = io.StringIO()
        for row in csv_rows:
            output.write(','.join(str(cell) for cell in row) + '\n')
        
        # Create BytesIO for download
        output.seek(0)
        mem = io.BytesIO(output.getvalue().encode('utf-8'))
        mem.seek(0)
        
        return send_file(
            mem,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'inventory_complete_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        )
    
    except Exception as e:
        current_app.logger.error(f"CSV export error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# API: Get manual styles from JSON file
@external_stock_check_bp.route('/api/external_stock_check/get_manual_styles', methods=['GET'])
def get_manual_styles():
    """Get manual styles from JSON file."""
    try:
        if not has_external_stock_access():
            return jsonify({'error': 'Unauthorized'}), 401
        
        # Load manual styles from JSON
        import os
        manual_styles_path = os.path.join(current_app.root_path, 'data', 'manual_styles.json')
        
        if not os.path.exists(manual_styles_path):
            return jsonify({'success': True, 'styles': []})
        
        with open(manual_styles_path, 'r', encoding='utf-8') as f:
            manual_data = json.load(f)
        
        styles_list = manual_data.get('styles', [])
        
        return jsonify({
            'success': True,
            'styles': styles_list
        })
    except Exception as e:
        current_app.logger.error(f"Error fetching manual styles: {e}")
        return jsonify({'error': str(e)}), 500


# API: Save manual styles to JSON file
@external_stock_check_bp.route('/api/external_stock_check/save_manual_styles', methods=['POST'])
def save_manual_styles():
    """Save edited manual styles to JSON file."""
    try:
        if not has_external_stock_access():
            return jsonify({'error': 'Unauthorized'}), 401
        
        data = request.json
        styles = data.get('styles', [])
        
        # Load current data to preserve any other fields
        import os
        manual_styles_path = os.path.join(current_app.root_path, 'data', 'manual_styles.json')
        
        # Get all SKUs from TikTok inventory to filter
        mysql_conn = get_mysql_connection()
        if mysql_conn:
            mysql_conn.database = 'externalcheck'
            cursor = mysql_conn.cursor()
            cursor.execute("SELECT sku FROM tiktok_inventory")
            tk_skus = {row[0] for row in cursor.fetchall()}
            cursor.close()
            mysql_conn.close()
        else:
            tk_skus = set()
        
        # Filter out styles that are in TikTok inventory
        filtered_styles = [sku for sku in styles if sku not in tk_skus]
        
        # Save to file
        output_data = {
            'styles': filtered_styles
        }
        
        # Ensure directory exists
        os.makedirs(os.path.dirname(manual_styles_path), exist_ok=True)
        
        with open(manual_styles_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2, ensure_ascii=False)
        
        return jsonify({
            'success': True,
            'message': f'Saved {len(filtered_styles)} styles (excluded {len(styles) - len(filtered_styles)} that are in TikTok inventory)'
        })
    except Exception as e:
        current_app.logger.error(f"Error saving manual styles: {e}")
        return jsonify({'error': str(e)}), 500


# API: Logout
@external_stock_check_bp.route('/external_stock_check/logout', methods=['POST'])
def logout():
    session.pop('external_user', None)
    flash('Logged out successfully.', 'info')
    return redirect(url_for('external_stock_check.external_stock_check_page'))
