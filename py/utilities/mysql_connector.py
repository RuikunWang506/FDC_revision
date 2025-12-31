from mysql.connector import Error
from py.utilities.database_utils import get_mysql_connection
from flask import Blueprint, request, jsonify, session
from py.utilities.setting import Config, login_required
import os
import logging

mysql_connector_bp = Blueprint('mysql_connector', __name__)
logger = logging.getLogger(__name__)

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()

def connect_to_db():
    """Helper to establish a database connection."""
    try:
        return get_db_connection()
    except Error as e:
        return None

def check_product_exists(cursor, sku, style):
    """Check if the product exists in the database."""
    select_query = "SELECT * FROM products WHERE SKU = %s AND Style = %s"
    cursor.execute(select_query, (sku, style))
    return cursor.fetchone()

def update_product_location(cursor, location, sku, style):
    """Update the location for an existing product."""
    update_query = "UPDATE products SET Location = %s, Update_Date = NOW() WHERE SKU = %s AND Style = %s"
    cursor.execute(update_query, (location, sku, style))

def insert_new_product(cursor, sku, style, location):
    """Insert a new product into the database."""
    insert_query = "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())"
    cursor.execute(insert_query, (sku, style, location))

def extract_base_style(sku):
    """
    Extract base style code from full SKU.
    Examples:
      M1141 BK → M1141
      M1141 BR → M1141
      M1152    → M1152
    """
    if not sku:
        return ''
    
    # Take the first word/part before space
    sku_parts = sku.strip().split()
    if sku_parts:
        return sku_parts[0]  # Return base code (M1141, M1152, etc.)
    return sku.strip()

def delete_location_from_db(sku, username='UNKNOWN_USER'):
    """Delete location record from database for a given SKU."""
    if not sku:
        return {'success': False, 'message': 'SKU is missing.'}

    connection = connect_to_db()
    if connection is None:
        return {'success': False, 'message': 'Failed to connect to the database.'}

    delete_cursor = None
    try:
        delete_cursor = connection.cursor()
        
        # Trim and use case-insensitive matching for database normalization
        clean_sku = sku.strip()
        
        # Delete all rows with this SKU using LOWER(TRIM()) for consistency
        delete_cursor.execute("DELETE FROM products WHERE LOWER(TRIM(SKU)) = LOWER(%s)", (clean_sku,))
        connection.commit()
        
        deleted_rows = delete_cursor.rowcount
        log_msg = f"LOCATION DELETED: SKU={sku}, ROWS_DELETED={deleted_rows}, USER={username}"
        logger.info(log_msg)
        print(f"[DATABASE DELETE] {log_msg}")
        
        return {'success': True, 'message': f'Location deleted successfully ({deleted_rows} records).'}

    except Error as e:
        error_msg = f"Database error during location delete for SKU={sku}: {e}"
        logger.error(error_msg)
        print(f"[DATABASE ERROR] {error_msg}")
        return {'success': False, 'message': 'An error occurred while deleting the location.'}

    finally:
        if delete_cursor:
            delete_cursor.close()
        if connection:
            connection.close()

