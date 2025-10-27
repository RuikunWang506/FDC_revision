from flask import Blueprint, render_template, request, jsonify, send_file, current_app, url_for,session,redirect
import os
import io
import re
import datetime
import pandas as pd
import pyodbc
from openpyxl import Workbook
from openpyxl.styles import Alignment

from py.utilities.database_utils import get_mysql_connection
from py.utilities.dam_utils import get_canonical_asset_url, search_assets_by_sku_pattern
from py.utilities.setting import Config

bag_style_viewer_bp = Blueprint('bag_style_viewer', __name__, template_folder='templates')

sql_server_config = Config.SQL_SERVER_CONFIG

# ----- Helpers -----

def get_stock_data_for_styles(styles):
    """Query SQL Server to get stock and ocean data for a list of styles"""
    if not styles:
        return {}
    
    query = """
    SELECT 
        i.ALT_CD AS style,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS total_quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS total_ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD IN ({})
    GROUP BY i.ALT_CD
    """.format(','.join(['?' for _ in styles]))
    
    stock_data = {}
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        
        for conn in [conn_fdc, conn_ef]:
            cursor = conn.cursor()
            cursor.execute(query, styles)
            rows = cursor.fetchall()
            for style, quantity, ocean in rows:
                if style:
                    style_upper = style.strip().upper()
                    if style_upper not in stock_data:
                        stock_data[style_upper] = {'quantity': 0, 'ocean': 0}
                    stock_data[style_upper]['quantity'] += (quantity or 0)
                    stock_data[style_upper]['ocean'] += (ocean or 0)
            cursor.close()
            conn.close()
    except Exception as e:
        current_app.logger.error(f"Error getting stock data: {e}")
    
    return stock_data

def get_detailed_stock_for_style(style):
    """Query SQL Server to get stock and ocean data by SKU (style + color) for a single style"""
    
    query = """
    SELECT 
        i.PROD_CD AS sku,
        i.ALT_CD AS style,
        i.UNIT_COLOR AS color,
        SUM(CAST(ISNULL(id.in_stock, 0) AS INT) - CAST(ISNULL(id.ORDER_QTY, 0) AS INT)) AS quantity,
        SUM(CAST(ISNULL(id.on_order_qty, 0) AS INT)) AS ocean
    FROM dbo.inv AS i
    LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
    WHERE i.ALT_CD = ?
    GROUP BY i.PROD_CD, i.ALT_CD, i.UNIT_COLOR
    ORDER BY i.PROD_CD
    """
    
    detailed_stock = []
    try:
        conn_fdc = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        conn_ef = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};'
            f'SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_oms_ef"]};'
            f'UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        
        # Collect data from both databases
        sku_data = {}
        for conn in [conn_fdc, conn_ef]:
            cursor = conn.cursor()
            cursor.execute(query, [style])
            rows = cursor.fetchall()
            for sku, style_code, color, quantity, ocean in rows:
                if sku:
                    sku_key = sku.strip().upper()
                    if sku_key not in sku_data:
                        sku_data[sku_key] = {
                            'sku': sku_key,
                            'color': color.strip() if color else '',
                            'quantity': 0,
                            'ocean': 0
                        }
                    sku_data[sku_key]['quantity'] += (quantity or 0)
                    sku_data[sku_key]['ocean'] += (ocean or 0)
            cursor.close()
            conn.close()
        
        # Convert to list and calculate totals
        for sku_key, data in sku_data.items():
            data['total'] = data['quantity'] + data['ocean']
            detailed_stock.append(data)
        
        # Sort by SKU
        detailed_stock.sort(key=lambda x: x['sku'])
        
    except Exception as e:
        current_app.logger.error(f"Error getting detailed stock data: {e}")
    
    return detailed_stock

def fetch_all_descriptions():
    conn = get_mysql_connection()
    descriptions = []
    if not conn:
        return descriptions
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT description FROM onlinedb.description ORDER BY description")
        rows = cur.fetchall()
        descriptions = [r[0] for r in rows if r[0]]
    finally:
        cur.close()
        conn.close()
    return descriptions

def fetch_styles_by_description(description, stock_filter=None):
    conn = get_mysql_connection()
    results = []
    if not conn:
        return results
    try:
        cur = conn.cursor()
        cur.execute("SELECT style, description FROM onlinedb.description WHERE description = %s ORDER BY style", (description,))
        rows = cur.fetchall()
        
        # Get stock data for all styles if filter is applied
        styles = [row[0] for row in rows]
        stock_data = {}
        if stock_filter and styles:
            stock_data = get_stock_data_for_styles(styles)
        
        for style, desc in rows:
            # Apply stock filter
            if stock_filter:
                style_upper = style.upper()
                stock_info = stock_data.get(style_upper, {'quantity': 0, 'ocean': 0})
                quantity = stock_info['quantity']
                ocean = stock_info['ocean']
                
                if stock_filter == 'instock':
                    if quantity <= 0:
                        continue
                elif stock_filter == 'ocean':
                    if ocean <= 0:
                        continue
                elif stock_filter == 'both':
                    if quantity <= 0 or ocean <= 0:
                        continue
            
            # Try to get image via DAM (catalog_original preferred)
            img_url = get_canonical_asset_url(style, width=800, source_filter='catalog_original')
            if not img_url:
                # Fallback to any source
                assets = search_assets_by_sku_pattern(style, source_filter=['catalog_original', 'dropbox', 'big_picture'])
                if assets:
                    # Use first match
                    img_url = f"/image/{assets[0].id}/800"
            if not img_url:
                img_url = "/static/no_image_available.jpg"
            results.append({
                'style': style,
                'description': desc,
                'image': img_url
            })
    finally:
        cur.close()
        conn.close()
    return results

