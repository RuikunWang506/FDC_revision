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

from py.utilities.database_utils import get_mysql_connection
from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2

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
        image_data = []
        for root, dirs, files_in_dir in os.walk(temp_dir):
            for file in files_in_dir:
                file_path = os.path.join(root, file)
                
                if Path(file_path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                    # Extract SKU from filename or path
                    sku = organizer.extract_sku_from_path(str(file_path))
                    
                    # Create relative path for display
                    rel_path = os.path.relpath(file_path, temp_dir)
                    
                    image_data.append({
                        'filename': file,
                        'path': rel_path,
                        'sku': sku or 'UNKNOWN'
                    })
        
        if not image_data:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'success': False, 'message': 'No valid images found in uploaded files'})
        
        # Store session info for later processing
        session[f'bulk_catalog_session_{session_id}'] = {
            'temp_dir': temp_dir,
            'image_count': len(image_data)
        }
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'images': image_data,
            'message': f'Found {len(image_data)} images. Please review and confirm.'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@image_upload_bp.route('/upload_bulk_detail', methods=['POST'])
def upload_bulk_detail():
    """Handle bulk detail images upload - folders named by style containing images"""
    try:
        files = request.files.getlist('detail_files')
        
        if not files or len(files) == 0:
            return jsonify({'success': False, 'message': 'No files uploaded'})
        
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
        
        from py.utilities.image_organizer_service import ImageOrganizerService
        organizer = ImageOrganizerService()
        style_groups = {}  # {style_name: [file_info]}
        
        print(f"\n=== DEBUG: Processing {len(files)} files ===")
        print(f"has_archives: {has_archives}")
        
        # Save and analyze files in single pass
        for idx, file in enumerate(files):
            # Use index-based path lookup to handle duplicate filenames
            relative_path = request.form.get(f'path_{idx}', '')
            
            print(f"\nFile {idx}: filename='{file.filename}', relative_path='{relative_path}'")
            
            if relative_path:
                # Folder upload with webkitRelativePath
                target_path = os.path.join(temp_dir, relative_path)
                print(f"  Using relative_path -> target_path='{target_path}'")
            else:
                # Direct file upload - use only the basename to avoid path issues
                # Some browsers include path in filename, secure_filename converts / to _
                filename_only = os.path.basename(file.filename)
                target_path = os.path.join(temp_dir, secure_filename(filename_only))
                print(f"  Using basename: '{filename_only}' -> target_path='{target_path}'")
            
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            file.save(target_path)
            
            # Extract archives immediately
            if target_path.lower().endswith('.zip'):
                with zipfile.ZipFile(target_path, 'r') as zip_ref:
                    zip_ref.extractall(os.path.dirname(target_path))
                os.remove(target_path)
            elif target_path.lower().endswith('.rar'):
                try:
                    import rarfile
                    with rarfile.RarFile(target_path, 'r') as rar_ref:
                        rar_ref.extractall(os.path.dirname(target_path))
                    os.remove(target_path)
                except ImportError:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return jsonify({'success': False, 'message': 'RAR support not installed'})
            
            # For images saved directly (not archives), group immediately
            elif Path(target_path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                parent_dir = os.path.dirname(target_path)
                rel_parent = os.path.relpath(parent_dir, temp_dir) if parent_dir != temp_dir else ''
                
                # Determine SKU from TOP-LEVEL folder (not immediate parent)
                if parent_dir == temp_dir:
                    # Root level - extract from filename
                    sku = organizer.extract_sku_from_path(file.filename)
                else:
                    # Get the path parts: t9998/New folder -> ['t9998', 'New folder']
                    path_parts = rel_parent.split(os.sep) if rel_parent else []
                    
                    if path_parts:
                        # Use FIRST folder as style (top-level), not immediate parent
                        top_folder = path_parts[0]
                        sku = organizer.extract_sku_from_path(top_folder) or top_folder.upper()
                    else:
                        # Fallback to immediate parent
                        folder_name = os.path.basename(parent_dir)
                        sku = organizer.extract_sku_from_path(folder_name) or folder_name.upper()
                
                if sku:
                    if sku not in style_groups:
                        style_groups[sku] = []
                    
                    # Normalize paths to forward slashes
                    rel_path = os.path.relpath(target_path, temp_dir).replace('\\', '/')
                    rel_parent_normalized = rel_parent.replace('\\', '/') if rel_parent else ''
                    
                    print(f"  Adding to style_groups[{sku}]: path='{rel_path}', dir='{rel_parent_normalized}'")
                    
                    style_groups[sku].append({
                        'filename': os.path.basename(target_path),
                        'path': rel_path,
                        'directory': rel_parent_normalized
                    })
        
        # If archives were extracted, rescan directory
        if has_archives:
            style_groups = {}  # Reset
            for root, dirs, files_in_dir in os.walk(temp_dir):
                image_files = [f for f in files_in_dir if Path(f).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}]
                
                if image_files:
                    rel_root = os.path.relpath(root, temp_dir) if root != temp_dir else ''
                    
                    if root == temp_dir:
                        # Root level images - extract SKU from filename
                        for img_file in image_files:
                            file_path = os.path.join(root, img_file)
                            sku = organizer.extract_sku_from_path(img_file)
                            if sku:
                                if sku not in style_groups:
                                    style_groups[sku] = []
                                
                                # Normalize path
                                rel_path = os.path.relpath(file_path, temp_dir).replace('\\', '/')
                                
                                style_groups[sku].append({
                                    'filename': img_file,
                                    'path': rel_path,
                                    'directory': ''
                                })
                    else:
                        # Inside folders - use TOP-LEVEL folder as SKU
                        path_parts = rel_root.split(os.sep) if rel_root else []
                        
                        if path_parts:
                            # Use FIRST folder (top-level style folder)
                            top_folder = path_parts[0]
                            sku = organizer.extract_sku_from_path(top_folder) or top_folder.upper()
                        else:
                            # Fallback
                            folder_name = os.path.basename(root)
                            sku = organizer.extract_sku_from_path(folder_name) or folder_name.upper()
                        
                        if sku not in style_groups:
                            style_groups[sku] = []
                        
                        for img_file in image_files:
                            file_path = os.path.join(root, img_file)
                            
                            # Normalize paths to forward slashes
                            rel_path = os.path.relpath(file_path, temp_dir).replace('\\', '/')
                            rel_root_normalized = rel_root.replace('\\', '/') if rel_root else ''
                            
                            style_groups[sku].append({
                                'filename': img_file,
                                'path': rel_path,
                                'directory': rel_root_normalized
                            })
        
        # Remove duplicates based on full path (not just filename)
        # Normalize paths to use forward slashes for comparison
        print(f"\n=== Before deduplication ===")
        for sku in style_groups:
            print(f"{sku}: {len(style_groups[sku])} images")
        
        for sku in style_groups:
            seen_paths = set()
            unique_images = []
            for img in style_groups[sku]:
                # Normalize path for comparison (use forward slashes)
                normalized_path = img['path'].replace('\\', '/')
                if normalized_path not in seen_paths:
                    seen_paths.add(normalized_path)
                    unique_images.append(img)
                else:
                    print(f"  DUPLICATE REMOVED: {normalized_path}")
            style_groups[sku] = unique_images
        
        print(f"\n=== After deduplication ===")
        for sku in style_groups:
            print(f"{sku}: {len(style_groups[sku])} images")
        
        # Save all files and analyze on-the-fly
        for file in files:
            relative_path = request.form.get(f'path_{file.filename}', '')
            
            if relative_path:
                target_path = os.path.join(temp_dir, relative_path)
            else:
                target_path = os.path.join(temp_dir, secure_filename(file.filename))
            
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            file.save(target_path)
            
            # Extract archives immediately
            if target_path.lower().endswith('.zip'):
                extract_dir = os.path.dirname(target_path)
                with zipfile.ZipFile(target_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                os.remove(target_path)
            elif target_path.lower().endswith('.rar'):
                try:
                    import rarfile
                    extract_dir = os.path.dirname(target_path)
                    with rarfile.RarFile(target_path, 'r') as rar_ref:
                        rar_ref.extractall(extract_dir)
                    os.remove(target_path)
                except ImportError:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return jsonify({'success': False, 'message': 'RAR support not installed'})
            
            # For images, group immediately (no second pass needed)
            elif Path(target_path).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}:
                parent_dir = os.path.dirname(target_path)
                
                # Determine SKU from path
                if parent_dir == temp_dir:
                    sku = organizer.extract_sku_from_path(file.filename)
                else:
                    folder_name = os.path.basename(parent_dir)
                    sku = organizer.extract_sku_from_path(folder_name) or folder_name.upper()
                
                if sku:
                    if sku not in style_groups:
                        style_groups[sku] = []
                    style_groups[sku].append({
                        'filename': os.path.basename(target_path),
                        'path': os.path.relpath(target_path, temp_dir)
                    })
        
        # If archives were extracted, need to rescan directory
        if has_archives:
            style_groups = {}  # Reset
            for root, dirs, files_in_dir in os.walk(temp_dir):
                image_files = [f for f in files_in_dir if Path(f).suffix.lower() in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp'}]
                
                if image_files:
                    folder_name = os.path.basename(root)
                    
                    if root == temp_dir:
                        for img_file in image_files:
                            file_path = os.path.join(root, img_file)
                            sku = organizer.extract_sku_from_path(img_file)
                            if sku:
                                if sku not in style_groups:
                                    style_groups[sku] = []
                                style_groups[sku].append({
                                    'filename': img_file,
                                    'path': os.path.relpath(file_path, temp_dir)
                                })
                    else:
                        sku = organizer.extract_sku_from_path(folder_name) or folder_name.upper()
                        if sku not in style_groups:
                            style_groups[sku] = []
                        
                        for img_file in image_files:
                            file_path = os.path.join(root, img_file)
                            style_groups[sku].append({
                                'filename': img_file,
                                'path': os.path.relpath(file_path, temp_dir)
                            })
        
        if not style_groups:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify({'success': False, 'message': 'No valid images found in folder structure'})
        
        # Store session info
        session[f'bulk_detail_session_{session_id}'] = {
            'temp_dir': temp_dir,
            'style_count': len(style_groups),
            'image_count': sum(len(files) for files in style_groups.values()),
            'had_archives': has_archives
        }
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'styles': style_groups,
            'had_archives': has_archives,
            'message': f'Found {len(style_groups)} styles with {sum(len(f) for f in style_groups.values())} images total.'
        })
        
    except Exception as e:
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
                print(f"Auto-record failed: {e}")
        
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
                        print(f"Auto-record failed for {sku}: {e}")
                        
            except Exception as e:
                errors.append(f"Error processing style {sku}: {str(e)}")
        
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