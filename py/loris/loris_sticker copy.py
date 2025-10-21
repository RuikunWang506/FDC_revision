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

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Constants
LABELS_PER_PAGE = 30
LABELS_PER_ROW = 3  # Assuming 3 columns
LABELS_PER_COLUMN = 10  # 3 columns * 10 rows = 30 labels per page
PAGE_WIDTH, PAGE_HEIGHT = letter
LEFT_MARGIN = 0.25 * inch
TOP_MARGIN = 0.5 * inch
BARCODE_HEIGHT = 0.3 * inch
BARCODE_WIDTH = 2 * inch
ADJUSTED_ROW_HEIGHT = 1 * inch
COLUMN_WIDTH = 2.63 * inch
COLUMN_SPACING = 0.12 * inch

def generate_labels_per_page(labels, paper_size, left_margin, top_margin):
    """
    Generates a single PDF page with up to LABELS_PER_PAGE labels.

    Args:
        labels (list of tuples): Each tuple contains (additional_text, barcode_value, pil_image).
        paper_size (tuple): Size of the paper.
        left_margin (float): Left margin in points.
        top_margin (float): Top margin in points.

    Returns:
        bytes: PDF content as bytes.
    """
    from io import BytesIO
    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=paper_size)

    # Define grid parameters
    labels_per_row = LABELS_PER_ROW
    labels_per_column = LABELS_PER_COLUMN
    column_width = COLUMN_WIDTH
    column_spacing = COLUMN_SPACING
    row_height = ADJUSTED_ROW_HEIGHT

    # Calculate x positions for each column
    x_positions = [
        left_margin,
        left_margin + column_width + column_spacing,
        left_margin + 2 * (column_width + column_spacing)
    ]

    # Initial y position starts from top_margin
    y_initial = paper_size[1] - top_margin

    labels_generated = 0
    total_labels = len(labels)

    for row in range(labels_per_column):
        y_position = y_initial - row * row_height
        for col in range(labels_per_row):
                if labels_generated >= total_labels:
                    break  # No more labels to place

                label = labels[labels_generated]
                additional_text, barcode_value, pil_image = label
                x_position = x_positions[col]

                # Draw barcode image from memory
                if pil_image:
                    # Convert PIL image to temporary file for ReportLab
                    temp_fp = BytesIO()
                    pil_image.save(temp_fp, format='PNG')
                    temp_fp.seek(0)

                    c.drawImage(
                        temp_fp,
                        x_position,
                        y_position - BARCODE_HEIGHT,
                        width=BARCODE_WIDTH,
                        height=BARCODE_HEIGHT
                    )
                else:
                    logging.warning(f"Barcode image not available for: {barcode_value}")

                # Draw barcode text
                c.setFont("Times-Roman", 10)
                barcode_text = barcode_value
                text_width = c.stringWidth(barcode_text, "Times-Roman", 12)
                text_x_position = x_position + (BARCODE_WIDTH - text_width) / 2
                c.drawString(
                    text_x_position,
                    y_position - BARCODE_HEIGHT - 0.15 * inch,
                    barcode_text
                )

                # Draw additional text
                c.setFont("Helvetica", 8)
                additional_text_width = c.stringWidth(additional_text, "Helvetica", 8)
                additional_text_x_position = x_position + (BARCODE_WIDTH - additional_text_width) / 2
                c.drawString(
                    additional_text_x_position,
                    y_position - BARCODE_HEIGHT - 0.3 * inch,
                    additional_text
                )

                labels_generated += 1  # Move to the next label

        c.showPage()
        c.save()
        bio.seek(0)
        return bio.getvalue()

def generate_barcode_image(barcode_value):
    """
    Generates a barcode image in memory and returns it as PIL Image.

    Args:
        barcode_value (str): The value to encode in the barcode.

    Returns:
        PIL.Image: The barcode image.
    """
    from io import BytesIO
    Barcode.default_writer_options['write_text'] = False
    code128 = barcode.get_barcode_class('code128')
    barcode_img = code128(barcode_value, writer=ImageWriter())

    # Generate image in memory
    fp = BytesIO()
    barcode_img.write(fp)
    fp.seek(0)

    # Load as PIL Image
    from PIL import Image
    pil_image = Image.open(fp)
    return pil_image

def process_input_data(input_data):
    """
    Processes the input data and generates barcode images in memory.

    Args:
        input_data (str): Multiline string containing SKU, Barcode, Quantity.

    Returns:
        list of tuples: Each tuple contains (additional_text, barcode_value, barcode_quantity, pil_image).
    """
    data = []
    lines = input_data.strip().split('\n')

    # Check if the first line contains header keywords
    if lines and any(keyword in lines[0] for keyword in ["SKU", "Barcode", "Quantity"]):
        lines = lines[1:]

    for line in lines:
        parts = line.strip().split('\t')  # Split by tab instead of whitespace
        if len(parts) == 3:
            additional_text = parts[0] # Replace spaces with hyphens in SKU
            barcode_value = parts[1]
            try:
                barcode_quantity = int(parts[2])
                # Generate barcode image in memory
                pil_image = generate_barcode_image(barcode_value)
                data.append((additional_text, barcode_value, barcode_quantity, pil_image))
            except ValueError:
                logging.warning("Invalid quantity for line: %s", line)
        else:
            logging.warning("Incorrect data format: %s", line)
    return data


def merge_pdfs(pdfs_to_merge):
    """
    Merges multiple PDF bytes into a single PDF.

    Args:
        pdfs_to_merge (list of bytes): List of PDF content as bytes.

    Returns:
        bytes: Merged PDF content as bytes.
    """
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

def main(input_data):
    try:
        data = process_input_data(input_data)

        # For "30 stickers per page" PDF
        pdfs_to_merge_30stickers = []

        # For "exact stickers" PDF
        all_labels_exact = []

        for item in data:
            additional_text, barcode_value, barcode_quantity, pil_image = item

            # Generate 30 stickers per page version
            full_pages_30stickers = math.ceil(barcode_quantity / LABELS_PER_PAGE)
            for _ in range(full_pages_30stickers):
                # Calculate number of stickers to generate on this page
                stickers_on_page = 30
                temp_pdf_30stickers = generate_labels_per_page(
                    [(additional_text, barcode_value, pil_image)] * stickers_on_page,
                    letter,
                    0.18 * inch,
                    0.5 * inch
                )
                pdfs_to_merge_30stickers.append(temp_pdf_30stickers)
                barcode_quantity -= stickers_on_page

            # Aggregate labels for "exact stickers" PDF
            for _ in range(item[2]):  # item[2] is the original barcode_quantity
                all_labels_exact.append((additional_text, barcode_value, pil_image))

        # Paginate "exact stickers" into pages of 30
        pdfs_to_merge_exact = []
        for i in range(0, len(all_labels_exact), LABELS_PER_PAGE):
            page_labels = all_labels_exact[i:i + LABELS_PER_PAGE]
            temp_pdf_exact = generate_labels_per_page(
                page_labels,
                letter,
                0.18 * inch,
                0.5 * inch
            )
            pdfs_to_merge_exact.append(temp_pdf_exact)

        # Merge PDFs
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

if __name__ == "__main__":
    input_data = """test1	013000006057	44
tef34	013000001243	18"""
    main(input_data)