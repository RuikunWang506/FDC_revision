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
        
        return jsonify({
            'success': True, 
            'message': f'Uploaded {len(uploaded_files)} catalog images for {sku}',
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
        
        return jsonify({
            'success': True,
            'message': f'Uploaded {len(uploaded_files)} big pictures for {sku}',
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
                    else:
                        organizer.process_big_picture_image(str(file_path), sku)
                        processed_files.append(f"Big Picture: {sku}/{file}")
    
    return processed_files


@image_upload_bp.route('/sync_dam', methods=['POST'])
def sync_dam():
    """Sync newly uploaded images with DAM database"""
    try:
        # Import and run DAM sync service
        import subprocess
        import sys
        
        result = subprocess.run([
            sys.executable, 
            'py/utilities/dam_sync_service.py'
        ], capture_output=True, text=True, cwd=os.getcwd())
        
        if result.returncode == 0:
            return jsonify({
                'success': True, 
                'message': 'DAM synchronization completed successfully'
            })
        else:
            return jsonify({
                'success': False, 
                'message': f'DAM sync failed: {result.stderr}'
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
            'smart_auto_filter.py'
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