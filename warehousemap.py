from flask import request, render_template, Blueprint, flash, redirect, url_for,jsonify,session
import os,re
import pandas as pd
import json
import mysql.connector
from mysql.connector import Error
from datetime import datetime


warehousemap_bp = Blueprint('whmap', __name__, template_folder='templates')

# Database configuration
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'
}

# Directory to store warehouse files
WAREHOUSE_DIR = os.path.join('output', 'warehouse')
# Ensure the directory exists
os.makedirs(WAREHOUSE_DIR, exist_ok=True)

# Path to the latest data file
LATEST_DATA_FILE = os.path.join(WAREHOUSE_DIR, 'latest_data.json')

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def fetch_locations(skus):
    """
    Fetch locations for a list of SKUs from the database.
    For SKUs without locations, set location as 'STORAGE'.
    """
    location_dict = {}
    
    if not skus:
        print("No SKUs provided to fetch_locations")
        return location_dict
    
    connection = None
    cursor = None
    
    try:
        connection = get_db_connection()
        if connection is None:
            print("Failed to connect to the database for fetching locations.")
            return {sku.lower(): 'storage' for sku in skus}
        
        cursor = connection.cursor()
        
        # Process SKUs in batches to avoid overwhelming the database
        batch_size = 100
        for i in range(0, len(skus), batch_size):
            batch_skus = skus[i:i+batch_size]
            
            # Create a placeholder string for the IN clause
            placeholders = ', '.join(['%s'] * len(batch_skus))
            query = f"SELECT SKU, Location FROM products WHERE SKU IN ({placeholders})"
            
            cursor.execute(query, batch_skus)
            results = cursor.fetchall()  # Fetch all results at once
            
            # Process the results
            for sku, location in results:
                location_dict[sku.lower()] = location if location else 'storage'
            
            # For SKUs not found in the database, set location as 'STORAGE'
            found_skus = [result[0].lower() for result in results]
            for sku in batch_skus:
                if sku.lower() not in found_skus and sku.lower() not in location_dict:
                    # Try with just the first part of the SKU (before space)
                    stripped_sku = sku.split(' ')[0]
                    if stripped_sku != sku:
                        cursor.execute("SELECT SKU, Location FROM products WHERE SKU = %s", (stripped_sku,))
                        result = cursor.fetchone()
                        cursor.fetchall()  # Ensure all results are consumed
                        
                        if result:
                            location_dict[sku.lower()] = result[1] if result[1] else 'storage'
                        else:
                            location_dict[sku.lower()] = 'storage'
                    else:
                        location_dict[sku.lower()] = 'storage'
    
    except Error as e:
        print(f"Error fetching locations: {e}")
        # If there's an error, set all locations to 'STORAGE'
        location_dict = {sku.lower(): 'storage' for sku in skus}
    finally:
        # Ensure cursor and connection are properly closed
        if cursor:
            try:
                cursor.fetchall()  # Consume any remaining results
            except:
                pass
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
    
    return location_dict
def save_items_to_json(items):
    """Save processed items to a JSON file for persistence"""
    try:
        # Ensure all items have sku_url before saving
        for item in items:
            if 'sku_url' not in item:
                item['sku_url'] = f"https://www.fdcef.com/item_lookup/{item['sku'].replace(' ', '%20')}"
        
        with open(LATEST_DATA_FILE, 'w') as f:
            json.dump(items, f)
        return True
    except Exception as e:
        print(f"Error saving items to JSON: {e}")
        return False