def update_bulk_location_in_db(skus, location, username='UNKNOWN_USER'):
    """Update location for multiple SKUs in bulk."""
    if not skus or len(skus) == 0:
        return {'success': False, 'message': 'No SKUs provided.'}
    
    if not location:
        return {'success': False, 'message': 'Location is required.'}

    connection = connect_to_db()
    if connection is None:
        return {'success': False, 'message': 'Failed to connect to the database.'}

    update_cursor = None
    updated_count = 0
    try:
        update_cursor = connection.cursor()
        
        for sku in skus:
            if not sku or not sku.strip():
                continue
            
            # Trim the SKU to match database normalization
            clean_sku = sku.strip()
            
            # Split multiple locations by comma ONLY (preserves location keys like "L14-F BOT")
            locations = [loc.strip() for loc in location.split(',') if loc.strip()]
            
            if not locations:
                continue
            
            # Delete existing locations for this SKU
            update_cursor.execute("DELETE FROM products WHERE LOWER(TRIM(SKU)) = LOWER(%s)", (clean_sku,))
            connection.commit()
            deleted_count = update_cursor.rowcount
            
            # Insert new row for each location
            # Extract base style from SKU (M1141 BK → M1141)
            base_style = extract_base_style(clean_sku)
            for loc in locations:
                try:
                    update_cursor.execute(
                        "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())", 
                        (clean_sku, base_style, loc)  # SKU is full code, Style is base code
                    )
                    connection.commit()
                    updated_count += 1
                except Error as e:
                    logger.warning(f"Error inserting location {loc} for SKU {clean_sku}: {e}")
                    print(f"[DATABASE WARNING] Error inserting location {loc} for SKU {clean_sku}: {e}")
                    continue
        
        log_msg = f"BULK LOCATION UPDATE: SKUS={len(skus)}, LOCATIONS={location}, ROWS_UPDATED={updated_count}, USER={username}"
        logger.info(log_msg)
        print(f"[DATABASE BULK UPDATE] {log_msg}")
        
        return {
            'success': True, 
            'message': f'Bulk location updated successfully ({updated_count} records updated).',
            'updated_count': updated_count
        }

    except Error as e:
        error_msg = f"Database error during bulk location update: {e}"
        logger.error(error_msg)
        print(f"[DATABASE ERROR] {error_msg}")
        return {'success': False, 'message': 'An error occurred during bulk location update.'}

    finally:
        if update_cursor:
            update_cursor.close()
        if connection:
            connection.close()

def update_location_in_db(sku, style, location):
    """Update or insert product location in database."""
    if not sku:
        return {'success': False, 'message': 'SKU is missing.'}

    connection = connect_to_db()
    if connection is None:
        return {'success': False, 'message': 'Failed to connect to the database.'}

    check_cursor = None
    update_cursor = None
    try:
        # Trim the SKU to match database normalization
        clean_sku = sku.strip()
        
        # Create a separate cursor for checking and fetch the result
        check_cursor = connection.cursor(dictionary=True)
        # Query by SKU only (locations are per-SKU in the products table)
        check_cursor.execute("SELECT * FROM products WHERE LOWER(TRIM(SKU)) = LOWER(%s) LIMIT 1", (clean_sku,))
        product = check_cursor.fetchone()
        check_cursor.fetchall()  # Consume any remaining results
        check_cursor.close()
        check_cursor = None
        
        # Create a new cursor for updating
        update_cursor = connection.cursor()
        
        # Split multiple locations by comma ONLY (preserves location keys like "L14-F BOT")
        locations = [loc.strip() for loc in location.split(',') if loc.strip()]
        
        if not locations:
            return {'success': False, 'message': 'No valid locations provided.'}

        if product:
            old_location = product.get('Location', 'N/A')
            
            # Delete all existing rows for this SKU
            update_cursor.execute("DELETE FROM products WHERE LOWER(TRIM(SKU)) = LOWER(%s)", (clean_sku,))
            connection.commit()
            
            # Insert new row for each location
            # Extract base style from SKU (M1141 BK → M1141)
            base_style = extract_base_style(clean_sku)
            for loc in locations:
                update_cursor.execute(
                    "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())", 
                    (clean_sku, base_style, loc)  # SKU is full code, Style is base code
                )
                connection.commit()
            
            log_msg = f"LOCATION UPDATED: SKU={sku}, Style={style}, OLD_LOCATION={old_location}, NEW_LOCATION={location}"
            logger.info(log_msg)
            print(f"[DATABASE UPDATE] {log_msg}")
        else:
            # Insert new records - one for each location
            # Extract base style from SKU (M1141 BK → M1141)
            base_style = extract_base_style(clean_sku)
            for loc in locations:
                update_cursor.execute(
                    "INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())", 
                    (clean_sku, base_style, loc)  # SKU is full code, Style is base code
                )
                connection.commit()
            
            log_msg = f"LOCATION INSERTED: SKU={sku}, Style={style}, LOCATION={location}"
            logger.info(log_msg)
            print(f"[DATABASE INSERT] {log_msg}")

        return {'success': True, 'message': 'Location updated successfully.'}

    except Error as e:
        error_msg = f"Database error during location update for SKU={sku}, Style={style}: {e}"
        logger.error(error_msg)
        print(f"[DATABASE ERROR] {error_msg}")
        return {'success': False, 'message': 'An error occurred while updating the location.'}

    finally:
        if check_cursor:
            check_cursor.close()
        if update_cursor:
            update_cursor.close()
        if connection:
            connection.close()

