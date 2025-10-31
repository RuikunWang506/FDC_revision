from flask import Blueprint, render_template, request, jsonify, session
import py.utilities.models
from py.utilities.setting import login_required

master_image_viewer_bp = Blueprint('master_image_viewer', __name__)

def find_images_by_sku(sku):
    """Find all images related to a SKU using the DAM system and filesystem"""
    if not sku:
        return {'error': 'SKU is required'}
    
    sku_upper = sku.upper()
    
    try:
        db = py.utilities.models.SessionLocal()
        
        # Get all active assets matching the SKU pattern (like product_image does)
        assets = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.sku.ilike(f'%{sku_upper}%'),
            py.utilities.models.ImageAsset.is_active == True
        ).order_by(py.utilities.models.ImageAsset.sku, py.utilities.models.ImageAsset.version).all()
        
        # Group assets by source - use actual sources from DAM
        grouped_images = {
            'dam_dropbox': [],
            'dam_catalog': [],  # This includes catalog images from item_lookup
            'dam_big_picture': [],  # This includes product images from product_image
            'filesystem_missing': [],  # Images found in filesystem but not in DAM
            'dam_other': []
        }
        
        import os
        for asset in assets:
            # Skip if file does not exist (for assets with a local path)
            if hasattr(asset, 'path') and asset.path:
                if not os.path.exists(asset.path):
                    continue
            # Extract directory information for better organization
            directory = None
            relative_path = None
            if hasattr(asset, 'path') and asset.path:
                directory = os.path.dirname(asset.path)
                # Get relative path from common base directories
                common_bases = [
                    r'C:\Users\Stream\Dropbox\Pictures-China (1)',
                    r'C:\users\stream\Dropbox\Pictures-China (1)',
                    r'\\192.168.1.8\public'
                ]
                for base in common_bases:
                    if asset.path.startswith(base):
                        relative_path = os.path.relpath(asset.path, base)
                        break
                if not relative_path and directory:
                    relative_path = os.path.basename(directory)
            asset_info = {
                'id': asset.id,
                'sku': asset.sku,
                'version': asset.version,
                'filename': asset.filename,
                'source': asset.source,
                'width': asset.width,
                'height': asset.height,
                'is_canonical': asset.is_canonical,
                'created_at': asset.created_at.isoformat() if asset.created_at else None,
                'url': f'/image/{asset.id}/800',
                'full_url': f'/image/{asset.id}/2000',
                'original_url': f'/image/{asset.id}/0',
                'directory': directory,
                'relative_path': relative_path,
                'file_path': asset.path if hasattr(asset, 'path') else None,
                'file_size': asset.file_size if hasattr(asset, 'file_size') else None
            }
            source = asset.source.lower() if asset.source else 'unknown'
            if source == 'dropbox':
                grouped_images['dam_dropbox'].append(asset_info)
            elif source == 'catalog':
                grouped_images['dam_catalog'].append(asset_info)
            elif source == 'big_picture':
                grouped_images['dam_big_picture'].append(asset_info)
            else:
                grouped_images['dam_other'].append(asset_info)
        
        # Calculate totals
        total_count = len(assets)
        grouped_images['total_count'] = total_count
        grouped_images['sku'] = sku_upper
        
        return grouped_images
        
    except Exception as e:
        return {'error': f'Database error: {str(e)}'}
    finally:
        db.close()
@master_image_viewer_bp.route("/master_image_viewer/<string:sku>", methods=["GET"])
@master_image_viewer_bp.route('/master_image_viewer')
def master_image_viewer(sku=None):
    current_user = session['username']

    """Main page for master image viewer"""
    # If SKU is provided via URL, pass it to the template for auto-search
    return render_template('master_image_viewer.html', initial_sku=sku, current_user=current_user)

@master_image_viewer_bp.route('/api/search_images')
def search_images():
    """API endpoint to search for images by SKU"""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400
    
    try:
        results = find_images_by_sku(sku)
        print(f"Master Image Viewer API - SKU: {sku}, Found {results.get('total_count', 0)} images")
        return jsonify(results)
    except Exception as e:
        print(f"Master Image Viewer API Error: {str(e)}")
        return jsonify({'error': f'Search failed: {str(e)}'}), 500

# Image serving is now handled by the DAM system at /image/{asset_id}/{size}