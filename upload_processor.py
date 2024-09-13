import easyocr
import cv2
import numpy as np
import os
from config import Config
from flask import url_for

def process_upload(files, desired_filename):
    output_urls = []
    reader = easyocr.Reader(['ch_sim','en'])

    for i, file in enumerate(files):
        in_memory_file = np.frombuffer(file.read(), np.uint8)
        image = cv2.imdecode(in_memory_file, cv2.IMREAD_COLOR)

        gray_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        result = reader.readtext(gray_image)

        allowlist = '0123456789.'

        for detection in result:
            top_left = tuple(map(int, detection[0][0]))
            bottom_right = tuple(map(int, detection[0][2]))
            box_width = bottom_right[0] - top_left[0]
            box_height = bottom_right[1] - top_left[1]
            text = detection[1]

            # Check if the text contains only allowed characters (digits and dot)
            if not all(char in allowlist for char in text):
                continue

            # Ignore if the detected text is "1" or "0.0"
            if text == '1' or text == '0' or text == '0.0':
                continue

            if text.replace('.', '', 1).isdigit():
                inches_value = float(text)
                
                # Ignore if inches_value is 0
                if inches_value == 0.0:
                    continue
                
                cm_value = inches_value * 2.54
                text_with_inches = f"{inches_value} inches"
                text_with_cm = f"{cm_value:.2f} cm"

                cv2.rectangle(image, top_left, bottom_right, (255, 255, 255), -1)

                (inches_text_w, inches_text_h), _ = cv2.getTextSize(text_with_inches, cv2.FONT_HERSHEY_DUPLEX, 1, 2)
                (cm_text_w, cm_text_h), _ = cv2.getTextSize(text_with_cm, cv2.FONT_HERSHEY_DUPLEX, 1, 2)

                center_x = top_left[0] + box_width // 2
                center_y = top_left[1] + box_height // 2
                move_left = 30

                inches_text_x = center_x - inches_text_w // 2 - move_left
                inches_text_y = center_y - 10
                cm_text_x = center_x - cm_text_w // 2 - move_left
                cm_text_y = inches_text_y + inches_text_h + 10

                background_padding = 5
                background_top_left_x = min(inches_text_x, cm_text_x) - background_padding
                background_top_left_y = inches_text_y - inches_text_h - background_padding
                background_bottom_right_x = max(inches_text_x + inches_text_w, cm_text_x + cm_text_w) + background_padding
                background_bottom_right_y = cm_text_y + cm_text_h + background_padding

                cv2.rectangle(image, (background_top_left_x, background_top_left_y),
                              (background_bottom_right_x, background_bottom_right_y),
                              (255, 255, 255), -1)

                cv2.putText(image, text_with_inches, (inches_text_x, inches_text_y),
                            cv2.FONT_HERSHEY_DUPLEX, 1, (0, 0, 0), 2, cv2.LINE_AA)

                cv2.putText(image, text_with_cm, (cm_text_x, cm_text_y),
                            cv2.FONT_HERSHEY_DUPLEX, 1, (0, 0, 0), 2, cv2.LINE_AA)

        height, width, _ = image.shape
        if width > height:
            padding = (width - height) // 2
            image_square = cv2.copyMakeBorder(image, padding, padding, 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        elif height > width:
            padding = (height - width) // 2
            image_square = cv2.copyMakeBorder(image, 0, 0, padding, padding, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        else:
            image_square = image

        if not desired_filename:
            desired_filename = file.filename.rsplit('.', 1)[0]  # Use the original filename without extension

        output_image_path = os.path.join(Config.Dimension_DIR, f"{desired_filename}_sss_{i}.jpg")
        cv2.imwrite(output_image_path, image_square)

        output_urls.append({
            'url': url_for('serve_temp_size_pic', path=f"{desired_filename}_sss_{i}.jpg"),
            'name': f"{desired_filename}_sss_{i}.jpg"
        })

    return output_urls
