from flask import request, render_template, session, jsonify, current_app,send_file, redirect, url_for, flash,send_from_directory
import os
import json
import zipfile
import io
from PIL import Image, ImageOps
import time
from py.utilities.setting import Config
from py.utilities.dam_utils import search_assets_by_sku_pattern, get_canonical_assets_for_skus, DAMAssetInfo
import subprocess
import sys
import base64
import urllib.parse
from urllib.parse import quote
from py.utilities.auth import login_required


def basename_filter(path):
    return os.path.basename(path)


def register_product_image_routes(app):
    app.jinja_env.filters['basename'] = basename_filter

    # Legacy function removed - now using DAM system

    @app.route('/product_image', methods=['GET', 'POST'])
    @login_required
    def product_image():
        current_user = session['username']
        image_data = []

        if request.method == 'POST':
            search_str = request.form.get('search_str', '').strip()
            current_app.logger.info(f" ***{current_user}***___@@@product images DAM search @@@__ {search_str}")
            
            if search_str:
                try:
                    # Use DAM system to search for assets - only big_picture source for product images
                    matching_assets = search_assets_by_sku_pattern(search_str, source_filter='big_picture')
                    
                    for asset in matching_assets:
                        try:
                            # Create DAM asset info object for easy access to URLs
                            asset_info = DAMAssetInfo(asset)
                            
                            # Get image from asset path for processing
                            if os.path.exists(asset.path):
                                with Image.open(asset.path) as img:
                                    buffer = io.BytesIO()
                                    img.save(buffer, format="JPEG")
                                    buffer.seek(0)
                                    base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                                    modified_date = asset.created_at.strftime('%Y-%m-%d %H:%M:%S') if asset.created_at else 'Unknown'
                                    file_size = format_file_size(os.path.getsize(asset.path) // 1024)
                                    ratio = f"1:{round(img.height / img.width, 1)}"
                                    
                                    # Use DAM asset info instead of file path
                                    image_data.append((
                                        asset.filename, 
                                        base64_image, 
                                        modified_date, 
                                        file_size, 
                                        ratio, 
                                        asset_info.url  # Use DAM URL instead of file path
                                    ))
                            else:
                                current_app.logger.warning(f"Asset file not found: {asset.path}")
                                
                        except Exception as e:
                            current_app.logger.error(f"Error processing asset {asset.filename}: {str(e)}")
                            flash(f"Error processing image {asset.filename}: {str(e)}", "danger")
                            
                except Exception as e:
                    current_app.logger.error(f"Error searching DAM assets: {str(e)}")
                    flash(f"Error searching images: {str(e)}", "danger")

        return render_template('product_image.html', image_data=image_data, quote=quote, current_user=current_user)

    @app.route('/tk', methods=['GET', 'POST'])
    def tk():
        current_user = 'TK_User'  # Set a default user for logging
        image_data = []
        
        if request.method == 'POST':
            # Handle password submission and image search
            password = request.form.get('password', '')
            search_str = request.form.get('search_str', '').strip()
            
            # Check password first
            if password != 'ucandoit':
                return jsonify({'error': 'Incorrect password'}), 401
            
            # If password is correct and there's a search, process it
            if search_str:  # Only search if search string is provided
                current_app.logger.info(f" ***{current_user}***___@@@TK DAM search @@@__ {search_str}")
                
                try:
                    # Use DAM system to search for assets - only big_picture source for TK images
                    matching_assets = search_assets_by_sku_pattern(search_str, source_filter='big_picture')
                    
                    for asset in matching_assets:
                        try:
                            # Create DAM asset info object for easy access to URLs
                            asset_info = DAMAssetInfo(asset)
                            
                            # Get image from asset path for processing
                            if os.path.exists(asset.path):
                                with Image.open(asset.path) as img:
                                    buffer = io.BytesIO()
                                    img.save(buffer, format="JPEG")
                                    buffer.seek(0)
                                    base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                                    modified_date = asset.created_at.strftime('%Y-%m-%d %H:%M:%S') if asset.created_at else 'Unknown'
                                    file_size = format_file_size(os.path.getsize(asset.path) // 1024)
                                    ratio = f"1:{round(img.height / img.width, 1)}"
                                    
                                    # Use DAM asset info instead of file path
                                    image_data.append((
                                        asset.filename, 
                                        base64_image, 
                                        modified_date, 
                                        file_size, 
                                        ratio, 
                                        asset_info.url  # Use DAM URL instead of file path
                                    ))
                            else:
                                current_app.logger.warning(f"Asset file not found: {asset.path}")
                                
                        except Exception as e:
                            current_app.logger.error(f"Error processing asset {asset.filename}: {str(e)}")
                            
                except Exception as e:
                    current_app.logger.error(f"Error searching DAM assets: {str(e)}")
                    return jsonify({'error': f'Error searching images: {str(e)}'}), 500

            return render_template('tk_main.html', image_data=image_data, quote=quote, current_user=current_user)
        
        # GET request - show the main page (password popup will be handled by JavaScript)
        return render_template('tk_main.html', image_data=image_data, quote=quote, current_user=current_user)

    @app.route('/get_image_data', methods=['GET'])
    @login_required
    def get_image_data():
        filename = request.args.get('filename')
        
        # Use DAM system to find asset by filename
        import py.utilities.models
        db = py.utilities.models.SessionLocal()
        try:
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                filename=filename,
                is_active=True
            ).first()
            
            if not asset or not os.path.exists(asset.path):
                return jsonify({'error': 'Image not found'}), 404
                
            with Image.open(asset.path) as img:
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG")
                buffer.seek(0)
                base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                return jsonify({'base64_image': base64_image})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            db.close()
    
    @app.route('/reindex', methods=['POST'])
    @login_required
    def reindex(): 
        app.logger.info("Running re-indexing script")

        try:
            # Run indexing_app.py using subprocess with UTF-8 encoding
            result = subprocess.run(
                [sys.executable, os.path.join(os.path.dirname(__file__), 'indexing_app.py')],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            # Capture the stdout from the script
            reindex_result = result.stdout
        except Exception as e:
            reindex_result = f"Error during re-indexing: {str(e)}"
        
        return render_template('product_image.html', reindex_result=reindex_result)
    

    @app.route('/download_product_images', methods=['GET', 'POST'])
    @login_required
    def download_product_images():
        current_user = session['username']

        search_str = request.args.get('search_str', '').strip() if request.method == 'GET' else None
        selected_asset_ids = request.form.getlist('selected_images') if request.method == 'POST' else None
        image_type = request.args.get('image_type', 'normal')
        download_type = request.args.get('download_type', 'all')  # 'all' or 'selected'

        # Determine assets to download
        assets_to_download = []
        
        if download_type == 'all' and search_str:
            # Get all assets matching the search pattern - only big_picture source for product downloads
            assets_to_download = search_assets_by_sku_pattern(search_str, source_filter='big_picture')
        elif download_type == 'selected' and selected_asset_ids:
            try:
                import py.utilities.models
                db = py.utilities.models.SessionLocal()
                # Convert asset IDs to integers and get assets
                asset_ids = [int(aid.split('/')[-2]) for aid in selected_asset_ids if aid.startswith('/image/')]
                assets_to_download = db.query(py.utilities.models.ImageAsset).filter(
                    py.utilities.models.ImageAsset.id.in_(asset_ids),
                    py.utilities.models.ImageAsset.is_active == True
                ).all()
                db.close()
            except Exception as e:
                current_app.logger.error(f"Error getting selected assets: {str(e)}")
                return f"Error processing selected images: {str(e)}", 400

        if not assets_to_download:
            return "No images selected or found.", 404

        # Generate zip file for download
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            for asset in assets_to_download:
                if not os.path.exists(asset.path):
                    current_app.logger.warning(f"Asset file not found: {asset.path}")
                    continue
                    
                try:
                    if image_type == '1_1':
                        output_image = create_square_image(asset.path)
                        if output_image:
                            zip_filename = os.path.splitext(asset.filename)[0].lower() + '.jpg'
                            zf.writestr(zip_filename, output_image.getvalue())
                    else:
                        zf.write(asset.path, asset.filename)
                except Exception as e:
                    current_app.logger.error(f"Error processing asset {asset.filename}: {str(e)}")
                    continue

        memory_file.seek(0)
        zip_filename = f"{search_str or 'selected'}_{image_type}.zip"
        return send_file(memory_file, download_name=zip_filename, as_attachment=True)


    def resize_image(img_file, target_size):
        """Resize image from file object to target width, maintaining aspect ratio"""
        try:
            with Image.open(img_file) as img:
                img = img.convert("RGB")
                aspect_ratio = img.width / img.height
                new_width = int(target_size)
                new_height = int(target_size / aspect_ratio)
                img = img.resize((new_width, new_height), Image.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG")
                buffer.seek(0)
                return buffer
        except Exception as e:
            return None


    @app.route('/download_images', methods=['POST'])
    def download_images():
        user_input = request.form.get('user_input', '')
        image_type = request.form.get('image_type', 'resized')
        selected_images = request.form.get('selected_images', '')
        image_size = request.form.get('image_size')

        memory_file = io.BytesIO()
        assets_to_download = []
        
        try:
            if selected_images:
                # Handle selected images - expect filenames
                selected_filenames = [f.strip() for f in selected_images.split(',') if f.strip()]
                
                import py.utilities.models
                db = py.utilities.models.SessionLocal()
                try:
                    for filename in selected_filenames:
                        asset = db.query(py.utilities.models.ImageAsset).filter_by(
                            filename=filename,
                            is_active=True
                        ).first()
                        if asset:
                            assets_to_download.append(asset)
                finally:
                    db.close()
            else:
                # Handle search terms
                search_terms = [term.split()[0].strip() for term in user_input.split(",")]
                for term in search_terms:
                    if term:
                        assets = search_assets_by_sku_pattern(term, source_filter='big_picture')
                        assets_to_download.extend(assets)
            
            # Remove duplicates
            unique_assets = {}
            for asset in assets_to_download:
                unique_assets[asset.id] = asset
            assets_to_download = list(unique_assets.values())
            
            with zipfile.ZipFile(memory_file, 'w') as zf:
                for asset in assets_to_download:
                    if not os.path.exists(asset.path):
                        current_app.logger.warning(f"Asset file not found: {asset.path}")
                        continue
                        
                    try:
                        if image_size and image_size != 'full':
                            # Resize image
                            try:
                                with open(asset.path, 'rb') as img_file:
                                    resized_img = resize_image(img_file, int(image_size))
                                    if resized_img:
                                        zf.writestr(asset.filename, resized_img.getvalue())
                                    else:
                                        zf.write(asset.path, arcname=asset.filename)
                            except (ValueError, TypeError):
                                zf.write(asset.path, arcname=asset.filename)
                        elif image_type == 'resized':
                            # Apply quality compression
                            with Image.open(asset.path) as img:
                                buffer = io.BytesIO()
                                if img.mode != 'RGB':
                                    img = img.convert('RGB')
                                img.save(buffer, format='JPEG', quality=20)
                                buffer.seek(0)
                                zf.writestr(asset.filename, buffer.getvalue())
                        else:
                            # Use original file
                            zf.write(asset.path, arcname=asset.filename)
                    except Exception as e:
                        current_app.logger.error(f"Error processing asset {asset.filename}: {str(e)}")
                        continue

        except Exception as e:
            current_app.logger.error(f"Error in download_images: {str(e)}")
            return f"Error downloading images: {str(e)}", 500

        memory_file.seek(0)
        zip_filename = "selected_images.zip" if selected_images else "images.zip"
        return send_file(
            memory_file,
            download_name=zip_filename,
            as_attachment=True
        )


    def resize_image_by_height(image_path, fixed_height=300):
        try:
            with Image.open(image_path) as img:
                img = img.convert("RGB")
                aspect_ratio = img.width / img.height
                new_width = int(fixed_height * aspect_ratio)
                img = img.resize((new_width, fixed_height), Image.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG")
                buffer.seek(0)
                return buffer
        except Exception as e:
            return None

    def create_square_image(image_path):
        try:
            image = Image.open(image_path)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            size = max(image.size)
            square_image = ImageOps.expand(image, (
                (size - image.width) // 2,
                (size - image.height) // 2,
                (size - image.width + 1) // 2,
                (size - image.height + 1) // 2
            ), fill='white')
            output_image = io.BytesIO()
            quality = 95
            square_image.save(output_image, format='JPEG', subsampling=0, quality=quality)
            while output_image.tell() > 2 * 1024 * 1024 and quality > 10:
                quality -= 5
                output_image.seek(0)
                output_image.truncate(0)
                square_image.save(output_image, format='JPEG', subsampling=0, quality=quality)
            output_image.seek(0)
            return output_image
        except Exception as e:
            return None

    def format_file_size(size):
        return f"{size} KB" if size < 1000 else f"{size / 1000:.2f} MB"

    def b64encode_filter(data):
        data.seek(0)
        return base64.b64encode(data.read()).decode('utf-8')

    app.jinja_env.filters['b64encode'] = b64encode_filter
    

    @app.route('/ftp3/<path:encoded_path>', methods=['GET'])
    def serve_file3(encoded_path):
        # Decode the path and serve the file from the correct location
        decoded_path = urllib.parse.unquote(encoded_path)
        full_path = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=False)

    @app.route('/ftp3/download/<path:encoded_path>', methods=['GET'])
    def download_file5(encoded_path):
        # Decode the path and serve the file for download
        decoded_path = urllib.parse.unquote(encoded_path)
        full_path = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=True)

    @app.route('/json_data')
    def get_json_data():
        index_file_path = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, 'index.json')
        white_bg_file_path = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, 'white_background_images.json')

        if os.path.exists(index_file_path) and os.path.exists(white_bg_file_path):
            with open(index_file_path, 'r') as index_file, open(white_bg_file_path, 'r') as white_bg_file:
                index_data = json.load(index_file)
                white_bg_data = json.load(white_bg_file)
            return jsonify({"index_data": index_data, "white_background_data": white_bg_data})
        
        return jsonify({"error": "Files not found"}), 404