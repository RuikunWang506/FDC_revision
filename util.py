from PIL import Image
import io

def resize_image(image_file, new_width):
    """
    Resizes an image to the specified width while maintaining aspect ratio.

    Args:
        image_file: A file-like object containing the image to resize.
        new_width: The desired width.

    Returns:
        A BytesIO object containing the resized image.
    """
    with Image.open(image_file) as img:
        orig_width, orig_height = img.size
        new_height = int((new_width / orig_width) * orig_height)  # Calculate new height to maintain aspect ratio
        resized_img = img.resize((new_width, new_height), Image.LANCZOS)  # Use LANCZOS for high-quality resizing
        img_byte_arr = io.BytesIO()
        resized_img.save(img_byte_arr, format='JPEG')
        img_byte_arr.seek(0)
        return img_byte_arr