import os
import json
import time
from datetime import datetime
from PIL import Image
from concurrent.futures import ProcessPoolExecutor, as_completed
import shutil  # Import shutil to use shutil.move

BIG_PICTURE_DIR = r"D:\Individual_big_picture"
saved_index_dir = r"C:\FDC_FLASK_APP\output\big_image_index_backup"
WHITE_THRESHOLD = 240
MAX_WORKERS = 8

def backup_file(file_path):
    """Create a backup of a file with a date-based suffix in the saved_index_dir."""
    if os.path.exists(file_path):
        if not os.path.exists(saved_index_dir):
            os.makedirs(saved_index_dir)
        backup_suffix = datetime.now().strftime('%d%m%Y')
        backup_path = os.path.join(saved_index_dir, os.path.basename(file_path)) + f".{backup_suffix}"
        shutil.move(file_path, backup_path)  # Use shutil.move instead of os.rename

def load_json(file_path):
    """Load JSON file if exists, otherwise return an empty list."""
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                # If JSON is corrupted, return empty list
                return []
    return []

def is_white_background(image_path):
    try:
        img = Image.open(image_path).convert("RGB")
        width, height = img.size
        margin_size = 20
        margins = [
            img.crop((0, 0, width, margin_size)),  # Top
            img.crop((0, height - margin_size, width, height)),  # Bottom
            img.crop((0, 0, margin_size, height)),  # Left
            img.crop((width - margin_size, 0, width, height))  # Right
        ]
        white_margins = 0
        for margin in margins:
            pixels = margin.getdata()
            if all(r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD for r, g, b in pixels):
                white_margins += 1
        return (os.path.relpath(image_path, BIG_PICTURE_DIR).replace("\\", "/"), white_margins >= 2)
    except Exception:
        relative_path = os.path.relpath(image_path, BIG_PICTURE_DIR).replace("\\", "/")
        return (relative_path, False)

def format_elapsed_time(seconds):
    """Format elapsed time into hours, minutes, and seconds."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"

def generate_index():
    start_time = time.time()  # Record the start time
    print("starting re-index timer")
    try:
        index = []
        white_background_images = load_json(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'))
        old_index = load_json(os.path.join(BIG_PICTURE_DIR, 'index.json'))

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

        if total_files == 0:
            end_time = time.time()
            elapsed_time = end_time - start_time
            formatted_elapsed_time = format_elapsed_time(elapsed_time)
            return {
                "status": "success",
                "message": "No new images to process."
                    f"Re-index took {formatted_elapsed_time} seconds.... "
                    f"total of {len(old_index) + len(index)} entries...."
                    f"{len(white_background_images)} images have white backgrounds."
            }

        # Process images to check for white backgrounds
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    is_white_background,
                    os.path.join(BIG_PICTURE_DIR, img_path)
                ): img_path for img_path in new_images
            }

            for future in as_completed(futures):
                img_path, is_white = future.result()
                index.append(img_path)
                if is_white:
                    white_background_images.append(img_path)

        end_time = time.time()
        elapsed_time = end_time - start_time
        formatted_elapsed_time = format_elapsed_time(elapsed_time)

        # Backing up files to saved_index_dir
        backup_file(os.path.join(BIG_PICTURE_DIR, 'index.json'))
        backup_file(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'))

        # Write the new index and white background images (saved in BIG_PICTURE_DIR)
        with open(os.path.join(BIG_PICTURE_DIR, 'index.json'), 'w') as f:
            json.dump(old_index + index, f, indent=4)
        
        with open(os.path.join(BIG_PICTURE_DIR, 'white_background_images.json'), 'w') as f:
            json.dump(white_background_images, f, indent=4)

        return {
            "status": "success",
            "message": (
                f"Re-index took {formatted_elapsed_time} seconds ..."
                f"Index updated with {len(index)} new entries... total of {len(old_index) + len(index)} entries..."
                f"{len(white_background_images)} images have white backgrounds..."
            )
        }

    except Exception as e:
        end_time = time.time()
        elapsed_time = end_time - start_time
        formatted_elapsed_time = format_elapsed_time(elapsed_time)
        return {
            "status": "error",
            "elapsed_time": formatted_elapsed_time,
            "message": str(e)
        }

if __name__ == "__main__":
    result = generate_index()
    # Output the result as JSON
    print(json.dumps(result))
