import os
import math
import tempfile
import barcode
from barcode import Code128
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from barcode.base import Barcode
import logging
import datetime
from PyPDF2 import PdfMerger

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

AVERY_SPECS = {
    'label_width': 2.625 * inch,
    'label_height': 1.0 * inch,
    'page_width': 8.5 * inch,
    'page_height': 11.0 * inch,
    'left_margin': 0.1875 * inch,
    'top_margin': 0.5 * inch,
    'horizontal_pitch': 2.75 * inch,
    'vertical_pitch': 1.0 * inch,
    'labels_per_row': 3,
    'labels_per_column': 10,
    'labels_per_page': 30
}

def get_avery_layout_specs(custom_specs=None):
    specs = AVERY_SPECS.copy()
    if custom_specs:
        specs.update(custom_specs)
    return specs

BARCODE_WIDTH = 2.0 * inch
BARCODE_HEIGHT = 0.3 * inch
TEXT_FONT_SIZE = 8
BARCODE_FONT_SIZE = 10

def calculate_label_positions(specs):
    positions = []
    for row in range(specs['labels_per_column']):
        for col in range(specs['labels_per_row']):
            x = specs['left_margin'] + col * specs['horizontal_pitch']
            y = specs['page_height'] - specs['top_margin'] - (row + 1) * specs['vertical_pitch']
            positions.append((x, y))
    return positions

def generate_labels_per_page(labels, custom_specs=None):
    from io import BytesIO

    specs = get_avery_layout_specs(custom_specs)
    positions = calculate_label_positions(specs)

    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=(specs['page_width'], specs['page_height']))

    for i, label in enumerate(labels):
        if i >= len(positions):
            break

        additional_text, barcode_value, pil_image = label
        x_pos, y_pos = positions[i]

        if pil_image:
            import tempfile
            import os
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
                pil_image.save(temp_file, format='PNG')
                temp_file_path = temp_file.name

            try:
                barcode_x = x_pos + (specs['label_width'] - BARCODE_WIDTH) / 2
                barcode_y = y_pos + specs['label_height'] - BARCODE_HEIGHT - 0.05 * inch

                c.drawImage(
                    temp_file_path,
                    barcode_x,
                    barcode_y,
                    width=BARCODE_WIDTH,
                    height=BARCODE_HEIGHT
                )
            finally:
                os.unlink(temp_file_path)

        c.setFont("Times-Roman", BARCODE_FONT_SIZE)
        barcode_text = barcode_value
        text_width = c.stringWidth(barcode_text, "Times-Roman", BARCODE_FONT_SIZE)
        text_x = x_pos + (specs['label_width'] - text_width) / 2
        text_y = barcode_y - 0.15 * inch

        c.drawString(text_x, text_y, barcode_text)

        c.setFont("Helvetica", TEXT_FONT_SIZE)
        additional_text_width = c.stringWidth(additional_text, "Helvetica", TEXT_FONT_SIZE)
        additional_text_x = x_pos + (specs['label_width'] - additional_text_width) / 2
        additional_text_y = text_y - 0.15 * inch

        c.drawString(additional_text_x, additional_text_y, additional_text)

    c.showPage()
    c.save()
    bio.seek(0)
    return bio.getvalue()

def generate_barcode_image(barcode_value):
    from io import BytesIO
    Barcode.default_writer_options['write_text'] = False
    code128 = barcode.get_barcode_class('code128')
    barcode_img = code128(barcode_value, writer=ImageWriter())

    fp = BytesIO()
    barcode_img.write(fp)
    fp.seek(0)

    from PIL import Image
    pil_image = Image.open(fp)
    return pil_image

def process_input_data(input_data):
    data = []
    lines = input_data.strip().split('\n')

    if lines and any(keyword in lines[0] for keyword in ["SKU", "Barcode", "Quantity"]):
        lines = lines[1:]

    for line in lines:
        parts = line.strip().split('\t')
        if len(parts) == 3:
            additional_text = parts[0]
            barcode_value = parts[1]
            try:
                barcode_quantity = int(parts[2])
                pil_image = generate_barcode_image(barcode_value)
                data.append((additional_text, barcode_value, barcode_quantity, pil_image))
            except ValueError:
                logging.warning("Invalid quantity for line: %s", line)
        else:
            logging.warning("Incorrect data format: %s", line)
    return data


def merge_pdfs(pdfs_to_merge):

    from io import BytesIO
    pdf_merger = PdfMerger()
    for pdf_bytes in pdfs_to_merge:
        pdf_bio = BytesIO(pdf_bytes)
        pdf_merger.append(pdf_bio)
    
    output_bio = BytesIO()
    pdf_merger.write(output_bio)
    pdf_merger.close()
    output_bio.seek(0)
    return output_bio.getvalue()

def main(input_data, custom_specs=None):
    try:
        data = process_input_data(input_data)
        specs = get_avery_layout_specs(custom_specs)
        labels_per_page = specs['labels_per_page']

        pdfs_to_merge_30stickers = []

        all_labels_exact = []

        for item in data:
            additional_text, barcode_value, barcode_quantity, pil_image = item

            full_pages_30stickers = math.ceil(barcode_quantity / labels_per_page)
            for _ in range(full_pages_30stickers):
                stickers_on_page = labels_per_page
                temp_pdf_30stickers = generate_labels_per_page(
                    [(additional_text, barcode_value, pil_image)] * stickers_on_page,
                    specs
                )
                pdfs_to_merge_30stickers.append(temp_pdf_30stickers)
                barcode_quantity -= stickers_on_page

            for _ in range(item[2]):
                all_labels_exact.append((additional_text, barcode_value, pil_image))

        pdfs_to_merge_exact = []
        for i in range(0, len(all_labels_exact), labels_per_page):
            page_labels = all_labels_exact[i:i + labels_per_page]
            temp_pdf_exact = generate_labels_per_page(
                page_labels,
                specs
            )
            pdfs_to_merge_exact.append(temp_pdf_exact)

        pdf_30stickers_bytes = None
        pdf_exact_bytes = None

        if pdfs_to_merge_30stickers:
            pdf_30stickers_bytes = merge_pdfs(pdfs_to_merge_30stickers)
        if pdfs_to_merge_exact:
            pdf_exact_bytes = merge_pdfs(pdfs_to_merge_exact)

        logging.info("PDF merging completed successfully.")

        return pdf_30stickers_bytes, pdf_exact_bytes

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return None, None

def download_loris_label(filename):
    """Download utility for loris label files."""
    from py.utilities.setting import Config
    from flask import send_file
    
    try:
        file_path = Config.OUTPUT_DIRS['label_output'] / filename
        if file_path.exists():
            return send_file(
                str(file_path),
                as_attachment=False,
                mimetype='application/pdf'
            )
        else:
            return None
    except Exception as e:
        logging.error("Error downloading file %s: %s", filename, str(e))
        return None

if __name__ == "__main__":
    input_data = """test1	013000006057	44
tef34	013000001243	18"""
    main(input_data)