def load_items_from_json():
    """Load items from the latest JSON file if it exists"""
    if os.path.exists(LATEST_DATA_FILE):
        try:
            with open(LATEST_DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading items from JSON: {e}")
    return []

@warehousemap_bp.route("/warehousemap", methods=["GET", "POST"])
def warehouse():
    items = load_items_from_json()  # Load existing items by default
    # Re-fetch all locations from database on every page load
    if items:
        # Extract all SKUs from items
        skus = [item['sku'] for item in items]
        
        # Fetch fresh location data from database
        location_dict = fetch_locations(skus)
        
        # Update items with the latest location data and add hyperlinks
        for item in items:
            sku_lower = item['sku'].lower()
            if sku_lower in location_dict:
                item['loc'] = location_dict[sku_lower]
            # Add hyperlink URL for each SKU
            item['sku_url'] = f"https://www.fdcef.com/item_lookup/{item['sku'].replace(' ', '%20')}"
        
        # Save updated items back to JSON
        save_items_to_json(items)
        
    if request.method == "POST":
        if 'clear' in request.form:
            # Clear the current data
            if os.path.exists(LATEST_DATA_FILE):
                os.remove(LATEST_DATA_FILE)
            items = []
            flash('Data cleared successfully')
            return redirect(url_for('whmap.warehouse'))

        # --- Manual SKU input handling ---
        single_sku = request.form.get('single_sku')
        if single_sku:
            sku = single_sku.strip()
            if not sku:
                flash('Please enter a SKU.')
                return redirect(url_for('whmap.warehouse'))
            # Check if SKU already exists (case-insensitive)
            if any(item['sku'].lower() == sku.lower() for item in items):
                flash(f'SKU "{sku}" already exists in inventory.')
                return redirect(url_for('whmap.warehouse'))
            # Fetch location for this SKU
            location_dict = fetch_locations([sku])
            items.append({
                'sku': sku,
                'qty': 1,
                'loc': location_dict.get(sku.lower(), 'storage'),
                'shop': 'Manual Input',
                'order_number': 'N/A',
                'sku_url': f"https://www.fdcef.com/item_lookup/{sku.replace(' ', '%20')}"
            })
            save_items_to_json(items)
            flash(f'SKU "{sku}" added to inventory (qty 1, loc: {location_dict.get(sku.lower(), "storage")}).')
            return redirect(url_for('whmap.warehouse'))
        
        if 'file' in request.files:
            file = request.files['file']
            
            if file.filename == '':
                flash('No file selected')
                return redirect(url_for('whmap.warehouse'))
            
            if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.csv')):
                try:
                    # Create a timestamped filename
                    timestamp = datetime.now().strftime('%m-%d-%Y')
                    file_ext = os.path.splitext(file.filename)[1]
                    saved_filename = f"warehouse_{timestamp}{file_ext}"
                    saved_path = os.path.join(WAREHOUSE_DIR, saved_filename)
                    
                    # Save the uploaded file
                    file.save(saved_path)
                    
                    # Read the file based on its extension
                    if file.filename.endswith('.xlsx'):
                        df = pd.read_excel(saved_path)
                    else:  # CSV file
                        df = pd.read_csv(saved_path)
                    
                    # Check if this is a platform export file
                    platform_columns = ['Stock SKU', 'Quantity', 'Platform Order Number', 'Shop']
                    simple_columns = ['sku', 'qty']
                    
                    # Check if it's a platform export file
                    if any(col in df.columns for col in platform_columns):
                        # Map column names for platform export
                        column_mapping = {}
                        
                        # Find the SKU column - specifically look for "Stock SKU" with case-insensitive matching
                        sku_columns = [col for col in df.columns if 'Stock SKU' in col or col.lower() == 'sku']
                        if sku_columns:
                            column_mapping[sku_columns[0]] = 'sku'
                        
                        # Find the Quantity column
                        qty_columns = [col for col in df.columns if 'Quantity' in col or col.lower() == 'qty']
                        if qty_columns:
                            column_mapping[qty_columns[0]] = 'qty'
                        
                        # Find the Shop/Platform column
                        shop_columns = [col for col in df.columns if 'Shop' in col or 'Platform' in col]
                        if shop_columns:
                            column_mapping[shop_columns[0]] = 'shop'
                        
                        # Find the Order Number column
                        order_columns = [col for col in df.columns if 'Order' in col and 'Number' in col]
                        if order_columns:
                            column_mapping[order_columns[0]] = 'order_number'
                        
                        # Rename columns
                        df = df.rename(columns=column_mapping)
                        
                        # Ensure required columns exist
                        if 'sku' not in df.columns or 'qty' not in df.columns:
                            missing = []
                            if 'sku' not in df.columns:
                                missing.append('Stock SKU')
                            if 'qty' not in df.columns:
                                missing.append('Quantity')
                            flash(f'File must contain the following columns: {", ".join(missing)}')
                            return redirect(url_for('whmap.warehouse'))
                        
                        # Add shop and order_number if they don't exist
                        if 'shop' not in df.columns:
                            df['shop'] = 'Unknown'
                        if 'order_number' not in df.columns:
                            df['order_number'] = 'N/A'
                        
                    # Check if it's a simple SKU/QTY file
                    elif all(col.lower() in map(str.lower, df.columns) for col in simple_columns):
                        # Map column names case-insensitively
                        column_mapping = {
                            col: 'sku' for col in df.columns if col.lower() == 'sku'
                        }
                        column_mapping.update({
                            col: 'qty' for col in df.columns if col.lower() == 'qty'
                        })
                        
                        # Rename columns
                        df = df.rename(columns=column_mapping)
                        
                        # Add shop and order_number columns
                        df['shop'] = 'Manual Upload'
                        df['order_number'] = 'N/A'
                    else:
                        flash('File format not recognized. Please upload a file with SKU and Quantity columns or a platform export file.')
                        return redirect(url_for('whmap.warehouse'))
                    
                    # Ensure SKUs are properly formatted (replace hyphens with spaces if needed)
                    if 'sku' in df.columns:
                        # Convert all SKUs to string type first
                        df['sku'] = df['sku'].astype(str)
                        
                        # If the SKU contains hyphens, replace them with spaces
                        # This ensures consistency with how SKUs are stored in the database
                        df['sku'] = df['sku'].str.replace('-', ' ')
                    
                    # If LOC column doesn't exist, add it
                    if 'loc' not in df.columns:
                        # Get list of SKUs
                        skus = df['sku'].astype(str).tolist()
                        
                        # Fetch locations from database
                        location_dict = fetch_locations(skus)
                        
                        # Add locations to dataframe
                        df['loc'] = df['sku'].str.lower().map(location_dict).fillna('storage')
                    
                    # Add SKU URLs to dataframe
                    df['sku_url'] = df['sku'].apply(lambda x: f"https://www.fdcef.com/item_lookup/{x.replace(' ', '%20')}")
                    
                    # Convert DataFrame to list of dictionaries
                    new_items = df[['sku', 'qty', 'loc', 'shop', 'order_number', 'sku_url']].to_dict('records')
                    
                    # MODIFICATION: Merge new items with existing items instead of replacing
                    # Create a dictionary to track existing SKUs
                    existing_skus = {item['sku'].lower(): item for item in items}
                    
                    # Process new items
                    for new_item in new_items:
                        sku_lower = new_item['sku'].lower()
                        if sku_lower in existing_skus:
                            # If SKU exists, update quantity and ensure URL is present
                            existing_skus[sku_lower]['qty'] += int(new_item['qty'])
                            existing_skus[sku_lower]['sku_url'] = new_item['sku_url']
                        else:
                            # If SKU is new, add to items list
                            items.append(new_item)
                    
                    # Save updated items to JSON for persistence
                    save_items_to_json(items)
                    
                    flash(f'File uploaded and processed successfully: {saved_filename}. Added to existing inventory.')
                    return redirect(url_for('whmap.warehouse'))
                except Exception as e:
                    flash(f'Error processing file: {str(e)}')
                    return redirect(url_for('whmap.warehouse'))
            else:
                flash('Please upload a valid Excel (.xlsx) or CSV file')
                return redirect(url_for('whmap.warehouse'))
            
   
                
    # Ensure all existing items have sku_url if they don't already
    for item in items:
        if 'sku_url' not in item:
            item['sku_url'] = f"https://www.fdcef.com/item_lookup/{item['sku'].replace(' ', '%20')}"
    
    # Convert items to JSON for the template
    items_json = json.dumps(items)
    
    return render_template("warehouse_map.html", items=items, items_json=items_json)



@warehousemap_bp.route("/warehousemap/delete", methods=["POST"])
def delete_item():
    sku = request.form.get('sku')
    loc = request.form.get('loc')
    if not sku or not loc:
        return jsonify({'success': False, 'error': 'Missing SKU or location'}), 400

    items = load_items_from_json()
    # Remove the item with matching SKU and normalized location
    def normalize_location(loc):
        import re
        base_match = re.match(r"([A-Z]\d+(?:-[A-Z0-9]+)?)", loc, re.I)
        base_location = base_match.group(1).upper() if base_match else loc
        loc_lower = loc.lower()
        if ' mid' in loc_lower:
            base_location += ' MID'
        elif ' top' in loc_lower or ' up' in loc_lower:
            base_location += ' TOP'
        return base_location

    items = [item for item in items if not (item['sku'] == sku and normalize_location(item['loc']) == loc)]
    save_items_to_json(items)
    return jsonify({'success': True})

@warehousemap_bp.route("/warehousemap/clear", methods=["POST"])
def clear_warehouse_data():
    """Clear the current warehouse data"""
    if os.path.exists(LATEST_DATA_FILE):
        os.remove(LATEST_DATA_FILE)
    flash('Data cleared successfully')
    return redirect(url_for('whmap.warehouse'))
@warehousemap_bp.route("/warehousemap/update_qty", methods=["POST"])
def update_qty():
    """Update the quantity for a specific SKU at a specific location"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        sku = data.get('sku')
        loc = data.get('loc')
        qty = data.get('qty')
        
        if not sku or not loc or qty is None:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
        # Load current items
        items = load_items_from_json()
        
        # Find and update the matching item(s)
        updated = False
        for item in items:
            # Convert quantities to integers for comparison
            item_sku = item.get('sku', '').strip()
            item_loc = item.get('loc', '').strip()
            
            # Check if this is the item we want to update
            if item_sku.lower() == sku.lower() and item_loc.lower() == loc.lower():
                # Update the quantity
                try:
                    item['qty'] = int(qty)
                    updated = True
                    print(f"Updated quantity for {sku} at {loc} to {qty}")
                except ValueError:
                    return jsonify({'success': False, 'message': 'Invalid quantity value'}), 400
        
        if not updated:
            print(f"Item not found: SKU={sku}, loc={loc}")
            print(f"Available items: {[(item.get('sku'), item.get('loc')) for item in items]}")
            return jsonify({'success': False, 'message': 'Item not found'}), 404
        
        # Save updated items
        save_items_to_json(items)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Error updating quantity: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

