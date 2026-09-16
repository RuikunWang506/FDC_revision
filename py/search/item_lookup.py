from flask import Blueprint, request, render_template, session, current_app, jsonify
from py.utilities.setting import login_required
import os
import time
import json
import pandas as pd
from py.utilities.database_utils import get_mysql_connection
from py.utilities.SQL_connector import generate_excel
from py.utilities.models import SessionLocal, ImageAsset
from py.utilities.logging_setup import log_event, log_user_action
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
        # Use case-insensitive matching: compare LOWER(SKU) to provided lowercased, stripped SKU list
        # strip whitespace (including trailing spaces) from input SKUs before querying
        def _clean_input(s):
            try:
                return s.replace('\u00A0', ' ').strip().lower()
            except Exception:
                return str(s).strip().lower()

        lowered = [_clean_input(s) for s in skus]
        placeholders = ', '.join(['%s'] * len(lowered))
        # With one row per SKU-Location, we aggregate locations with GROUP_CONCAT
        query = f"""
            SELECT SKU, GROUP_CONCAT(DISTINCT Location ORDER BY Location SEPARATOR ', ') as locations
            FROM products 
            WHERE LOWER(SKU) IN ({placeholders}) AND Location IS NOT NULL AND Location != ''
            GROUP BY SKU
        """
        
        cursor.execute(query, lowered)
        results = cursor.fetchall()
        
        for sku, locations in results:
            # store under lowercase, stripped key for consistent lookup
            try:
                k = sku.replace('\u00A0', ' ').strip().lower()
            except Exception:
                k = str(sku).strip().lower()
            location_dict[k] = locations
        
        # Fallback for SKUs not found - try stripped version
        for sku in skus:
            try:
                key = sku.replace('\u00A0', ' ').strip().lower()
            except Exception:
                key = str(sku).strip().lower()

            if key not in location_dict:
                # stripped variant removes all spaces
                try:
                    stripped_sku = sku.replace('\u00A0', ' ').replace(' ', '').strip().lower()
                except Exception:
                    stripped_sku = str(sku).replace(' ', '').strip().lower()

                if stripped_sku != key:
                    cursor.execute(
                        "SELECT GROUP_CONCAT(DISTINCT Location ORDER BY Location SEPARATOR ', ') FROM products WHERE LOWER(SKU) = %s",
                        (stripped_sku,)
                    )
                    result = cursor.fetchone()
                    if result and result[0]:
                        location_dict[key] = result[0]
    
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

def _looks_like_hash_sku(value):
    """True when DAM sku is a hex file id, not a product SKU."""
    if not value:
        return False
    s = str(value).strip().upper()
    return len(s) >= 16 and all(c in '0123456789ABCDEF' for c in s)

def _resolve_image_search_terms(user_input, df):
    """Wildcard searches should use result SKUs, not the raw pattern."""
    terms = [term.split()[0].strip() for term in user_input.split(",") if term.split()[0].strip()]
    if not any('%' in term for term in terms):
        return terms, 'prefix'

    if df is not None and 'SKU' in df.columns:
        skus = []
        seen = set()
        for raw in df['SKU'].astype(str).str.strip().str.upper():
            if not raw or raw in seen or _looks_like_hash_sku(raw):
                continue
            seen.add(raw)
            skus.append(raw)
        if skus:
            return skus, 'exact'

    return [], 'exact'


ITEM_LOOKUP_IMAGE_SOURCES = ('catalog_original', 'catalog', 'dropbox')


def _dataframe_has_inventory_rows(df):
    return df is not None and hasattr(df, 'empty') and not df.empty


