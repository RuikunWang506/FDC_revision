import os
import json
import time
from datetime import datetime
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm  # Library to show progress bars

BIG_PICTURE_DIR = r"D:\Individual_big_picture"
saved_index_dir =r"C:\FDC_FLASK_APP\output\big_image_index_backup"
WHITE_THRESHOLD = 240  # A threshold to classify something as "white". Adjust if needed.
MAX_WORKERS = 8  # Adjust the number of workers based on your CPU cores
BACKUP_SUFFIX_FORMAT = "%d%m%Y"

def backup_file(file_path):
    """Create a backup of a file with a date-based suffix."""
    if os.path.exists(file_path):
        backup_path = f"{file_path}.{datetime.now().strftime(BACKUP_SUFFIX_FORMAT)}"
        os.rename(file_path, backup_path)
        print(f"Backup created: {backup_path}")

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

        # Define the 20px margins
        margin_size = 20
        margins = {
            'top': img.crop((0, 0, width, margin_size)),
            'bottom': img.crop((0, height - margin_size, width, height)),
            'left': img.crop((0, 0, margin_size, height)),
            'right': img.crop((width - margin_size, 0, width, height)),
        }

        # Check each margin to see if it's all white
        white_margins = 0  # Counter for white sides

        for margin_name, margin in margins.items():
            for pixel in margin.getdata():
                r, g, b = pixel
                if r < WHITE_THRESHOLD or g < WHITE_THRESHOLD or b < WHITE_THRESHOLD:
                    break
            else:
                # If we didn't break, it means the margin is entirely white
                white_margins += 1

        # If at least 2 sides are white, consider the image to have a white background
        if white_margins >= 2:
            return image_path, True
        else:
            return image_path, False

    except Exception as e:
        print(f"Error processing {image_path}: {e}")
        return image_path, False

def generate_index():
    try:
        index = []
        white_background_images = load_json(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'))
        old_index = load_json(os.path.join(BIG_PICTURE_DIR, 'index.json'))
        
        # Debugging: Check the first few entries in the old index
        print(f"Sample old index paths: {old_index[:5]}")  # Debugging: Print first 5 old index paths
        
        # Create a backup if the files exist
        backup_file(os.path.join(saved_index_dir, 'index.json'))
        backup_file(os.path.join(saved_index_dir, 'white_background_images.json'))

        # Collect all image paths and get their relative paths
        image_paths = [
            os.path.relpath(os.path.join(root, filename), BIG_PICTURE_DIR).replace("\\", "/")
            for root, dirs, files in os.walk(BIG_PICTURE_DIR)
            for filename in files
            if filename.lower().endswith(('.jpg', '.jpeg', '.png'))
        ]

        # Debugging: Check the first few entries of the newly found image paths
        print(f"Sample image paths (after stripping): {image_paths[:5]}")  # Debugging: Print first 5 image paths after stripping
        
        # Find new images by comparing with old index
        new_images = [img for img in image_paths if img not in old_index]
        total_files = len(new_images)
        print(f"Total new files to process: {total_files}")

        if not new_images:
            return {"status": "success", "message": "No new images to process."}

        start_time = time.time()

        # Using ThreadPoolExecutor for concurrent execution
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(is_white_background, os.path.join(BIG_PICTURE_DIR, img_path)): img_path for img_path in new_images}

            # Use tqdm to display progress bar
            with tqdm(total=total_files, desc="Processing images") as pbar:
                for count, future in enumerate(as_completed(futures), start=1):
                    img_path, is_white = future.result()
                    index.append(img_path)
                    if is_white:
                        white_background_images.append(img_path)
                    
                    # Progress update
                    pbar.update(1)

        # Write the new index and white background images
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
