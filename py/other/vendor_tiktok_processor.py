"""
TikTok vendor tools: SKU ↔ TikTok SKU ID map stored as JSON (dictionary table).
"""
import json
import os

from flask import Blueprint, jsonify, render_template, request, session

from py.utilities.auth import login_required

vendor_tiktok_processor_bp = Blueprint('vendor_tiktok_processor', __name__)

SKU_MAP_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'static', 'tiktok_vendor_sku_map.json'
)


@vendor_tiktok_processor_bp.route('/vendor/tiktok')
@login_required
def vendor_tiktok_home():
    return render_template(
        'vendor_tiktok.html',
        current_user=session.get('username', ''),
    )


@vendor_tiktok_processor_bp.route('/vendor/tiktok/sku_map', methods=['GET'])
@login_required
def tiktok_sku_map_get():
    """Return [{sku, sku_id}, ...] from JSON file."""
    try:
        with open(SKU_MAP_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return jsonify([])
        return jsonify(data)
    except FileNotFoundError:
        return jsonify([])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vendor_tiktok_processor_bp.route('/vendor/tiktok/sku_map', methods=['POST'])
@login_required
def tiktok_sku_map_save():
    """Replace JSON with posted array of {sku, sku_id}."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, list):
            return jsonify({'error': 'Expected JSON array'}), 400
        cleaned = []
        for entry in data:
            sku = str(entry.get('sku', '')).strip()
            if not sku:
                continue
            sku_id = str(entry.get('sku_id', '')).strip()
            cleaned.append({'sku': sku, 'sku_id': sku_id})
        cleaned.sort(key=lambda x: x['sku'].upper())
        with open(SKU_MAP_PATH, 'w', encoding='utf-8') as f:
            json.dump(cleaned, f, indent=2, ensure_ascii=False)
        return jsonify({'saved': len(cleaned)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