def _lookup_item_catalog_images(user_input, df):
    """Catalog / Dropbox pictures for a lookup, even when OMS has no style rows."""
    search_terms, image_match_mode = _resolve_image_search_terms(user_input, df)
    has_comma = ',' in (user_input or '')
    sorted_image_urls = []
    file_info = []
    dam_assets = []
    db = None
    try:
        db = py.utilities.models.SessionLocal()
        for term in search_terms:
            sku_filter = ImageAsset.sku.ilike(term) if image_match_mode == 'exact' else ImageAsset.sku.ilike(f'{term}%')
            query = db.query(ImageAsset).filter(
                sku_filter,
                ImageAsset.is_active == True,
                ImageAsset.source.in_(ITEM_LOOKUP_IMAGE_SOURCES)
            )
            if has_comma:
                query = query.filter(ImageAsset.is_canonical == True)
            dam_assets.extend(query.order_by(ImageAsset.version).all())
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
        if db is not None:
            db.close()

    if dam_assets:
        for asset in dam_assets:
            if _looks_like_hash_sku(asset.sku):
                continue
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
            sorted_image_urls.append(f'/image/{asset.id}/300')
        combined = list(zip(sorted_image_urls, file_info))
        combined.sort(key=lambda x: (-int(bool(x[1][6])), x[1][5] if x[1][5] is not None else 0))
        sorted_image_urls, file_info = zip(*combined) if combined else ([], [])
        sorted_image_urls = list(sorted_image_urls)
        file_info = list(file_info)
        return sorted_image_urls, file_info, [info[0] for info in file_info]

    matching_assets = []
    for term in search_terms:
        matching_assets.extend(search_assets_by_sku_pattern(term, source_filter=list(ITEM_LOOKUP_IMAGE_SOURCES)))
    unique_assets = {}
    for asset in matching_assets:
        unique_assets[asset.id] = asset
    matching_assets = list(unique_assets.values())
    if has_comma:
        canonical_assets = [asset for asset in matching_assets if asset.is_canonical]
        if not canonical_assets:
            canonical_assets = matching_assets
    else:
        canonical_assets = matching_assets

    file_names = []
    for asset in canonical_assets:
        if _looks_like_hash_sku(asset.sku):
            continue
        file_names.append(asset.filename)
        size = 0
        if os.path.exists(asset.path):
            size = os.path.getsize(asset.path)
        modified_date = asset.created_at.strftime('%Y-%m-%d %H:%M:%S') if asset.created_at else 'Unknown'
        sku = asset.sku if asset.sku and not _looks_like_hash_sku(asset.sku) else ''
        if not sku:
            sku = asset.filename.split('.')[0].upper()
            if _looks_like_hash_sku(sku):
                sku = ''
        file_info.append((asset.filename, size, modified_date, None, None, asset.version, asset.is_canonical, sku))
        sorted_image_urls.append(get_asset_url(asset.id))
    return sorted_image_urls, file_info, sort_filenames(file_names)


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
        return render_template(
            "Item_lookup.html",
            excel_html=None,
            history_html=None,
            current_user=current_user,
            user_input=user_input,
            no_products=False,
        )

    # Continue with the rest of the logic if user_input is valid
    if user_input.endswith(","):
        user_input = user_input[:-1]

    # Generate Excel and history HTML
    excel_html, history_html, purchase_html, adjustment_html, _, user_input = generate_excel(user_input)

    # Process excel_html to add Location and Update_Date columns
    df = None
    if excel_html:
        from io import StringIO
        try:
            df = pd.read_html(StringIO(excel_html))[0]  # Convert HTML to DataFrame
        except Exception:
            df = None
        if not _dataframe_has_inventory_rows(df):
            excel_html = None
            df = None

    if _dataframe_has_inventory_rows(df):
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

    no_products = not _dataframe_has_inventory_rows(df)
    if no_products:
        excel_html = None
        if history_html and 'No item history' in history_html:
            history_html = None
        if purchase_html and 'No purchase history' in purchase_html:
            purchase_html = None
        if adjustment_html and 'No inventory adjustments' in adjustment_html:
            adjustment_html = None

    sorted_image_urls, file_info, sorted_file_names = _lookup_item_catalog_images(user_input, df)

    log_user_action(f"***{current_user}***__@@@search SKU@@@__ {user_input}", current_user)
    return render_template(
        "Item_lookup.html",
        excel_html=excel_html,
        history_html=history_html,
        purchase_html=purchase_html,
        adjustment_html=adjustment_html,
        image_urls=sorted_image_urls,
        file_names=sorted_file_names,
        file_info=file_info,
        current_user=current_user,
        user_input=user_input.capitalize(),
        no_products=no_products,
    )

@item_lookup_bp.route('/get_item_location', methods=['GET'])
def get_item_location():
    sku = request.args.get('sku', '').strip()
    skus = request.args.get('skus', '').strip()
    
    if sku:
        # Single SKU
        try:
            locations = fetch_locations([sku])
            # try exact lowercase, then stripped fallback
            location = locations.get(sku.lower()) or locations.get(sku.lower().replace(' ', '')) or 'N/A'
            return jsonify({'location': location})
        except Exception as e:
            current_app.logger.error(f"Error fetching location for SKU {sku}: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500
    elif skus:
        # Multiple SKUs
        sku_list = [s.strip() for s in skus.split(',') if s.strip()]
        try:
            locations = fetch_locations(sku_list)
            # Return dict of sku: location and include multiple normalized keys
            # to help clients match variants (case, spaces, punctuation)
            import re
            def alnum(s):
                return re.sub(r'[^0-9a-z]', '', s.lower())

            result = {}
            for s in sku_list:
                key = s
                lookup = s.lower()
                lookup_stripped = lookup.replace(' ', '')
                lookup_alnum = alnum(s)
                loc = locations.get(lookup) or locations.get(lookup_stripped) or locations.get(lookup_alnum) or 'N/A'

                # Populate multiple variants so clients can match robustly
                result[key] = loc
                result[lookup] = loc
                result[lookup_stripped] = loc
                result[lookup_alnum] = loc

            return jsonify(result)
        except Exception as e:
            current_app.logger.error(f"Error fetching locations for SKUs {sku_list}: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500
    else:
        return jsonify({'error': 'SKU or SKUs parameter is required'}), 400