def fetch_description_for_style(style):
    conn = get_mysql_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT description FROM onlinedb.description WHERE style = %s", (style,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()

def upsert_description(style, description):
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # Try update first
        cur.execute("UPDATE onlinedb.description SET description=%s WHERE style=%s", (description, style))
        if cur.rowcount == 0:
            # Insert
            cur.execute("INSERT INTO onlinedb.description(style, description) VALUES(%s, %s)", (style, description))
        conn.commit()
        return True
    except Exception as e:
        current_app.logger.error(f"upsert_description error: {e}")
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

def delete_description(style):
    conn = get_mysql_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM onlinedb.description WHERE style=%s", (style,))
        conn.commit()
        return True
    except Exception as e:
        current_app.logger.error(f"delete_description error: {e}")
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()

# ----- Routes -----

@bag_style_viewer_bp.route('/bag_style_viewer', methods=['GET'])
def index():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']
    descriptions = fetch_all_descriptions()
    selected_desc = request.args.get('description')
    sku_search = request.args.get('sku')
    stock_filter = request.args.get('stock_filter')  # 'instock', 'ocean', 'both', or None

    styles = []
    search_result = None

    if selected_desc:
        styles = fetch_styles_by_description(selected_desc, stock_filter)

    if sku_search:
        sku = sku_search.strip().upper()
        desc = fetch_description_for_style(sku)
        # Attempt to get image
        img_url = get_canonical_asset_url(sku, width=800, source_filter='catalog_original') or \
                  "/static/no_image_available.jpg"
        search_result = {'style': sku, 'description': desc or 'Not found', 'image': img_url}

    return render_template('bag_style_viewer.html',
                           descriptions=descriptions,
                           selected_desc=selected_desc,
                           styles=styles,
                           search_result=search_result,
                           stock_filter=stock_filter,current_user=current_user)

@bag_style_viewer_bp.route('/bag_style_viewer/update', methods=['POST'])
def update_description():
    data = request.get_json(force=True)
    style = data.get('style', '').strip().upper()
    description = data.get('description', '').strip()
    if not style or not description:
        return jsonify({'success': False, 'message': 'Style and description required'}), 400
    ok = upsert_description(style, description)
    return jsonify({'success': ok})

@bag_style_viewer_bp.route('/bag_style_viewer/stock/<style>', methods=['GET'])
def get_stock_info(style):
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    """Get detailed stock information for a specific style"""
    current_user = session['username']
    
    # Get detailed stock data by SKU (style + color)
    detailed_stock = get_detailed_stock_for_style(style.upper())
    
    # Calculate totals
    total_quantity = sum(item['quantity'] for item in detailed_stock)
    total_ocean = sum(item['ocean'] for item in detailed_stock)
    
    return jsonify({
        'style': style.upper(),
        'total_quantity': total_quantity,
        'total_ocean': total_ocean,
        'total': total_quantity + total_ocean,
        'details': detailed_stock,
        'current_user': current_user
    })

@bag_style_viewer_bp.route('/bag_style_viewer/export', methods=['GET'])
def export_template():
    # Export all description rows with an Action column and instruction row
    conn = get_mysql_connection()
    rows = []
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT style, description FROM onlinedb.description ORDER BY style")
            rows = cur.fetchall()
        finally:
            cur.close()
            conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Descriptions'

    # Row 1 instruction
    ws.append(["Action (DELETE|MODIFY|CREATE)", "Style", "Description"])  # Template header/instruction
    # Freeze top row
    ws.freeze_panes = 'A2'

    # Add data starting row 2 with empty Action
    for style, desc in rows:
        ws.append(["", style, desc])

    # Center headers
    for cell in ws[1]:
        cell.alignment = Alignment(horizontal='center')

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    filename = f"bag_descriptions_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(out, as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@bag_style_viewer_bp.route('/bag_style_viewer/upload', methods=['POST'])
def upload_template():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith(('.xlsx', '.xls')):
        return jsonify({'success': False, 'message': 'Invalid file type'}), 400

    # Read with pandas
    try:
        df = pd.read_excel(f, engine='openpyxl')
    except Exception as e:
        return jsonify({'success': False, 'message': f'Failed to read Excel: {e}'}), 400

    # Expect columns: Action, Style, Description (case-insensitive)
    cols = {c.lower(): c for c in df.columns}
    if not {'action', 'style', 'description'}.issubset(set(cols.keys())):
        return jsonify({'success': False, 'message': 'Missing required columns: Action, Style, Description'}), 400

    actions_applied = []
    for _, row in df.iterrows():
        action = str(row[cols['action']]).strip().lower() if pd.notna(row[cols['action']]) else ''
        style = str(row[cols['style']]).strip().upper() if pd.notna(row[cols['style']]) else ''
        description = str(row[cols['description']]).strip() if pd.notna(row[cols['description']]) else ''
        if not style:
            continue
        if action == 'delete':
            ok = delete_description(style)
        elif action == 'modify':
            ok = upsert_description(style, description)
        elif action == 'create':
            ok = upsert_description(style, description)
        else:
            # Skip rows without action
            continue
        actions_applied.append({'style': style, 'action': action, 'ok': ok})

    return jsonify({'success': True, 'applied': actions_applied})
