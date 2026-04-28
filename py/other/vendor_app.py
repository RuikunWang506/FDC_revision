from flask import (Blueprint, render_template, session, request,
                   jsonify, send_file)
from io import BytesIO
import json
import os
import uuid
from py.utilities.auth import login_required
from py.other.vendor_erwan_processor import process_erwan_file, process_erwan_preview

vendor_app_bp = Blueprint('vendor_app', __name__)

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}
SKU_DB_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'static', 'erwan_sku_database.json')

# In-memory store for pending downloads {token: (bytes, filename)}
_pending_downloads = {}


def _allowed(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@vendor_app_bp.route('/vendor_app')
@login_required
def vendor_app_home():
    current_user = session.get('username', '')
    return render_template('vendor_app.html', current_user=current_user)


@vendor_app_bp.route('/vendor/erwan')
@login_required
def vendor_erwan():
    current_user = session.get('username', '')
    return render_template('vendor_erwan.html', current_user=current_user)


@vendor_app_bp.route('/vendor/erwan/preview', methods=['POST'])
@login_required
def vendor_erwan_preview():
    """
    Processes the billing file and returns:
      - JSON table data for rendering on the page
      - A download token stored server-side
    """
    billing = request.files.get('billing_file')
    if not billing or not _allowed(billing.filename):
        return jsonify({'error': 'Please upload a valid .xlsx billing file.'}), 400

    billing_bytes = billing.read()
    filename = billing.filename

    # Get preview data (JSON) + excel bytes
    preview_data, error = process_erwan_preview(billing_bytes, filename)
    if error:
        return jsonify({'error': error}), 422

    # Build Excel and store with a token
    result_bytes, excel_error = process_erwan_file(billing_bytes, filename)
    if excel_error:
        return jsonify({'error': excel_error}), 422

    token = str(uuid.uuid4())
    _pending_downloads[token] = (result_bytes, f"Channel_ERWAN_{preview_data['tab_name']}.xlsx")

    preview_data['download_token'] = token
    return jsonify(preview_data)


@vendor_app_bp.route('/vendor/erwan/download/<token>')
@login_required
def vendor_erwan_download(token):
    entry = _pending_downloads.pop(token, None)
    if not entry:
        return jsonify({'error': 'Download expired or invalid. Please process again.'}), 404
    result_bytes, filename = entry
    return send_file(
        BytesIO(result_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@vendor_app_bp.route('/vendor/erwan/sku_db', methods=['GET'])
@login_required
def vendor_erwan_sku_db_get():
    """Return the Erwan SKU database as JSON."""
    try:
        with open(SKU_DB_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vendor_app_bp.route('/vendor/erwan/sku_db', methods=['POST'])
@login_required
def vendor_erwan_sku_db_save():
    """Save the full SKU database JSON (array of {sku, cost, pkg_fee})."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({'error': 'Expected JSON array'}), 400
        # Validate entries minimally
        cleaned = []
        for entry in data:
            sku = str(entry.get('sku', '')).strip()
            if not sku:
                continue
            cleaned.append({
                'sku':     sku,
                'cost':    round(float(entry.get('cost', 0)), 2),
                'msrp':    round(float(entry.get('msrp', 0)), 2),
                'pkg_fee': round(float(entry.get('pkg_fee', 3)), 2),
            })
        cleaned.sort(key=lambda x: x['sku'])
        with open(SKU_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
        return jsonify({'saved': len(cleaned)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vendor_app_bp.route('/vendor/erwan/debug', methods=['POST'])
@login_required
def vendor_erwan_debug():
    """Debug endpoint: returns JSON showing sheet names, column detection, and first rows."""
    import openpyxl
    from io import BytesIO as _BytesIO
    from py.other.vendor_erwan_processor import _find_sheet, _read_sku_qty, _sheet_preview

    billing = request.files.get('billing_file')
    if not billing or not _allowed(billing.filename):
        return jsonify({'error': 'No valid file uploaded'}), 400

    billing_bytes = billing.read()
    try:
        wb = openpyxl.load_workbook(_BytesIO(billing_bytes), data_only=True)
    except Exception as e:
        return jsonify({'error': f'Cannot open file: {e}'}), 422

    result = {'sheets': wb.sheetnames, 'sheet_data': {}}
    for name in wb.sheetnames:
        ws = wb[name]
        data, debug = _read_sku_qty(ws)
        result['sheet_data'][name] = {
            'sku_col': debug['sku_col'],
            'qty_col': debug['qty_col'],
            'header_row': debug['header_row'],
            'rows_found': len(data),
            'first_5_rows': data[:5],
            'preview': debug['preview'][:5],
        }
    return jsonify(result)
