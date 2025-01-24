from flask import request, render_template, session, jsonify, send_file, redirect, url_for, flash,send_from_directory
import os
import json
import zipfile
import io
from PIL import Image, ImageOps
import time
from config import Config
import subprocess
import sys
import base64
import urllib.parse
from urllib.parse import quote


def basename_filter(path):
    return os.path.basename(path)


def register_big_img_routes(app):
    app.jinja_env.filters['basename'] = basename_filter

    def prepend_path_to_file(file_path):
    # Construct the full path by prepending the missing directory structure
        return os.path.join(Config.BIG_PICTURE_DIR, file_path)

    @app.route('/big_img', methods=['GET', 'POST'])
    def big_img():
        if 'username' not in session:
            return redirect(url_for("login"))
        current_user = session['username']

        image_data = []

        if request.method == 'POST':
            search_str = request.form.get('search_str', '').lower()
            white_background = request.form.get('white_background', False)
            include_folders = request.form.get('include_folders', False)
            
            index_file = os.path.join(Config.BIG_PICTURE_DIR, 'white_background_images.json') if white_background else os.path.join(Config.BIG_PICTURE_DIR, 'index.json')

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

        return render_template('big_img.html', image_data=image_data, quote=quote,current_user=current_user)
    @app.route('/get_image_data', methods=['GET'])
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
        
        return render_template('big_img.html', reindex_result=reindex_result)
    

    @app.route('/download_big_images', methods=['GET', 'POST'])
    def download_big_images():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
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


    def resize_image(image_path, fixed_height=300):
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