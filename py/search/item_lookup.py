from flask import Blueprint, request, render_template, session, current_app
from py.utilities.setting import login_required
import os
import time
import json
import pandas as pd
from py.utilities.database_utils import get_mysql_connection
from py.utilities.SQL_connector import generate_excel
from py.utilities.models import SessionLocal, ImageAsset
from py.utilities.logging_setup import log_event
from py.utilities.setting import Config
from py.utilities.dam_utils import search_assets_by_sku_pattern, get_canonical_assets_for_skus, get_asset_url
import py.utilities.models

item_lookup_bp = Blueprint('item_lookup', __name__)

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def fetch_locations(skus):
    location_dict = {}
    
    if not skus:
        current_app.logger.info("No SKUs provided to fetch_locations")
        return location_dict
    
    try:
        connection = get_db_connection()
        if connection is None:
            current_app.logger.error("Failed to connect to the database for fetching locations.")
            return location_dict
        
        cursor = connection.cursor()
        placeholders = ', '.join(['%s'] * len(skus))
        query = f"SELECT SKU, Location FROM products WHERE SKU IN ({placeholders})"
        
        
        cursor.execute(query, skus)
        results = cursor.fetchall()
        
        for sku, location in results:
            location_dict[sku.lower()] = location
        
        for sku in skus:
            if sku.lower() not in location_dict or not location_dict[sku.lower()]:
                stripped_sku = sku.split(' ')[0]
                if stripped_sku != sku:
                    cursor.execute(
                        "SELECT SKU, Location, Update_Date FROM products WHERE SKU = %s",
                        (stripped_sku,)
                    )
                    result = cursor.fetchone()
                    if result:
                        location_dict[sku.lower()] = result[1]
    
    except Exception as e:
        current_app.logger.error(f"Error fetching locations: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
    
    return location_dict

def sort_filenames(filenames):
    import re
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

@item_lookup_bp.route("/item_lookup", methods=["GET", "POST"])
@item_lookup_bp.route("/item_lookup/<string:user_input>", methods=["GET"])
@login_required
def item_lookup(user_input=None):
    current_user = session['username']
    excel_html = None
    history_html = None  # Initialize history_html
    purchase_html = None
    adjustment_html = None  # Initialize adjustment_html
    
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
        return render_template("Item_lookup.html", excel_html=None, history_html=None, current_user=current_user, user_input=user_input)

    # Continue with the rest of the logic if user_input is valid
    if user_input.endswith(","):
        user_input = user_input[:-1]

    # Generate Excel and history HTML
    excel_html, history_html, purchase_html, adjustment_html, _, user_input = generate_excel(user_input)

    if not excel_html and not history_html and not purchase_html and not adjustment_html:  # If no valid data is found
        return render_template("Item_lookup.html", excel_html=None, history_html=None, purchase_html=None, adjustment_html=None, current_user=current_user, user_input=user_input)
    # Process excel_html to add Location and Update_Date columns
    if excel_html:
        from io import StringIO
        df = pd.read_html(StringIO(excel_html))[0]  # Convert HTML to DataFrame

        # Add a row number column
        df.insert(0, '#', range(1, len(df) + 1))

        # Fetch SKUs and update location and date columns
        if 'SKU' in df.columns:
            skus = df['SKU'].astype(str).str.lower().tolist()
        else:
            skus = []

        # Fetch locations and update dates using the helper function
        location_dict = fetch_locations(skus)

        # Add the 'Location' and 'Update_Date' columns
        df['Location'] = df['SKU'].astype(str).str.lower().map(location_dict).fillna('')
        if 'DB' in df.columns:
            replacement_dict = {
                'oms_ef': 'E_FAS',
                'omsdata_fdc': 'FDC'
            }
            # Perform case-insensitive replacement
            df['DB'] = df['DB'].str.lower().replace(replacement_dict)
        else:
            current_app.logger.warning("Database column not found in the DataFrame.")
            
        if 'Location' in df.columns:
            df['Location'] = df.apply(
                lambda row: (
                    f'<span class="editable">'
                    f'{row["Location"] if pd.notnull(row["Location"]) and row["Location"] != "" else "*"}</span>'
                ),
                axis=1
            )

        # Adjust column order to place 'Location' after the third column
        if 'Location' in df.columns:
            cols = list(df.columns)
            loc_index = 3  # After the third column
            cols.insert(loc_index, cols.pop(cols.index('Location')))
            df = df[cols]

        # Replace '<br>' in column headers with spaces
        df.columns = df.columns.str.replace('<br>', ' ')
        df.columns = df.columns.str.replace('\n', '<br>')

        # Convert DataFrame back to HTML for rendering
        excel_html = df.to_html(
            index=False,
            classes="table table-bordered table-striped",
            table_id="excel_table",
            escape=False  # Allow HTML in headers and cells
        )

    # Prepare image and file information - Enhanced with DAM system
    search_terms = [term.split()[0].strip() for term in user_input.split(",")]
    
    # Determine if we should filter to canonical only (when multiple SKUs with comma)
    has_comma = ',' in user_input
    
    # First, try to get assets from DAM database - only catalog_original source
    dam_assets = []
    try:
        db = py.utilities.models.SessionLocal()
        for term in search_terms:
            # Build query - only filter canonical if input has comma (multiple SKUs)
            query = db.query(ImageAsset).filter(
                ImageAsset.sku.ilike(f'{term}%'),
                ImageAsset.is_active == True,
                ImageAsset.source == 'catalog_original'
            )
            
            # Only filter to canonical images if input has comma
            if has_comma:
                query = query.filter(ImageAsset.is_canonical == True)
            
            assets = query.order_by(ImageAsset.version).all()
            dam_assets.extend(assets)
        
        # Remove duplicates based on filename
        seen_filenames = set()
        unique_dam_assets = []
        for asset in dam_assets:
            if asset.filename not in seen_filenames:
                seen_filenames.add(asset.filename)
                unique_dam_assets.append(asset)
        dam_assets = unique_dam_assets
        
    except Exception as e:
        current_app.logger.error(f"Error querying DAM database: {e}")
        dam_assets = []
    finally:
        if 'db' in locals():
            db.close()

    # If DAM has assets, use them; otherwise fall back to filesystem search
    if dam_assets:
        # Use DAM assets
        sorted_file_names = [asset.filename for asset in dam_assets]
        sorted_image_urls = [f'/image/{asset.id}/300' for asset in dam_assets]  # Use DAM endpoint
        
        # Enhanced file info with DAM metadata
        file_info = []
        for asset in dam_assets:
            file_info.append((
                asset.filename,
                asset.file_size,
                asset.created_at.strftime('%Y-%m-%d %H:%M:%S') if asset.created_at else 'Unknown',
                asset.width,
                asset.height,
                asset.version,
                asset.is_canonical,
                asset.sku
            ))
        
        # Sort by canonical status (canonical first), then by version
        combined = list(zip(sorted_image_urls, file_info))
        combined.sort(key=lambda x: (-x[1][5], x[1][4]))  # canonical desc, version asc
        sorted_image_urls, file_info = zip(*combined) if combined else ([], [])
        
    else:
        # Use DAM system to search for assets - only catalog_original source for item lookup
        matching_assets = []
        for term in search_terms:
            assets = search_assets_by_sku_pattern(term, source_filter='catalog_original')
            matching_assets.extend(assets)
            
        # Remove duplicates by asset ID
        unique_assets = {}
        for asset in matching_assets:
            unique_assets[asset.id] = asset
        matching_assets = list(unique_assets.values())
        
        # Filter to only canonical images if input has comma (multiple SKUs)
        if has_comma:
            canonical_assets = [asset for asset in matching_assets if asset.is_canonical]
            # If no canonical assets found, fall back to all assets
            if not canonical_assets:
                canonical_assets = matching_assets
        else:
            # Single SKU - show all images
            canonical_assets = matching_assets
        
        # Convert assets to file_info format expected by template
        file_info = []
        file_names = []
        sorted_image_urls = []
        
        for asset in canonical_assets:
            file_names.append(asset.filename)
            
            # Get file size if path exists
            size = 0
            if os.path.exists(asset.path):
                size = os.path.getsize(asset.path)
            
            modified_date = asset.created_at.strftime('%Y-%m-%d %H:%M:%S') if asset.created_at else 'Unknown'
            
            # Extract SKU from filename (remove extension and convert to uppercase)
            sku = asset.filename.split('.')[0].upper()
            
            file_info.append((asset.filename, size, modified_date, None, None, asset.version, asset.is_canonical, sku))
            
            # Use DAM URL for image access
            image_url = get_asset_url(asset.id)
            sorted_image_urls.append(image_url)
        
        # Sort file names using existing function
        sorted_file_names = sort_filenames(file_names)

    current_app.logger.info("***%s***__@@@search SKU@@@__ %s",current_user, user_input)
    return render_template(
        "Item_lookup.html",
        excel_html=excel_html,
        history_html=history_html,
        purchase_html=purchase_html,
        adjustment_html=adjustment_html,  # Add this
        image_urls=sorted_image_urls,
        file_names=sorted_file_names,
        file_info=file_info,
        current_user=current_user,
        user_input=user_input.capitalize()
    )