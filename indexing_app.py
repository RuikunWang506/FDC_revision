import os
import json
import time
from datetime import datetime
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import shutil  # Import shutil to use shutil.move


BIG_PICTURE_DIR = r"D:\Individual_big_picture"
saved_index_dir = r"C:\FDC_FLASK_APP\output\big_image_index_backup"
WHITE_THRESHOLD = 240
MAX_WORKERS = 8

def backup_file(file_path):
    """Create a backup of a file with a date-based suffix in the saved_index_dir."""
    if os.path.exists(file_path):
        backup_path = os.path.join(saved_index_dir, os.path.basename(file_path)) + f".{datetime.now().strftime('%d%m%Y')}"
        shutil.move(file_path, backup_path)  # Use shutil.move instead of os.rename

def load_json(file_path):
    """Load JSON file if exists, otherwise return an empty list."""
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f)
    return []

def is_white_background(image_path):
    try:
        img = Image.open(image_path)
        img = img.convert("RGB")
        width, height = img.size
        margin_size = 20
        margins = {
            'top': img.crop((0, 0, width, margin_size)),
            'bottom': img.crop((0, height - margin_size, width, height)),
            'left': img.crop((0, 0, margin_size, height)),
            'right': img.crop((width - margin_size, 0, width, height)),
        }
        white_margins = 0
        for margin in margins.values():
            for pixel in margin.getdata():
                r, g, b = pixel
                if r < WHITE_THRESHOLD or g < WHITE_THRESHOLD or b < WHITE_THRESHOLD:
                    break
            else:
                white_margins += 1
        if white_margins >= 2:
            return os.path.relpath(image_path, BIG_PICTURE_DIR).replace("\\", "/"), True
        else:
            return os.path.relpath(image_path, BIG_PICTURE_DIR).replace("\\", "/"), False
    except Exception as e:
        print(f"Error processing {image_path}: {e}")
        return os.path.relpath(image_path, BIG_PICTURE_DIR).replace("\\", "/"), False

def generate_index():
    try:
        index = []
        white_background_images = load_json(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'))
        old_index = load_json(os.path.join(BIG_PICTURE_DIR, 'index.json'))

        # Backing up files to saved_index_dir
        backup_file(os.path.join(BIG_PICTURE_DIR, 'index.json'))
        backup_file(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'))

        # Collect all image paths and get their relative paths
        image_paths = [
            os.path.relpath(os.path.join(root, filename), BIG_PICTURE_DIR).replace("\\", "/")
            for root, dirs, files in os.walk(BIG_PICTURE_DIR)
            for filename in files
            if filename.lower().endswith(('.jpg', '.jpeg', '.png'))
        ]

        # Find new images by comparing with old index
        new_images = [img for img in image_paths if img not in old_index]
        total_files = len(new_images)
        print(f"Total new files to process: {total_files}")

        if not new_images:
            return {"status": "success", "message": "No new images to process."}

        start_time = time.time()

        # Process images to check for white backgrounds
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(is_white_background, os.path.join(BIG_PICTURE_DIR, img_path)): img_path for img_path in new_images}

            # Use tqdm to display progress bar
            with tqdm(total=total_files, desc="Processing images") as pbar:
                for count, future in enumerate(as_completed(futures), start=1):
                    img_path, is_white = future.result()

                    # img_path is now relative and returned by is_white_background
                    index.append(img_path)
                    if is_white:
                        white_background_images.append(img_path)  # Append relative path
                    pbar.update(1)

        # Write the new index and white background images (saved in BIG_PICTURE_DIR)
        with open(os.path.join(BIG_PICTURE_DIR, 'index.json'), 'w') as f:
            json.dump(old_index + index, f)
        
        with open(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'), 'w') as f:
            json.dump(white_background_images, f)

        return {"status": "success", "message": f"Index created with {len(old_index) + len(index)} entries. {len(white_background_images)} have white backgrounds."}

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    result = generate_index()
    if result["status"] == "success":
        print(result["message"])
    else:
        print(f"Error: {result['message']}")
