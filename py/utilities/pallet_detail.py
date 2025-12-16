from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, session, jsonify
from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config
import json
from datetime import datetime
import hashlib


pallet_detail_bp = Blueprint('pallet_detail', __name__, template_folder='../../templates')

def get_all_pallet_names():
    """Get all unique pallet names from database"""
    try:
        connection = get_mysql_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT DISTINCT location FROM pallet_details ORDER BY location")
        pallets = [row[0] for row in cursor.fetchall()]
        cursor.close()
        connection.close()
        return pallets
    except Exception as e:
        return []

def encode_location(location):
    """Generate a 5-character hash code from location name"""
    hash_object = hashlib.md5(location.encode())
    hash_hex = hash_object.hexdigest()
    # Take first 5 characters and make uppercase for readability
    return hash_hex[:5].upper()

def decode_location(code):
    """Find location that matches the given hash code"""
    all_pallets = get_all_pallet_names()
    for pallet in all_pallets:
        if encode_location(pallet) == code.upper():
            return pallet
    return None

@pallet_detail_bp.route('/p/<code>')
def pallet_short_url(code):
    """Redirect short URL to full pallet detail page"""
    try:
        location = decode_location(code)
        if location is None:
            flash(f'Invalid short code', 'error')
            return redirect(url_for('pallet_detail.pallet_detail_root'))
        return redirect(url_for('pallet_detail.pallet_detail', location_suffix=location))
    except Exception as e:
        flash(f'Invalid short URL', 'error')
        return redirect(url_for('pallet_detail.pallet_detail_root'))

@pallet_detail_bp.route('/palletdetail/')
def pallet_detail_root():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    all_pallets = get_all_pallet_names()
    
    return render_template('pallet_detail.html',
                         location_suffix="",
                         all_pallets=all_pallets,
                         pallet_items=[],
                         current_user=current_user)
    
@pallet_detail_bp.route('/palletdetail/<path:location_suffix>')
def pallet_detail(location_suffix):
    """Display detailed pallet view for a specific pallet identifier"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    # Decode the location_suffix from URL
    location_suffix = location_suffix.strip()
    
    # Get all existing pallets for sidebar
    all_pallets = get_all_pallet_names()
    
    # Get existing pallet items from database
    connection = get_mysql_connection()
    cursor = connection.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT id, item_text, item_quantity, item_memo, created_at, updated_at
        FROM pallet_details
        WHERE location = %s
        ORDER BY id ASC
    """, (location_suffix,))
    pallet_items = cursor.fetchall()
    # If no items exist for this pallet, create an empty row so the pallet is registered
    if not pallet_items:
        cursor2 = connection.cursor()
        cursor2.execute("""
            INSERT INTO pallet_details (location, item_text, item_quantity, item_memo, created_at, updated_at)
            VALUES (%s, '', 0, '', NOW(), NOW())
        """, (location_suffix,))
        connection.commit()
        cursor2.close()
        # Re-fetch the empty row
        cursor.execute("""
            SELECT id, item_text, item_quantity, item_memo, created_at, updated_at
            FROM pallet_details
            WHERE location = %s
            ORDER BY id ASC
        """, (location_suffix,))
        pallet_items = cursor.fetchall()
    # Get last modified date for this pallet (max updated_at)
    cursor.execute("""
        SELECT MAX(updated_at) as last_modified
        FROM pallet_details
        WHERE location = %s
    """, (location_suffix,))
    result = cursor.fetchone()
    last_modified = result['last_modified'] if result and result['last_modified'] else None
    cursor.close()
    connection.close()
    # Generate short URL for this pallet
    short_code = encode_location(location_suffix)
    short_url = f"/p/{short_code}"
    return render_template('pallet_detail.html',
                         location_suffix=location_suffix,
                         all_pallets=all_pallets,
                         pallet_items=pallet_items,
                         current_user=current_user,
                         short_url=short_url,
                         last_modified=last_modified)


@pallet_detail_bp.route('/palletdetail/save', methods=['POST'])
def save_pallet_detail():
    """Save pallet detail items for a location"""
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    try:
        data = request.get_json()
        location = data.get('location', '').strip()
        items = data.get('items', [])
        
        if not location:
            return jsonify({'success': False, 'message': 'Location is required'}), 400
        
        connection = get_mysql_connection()
        cursor = connection.cursor()
        # Delete existing items for this location
        cursor.execute("DELETE FROM pallet_details WHERE location = %s", (location,))
        # Insert new items or create empty pallet
        inserted_count = 0
        if items:
            for item in items:
                item_text = item.get('text', '').strip()
                item_quantity = item.get('quantity', 0)
                item_memo = item.get('memo', '').strip()
                if item_text or item_quantity or item_memo:
                    cursor.execute("""
                        INSERT INTO pallet_details (location, item_text, item_quantity, item_memo, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, NOW(), NOW())
                    """, (location, item_text, item_quantity, item_memo))
                    inserted_count += 1
        if inserted_count == 0:
            # Create empty pallet row
            cursor.execute("""
                INSERT INTO pallet_details (location, item_text, item_quantity, item_memo, created_at, updated_at)
                VALUES (%s, '', 0, '', NOW(), NOW())
            """, (location,))
        # Log the modification with full content using Flask logger
        username = session.get('username', 'unknown')
        log_content = json.dumps(items)
        current_app.logger.info(f"Pallet modified: location={location}, user={username}, items={log_content}")
        connection.commit()
        cursor.close()
        connection.close()
        return jsonify({
            'success': True,
            'message': f'Saved {inserted_count} items for {location}'
        })
        
    except Exception as e:
        current_app.logger.error(f"Error saving pallet detail: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@pallet_detail_bp.route('/palletdetail/delete/<int:item_id>', methods=['POST'])
def delete_pallet_item(item_id):
    """Delete a specific pallet item"""
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    try:
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        cursor.execute("DELETE FROM pallet_details WHERE id = %s", (item_id,))
        
        connection.commit()
        deleted = cursor.rowcount > 0
        cursor.close()
        connection.close()
        
        if deleted:
            return jsonify({'success': True, 'message': 'Item deleted'})
        else:
            return jsonify({'success': False, 'message': 'Item not found'}), 404
            
    except Exception as e:
        current_app.logger.error(f"Error deleting pallet item: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500


@pallet_detail_bp.route('/palletdetail/delete_pallet/<path:pallet_name>', methods=['POST'])
def delete_pallet(pallet_name):
    """Delete an entire pallet and all its items"""
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    try:
        connection = get_mysql_connection()
        cursor = connection.cursor()
        
        cursor.execute("DELETE FROM pallet_details WHERE location = %s", (pallet_name,))
        
        connection.commit()
        deleted_count = cursor.rowcount
        cursor.close()
        connection.close()
        
        if deleted_count > 0:
            return jsonify({'success': True, 'message': f'Deleted pallet "{pallet_name}" with {deleted_count} items'})
        else:
            return jsonify({'success': True, 'message': 'Pallet deleted'})
            
    except Exception as e:
        current_app.logger.error(f"Error deleting pallet: {e}")
        return jsonify({'success': False, 'message': f'Error: {str(e)}'}), 500
