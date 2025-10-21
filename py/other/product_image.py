from flask import request, render_template, session, jsonify, current_app,send_file, redirect, url_for, flash,send_from_directory
import os
import json
import zipfile
import io
from PIL import Image, ImageOps
import time
from py.utilities.setting import Config
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

    def prepend_path_to_file(file_path):
    # Construct the full path by prepending the missing directory structure
        return os.path.join(Config.BIG_PICTURE_DIR, file_path)

    @app.route('/product_image', methods=['GET', 'POST'])
    @login_required
    def product_image():
        current_user = session['username']

        image_data = []

        if request.method == 'POST':
            search_str = request.form.get('search_str', '').lower()
            white_background = request.form.get('white_background', False)
            include_folders = request.form.get('include_folders', False)
            
            index_file = os.path.join(Config.BIG_PICTURE_DIR, 'white_background_images.json') if white_background else os.path.join(Config.BIG_PICTURE_DIR, 'index.json')
            current_app.logger.info(f" ***{current_user}***___@@@product images @@@__ {search_str}")
            if not os.path.exists(index_file):
                return "Index file not found. Please generate the index first.", 500

            with open(index_file, 'r') as f:
                index = json.load(f)

            # Ensure file paths are strings
            matching_files = [str(file_path) for file_path in index if search_str in (file_path.lower() if include_folders else os.path.basename(file_path).lower())]

            for file_path in matching_files:
                full_path = prepend_path_to_file(file_path)
                try:
                    with Image.open(full_path) as img:
                        buffer = io.BytesIO()
                        img.save(buffer, format="JPEG")
                        buffer.seek(0)
                        base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                        modified_time = os.path.getmtime(full_path)
                        modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                        file_size = format_file_size(os.path.getsize(full_path) // 1024)
                        ratio = f"1:{round(img.height / img.width, 1)}"
                        filename = os.path.basename(file_path)
                        image_data.append((filename, base64_image, modified_date, file_size, ratio, file_path))
                except Exception as e:
                    flash(f"Error processing image {file_path}: {str(e)}", "danger")

        return render_template('product_image.html', image_data=image_data, quote=quote,current_user=current_user)

    @app.route('/tk', methods=['GET', 'POST'])
    def tk():
        current_user = 'TK_User'  # Set a default user for logging
        image_data = []
        
        if request.method == 'POST':
            # Handle password submission and image search
            password = request.form.get('password', '')
            search_str = request.form.get('search_str', '').lower()
            
            # Check password first
            if password != 'ucandoit':
                return jsonify({'error': 'Incorrect password'}), 401
            
            # If password is correct and there's a search, process it
            if search_str:  # Only search if search string is provided
                white_background = request.form.get('white_background', False)
                include_folders = request.form.get('include_folders', False)
                
                index_file = os.path.join(Config.BIG_PICTURE_DIR, 'white_background_images.json') if white_background else os.path.join(Config.BIG_PICTURE_DIR, 'index.json')
                current_app.logger.info(f" ***{current_user}***___@@@TK images @@@__ {search_str}")
                
                if not os.path.exists(index_file):
                    return jsonify({'error': 'Index file not found. Please generate the index first.'}), 500

                with open(index_file, 'r') as f:
                    index = json.load(f)

                # Ensure file paths are strings
                matching_files = [str(file_path) for file_path in index if search_str in (file_path.lower() if include_folders else os.path.basename(file_path).lower())]

                for file_path in matching_files:
                    full_path = prepend_path_to_file(file_path)
                    try:
                        with Image.open(full_path) as img:
                            buffer = io.BytesIO()
                            img.save(buffer, format="JPEG")
                            buffer.seek(0)
                            base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                            modified_time = os.path.getmtime(full_path)
                            modified_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(modified_time))
                            file_size = format_file_size(os.path.getsize(full_path) // 1024)
                            ratio = f"1:{round(img.height / img.width, 1)}"
                            filename = os.path.basename(file_path)
                            image_data.append((filename, base64_image, modified_date, file_size, ratio, file_path))
                    except Exception as e:
                        current_app.logger.error(f"Error processing image {file_path}: {str(e)}")

            return render_template('tk_main.html', image_data=image_data, quote=quote, current_user=current_user)
        
        # GET request - show the main page (password popup will be handled by JavaScript)
        return render_template('tk_main.html', image_data=image_data, quote=quote, current_user=current_user)

    @app.route('/get_image_data', methods=['GET'])
    @login_required
    def get_image_data():
        filename = request.args.get('filename')
        image_path = os.path.join(app.config['BIG_PICTURE_DIR'], filename)
        try:
            with Image.open(image_path) as img:
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG")
                buffer.seek(0)
                base64_image = base64.b64encode(buffer.read()).decode('utf-8')
                return jsonify({'base64_image': base64_image})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
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

        search_str = request.args.get('search_str', '').lower() if request.method == 'GET' else None
        selected_images = request.form.getlist('selected_images') if request.method == 'POST' else None
        image_type = request.args.get('image_type', 'normal')
        download_type = request.args.get('download_type', 'all')  # 'all' or 'selected'

        # Determine the correct image list
        if download_type == 'all':
            index_file = os.path.join(Config.BIG_PICTURE_DIR, 'index.json')
            if not os.path.exists(index_file):
                return "Index file not found. Please generate the index first.", 500
            with open(index_file, 'r') as f:
                index = json.load(f)
            matching_files = [file_path for file_path in index if search_str in file_path.lower()]
            file_list = matching_files
        else:
            file_list = selected_images  # selected_images now contains the relative paths

        if not file_list:
            return "No images selected or found.", 404

        # Generate zip file for download
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w') as zf:
            for file_path in file_list:
                # Ensure the full path is constructed by combining the base directory and the relative path
                image_path = os.path.join(Config.BIG_PICTURE_DIR, file_path)  # Now uses the full relative path
                if not os.path.exists(image_path):
                    return f"File {image_path} not found.", 404  # Better error handling
                if image_type == '1_1':
                    output_image = create_square_image(image_path)
                    if output_image:
                        zip_filename = os.path.splitext(os.path.basename(file_path))[0].lower() + '.jpg'
                        zf.writestr(zip_filename, output_image.getvalue())
                else:
                    zf.write(image_path, os.path.basename(image_path))

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
        with zipfile.ZipFile(memory_file, 'w') as zf:
            if selected_images:
                selected_files = selected_images.split(',')
                for filename in selected_files:
                    if filename:
                        file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                        if os.path.exists(file_path):
                            # Skip resizing if image_size is 'full' or None
                            if image_size and image_size != 'full':
                                try:
                                    with open(file_path, 'rb') as img_file:
                                        resized_img = resize_image(img_file, int(image_size))
                                        zf.writestr(filename, resized_img.getvalue())
                                except (ValueError, TypeError):
                                    # Fallback to original file if size conversion fails
                                    zf.write(file_path, arcname=filename)
                            else:
                                # Use original file for 'full' size or when size is not specified
                                zf.write(file_path, arcname=filename)
            else:
                # Original logic for all images remains unchanged
                search_terms = [term.split()[0].strip() for term in user_input.split(",")]
                for term in search_terms:
                    try:
                        for filename in os.listdir(Config.CATALOG_ORIGINAL):
                            if filename.lower().startswith(term.lower()):
                                file_path = os.path.join(Config.CATALOG_ORIGINAL, filename)
                                if os.path.exists(file_path):
                                    if image_type == 'resized':
                                        with Image.open(file_path) as img:
                                            buffer = io.BytesIO()
                                            if img.mode != 'RGB':
                                                img = img.convert('RGB')
                                            img.save(buffer, format='JPEG', quality=20)
                                            buffer.seek(0)
                                            zf.writestr(filename, buffer.getvalue())
                                    else:
                                        zf.write(file_path, arcname=filename)
                    except FileNotFoundError:
                        app.logger.error("Directory not found: %s", Config.CATALOG_ORIGINAL)

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
        full_path = os.path.join(Config.BIG_PICTURE_DIR, decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=False)

    @app.route('/ftp3/download/<path:encoded_path>', methods=['GET'])
    def download_file5(encoded_path):
        # Decode the path and serve the file for download
        decoded_path = urllib.parse.unquote(encoded_path)
        full_path = os.path.join(Config.BIG_PICTURE_DIR, decoded_path)
        directory, filename = os.path.split(full_path)
        return send_from_directory(directory, filename, as_attachment=True)

    @app.route('/json_data')
    def get_json_data():
        index_file_path = os.path.join(app.config['BIG_PICTURE_DIR'], 'index.json')
        white_bg_file_path = os.path.join(app.config['BIG_PICTURE_DIR'], 'white_background_images.json')

        if os.path.exists(index_file_path) and os.path.exists(white_bg_file_path):
            with open(index_file_path, 'r') as index_file, open(white_bg_file_path, 'r') as white_bg_file:
                index_data = json.load(index_file)
                white_bg_data = json.load(white_bg_file)
            return jsonify({"index_data": index_data, "white_background_data": white_bg_data})
        
        return jsonify({"error": "Files not found"}), 404