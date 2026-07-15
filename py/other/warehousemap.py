from py.utilities.setting import login_required
from flask import request, render_template, Blueprint, flash, redirect, url_for, jsonify, session, send_file
import io
import os
import re
import zipfile
from pathlib import Path
import pandas as pd
import json
import mysql.connector
from mysql.connector import Error
from datetime import datetime
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection


warehousemap_bp = Blueprint('whmap', __name__, template_folder='templates')

EXTENSION_DIR_NAME = "fdc-4seller-picklist"


def _four_seller_extension_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "chrome_extension" / EXTENSION_DIR_NAME


# Database configuration
db_config = Config.DB_CONFIG

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def fetch_locations(styles):
    """
    Fetch locations for a list of styles from the database.
    For styles without locations, set location as 'STORAGE'.
    Returns a dict with style as key and list of locations as value.
    """
    location_dict = {}
    
    if not styles:
        print("No styles provided to fetch_locations")
        return location_dict
    
    connection = None
    cursor = None
    
    try:
        connection = get_db_connection()
        if connection is None:
            print("Failed to connect to the database for fetching locations.")
            return {style.lower(): ['storage'] for style in styles}
        
        cursor = connection.cursor()
        
        # Process styles in batches to avoid overwhelming the database
        batch_size = 100
        for i in range(0, len(styles), batch_size):
            batch_styles = styles[i:i+batch_size]
            
            # Create a placeholder string for the IN clause
            placeholders = ', '.join(['%s'] * len(batch_styles))
            query = f"SELECT style, Location FROM products WHERE style IN ({placeholders})"
            
            cursor.execute(query, batch_styles)
            results = cursor.fetchall()  # Fetch all results at once
            
            # Process the results - collect all locations per style
            for style, location in results:
                style_lower = style.lower()
                loc = location if location else 'storage'
                if style_lower not in location_dict:
                    location_dict[style_lower] = []
                if loc not in location_dict[style_lower]:
                    location_dict[style_lower].append(loc)
            
            # For styles not found in the database, set location as ['STORAGE']
            found_styles = set(result[0].lower() for result in results)
            for style in batch_styles:
                style_lower = style.lower()
                if style_lower not in found_styles and style_lower not in location_dict:
                    # Try with just the first part of the style (before space)
                    stripped_style = style.split(' ')[0]
                    if stripped_style != style:
                        cursor.execute("SELECT style, Location FROM products WHERE style = %s", (stripped_style,))
                        result = cursor.fetchone()
                        cursor.fetchall()  # Ensure all results are consumed
                        
                        if result:
                            loc = result[1] if result[1] else 'storage'
                            location_dict[style_lower] = [loc]
                        else:
                            location_dict[style_lower] = ['storage']
                    else:
                        location_dict[style_lower] = ['storage']
    
    except Error as e:
        print(f"Error fetching locations: {e}")
        # If there's an error, set all locations to ['STORAGE']
        location_dict = {style.lower(): ['storage'] for style in styles}
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
        # Ensure all items have 'style' key (convert from 'sku' if needed)
        for item in items:
            if 'sku' in item and 'style' not in item:
                item['style'] = item.pop('sku')
            if 'style_url' not in item:
                item['style_url'] = f"https://www.fdcef.com/item_lookup/{item['style'].replace(' ', '%20')}"
        
        # Store items in session instead of file
        session['warehouse_items'] = items
        return True
    except Exception as e:
        print(f"Error saving items to session: {e}")
        return False

def load_items_from_session():
    """Load items from session if they exist, converting old 'sku' keys to 'style' for backward compatibility"""
    items = session.get('warehouse_items', [])
    # Convert old 'sku' keys to 'style' for backward compatibility
    for item in items:
        if 'sku' in item and 'style' not in item:
            item['style'] = item.pop('sku')
    return items

