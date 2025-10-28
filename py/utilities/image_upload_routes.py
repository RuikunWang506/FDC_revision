"""
Image Bulk Upload Web Interface
"""
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
import os
import re
from werkzeug.utils import secure_filename
from PIL import Image
import zipfile
import tempfile
from pathlib import Path
import shutil
from datetime import datetime

from py.utilities.database_utils import get_mysql_connection
from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2

image_upload_bp = Blueprint('image_upload', __name__)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@image_upload_bp.route('/image_upload')
def bulk_upload_page():
    """Main bulk upload page"""
    return render_template('image_upload.html')

@image_upload_bp.route('/upload_catalog', methods=['POST'])
def upload_catalog_images():
    """Handle catalog images upload"""
    try:
        files = request.files.getlist('catalog_files')
        sku = request.form.get('sku', '').strip().upper()
        
        if not sku:
            return jsonify({'success': False, 'message': 'SKU is required'})
        
        uploaded_files = []
        saved_paths = []
        catalog_dir = r"D:\Catalog_pictures\catalog_original"
        os.makedirs(catalog_dir, exist_ok=True)
        
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                
                # Determine version number
                version_num = get_next_catalog_version(sku, catalog_dir)
                
                if version_num == 1:
                    new_filename = f"{sku}.jpg"
                else:
                    new_filename = f"{sku}_v{version_num}.jpg"
                
                file_path = os.path.join(catalog_dir, new_filename)
                file.save(file_path)
                
                # Optimize image
                optimize_image(file_path)
                
                uploaded_files.append(new_filename)
                saved_paths.append(file_path)

        # Auto-record in DAM DB
        if saved_paths:
            try:
                _record_catalog_uploads(saved_paths, sku)
            except Exception as e:
                # Log but don't fail the upload response
                print(f"Auto-record (catalog) failed: {e}")
        
        return jsonify({
            'success': True, 
            'message': f'Uploaded {len(uploaded_files)} catalog images for {sku}. Recorded in DAM.',
            'files': uploaded_files
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@image_upload_bp.route('/upload_big_pictures', methods=['POST'])
def upload_big_pictures():
    """Handle big picture images upload"""
    try:
        files = request.files.getlist('big_picture_files')
        sku = request.form.get('sku', '').strip().upper()
        
        if not sku:
            return jsonify({'success': False, 'message': 'SKU is required'})
        
        uploaded_files = []
        saved_paths = []
        big_picture_dir = os.path.join(r"D:\Individual_big_picture", sku)
        os.makedirs(big_picture_dir, exist_ok=True)
        
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(big_picture_dir, filename)
                
                # Handle naming conflicts
                counter = 1
                original_path = file_path
                while os.path.exists(file_path):
                    name, ext = os.path.splitext(original_path)
                    file_path = f"{name}_{counter:02d}{ext}"
                    counter += 1
                
                file.save(file_path)
                optimize_image(file_path)
                uploaded_files.append(os.path.basename(file_path))
                saved_paths.append(file_path)

        # Auto-record in DAM DB
        if saved_paths:
            try:
                _record_big_picture_uploads(saved_paths, sku)
            except Exception as e:
                print(f"Auto-record (big_picture) failed: {e}")
        
        return jsonify({
            'success': True,
            'message': f'Uploaded {len(uploaded_files)} big pictures for {sku}. Recorded in DAM.',
            'files': uploaded_files
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@image_upload_bp.route('/upload_zip', methods=['POST'])
def upload_zip_archive():
    """Handle ZIP file upload with automatic organization"""
    try:
        zip_file = request.files.get('zip_file')
        
        if not zip_file or not zip_file.filename.endswith('.zip'):
            return jsonify({'success': False, 'message': 'Please upload a ZIP file'})
        
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, 'upload.zip')
            zip_file.save(zip_path)
            
            # Extract ZIP
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Process extracted files
            processed_files = process_extracted_files(temp_dir)
            
        return jsonify({
            'success': True,
            'message': f'Processed ZIP archive with {len(processed_files)} images',
            'files': processed_files
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

def get_next_catalog_version(sku, catalog_dir):
    """Get next version number for catalog image"""
    existing_files = list(Path(catalog_dir).glob(f"{sku}*.jpg"))
    version_numbers = [1]  # Start with version 1
    
    for file_path in existing_files:
        filename = file_path.stem
        if filename == sku:
            continue  # Main version
        else:
            match = re.search(r'_v(\d+)$', filename)
            if match:
                version_numbers.append(int(match.group(1)) + 1)
    
    return max(version_numbers)

def optimize_image(file_path):
    """Optimize image for web and storage"""
    try:
        with Image.open(file_path) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            # Optimize quality and size
            img.save(file_path, 'JPEG', quality=85, optimize=True)
            
    except Exception as e:
        print(f"Error optimizing image {file_path}: {e}")

def process_extracted_files(temp_dir):
    """Process files extracted from ZIP"""
    from py.utilities.image_organizer_service import ImageOrganizerService
    
    organizer = ImageOrganizerService()
    processed_files = []
    
    for root, dirs, files in os.walk(temp_dir):
        for file in files:
            file_path = Path(root) / file
            
            if file_path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff'}:
                sku = organizer.extract_sku_from_path(str(file_path))
                
                if sku:
                    if organizer.is_catalog_image(str(file_path)):
                        organizer.process_catalog_image(str(file_path), sku)
                        processed_files.append(f"Catalog: {sku}/{file}")
                        # Note: DAM DB recording for ZIP uploads is handled by full sync; per-file would require detecting exact dest path
                    else:
                        organizer.process_big_picture_image(str(file_path), sku)
                        processed_files.append(f"Big Picture: {sku}/{file}")
                        # Note: DAM DB recording for ZIP uploads is handled by full sync
    
    return processed_files


# ===== Helper functions: auto-record uploads into DAM DB =====
def _get_image_info_and_hash(file_path: str):
    svc = DAMSyncServiceV2()
    info = svc.get_image_info(file_path)
    sha256 = svc.calculate_file_hash(file_path) if info else None
    return info, sha256

def _record_catalog_uploads(file_paths, sku: str):
    conn = get_mysql_connection()
    if not conn:
        raise RuntimeError('Cannot connect to MySQL')
    cur = conn.cursor()
    try:
        for file_path in file_paths:
            filename = os.path.basename(file_path)
            info, sha256 = _get_image_info_and_hash(file_path)
            if not info or not sha256:
                continue
            # Check existing by hash
            cur.execute("SELECT id, source, path FROM image_assets WHERE sha256 = %s", (sha256,))
            row = cur.fetchone()
            if row:
                img_id, existing_source, existing_path = row
                if existing_source in ['dropbox', 'big_picture']:
                    # Upgrade to catalog_original
                    import re
                    m = re.search(r'_v(\d+)', os.path.splitext(filename)[0])
                    version = int(m.group(1)) if m else 1
                    is_canonical = os.path.splitext(filename)[0] == sku
                    cur.execute("""
                        UPDATE image_assets SET 
                            sku=%s, version=%s, filename=%s, path=%s, source=%s,
                            width=%s, height=%s, file_size=%s,
                            is_active=TRUE, is_canonical=%s, updated_at=%s
                        WHERE id=%s
                    """, (
                        sku, version, filename, file_path, 'catalog_original',
                        info['width'], info['height'], info['size_bytes'],
                        is_canonical, datetime.now(), img_id
                    ))
                else:
                    # Already catalog-original (or other), no action
                    pass
                continue
            # Check by filename/source
            cur.execute(
                "SELECT id, is_active, path FROM image_assets WHERE filename=%s AND source=%s",
                (filename, 'catalog_original')
            )
            row = cur.fetchone()
            if row:
                img_id, is_active, existing_path = row
                if (not is_active) or (existing_path != file_path):
                    cur.execute("""
                        UPDATE image_assets SET 
                            path=%s, is_active=TRUE,
                            width=%s, height=%s, file_size=%s, sha256=%s, updated_at=%s
                        WHERE id=%s
                    """, (
                        file_path, info['width'], info['height'], info['size_bytes'], sha256, datetime.now(), img_id
                    ))
                continue
            # Insert new
            import re
            m = re.search(r'_v(\d+)', os.path.splitext(filename)[0])
            version = int(m.group(1)) if m else 1
            is_canonical = os.path.splitext(filename)[0] == sku
            cur.execute("""
                INSERT INTO image_assets
                (sku, version, filename, path, source, width, height, file_size, sha256,
                 is_active, is_canonical, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                sku, version, filename, file_path, 'catalog_original',
                info['width'], info['height'], info['size_bytes'], sha256,
                True, is_canonical, datetime.now(), datetime.now()
            ))
        conn.commit()
        # Update canonical for this SKU
        _update_canonical_for_sku(conn, sku)
        conn.commit()
    finally:
        cur.close()
        conn.close()

def _record_big_picture_uploads(file_paths, sku: str):
    conn = get_mysql_connection()
    if not conn:
        raise RuntimeError('Cannot connect to MySQL')
    cur = conn.cursor()
    try:
        for file_path in file_paths:
            filename = os.path.basename(file_path)
            info, sha256 = _get_image_info_and_hash(file_path)
            if not info or not sha256:
                continue
            # If hash exists with higher or equal priority, skip
            cur.execute("SELECT id, source FROM image_assets WHERE sha256=%s", (sha256,))
            row = cur.fetchone()
            if row:
                _, existing_source = row
                # Skip if duplicate content exists in any source
                if existing_source in ['catalog_original', 'dropbox', 'big_picture']:
                    continue
            # If already exists by filename/source/sku, skip
            cur.execute(
                "SELECT id FROM image_assets WHERE filename=%s AND source=%s AND sku=%s",
                (filename, 'big_picture', sku)
            )
            if cur.fetchone():
                continue
            # Insert new
            cur.execute("""
                INSERT INTO image_assets
                (sku, version, filename, path, source, width, height, file_size, sha256,
                 is_active, is_canonical, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                sku, 1, filename, file_path, 'big_picture',
                info['width'], info['height'], info['size_bytes'], sha256,
                True, False, datetime.now(), datetime.now()
            ))
        conn.commit()
    finally:
        cur.close()
        conn.close()

def _update_canonical_for_sku(conn, sku: str):
    cur = conn.cursor()
    try:
        # Clear all canonical for SKU
        cur.execute("UPDATE image_assets SET is_canonical=FALSE WHERE sku=%s", (sku,))
        # Set best catalog_original (highest resolution)
        cur.execute("""
            SELECT id FROM image_assets
            WHERE sku=%s AND source='catalog_original' AND is_active=TRUE
            ORDER BY width*height DESC, filename ASC
            LIMIT 1
        """, (sku,))
        row = cur.fetchone()
        if row:
            best_id = row[0]
            cur.execute("UPDATE image_assets SET is_canonical=TRUE WHERE id=%s", (best_id,))
    finally:
        cur.close()


@image_upload_bp.route('/sync_dam', methods=['POST'])
def sync_dam():
    """Run Smart Auto-Filter (fetch from Dropbox) then sync DAM database"""
    try:
        import subprocess
        import sys

        # Helper to run a Python script and capture output
        def run_script(script_path):
            return subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                cwd=os.getcwd()
            )

        # 1) Run Smart Auto-Filter (incremental fetch from Dropbox → catalog_original / big pictures)
        auto_filter_result = run_script('py/standalone/smart_auto_filter.py')

        # 2) Run DAM Sync V2 to update database from organized folders (with catalog_original priority)
        dam_sync_result = run_script('py/utilities/dam_sync_service_v2.py')

        # Prepare combined response
        success = (dam_sync_result.returncode == 0)

        # Build a concise message; prefer stderr details if failures occur
        messages = []
        if auto_filter_result.returncode == 0:
            messages.append('Smart Auto-Filter completed successfully.')
        else:
            messages.append(f"Smart Auto-Filter failed: {auto_filter_result.stderr.strip() or 'See server logs.'}")

        if dam_sync_result.returncode == 0:
            messages.append('DAM synchronization completed successfully.')
        else:
            messages.append(f"DAM sync failed: {dam_sync_result.stderr.strip() or 'See server logs.'}")

        # Optionally include brief stdout tails for visibility
        details = {
            'auto_filter_stdout': auto_filter_result.stdout[-2000:],
            'auto_filter_stderr': auto_filter_result.stderr[-2000:],
            'dam_sync_stdout': dam_sync_result.stdout[-2000:],
            'dam_sync_stderr': dam_sync_result.stderr[-2000:],
        }

        return jsonify({
            'success': success,
            'message': ' '.join(messages),
            'details': details
        })

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/run_auto_filter', methods=['POST'])
def run_auto_filter():
    """Run the smart auto filter on Dropbox images"""
    try:
        import subprocess
        import sys
        
        result = subprocess.run([
            sys.executable, 
            'py/standalone/smart_auto_filter.py'
        ], capture_output=True, text=True, cwd=os.getcwd())
        
        if result.returncode == 0:
            # Parse output to get count of processed files
            output_lines = result.stdout.split('\n')
            processed_count = 0
            
            for line in output_lines:
                if 'New images found and processed:' in line:
                    try:
                        processed_count = int(line.split(':')[1].strip())
                    except:
                        processed_count = 0
                        
            return jsonify({
                'success': True, 
                'message': 'Auto-filter completed successfully',
                'count': processed_count
            })
        else:
            return jsonify({
                'success': False, 
                'message': f'Auto-filter failed: {result.stderr}'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/view_logs')
def view_logs():
    """Redirect to log viewer"""
    return redirect('/log_viewer')