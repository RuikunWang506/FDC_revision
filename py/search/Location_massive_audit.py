from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app
from py.utilities.database_utils import get_db_connection

location_massive_audit_bp = Blueprint('location_massive_audit', __name__, template_folder='../../templates')

# List of locations (from user)
LOCATIONS = [
    "823", "dock", "L1", "L1-A", "L1-B", "L1-C", "L1-D", "L1-E", "L1-F", "L1-G", "L1-H", "L1-I", "L1-J", "L1-K", "L1-L", "L1-M", "L1-Wall", "dock", "L2", "L2-A", "L2-B", "L2-C", "L2-D", "L2-E", "L2-F", "L2-G", "L2-H", "L2-I", "L2-Wall", "L3", "L3-A", "L3-B", "L3-C", "L3-D", "L3-E", "L3-F", "L3-G", "L3-H", "L3-I", "L3-Wall", "dock", "L4", "L4-A", "L4-B", "L4-C", "L4-D", "L4-E", "L4-F", "L4-G", "L4-H", "L4-I", "L4-J", "L4-K", "L4-L", "L4-Wall", "office", "L22", "L5", "L5-A", "L5-B", "L5-C", "L5-D", "L5-E", "L5-F", "L5-G", "L5-H", "L5-I", "L5-J", "L5-K", "L5-L", "L5-Wall", "L6", "L6-A", "L6-B", "L6-C", "L6-D", "L6-E", "L6-F", "L6-G", "L6-H", "L6-I", "L6-J", "L6-K", "L6-L", "L6-Wall", "L20-D", "L20-C", "L20-B", "L20-A", "L20", "L7", "L7-A", "L7-B", "L7-C", "L7-D", "L7-E", "L7-F", "L7-G", "L7-H", "L7-I", "L7-J", "L7-K", "L7-L", "L7-Wall", "Gate", "19-D", "L19-C", "L19-B", "L19-A", "L19", "L18-D", "L18-C", "L18-B", "L18-A", "L18", "L8", "L8-A", "L8-B", "L8-C", "L8-D", "L8-E", "L8-F", "L8-G", "L8-H", "L8-I", "L8-J", "L8-K", "L8-L", "L8-Wall", "Pile", "L9", "L9-A", "L9-B", "L9-C", "L9-D", "L9-E", "L9-F", "L9-G", "L9-H", "L9-I", "L9-J", "L9-K", "L9-L", "L9-Wall", "L17-D", "L17-C", "L17-B", "L17-A", "L17", "N1151", "N1151", "N1324", "N1116", "L16", "L10", "L10-A", "L10-B", "L10-C", "L10-D", "L10-E", "L10-F", "L10-G", "L10-H", "L10-I", "L10-J", "L10-K", "L10-L", "L11", "L11-A", "L11-B", "L11-C", "L11-D", "L11-E", "L11-F", "L11-G", "L11-H", "L11-I", "L11-J", "L11-K", "L11-L", "N1223", "N1223", "N1223", "crystal", "L15", "storage", "L12", "L12-A", "L12-B", "L12-C", "L12-D", "L12-E", "L12-F", "L12-G", "L12-H", "L12-I", "L12-J", "L12-K", "L12-L", "gate", "L13", "L13-A", "L13-B", "L13-C", "L13-D", "L13-E", "L13-F", "L13-G", "door", "door", "Pile", "office", "L14", "L14-A", "L14-B", "L14-C", "L14-D", "L14-E", "L14-F", "L14-G", "L15-H"]

SUFFIXES = ["BOT", "MID", "TOP"]

def get_available_styles():
    # Example: fetch distinct styles from products table
    connection = get_db_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT DISTINCT Style FROM products")
    styles = [row[0] for row in cursor.fetchall()]
    cursor.close()
    connection.close()
    return styles

@location_massive_audit_bp.route('/location_massive_update', methods=['GET', 'POST'])
def location_massive_update():
    styles = get_available_styles()
    locations_with_suffix = [f"{loc} {suffix}" for loc in LOCATIONS for suffix in SUFFIXES]
    if request.method == 'POST':
        updates = []
        for loc in locations_with_suffix:
            style = request.form.get(f'style_{loc}', '').strip()
            if style:
                updates.append((loc, style))
        if not updates:
            flash('No updates submitted.', 'error')
            return render_template('location_massive_update.html', locations=locations_with_suffix, styles=styles)
        connection = get_db_connection()
        cursor = connection.cursor()
        updated_count = 0
        for loc, style in updates:
            # Update all colors for the given style and location
            update_query = """
                UPDATE products SET Location = %s, Update_Date = NOW()
                WHERE Style = %s
            """
            cursor.execute(update_query, (loc, style))
            updated_count += cursor.rowcount
        connection.commit()
        cursor.close()
        connection.close()
        flash(f'Updated {updated_count} products successfully.', 'message')
        return redirect(url_for('location_massive_audit.location_massive_update'))
    return render_template('location_massive_update.html', locations=locations_with_suffix, styles=styles)
