from flask import Blueprint, render_template, request, jsonify, session, send_file, redirect, url_for, flash, make_response, current_app
import py.utilities.models
from py.utilities.setting import login_required
from py.utilities.logging_setup import log_event
import paramiko
import io
import os
import zipfile

master_image_viewer_bp = Blueprint('master_image_viewer', __name__)

# TK/external user password (simple hardcoded for demo)
TK_PASSWORD = 'ucandoit'

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

# TK password entry (popup form POST)
@master_image_viewer_bp.route('/tk_password', methods=['POST'])
def tk_password():
    password = request.form.get('password')
    if password == TK_PASSWORD:
        session['tk_user'] = True
        session['internal_user'] = False
        flash('Access granted for TK user.', 'info')
        return redirect(url_for('master_image_viewer.master_image_viewer'))
    else:
        flash('Incorrect password. Please try again.', 'danger')
        return redirect(url_for('master_image_viewer.master_image_viewer'))

# Main page for master image viewer
@master_image_viewer_bp.route("/master_image_viewer/<string:sku>", methods=["GET"])
@master_image_viewer_bp.route('/master_image_viewer', methods=['GET'])
def master_image_viewer(sku=None):
    # Internal user: must be logged in
    current_user = session.get('username')
    internal_user = bool(current_user)
    # If TK/external user, restrict session
    tk_user = session.get('tk_user', False)
    if not internal_user and not tk_user:
        # Show password popup via template logic
        return render_template('master_image_viewer.html', initial_sku=sku)
    # If TK user, restrict access to only this page
    if tk_user:
        session['internal_user'] = False
    else:
        session['internal_user'] = True
    return render_template('master_image_viewer.html', initial_sku=sku, current_user=current_user)
# Image serving is now handled by the DAM system at /image/{asset_id}/{size}

# --- ZIP download endpoint for TK/external users ---
import tempfile
import requests

@master_image_viewer_bp.route('/download_zip', methods=['POST'])
def download_zip():
    # Allow both internal and TK/external users
    data = request.get_json(force=True, silent=True)
    if not (session.get('tk_user') or session.get('username')):
        current_app.logger.error("Unauthorized access attempt to /download_zip")
        return make_response('Unauthorized', 403)
    images = data.get('images', []) if data else []
    if not images:
        current_app.logger.error("No images provided in /download_zip payload")
        return make_response('No images provided', 400)
    # Download images to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        file_paths = []
        for idx, img in enumerate(images):
            # Use file_path if available (works for both internal and TK users)
            if img.get('file_path') and os.path.exists(img['file_path']):
                fpath = img['file_path']
                fname = img['filename']
                dest_path = os.path.join(tmpdir, fname)
                try:
                    with open(fpath, 'rb') as src, open(dest_path, 'wb') as dst:
                        dst.write(src.read())
                    file_paths.append(dest_path)
                except Exception as e:
                    current_app.logger.error(f"Exception copying {fpath}: {str(e)}")
                    continue
            # Fallback: fetch via HTTP (for China FTP or other remote images)
            elif img.get('original_url') and (img['original_url'].startswith('/image/') or img['original_url'].startswith('/china_ftp_image')):
                full_url = request.host_url.rstrip('/') + img['original_url']
                try:
                    resp = requests.get(full_url, stream=True)
                    if resp.status_code == 200:
                        fname = img['filename']
                        fpath = os.path.join(tmpdir, fname)
                        with open(fpath, 'wb') as f:
                            for chunk in resp.iter_content(4096):
                                f.write(chunk)
                        file_paths.append(fpath)
                    else:
                        current_app.logger.error(f"Failed to download {full_url}: status {resp.status_code}")
                except Exception as e:
                    current_app.logger.error(f"Exception downloading {full_url}: {str(e)}")
                    continue
            else:
                current_app.logger.error(f"Invalid image entry: {img}")
                continue
        # Create zip
        zip_path = os.path.join(tmpdir, 'FDC_images.zip')
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for fpath in file_paths:
                zipf.write(fpath, arcname=os.path.basename(fpath))
        # Send zip file (read into memory to avoid Windows temp file lock)
        with open(zip_path, 'rb') as f:
            zip_bytes = f.read()
        return send_file(
            io.BytesIO(zip_bytes),
            mimetype='application/zip',
            as_attachment=True,
            download_name='FDC_images.zip'
        )

@master_image_viewer_bp.route('/api/search_images')
def search_images():
    """API endpoint to search for images by SKU"""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400
    
    # Log the search
    user = session.get('username')
    tk_user = session.get('tk_user', False)
    if tk_user:
        log_event('info', f"***TK_USER***__@@@searched image for SKU@@@__ {sku}")
    else:
        log_event('info', f"***{(user or 'Unknown').upper()}***__@@@searched image for SKU@@@__ {sku}")
    
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
        current_app.logger.error("China FTP image: No path provided")
        return jsonify({'error': 'Path required'}), 400
    
    try:
        current_app.logger.info(f"China FTP image: Attempting to fetch {remote_path}")
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
        
        current_app.logger.info(f"China FTP image: Successfully fetched {remote_path}")
        # If size != '0', resize using PIL (add import: from PIL import Image)
        # For now, serve original
        
        # Determine mimetype based on file extension
        filename = os.path.basename(remote_path).lower()
        if filename.endswith('.png'):
            mimetype = 'image/png'
        elif filename.endswith('.gif'):
            mimetype = 'image/gif'
        elif filename.endswith('.bmp'):
            mimetype = 'image/bmp'
        else:
            mimetype = 'image/jpeg'  # Default to JPEG
        
        return send_file(buffer, mimetype=mimetype)
    except paramiko.AuthenticationException as e:
        current_app.logger.error(f"China FTP image: Authentication failed for {remote_path}: {str(e)}")
        return jsonify({'error': f'Authentication failed: {str(e)}'}), 500
    except paramiko.SSHException as e:
        current_app.logger.error(f"China FTP image: SSH connection failed for {remote_path}: {str(e)}")
        return jsonify({'error': f'SSH connection failed: {str(e)}'}), 500
    except FileNotFoundError as e:
        current_app.logger.error(f"China FTP image: File not found {remote_path}: {str(e)}")
        return jsonify({'error': f'File not found: {str(e)}'}), 404
    except Exception as e:
        current_app.logger.error(f"China FTP image: Failed to fetch {remote_path}: {str(e)}")
        return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 500

# Image serving is now handled by the DAM system at /image/{asset_id}/{size}
 