@mysql_connector_bp.route('/update_location', methods=['POST'])
@login_required
def update_location():
    data = request.get_json()
    username = session.get('username', 'UNKNOWN_USER')
    
    # Check if this is a bulk update
    bulk = data.get('bulk', False)
    
    if bulk:
        skus = data.get('skus', [])
        
        # Handle both single location (legacy) and multiple locations (new)
        locations = data.get('locations', [])
        location = data.get('location')  # For backwards compatibility
        
        # If locations array is empty but single location is provided, use it
        if not locations and location:
            locations = [location]
        
        # Convert locations array to comma-separated string for processing
        location_str = ','.join(locations) if locations else ''
        
        # Debug: Print to console
        print(f"[DEBUG] Bulk location update request: SKUS={len(skus)}, LOCATIONS={location_str}, USER={username}")
        
        result = update_bulk_location_in_db(skus, location_str, username)
    else:
        sku = data.get('sku')
        style = data.get('style')
        location = data.get('location')
        delete_flag = data.get('delete', False)
        
        # Debug: Print to console
        print(f"[DEBUG] Location update request: SKU={sku}, STYLE={style}, LOCATION={location}, DELETE={delete_flag}, USER={username}")

        if delete_flag or not location:
            # Delete location from database
            result = delete_location_from_db(sku, username)
        else:
            # Update location in database
            result = update_location_in_db(sku, style, location)
    
    # Log the change with the user who made it
    if result.get('success'):
        log_msg = f"USER={username} - Location change successful"
        logger.info(log_msg)
        print(f"[LOG] {log_msg}")
    else:
        log_msg = f"USER={username} - Location change failed: MESSAGE={result.get('message')}"
        logger.warning(log_msg)
        print(f"[LOG WARNING] {log_msg}")
    
    return jsonify(result)

@mysql_connector_bp.route('/delete_bulk_locations', methods=['POST'])
@login_required
def delete_bulk_locations():
    """Delete all location records for multiple SKUs."""
    data = request.get_json()
    username = session.get('username', 'UNKNOWN_USER')
    skus = data.get('skus', [])
    
    if not skus:
        return jsonify({'success': False, 'message': 'No SKUs provided'})
    
    connection = connect_to_db()
    if connection is None:
        return jsonify({'success': False, 'message': 'Failed to connect to database'})
    
    try:
        cursor = connection.cursor()
        deleted_count = 0
        
        for sku in skus:
            if not sku or not sku.strip():
                continue
            
            clean_sku = sku.strip()
            # Delete all rows for this SKU
            cursor.execute("DELETE FROM products WHERE LOWER(TRIM(SKU)) = LOWER(%s)", (clean_sku,))
            connection.commit()
            deleted_count += cursor.rowcount
            
            print(f"[DATABASE DELETE] Deleted all locations for SKU={clean_sku}, ROWS_DELETED={cursor.rowcount}")
        
        log_msg = f"BULK DELETE LOCATIONS: SKUS={len(skus)}, ROWS_DELETED={deleted_count}, USER={username}"
        logger.info(log_msg)
        print(f"[LOG] {log_msg}")
        
        cursor.close()
        connection.close()
        
        return jsonify({
            'success': True,
            'message': f'Deleted all location records for {len(skus)} SKUs ({deleted_count} rows)',
            'deleted_count': deleted_count
        })
        
    except Error as e:
        error_msg = f"Database error during bulk delete: {e}"
        logger.error(error_msg)
        print(f"[DATABASE ERROR] {error_msg}")
        return jsonify({'success': False, 'message': 'Database error occurred'})
