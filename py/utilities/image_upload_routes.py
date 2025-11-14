"""
Image Bulk Upload Web Interface
"""
from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, session
import os
import re
from werkzeug.utils import secure_filename
from PIL import Image
import zipfile
import tempfile
from pathlib import Path
import shutil
from datetime import datetime
import logging

from py.utilities.database_utils import get_mysql_connection
from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2

# Set up logger
logger = logging.getLogger(__name__)

image_upload_bp = Blueprint('image_upload', __name__)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'webp'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@image_upload_bp.route('/image_upload')
def bulk_upload_page():
    """Main bulk upload page"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    current_user = session['username']
    
    return render_template('image_upload.html',current_user=current_user)

@image_upload_bp.route('/upload_bulk_catalog', methods=['POST'])
def upload_bulk_catalog():
    """Handle bulk catalog images upload - supports multiple files, zip/rar archives"""
    try:
        files = request.files.getlist('catalog_files')
        
        if not files or len(files) == 0:
            return jsonify({'success': False, 'message': 'No files uploaded'})
        
        # Create session-specific temp directory
        session_id = session.get('username', 'anonymous') + '_' + datetime.now().strftime('%Y%m%d%H%M%S')
        temp_base = os.path.join(tempfile.gettempdir(), 'fdc_bulk_uploads')
        os.makedirs(temp_base, exist_ok=True)
        temp_dir = os.path.join(temp_base, session_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Process each file
        extracted_files = []
        for file in files:
            filename = secure_filename(file.filename)
            file_path = os.path.join(temp_dir, filename)
            file.save(file_path)
            
            # Check if it's an archive
            if filename.lower().endswith('.zip'):
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                os.remove(file_path)  # Remove the zip file after extraction
            elif filename.lower().endswith('.rar'):
                try:
                    import rarfile
                    with rarfile.RarFile(file_path, 'r') as rar_ref:
                        rar_ref.extractall(temp_dir)
                    os.remove(file_path)
                except ImportError:
                    return jsonify({'success': False, 'message': 'RAR support not installed. Please install rarfile package.'})
            elif allowed_file(filename):
                extracted_files.append(file_path)
        
        # Analyze extracted files
        from py.utilities.image_organizer_service import ImageOrganizerService
        
        organizer = ImageOrganizerService()
        
        # Collect all image files from temp directory
        # Duplicates are checked and removed immediately to save disk space
        image_data = []
        duplicates = []
        for root, dirs, files_in_dir in os.walk(temp_dir):
            for file in files_in_dir:
                file_path = os.path.join(root, file)
                
                if Path(file_path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                    # Extract SKU from filename or path
                    sku = organizer.extract_sku_from_path(str(file_path))
                    
                    # Create relative path for display
                    rel_path = os.path.relpath(file_path, temp_dir)
                    
                    # Check if duplicate IMMEDIATELY
                    is_dup, dup_info = _check_duplicate_by_hash(file_path)
                    
                    if is_dup:
                        duplicates.append({
                            'filename': file,
                            'path': rel_path,
                            'sku': sku or 'UNKNOWN',
                            'duplicate_of': dup_info
                        })
                        # DELETE duplicate file immediately to save disk space
                        try:
                            os.remove(file_path)
                            logger.info(f"  [DUPLICATE REMOVED] {file} - already exists in {dup_info.get('source', 'database')}")
                        except Exception as e:
                            logger.error(f"  [ERROR] Could not remove duplicate {file}: {e}")
                    else:
                        image_data.append({
                            'filename': file,
                            'path': rel_path,
                            'sku': sku or 'UNKNOWN'
                        })
        
        if not image_data and not duplicates:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'success': False, 'message': 'No valid images found in uploaded files'})
        
        # Store session info for later processing
        session[f'bulk_catalog_session_{session_id}'] = {
            'temp_dir': temp_dir,
            'image_count': len(image_data),
            'duplicates_count': len(duplicates)
        }
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'images': image_data,
            'duplicates': duplicates,
            'message': f'Found {len(image_data)} new images. Skipped {len(duplicates)} duplicates. Please review and confirm.'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/upload_bulk_detail', methods=['POST'])
def upload_bulk_detail():
    """Handle bulk detail images upload - folders named by style containing images"""
    # Debug: Write directly to file to verify function is called
    with open('C:\\FDC_FLASK_APP\\debug_upload.txt', 'a', encoding='utf-8') as f:
        f.write(f"UPLOAD CALLED: {datetime.now()}\n")
    
    logger.info("=== BULK DETAIL UPLOAD ACCESSED ===")
    try:
        files = request.files.getlist('detail_files')
        
        with open('C:\\FDC_FLASK_APP\\debug_upload.txt', 'a', encoding='utf-8') as f:
            f.write(f"Files received: {len(files) if files else 0}\n")
            if files:
                for i, file in enumerate(files[:3]):  # Log first 3 files
                    f.write(f"  File {i}: {file.filename}\n")
        
        logger.info(f"Received {len(files) if files else 0} files")
        
        if not files or len(files) == 0:
            logger.warning("No files uploaded")
            return jsonify({'success': False, 'message': 'No files uploaded'})
        
        # Log upload stats
        total_size_mb = sum(file.content_length or 0 for file in files) / (1024 * 1024)
        logger.info(f"{'=' * 60}")
        logger.info(f"BULK DETAIL UPLOAD STARTED")
        logger.info(f"Files: {len(files)}, Total size: {total_size_mb:.2f} MB")
        logger.info(f"{'=' * 60}")
        
        # Create session-specific temp directory
        session_id = session.get('username', 'anonymous') + '_' + datetime.now().strftime('%Y%m%d%H%M%S')
        temp_base = os.path.join(tempfile.gettempdir(), 'fdc_bulk_uploads')
        os.makedirs(temp_base, exist_ok=True)
        temp_dir = os.path.join(temp_base, session_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # Quick check: detect if we have archives
        has_archives = False
        for file in files:
            if file.filename.lower().endswith(('.zip', '.rar')):
                has_archives = True
                break
        
        style_groups = {}  # {style_name: [file_info]}
        duplicate_stats = {'count': 0, 'size_mb': 0}  # Track duplicates
        uncategorized_files = []  # Files that couldn't get SKU from filename
        
        logger.info(f"=== Processing {len(files)} files ===")
        logger.info(f"has_archives: {has_archives}")
        
        # Helper function to extract SKU from text (before first space)
        def extract_sku_from_text(text):
            """Extract SKU - everything before first space, uppercase"""
            if not text:
                return None
            # Get basename without extension
            name_without_ext = os.path.splitext(text)[0]
            # Take everything before first space
            sku = name_without_ext.split()[0] if name_without_ext else None
            return sku.upper() if sku else None
        
        # FIRST PASS: Save all files and try to extract SKU from filename
        for idx, file in enumerate(files):
            relative_path = request.form.get(f'path_{idx}', '')
            
            logger.info(f"File {idx}: filename='{file.filename}', relative_path='{relative_path}'")
            
            if relative_path:
                target_path = os.path.join(temp_dir, relative_path)
                logger.info(f"  Using relative_path -> target_path='{target_path}'")
            else:
                filename_only = os.path.basename(file.filename)
                target_path = os.path.join(temp_dir, secure_filename(filename_only))
                logger.info(f"  Using basename: '{filename_only}' -> target_path='{target_path}'")
            
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            file.save(target_path)
            logger.info(f"  Saved: {os.path.basename(target_path)}")
            
            # Extract archives immediately
            if target_path.lower().endswith('.zip'):
                logger.info(f"  Extracting ZIP: {os.path.basename(target_path)}")
                with zipfile.ZipFile(target_path, 'r') as zip_ref:
                    zip_ref.extractall(os.path.dirname(target_path))
                os.remove(target_path)
            elif target_path.lower().endswith('.rar'):
                logger.info(f"  Extracting RAR: {os.path.basename(target_path)}")
                try:
                    import rarfile
                    with rarfile.RarFile(target_path, 'r') as rar_ref:
                        rar_ref.extractall(os.path.dirname(target_path))
                    os.remove(target_path)
                except ImportError:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return jsonify({'success': False, 'message': 'RAR support not installed'})
            
            # For images, check duplicate and try to extract SKU from filename
            elif Path(target_path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                # Check duplicate
                is_dup, dup_info = _check_duplicate_by_hash(target_path)
                if is_dup:
                    file_size_mb = os.path.getsize(target_path) / (1024 * 1024)
                    duplicate_stats['count'] += 1
                    duplicate_stats['size_mb'] += file_size_mb
                    logger.info(f"  [SKIP DUPLICATE] {os.path.basename(target_path)} ({file_size_mb:.2f} MB)")
                    
                    # Log where the duplicate exists
                    if dup_info:
                        logger.info(f"    Duplicate found in: {dup_info.get('source', 'unknown')} - {dup_info.get('path', 'unknown')}")
                        with open('C:\\FDC_FLASK_APP\\debug_upload.txt', 'a', encoding='utf-8') as f:
                            f.write(f"  DUPE: {os.path.basename(target_path)} exists in {dup_info.get('source')} at {dup_info.get('path')}\n")
                    
                    os.remove(target_path)
                    continue
                
                # Try to extract SKU from filename
                logger.info(f"  [NEW IMAGE] {os.path.basename(target_path)}")
                filename_only = os.path.basename(target_path)
                sku = extract_sku_from_text(filename_only)
                
                if sku:
                    logger.info(f"    ✓ SKU from filename: {sku}")
                    if sku not in style_groups:
                        style_groups[sku] = []
                    
                    rel_path = os.path.relpath(target_path, temp_dir).replace('\\', '/')
                    style_groups[sku].append({
                        'filename': filename_only,
                        'path': rel_path,
                        'directory': os.path.dirname(rel_path) if '/' in rel_path else ''
                    })
                else:
                    # Couldn't extract SKU from filename - save for second pass
                    logger.info(f"    ⚠ No SKU in filename - will check folder name")
                    uncategorized_files.append(target_path)
        
        # Log first pass results
        logger.info(f"=== FIRST PASS COMPLETE ===")
        logger.info(f"Style groups: {len(style_groups)}")
        logger.info(f"Uncategorized files: {len(uncategorized_files)}")
        logger.info(f"Duplicates removed: {duplicate_stats['count']}")
        
        with open('C:\\FDC_FLASK_APP\\debug_upload.txt', 'a', encoding='utf-8') as f:
            f.write(f"After first pass - Groups: {len(style_groups)}, Uncategorized: {len(uncategorized_files)}, Dupes: {duplicate_stats['count']}\n")
            for sku in style_groups:
                f.write(f"  {sku}: {len(style_groups[sku])} files\n")
        
        # SECOND PASS: Handle archives and uncategorized files
        if has_archives:
            logger.info(f"=== Processing extracted archive files ===")
            for root, dirs, files_in_dir in os.walk(temp_dir):
                for img_file in files_in_dir:
                    if Path(img_file).suffix.lower() not in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                        continue
                    
                    file_path = os.path.join(root, img_file)
                    
                    # Skip if already processed
                    rel_path = os.path.relpath(file_path, temp_dir).replace('\\', '/')
                    already_processed = any(
                        rel_path in [f['path'] for files in style_groups.values() for f in files]
                    )
                    if already_processed:
                        continue
                    
                    # Check duplicate
                    is_dup, dup_info = _check_duplicate_by_hash(file_path)
                    if is_dup:
                        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                        duplicate_stats['count'] += 1
                        duplicate_stats['size_mb'] += file_size_mb
                        logger.info(f"  [ARCHIVE DUP] {img_file} ({file_size_mb:.2f} MB) - skipped")
                        os.remove(file_path)
                        continue
                    
                    # Try filename first
                    sku = extract_sku_from_text(img_file)
                    
                    if not sku:
                        # Try folder name (immediate parent)
                        folder_name = os.path.basename(root)
                        sku = extract_sku_from_text(folder_name)
                        
                        if not sku and root != temp_dir:
                            # Try one level up
                            parent_path = os.path.dirname(root)
                            parent_folder = os.path.basename(parent_path)
                            sku = extract_sku_from_text(parent_folder)
                            logger.info(f"  {img_file} - SKU from parent folder '{parent_folder}': {sku}")
                        elif sku:
                            logger.info(f"  {img_file} - SKU from folder '{folder_name}': {sku}")
                    else:
                        logger.info(f"  {img_file} - SKU from filename: {sku}")
                    
                    if sku:
                        if sku not in style_groups:
                            style_groups[sku] = []
                        style_groups[sku].append({
                            'filename': img_file,
                            'path': rel_path,
                            'directory': os.path.dirname(rel_path) if '/' in rel_path else ''
                        })
                    else:
                        logger.warning(f"  ✗ Could not extract SKU for {img_file} - skipped")
        
        # Process uncategorized files from first pass (try folder names)
        if uncategorized_files:
            logger.info(f"=== Processing {len(uncategorized_files)} uncategorized files ===")
            for file_path in uncategorized_files:
                filename = os.path.basename(file_path)
                parent_dir = os.path.dirname(file_path)
                
                # Try immediate parent folder
                if parent_dir != temp_dir:
                    folder_name = os.path.basename(parent_dir)
                    sku = extract_sku_from_text(folder_name)
                    
                    if not sku:
                        # Try one level up
                        grandparent_dir = os.path.dirname(parent_dir)
                        if grandparent_dir != temp_dir:
                            grandparent_name = os.path.basename(grandparent_dir)
                            sku = extract_sku_from_text(grandparent_name)
                            logger.info(f"  {filename} - SKU from grandparent '{grandparent_name}': {sku}")
                        else:
                            logger.info(f"  {filename} - SKU from folder '{folder_name}': {sku}")
                    else:
                        logger.info(f"  {filename} - SKU from folder '{folder_name}': {sku}")
                else:
                    sku = None
                    logger.warning(f"  {filename} - at root level, no folder to extract SKU from")
                
                if sku:
                    if sku not in style_groups:
                        style_groups[sku] = []
                    
                    rel_path = os.path.relpath(file_path, temp_dir).replace('\\', '/')
                    style_groups[sku].append({
                        'filename': filename,
                        'path': rel_path,
                        'directory': os.path.dirname(rel_path) if '/' in rel_path else ''
                    })
                else:
                    logger.warning(f"  ✗ Could not extract SKU for {filename} - skipped")
        
        # Remove duplicates based on full path (not just filename)
        logger.info(f"=== Deduplicating paths ===")
        for sku in style_groups:
            seen_paths = set()
            unique_images = []
            for img in style_groups[sku]:
                normalized_path = img['path'].replace('\\', '/')
                if normalized_path not in seen_paths:
                    seen_paths.add(normalized_path)
                    unique_images.append(img)
            style_groups[sku] = unique_images
        
        if not style_groups:
            logger.warning(f"=== NO VALID IMAGES FOUND ===")
            logger.warning(f"Duplicates skipped: {duplicate_stats['count']}")
            logger.warning(f"Uncategorized files attempted: {len(uncategorized_files)}")
            
            with open('C:\\FDC_FLASK_APP\\debug_upload.txt', 'a', encoding='utf-8') as f:
                f.write(f"NO VALID IMAGES - Duplicates: {duplicate_stats['count']}, Uncategorized: {len(uncategorized_files)}\n")
            
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            # Provide better message based on what happened
            if duplicate_stats['count'] > 0 and len(uncategorized_files) == 0:
                message = f'All {duplicate_stats["count"]} files were duplicates and were skipped. No new images to process.'
            else:
                message = 'No valid images found in folder structure'
            
            return jsonify({'success': False, 'message': message})
        
        # Log final statistics
        total_new_images = sum(len(files) for files in style_groups.values())
        logger.info(f"{'=' * 60}")
        logger.info(f"UPLOAD PROCESSING COMPLETE")
        logger.info(f"Styles found: {len(style_groups)}")
        for sku in style_groups:
            logger.info(f"  {sku}: {len(style_groups[sku])} images")
        logger.info(f"New images: {total_new_images}")
        logger.info(f"Duplicates removed: {duplicate_stats['count']} ({duplicate_stats['size_mb']:.2f} MB saved)")
        logger.info(f"{'=' * 60}")
        
        # Store session info
        session[f'bulk_detail_session_{session_id}'] = {
            'temp_dir': temp_dir,
            'style_count': len(style_groups),
            'image_count': total_new_images,
            'had_archives': has_archives,
            'duplicates_count': duplicate_stats['count']
        }
        
        message = f'Found {len(style_groups)} styles with {total_new_images} new images.'
        if duplicate_stats['count'] > 0:
            message += f' Skipped {duplicate_stats["count"]} duplicates ({duplicate_stats["size_mb"]:.1f} MB saved).'
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'styles': style_groups,
            'duplicates': {},  # Duplicates already removed, no details to show
            'had_archives': has_archives,
            'message': message
        })
        
    except Exception as e:
        logger.error(f"=== BULK DETAIL UPLOAD ERROR ===")
        logger.error(f"Error: {str(e)}")
        logger.error(f"Type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/bulk_catalog_thumbnail/<session_id>/<path:image_path>')
def get_bulk_catalog_thumbnail(session_id, image_path):
    """Serve thumbnail for bulk catalog preview images"""
    try:
        from flask import send_file
        import io
        from urllib.parse import unquote
        
        # Retrieve session data
        session_key = f'bulk_catalog_session_{session_id}'
        if session_key not in session:
            return "Session expired", 404
        
        session_data = session[session_key]
        temp_dir = session_data['temp_dir']
        
        decoded_path = unquote(image_path)
        full_path = os.path.normpath(os.path.join(temp_dir, decoded_path))
        
        # Security check
        if not full_path.startswith(os.path.normpath(temp_dir)):
            return "Invalid path", 403
        
        if not os.path.exists(full_path):
            return f"Image not found: {decoded_path}", 404
        
        # Create thumbnail
        img = Image.open(full_path)
        
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        
        img.thumbnail((200, 200), Image.Resampling.LANCZOS)
        
        img_io = io.BytesIO()
        img.save(img_io, 'JPEG', quality=85)
        img_io.seek(0)
        
        return send_file(img_io, mimetype='image/jpeg')
        
    except Exception as e:
        return f"Error: {str(e)}", 500


@image_upload_bp.route('/process_bulk_catalog', methods=['POST'])
def process_bulk_catalog():
    """Process bulk catalog images after user review"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        image_data = data.get('images', [])
        
        session_key = f'bulk_catalog_session_{session_id}'
        if session_key not in session:
            return jsonify({'success': False, 'message': 'Session expired or invalid'})
        
        session_data = session[session_key]
        temp_dir = session_data['temp_dir']
        
        if not os.path.exists(temp_dir):
            return jsonify({'success': False, 'message': 'Temporary files not found'})
        
        catalog_dir = r"D:\Catalog_pictures\catalog_original"
        os.makedirs(catalog_dir, exist_ok=True)
        
        processed_files = []
        saved_paths = []
        errors = []
        
        # Track version numbers per SKU during this batch
        sku_version_tracker = {}
        
        for img in image_data:
            try:
                rel_path = img['path']
                sku = img['sku'].strip().upper()
                
                if not sku or sku == 'UNKNOWN':
                    errors.append(f"Invalid SKU for {rel_path}")
                    continue
                
                file_path = os.path.join(temp_dir, rel_path)
                
                if not os.path.exists(file_path):
                    errors.append(f"File not found: {rel_path}")
                    continue
                
                # Get next version for this SKU (only once per SKU)
                if sku not in sku_version_tracker:
                    sku_version_tracker[sku] = get_next_catalog_version(sku, catalog_dir)
                else:
                    # Increment for each additional image of same SKU in this batch
                    sku_version_tracker[sku] += 1
                
                version_num = sku_version_tracker[sku]
                
                if version_num == 1:
                    new_filename = f"{sku}.jpg"
                else:
                    new_filename = f"{sku}_v{version_num}.jpg"
                
                dest_path = os.path.join(catalog_dir, new_filename)
                shutil.copy2(file_path, dest_path)
                optimize_image(dest_path)
                
                processed_files.append(new_filename)
                saved_paths.append(dest_path)
                
            except Exception as e:
                errors.append(f"Error processing {img.get('path', 'unknown')}: {str(e)}")
        
        # Auto-record in DAM DB
        if saved_paths:
            try:
                # Group by SKU
                sku_files = {}
                for path in saved_paths:
                    filename = os.path.basename(path)
                    sku = filename.split('_v')[0] if '_v' in filename else filename.replace('.jpg', '')
                    if sku not in sku_files:
                        sku_files[sku] = []
                    sku_files[sku].append(path)
                
                for sku, paths in sku_files.items():
                    _record_catalog_uploads(paths, sku)
            except Exception as e:
                logger.error(f"Auto-record failed: {e}")
        
        # Trigger background sync to update index files and ensure everything is in sync
        if saved_paths:
            try:
                _trigger_background_sync(rebuild_index=False)
                logger.info(f"Background sync triggered after catalog upload")
            except Exception as e:
                logger.error(f"Background sync trigger failed: {e}")
        
        # Cleanup
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            del session[session_key]
        except:
            pass
        
        message = f"Successfully processed {len(processed_files)} catalog images."
        if errors:
            message += f" {len(errors)} errors occurred."
        
        return jsonify({
            'success': True,
            'message': message,
            'processed': processed_files,
            'errors': errors
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/process_bulk_detail', methods=['POST'])
def process_bulk_detail():
    """Process bulk detail images organized by style folders"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        styles = data.get('styles', {})
        
        session_key = f'bulk_detail_session_{session_id}'
        if session_key not in session:
            return jsonify({'success': False, 'message': 'Session expired or invalid'})
        
        session_data = session[session_key]
        temp_dir = session_data['temp_dir']
        
        if not os.path.exists(temp_dir):
            return jsonify({'success': False, 'message': 'Temporary files not found'})
        
        processed_files = []
        errors = []
        
        for sku, images in styles.items():
            try:
                sku = sku.strip().upper()
                if not sku or sku == 'UNKNOWN':
                    errors.append(f"Invalid SKU: {sku}")
                    continue
                
                big_picture_dir = os.path.join(r"D:\Individual_big_picture", sku)
                os.makedirs(big_picture_dir, exist_ok=True)
                
                saved_paths = []
                
                for img in images:
                    try:
                        rel_path = img['path']
                        file_path = os.path.join(temp_dir, rel_path)
                        
                        if not os.path.exists(file_path):
                            errors.append(f"File not found: {rel_path}")
                            continue
                        
                        # Get original filename and extension
                        original_filename = secure_filename(img['filename'])
                        name_part, ext_part = os.path.splitext(original_filename)
                        
                        dest_path = os.path.join(big_picture_dir, original_filename)
                        
                        # Handle naming conflicts - add suffix before extension
                        counter = 1
                        while os.path.exists(dest_path):
                            new_filename = f"{name_part}_{counter:02d}{ext_part}"
                            dest_path = os.path.join(big_picture_dir, new_filename)
                            counter += 1
                        
                        shutil.copy2(file_path, dest_path)
                        optimize_image(dest_path)
                        
                        processed_files.append(f"{sku}/{os.path.basename(dest_path)}")
                        saved_paths.append(dest_path)
                        
                    except Exception as e:
                        errors.append(f"Error processing {img.get('filename', 'unknown')}: {str(e)}")
                
                # Auto-record in DAM DB
                if saved_paths:
                    try:
                        _record_big_picture_uploads(saved_paths, sku)
                    except Exception as e:
                        logger.error(f"Auto-record failed for {sku}: {e}")
                        
            except Exception as e:
                errors.append(f"Error processing style {sku}: {str(e)}")
        
        # Trigger background sync to update index files and ensure everything is in sync
        if processed_files:
            try:
                _trigger_background_sync(rebuild_index=False)
                logger.info(f"Background sync triggered after detail upload")
            except Exception as e:
                logger.error(f"Background sync trigger failed: {e}")
        
        # Cleanup
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
            del session[session_key]
        except:
            pass
        
        message = f"Successfully processed {len(processed_files)} detail images."
        if errors:
            message += f" {len(errors)} errors occurred."
        
        return jsonify({
            'success': True,
            'message': message,
            'processed': processed_files,
            'errors': errors
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/zip_thumbnail/<session_id>/<path:image_path>')
def get_zip_thumbnail(session_id, image_path):
    """Legacy route - redirect to bulk catalog thumbnail"""
    return get_bulk_catalog_thumbnail(session_id, image_path)


@image_upload_bp.route('/upload_zip', methods=['POST'])
def upload_zip_archive():
    """Legacy route - redirect to bulk catalog"""
    return upload_bulk_catalog()

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
        logger.error(f"Error optimizing image {file_path}: {e}")

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

def _check_duplicate_by_hash(file_path: str):
    """
    Check if a file already exists in database by SHA256 hash.
    Returns: (is_duplicate, existing_info) where existing_info is dict with source, path, sku
    """
    try:
        svc = DAMSyncServiceV2()
        sha256 = svc.calculate_file_hash(file_path)
        
        if not sha256:
            return False, None
        
        conn = get_mysql_connection()
        if not conn:
            return False, None
        
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT source, path, sku, filename 
                FROM image_assets 
                WHERE sha256 = %s AND is_active = TRUE
                LIMIT 1
            """, (sha256,))
            
            row = cur.fetchone()
            if row:
                source, path, sku, filename = row
                return True, {
                    'source': source,
                    'path': path,
                    'sku': sku,
                    'filename': filename
                }
            return False, None
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"Error checking duplicate: {e}")
        return False, None

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

def _trigger_background_sync(rebuild_index=False):
    """
    Trigger a background DAM sync after bulk uploads.
    This updates index files and ensures database is in sync with file system.
    Runs in a separate thread to not block the upload response.
    """
    from threading import Thread
    from py.utilities.logging_setup import log_event
    
    def run_sync():
        try:
            log_event('info', 'Background sync triggered after bulk upload')
            
            # Use DAMSyncServiceV2 to sync
            sync_service = DAMSyncServiceV2()
            
            # If rebuild_index=True, it will rescan directories
            # If False, it will use existing index files (faster)
            sync_service.run_full_sync(force_rebuild_index=rebuild_index)
            
            log_event('info', f'Background sync completed: {sync_service.stats}')
            
        except Exception as e:
            log_event('error', f'Background sync failed: {str(e)}')
            logger.error(f"Background sync error: {e}")
    
    # Start sync in background thread
    thread = Thread(target=run_sync, daemon=True)
    thread.start()
