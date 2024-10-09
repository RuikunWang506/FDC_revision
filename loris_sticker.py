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

# Define output folders
output_folder = "output/barcode_output/stickerimages"
pdf_output_folder = "output/barcode_output"
os.makedirs(output_folder, exist_ok=True)
os.makedirs(pdf_output_folder, exist_ok=True)

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
        labels (list of tuples): Each tuple contains (additional_text, barcode_value).
        paper_size (tuple): Size of the paper.
        left_margin (float): Left margin in points.
        top_margin (float): Top margin in points.

    Returns:
        str: Path to the temporary PDF file.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        temp_pdf = temp_file.name
        c = canvas.Canvas(temp_pdf, pagesize=paper_size)

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
                additional_text, barcode_value = label
                x_position = x_positions[col]

                # Draw barcode image
                image_path = os.path.join(output_folder, f"barcode_{barcode_value}.png")
                if os.path.exists(image_path):
                    c.drawImage(
                        image_path,
                        x_position,
                        y_position - BARCODE_HEIGHT,
                        width=BARCODE_WIDTH,
                        height=BARCODE_HEIGHT
                    )
                else:
                    logging.warning(f"Barcode image not found: {image_path}")

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
        return temp_pdf

def generate_barcode_image(barcode_value, output_folder):
    """
    Generates a barcode image and saves it to the specified folder.

    Args:
        barcode_value (str): The value to encode in the barcode.
        output_folder (str): Directory to save the barcode image.
    """
    Barcode.default_writer_options['write_text'] = False
    code128 = barcode.get_barcode_class('code128')
    barcode_img = code128(barcode_value, writer=ImageWriter())
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    barcode_img.save(os.path.join(output_folder, f"barcode_{barcode_value}"))

def process_input_data(input_data):
    """
    Processes the input data and generates barcode images.

    Args:
        input_data (str): Multiline string containing SKU, Barcode, Quantity.

    Returns:
        list of tuples: Each tuple contains (additional_text, barcode_value, barcode_quantity).
    """
    data = []
    lines = input_data.strip().split('\n')
        
    # Check if the first line contains header keywords
    if lines and any(keyword in lines[0] for keyword in ["SKU", "Barcode", "Quantity"]):
        lines = lines[1:]
    
    for line in lines:
        parts = line.strip().split()
        if len(parts) == 3:
            additional_text = parts[0]
            barcode_value = parts[1]
            try:
                barcode_quantity = int(parts[2])
                data.append((additional_text, barcode_value, barcode_quantity))
                generate_barcode_image(barcode_value, output_folder)
            except ValueError:
                logging.warning("Invalid quantity for line: %s", line)
        else:
            logging.warning("Incorrect data format: %s", line)
    return data

def merge_pdfs(output_pdf, pdfs_to_merge):
    """
    Merges multiple PDF files into a single PDF.

    Args:
        output_pdf (str): Path to the output merged PDF.
        pdfs_to_merge (list of str): List of PDF file paths to merge.
    """
    pdf_merger = PdfMerger()
    for pdf in pdfs_to_merge:
        pdf_merger.append(pdf)
    with open(output_pdf, "wb") as merged_output:
        pdf_merger.write(merged_output)
    pdf_merger.close()

def main(input_data):
    try:
        data = process_input_data(input_data)
        
        # For "30 stickers per page" PDF
        pdfs_to_merge_30stickers = []
        
        # For "exact stickers" PDF
        all_labels_exact = []

        for item in data:
            additional_text, barcode_value, barcode_quantity = item

            # Generate 30 stickers per page version
            full_pages_30stickers = math.ceil(barcode_quantity / LABELS_PER_PAGE)
            for _ in range(full_pages_30stickers):
                # Calculate number of stickers to generate on this page
                stickers_on_page = 30
                temp_pdf_30stickers = generate_labels_per_page(
                    [(additional_text, barcode_value)] * stickers_on_page,
                    letter,
                    0.18 * inch,
                    0.5 * inch
                )
                pdfs_to_merge_30stickers.append(temp_pdf_30stickers)
                barcode_quantity -= stickers_on_page

            # Aggregate labels for "exact stickers" PDF
            for _ in range(item[2]):  # item[2] is the original barcode_quantity
                all_labels_exact.append((additional_text, barcode_value))
        
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

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
        output_pdf_30stickers = os.path.join(pdf_output_folder, f"30_stickers_{timestamp}.pdf")
        output_pdf_exact = os.path.join(pdf_output_folder, f"exact_stickers_{timestamp}.pdf")

        # Merge PDFs
        if pdfs_to_merge_30stickers:
            merge_pdfs(output_pdf_30stickers, pdfs_to_merge_30stickers)
        if pdfs_to_merge_exact:
            merge_pdfs(output_pdf_exact, pdfs_to_merge_exact)
        
        logging.info("PDF merging completed successfully.")
        logging.info(f"'30 stickers' PDF: {output_pdf_30stickers}")
        logging.info(f"'Exact stickers' PDF: {output_pdf_exact}")

        filename_30stickers = os.path.basename(output_pdf_30stickers)
        filename_exact = os.path.basename(output_pdf_exact)

        return filename_30stickers, filename_exact

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return None, None

if __name__ == "__main__":
    input_data = """test1	013000006057	44
tef34	013000001243	18"""
    main(input_data)
