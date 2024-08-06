import os
import json

BIG_PICTURE_DIR = r"D:\Individual_big_picture"

def generate_index():
    index = []
    for root, dirs, files in os.walk(BIG_PICTURE_DIR):
        for filename in files:
            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                file_path = os.path.join(root, filename).replace("\\", "/")
                index.append(file_path)

    index_file = os.path.join(BIG_PICTURE_DIR, 'index.json')
    
    os.makedirs(os.path.dirname(index_file), exist_ok=True)
    
    with open(index_file, 'w') as f:
        json.dump(index, f)

if __name__ == "__main__":
    generate_index()
