from flask import Blueprint, render_template, request, jsonify, session, send_file
import py.utilities.models
from py.utilities.setting import login_required
from py.utilities.logging_setup import log_event
import paramiko
import io
import os

master_image_viewer_bp = Blueprint('master_image_viewer', __name__)

def find_images_by_sku(sku):
    """Find all images related to a SKU using the DAM system, filesystem, and remote China FTP"""
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
            'dam_other': [],
            'china_ftp': []
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
    
    # Log the search
    user = session.get('username', 'Unknown')
    log_event('info', f"***{user.upper()}***__@@@searched image for SKU@@@__ {sku}")
    
    try:
        results = find_images_by_sku(sku)
        return jsonify(results)
    except Exception as e:
        log_event('error', f"Master Image Viewer API Error for SKU {sku}: {str(e)}")
        return jsonify({'error': f'Search failed: {str(e)}'}), 500

@master_image_viewer_bp.route('/api/fetch_china_ftp')
def fetch_china_ftp():
    """API endpoint to fetch China FTP images for a SKU"""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400
    
    # Log the fetch
    user = session.get('username', 'Unknown')
    log_event('info', f"***{user.upper()}***__@@@fetched China FTP images for SKU@@@__ {sku}")
    
    sku_upper = sku.upper()
    
    try:
        # Fetch images from remote China FTP via SSH
        china_ftp_images = []
        try:
            # SSH connection details
            ssh_host = '219.135.198.131'
            ssh_user = 'fdc'
            ssh_password = 'Bst@93109310'
            
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh_client.connect(ssh_host, username=ssh_user, password=ssh_password)
            
            # Run PowerShell command to list files
            remote_base_path = 'D:\\BST\\BST_PIC\\BSTpic2\\梁佳怡'
            command = f"powershell -command \"Get-ChildItem -Path '{remote_base_path}' -Recurse -File -Filter '{sku_upper}*' | Select-Object FullName\""
            stdin, stdout, stderr = ssh_client.exec_command(command)
            output = stdout.read().decode('gbk', errors='ignore')
            error = stderr.read().decode('gbk', errors='ignore')
            if error:
                log_event('error', f"SSH Error for SKU {sku_upper}: {error}")
            
            # Parse output (skip header, split lines)
            lines = output.strip().split('\n')[2:]  # Skip "FullName" and "--------"
            for line in lines:
                if line.strip():
                    remote_path = line.strip()
                    filename = os.path.basename(remote_path)
                    # Create asset_info-like dict
                    asset_info = {
                        'id': None,  # No DB ID
                        'sku': sku_upper,
                        'version': None,
                        'filename': filename,
                        'source': 'china_ftp',
                        'width': None,
                        'height': None,
                        'is_canonical': False,
                        'created_at': None,
                        'url': f'/china_ftp_image?path={remote_path}&size=800',  # Thumbnail
                        'full_url': f'/china_ftp_image?path={remote_path}&size=2000',  # Full
                        'original_url': f'/china_ftp_image?path={remote_path}&size=0',  # Original
                        'directory': os.path.dirname(remote_path),
                        'relative_path': remote_path.replace(remote_base_path, '').lstrip('\\'),
                        'file_path': remote_path,
                        'file_size': None
                    }
                    china_ftp_images.append(asset_info)
            
            ssh_client.close()
        except Exception as e:
            log_event('error', f"China FTP fetch error for SKU {sku_upper}: {str(e)}")
            return jsonify({'error': f'Failed to fetch China FTP images: {str(e)}'}), 500
        
        return jsonify({'china_ftp': china_ftp_images})
    except Exception as e:
        log_event('error', f"Fetch China FTP API Error for SKU {sku}: {str(e)}")
        return jsonify({'error': f'Fetch failed: {str(e)}'}), 500

@master_image_viewer_bp.route('/china_ftp_image')
def china_ftp_image():
    """Serve images from remote China FTP via SFTP"""
    remote_path = request.args.get('path')
    size = request.args.get('size', '0')  # 0 for original, or resize if needed
    
    if not remote_path:
        return jsonify({'error': 'Path required'}), 400
    
    try:
        # SFTP connection using paramiko
        transport = paramiko.Transport(('219.135.198.131', 22))
        transport.connect(username='fdc', password='Bst@93109310')
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        # Download to memory
        buffer = io.BytesIO()
        sftp.getfo(remote_path, buffer)
        buffer.seek(0)
        sftp.close()
        transport.close()
        # If size != '0', resize using PIL (add import: from PIL import Image)
        # For now, serve original
        return send_file(buffer, mimetype='image/jpeg')  # Adjust mimetype as needed
    except Exception as e:
        return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 500

# Image serving is now handled by the DAM system at /image/{asset_id}/{size}