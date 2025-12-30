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

def update_location_in_db(sku, style, location):
    """Update or insert product location in database."""
    if not sku or not style:
        return {'success': False, 'message': 'SKU or Style is missing.'}

    connection = connect_to_db()
    if connection is None:
        return {'success': False, 'message': 'Failed to connect to the database.'}

    check_cursor = None
    update_cursor = None
    try:
        # Create a separate cursor for checking and fetch the result
        check_cursor = connection.cursor(dictionary=True)
        check_cursor.execute("SELECT * FROM products WHERE SKU = %s AND Style = %s", (sku, style))
        product = check_cursor.fetchone()
        check_cursor.fetchall()  # Consume any remaining results
        check_cursor.close()
        check_cursor = None
        
        # Create a new cursor for updating
        update_cursor = connection.cursor()

        if product:
            old_location = product.get('Location', 'N/A')  # Default to 'N/A' if Location is None
            update_cursor.execute("UPDATE products SET Location = %s, Update_Date = NOW() WHERE SKU = %s AND Style = %s", 
                                 (location, sku, style))
            connection.commit()
        else:
            update_cursor.execute("INSERT INTO products (SKU, Style, Location, Update_Date) VALUES (%s, %s, %s, NOW())", 
                                 (sku, style, location))
            connection.commit()

        return {'success': True, 'message': 'Location updated successfully.'}

    except Error as e:
        logger.error(f"Database error during location update: {e}")
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
    sku = data.get('sku')
    style = data.get('style')
    location = data.get('location')

    result = update_location_in_db(sku, style, location)
    return jsonify(result)