@warehousemap_bp.route("/warehousemap", methods=["GET", "POST"])
@login_required
def warehouse():
    items = load_items_from_session()  # Load existing items by default
    # Re-fetch all locations from database on every page load
    if items:
        # Extract all styles from items
        styles = [item['style'] for item in items]
        
        # Fetch fresh location data from database
        location_dict = fetch_locations(styles)
        
        # Update items with the latest location data and add hyperlinks
        for item in items:
            style_lower = item['style'].lower()
            if style_lower in location_dict:
                locs = location_dict[style_lower]
                item['loc'] = ', '.join(locs) if len(locs) > 1 else locs[0]
            # Add hyperlink URL for each style
            item['style_url'] = f"https://www.fdcef.com/item_lookup/{item['style'].replace(' ', '%20')}"
        
        # Split items with multiple locations into separate items
        new_items = []
        for item in items:
            locs = [l.strip() for l in item['loc'].split(',') if l.strip()]
            if len(locs) > 1:
                for loc in locs:
                    new_item = item.copy()
                    new_item['loc'] = loc
                    new_items.append(new_item)
            else:
                new_items.append(item)
        items = new_items
        
        # Save updated items back to JSON
        save_items_to_json(items)
        
    if request.method == "POST":
        if 'clear' in request.form:
            # Clear the current data from session
            session.pop('warehouse_items', None)
            items = []
            flash('Data cleared successfully')
            return redirect(url_for('whmap.warehouse'))

        # --- Manual style input handling ---
        single_style = request.form.get('single_style')
        if single_style:
            style = single_style.strip()
            if not style:
                flash('Please enter a style.')
                return redirect(url_for('whmap.warehouse'))
            # Check if style already exists (case-insensitive)
            if any(item['style'].lower() == style.lower() for item in items):
                flash(f'Style "{style}" already exists in inventory.')
                return redirect(url_for('whmap.warehouse'))
            # Fetch location for this style
            location_dict = fetch_locations([style])
            locs = location_dict.get(style.lower(), ['storage'])
            loc = ', '.join(locs) if len(locs) > 1 else locs[0]
            items.append({
                'style': style,
                'qty': 1,
                'loc': loc,
                'shop': 'Manual Input',
                'order_number': 'N/A',
                'style_url': f"https://www.fdcef.com/item_lookup/{style.replace(' ', '%20')}"
            })
            save_items_to_json(items)
            flash(f'Style "{style}" added to inventory (qty 1, loc: {loc}).')
            return redirect(url_for('whmap.warehouse'))
        
        if 'file' in request.files:
            file = request.files['file']
            
            if file.filename == '':
                flash('No file selected')
                return redirect(url_for('whmap.warehouse'))
            
            if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.csv')):
                try:
                    # Read the file directly from memory
                    if file.filename.endswith('.xlsx'):
                        df = pd.read_excel(file)
                    else:  # CSV file
                        df = pd.read_csv(file)
                    
                    # Check if this is a platform export file
                    platform_columns = ['Stock style', 'Quantity', 'Platform Order Number', 'Shop']
                    simple_columns = ['style', 'qty']
                    
                    # Check if it's a platform export file
                    if any(col in df.columns for col in platform_columns):
                        # Map column names for platform export
                        column_mapping = {}
                        
                        # Find the style column - specifically look for "Stock style" with case-insensitive matching
                        style_columns = [col for col in df.columns if 'Stock style' in col or col.lower() == 'style']
                        if style_columns:
                            column_mapping[style_columns[0]] = 'style'
                        
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
                        if 'style' not in df.columns or 'qty' not in df.columns:
                            missing = []
                            if 'style' not in df.columns:
                                missing.append('Stock style')
                            if 'qty' not in df.columns:
                                missing.append('Quantity')
                            flash(f'File must contain the following columns: {", ".join(missing)}')
                            return redirect(url_for('whmap.warehouse'))
                        
                        # Add shop and order_number if they don't exist
                        if 'shop' not in df.columns:
                            df['shop'] = 'Unknown'
                        if 'order_number' not in df.columns:
                            df['order_number'] = 'N/A'
                        
                    # Check if it's a simple style/QTY file
                    elif all(col.lower() in map(str.lower, df.columns) for col in simple_columns):
                        # Map column names case-insensitively
                        column_mapping = {
                            col: 'style' for col in df.columns if col.lower() == 'style'
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
                        flash('File format not recognized. Please upload a file with style and Quantity columns or a platform export file.')
                        return redirect(url_for('whmap.warehouse'))
                    
                    # Ensure styles are properly formatted (replace hyphens with spaces if needed)
                    if 'style' in df.columns:
                        # Convert all styles to string type first
                        df['style'] = df['style'].astype(str)
                        
                        # If the style contains hyphens, replace them with spaces
                        # This ensures consistency with how styles are stored in the database
                        df['style'] = df['style'].str.replace('-', ' ')
                    
                    # If LOC column doesn't exist, add it
                    if 'loc' not in df.columns:
                        # Get list of styles
                        styles = df['style'].astype(str).tolist()
                        
                        # Fetch locations from database
                        location_dict = fetch_locations(styles)
                        
                        # Add locations to dataframe
                        df['loc'] = df['style'].str.lower().map(location_dict).fillna(['storage']).apply(lambda x: ', '.join(x) if isinstance(x, list) and len(x) > 1 else (x[0] if isinstance(x, list) else x))
                    
                    # Add style URLs to dataframe
                    df['style_url'] = df['style'].apply(lambda x: f"https://www.fdcef.com/item_lookup/{x.replace(' ', '%20')}")
                    
                    # Convert DataFrame to list of dictionaries
                    new_items = df[['style', 'qty', 'loc', 'shop', 'order_number', 'style_url']].to_dict('records')
                    
                    # Split items with multiple locations into separate items
                    split_items = []
                    for item in new_items:
                        locs = [l.strip() for l in item['loc'].split(',') if l.strip()]
                        if len(locs) > 1:
                            for loc in locs:
                                new_item = item.copy()
                                new_item['loc'] = loc
                                split_items.append(new_item)
                        else:
                            split_items.append(item)
                    new_items = split_items
                    
                    # MODIFICATION: Merge new items with existing items instead of replacing
                    # Create a dictionary to track existing styles
                    existing_styles = {item['style'].lower(): item for item in items}
                    
                    # Process new items
                    for new_item in new_items:
                        style_lower = new_item['style'].lower()
                        if style_lower in existing_styles:
                            # If style exists, update quantity and ensure URL is present
                            existing_styles[style_lower]['qty'] += int(new_item['qty'])
                            existing_styles[style_lower]['style_url'] = new_item['style_url']
                        else:
                            # If style is new, add to items list
                            items.append(new_item)
                    
                    # Save updated items to JSON for persistence
                    save_items_to_json(items)
                    
                    flash(f'File uploaded and processed successfully. Added to existing inventory.')
                    return redirect(url_for('whmap.warehouse'))
                except Exception as e:
                    flash(f'Error processing file: {str(e)}')
                    return redirect(url_for('whmap.warehouse'))
            else:
                flash('Please upload a valid Excel (.xlsx) or CSV file')
                return redirect(url_for('whmap.warehouse'))
            
   
                
    # Ensure all existing items have style_url if they don't already
    for item in items:
        if 'style_url' not in item:
            item['style_url'] = f"https://www.fdcef.com/item_lookup/{item['style'].replace(' ', '%20')}"
    
    # Convert items to JSON for the template
    items_json = json.dumps(items)
    
    current_user = session['username']
    return render_template("warehouse_map.html", items=items, items_json=items_json, current_user=current_user)



@warehousemap_bp.route('/delete_item', methods=['POST'])
@login_required
def delete_item():
    style = request.form.get('style')
    loc = request.form.get('loc')
    if not style or not loc:
        return jsonify({'success': False, 'error': 'Missing style or location'}), 400

    items = load_items_from_session()
    # Remove the item with matching style and normalized location
    def normalize_location(loc):
        import re
        base_match = re.match(r"([A-Z]\d+(?:-[A-Z0-9]+)?)", loc, re.I)
        base_location = base_match.group(1).upper() if base_match else loc
        loc_lower = loc.lower()
        if ' mid' in loc_lower:
            base_location += ' MID'
        elif ' top' in loc_lower or ' up' in loc_lower:
            base_location += ' TOP'
        elif ' bot' in loc_lower or ' bottom' in loc_lower:
            base_location += ' BOT'
        return base_location

    items = [item for item in items if not (item['style'] == style and normalize_location(item['loc']) == loc)]
    return jsonify({'success': True})

@warehousemap_bp.route("/warehousemap/clear", methods=["POST"])
@login_required
def clear_warehouse_data():
    """Clear the current warehouse data"""
    session.pop('warehouse_items', None)
    flash('Data cleared successfully')
    return redirect(url_for('whmap.warehouse'))
@warehousemap_bp.route("/warehousemap/update_qty", methods=["POST"])
@login_required
def update_qty():
    """Update the quantity for a specific style at a specific location"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'No data provided'}), 400
        
        style = data.get('style')
        loc = data.get('loc')
        qty = data.get('qty')
        
        if not style or not loc or qty is None:
            return jsonify({'success': False, 'message': 'Missing required fields'}), 400
        
        # Load current items
        items = load_items_from_session()
        
        # Find and update the matching item(s)
        updated = False
        def normalize_location(loc):
            import re
            base_match = re.match(r"([A-Z]\d+(?:-[A-Z0-9]+)?)", loc, re.I)
            base_location = base_match.group(1).upper() if base_match else loc
            loc_lower = loc.lower()
            if ' mid' in loc_lower:
                base_location += ' MID'
            elif ' top' in loc_lower or ' up' in loc_lower:
                base_location += ' TOP'
            elif ' bot' in loc_lower or ' bottom' in loc_lower:
                base_location += ' BOT'
            return base_location
        
        for item in items:
            # Convert quantities to integers for comparison
            item_style = item.get('style', '').strip()
            item_loc = item.get('loc', '').strip()
            
            # Check if this is the item we want to update
            if item_style.lower() == style.lower() and normalize_location(item_loc) == normalize_location(loc):
                # Update the quantity
                try:
                    item['qty'] = int(qty)
                    updated = True
                    print(f"Updated quantity for {style} at {loc} to {qty}")
                except ValueError:
                    return jsonify({'success': False, 'message': 'Invalid quantity value'}), 400
        
        if not updated:
            print(f"Item not found: style={style}, loc={loc}")
            print(f"Available items: {[(item.get('style'), item.get('loc')) for item in items]}")
            return jsonify({'success': False, 'message': 'Item not found'}), 404
        
        # Save updated items
        save_items_to_json(items)
        
        return jsonify({'success': True})
    
    except Exception as e:
        print(f"Error updating quantity: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500


@warehousemap_bp.route("/warehousemap/download-4seller-extension", methods=["GET"])
@login_required
def download_4seller_extension():
    """Zip and download the 4Seller picklist Chrome extension."""
    ext_dir = _four_seller_extension_dir()
    if not ext_dir.is_dir():
        flash("Chrome extension package not found.")
        return redirect(url_for("whmap.warehouse"))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in ext_dir.rglob("*"):
            if path.is_file():
                arcname = Path(EXTENSION_DIR_NAME) / path.relative_to(ext_dir)
                archive.write(path, arcname.as_posix())

    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{EXTENSION_DIR_NAME}.zip",
    )

