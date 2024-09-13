import os
import json
import time
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm  # tqdm is a library to show progress bars

BIG_PICTURE_DIR = r"D:\Individual_big_picture"
WHITE_THRESHOLD = 240  # A threshold to classify something as "white". Adjust if needed.
MAX_WORKERS = 8  # Adjust the number of workers based on your CPU cores

def generate_index():
    is_white_background
    generate_index_with_background_check


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
        for margin in margins.values():
            for pixel in margin.getdata():
                r, g, b = pixel
                if r < WHITE_THRESHOLD or g < WHITE_THRESHOLD or b < WHITE_THRESHOLD:
                    return image_path, False
        return image_path, True

    except Exception as e:
        print(f"Error processing {image_path}: {e}")
        return image_path, False

def generate_index_with_background_check():
    try:
        index = []
        white_background_images = []
        
        # Collect all image paths
        image_paths = [
            os.path.join(root, filename).replace("\\", "/")
            for root, dirs, files in os.walk(BIG_PICTURE_DIR)
            for filename in files
            if filename.lower().endswith(('.jpg', '.jpeg', '.png'))
        ]
        
        total_files = len(image_paths)
        print(f"Total files to process: {total_files}")

        start_time = time.time()  # Start timer

        # Using ThreadPoolExecutor for concurrent execution
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(is_white_background, img_path): img_path for img_path in image_paths}

            # Use tqdm to display progress bar
            with tqdm(total=total_files, desc="Processing images") as pbar:
                for count, future in enumerate(as_completed(futures), start=1):
                    img_path, is_white = future.result()
                    index.append(img_path)
                    if is_white:
                        white_background_images.append(img_path)
                    
                    # Progress update
                    pbar.update(1)
                    
                    # Calculate elapsed time and estimate remaining time
                    elapsed_time = time.time() - start_time
                    estimated_total_time = (elapsed_time / count) * total_files
                    estimated_finish_time = start_time + estimated_total_time
                    
                    # Log elapsed time and estimated time
                    if count % 100 == 0:  # Print every 100 files processed
                        print(f"Processed {count}/{total_files} files. Elapsed time: {elapsed_time:.2f} seconds. "
                              f"Estimated completion time: {time.ctime(estimated_finish_time)}.")

        # Save results to files only at the end
        index_file = os.path.join(BIG_PICTURE_DIR, 'index.json')
        white_background_file = os.path.join(BIG_PICTURE_DIR, 'white_background_images.json')

        # Write the index to a file
        with open(index_file, 'w') as f:
            json.dump(index, f)

        # Write the white background images to a file
        with open(white_background_file, 'w') as f:
            json.dump(white_background_images, f)

        return {"status": "success", "message": f"Index created with {len(index)} entries. {len(white_background_images)} have white backgrounds."}
    
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    result = generate_index_with_background_check()
    if result["status"] == "success":
        print(result["message"])
    else:
        print(f"Error: {result['message']}")
