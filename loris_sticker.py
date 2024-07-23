import os
import math
import tempfile
import barcode
from PyPDF2 import PdfMerger
from barcode import Code128
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from barcode.base import Barcode
import logging
import datetime


logging.basicConfig(level=logging.INFO)

output_folder = "output/barcode_output/stickerimages"
today = datetime.datetime.now()
formatted_date = today.strftime("List-%Y%m%d-%H%M%S")

def generate_labels_per_page(additional_text, barcode_value, barcode_quantity, paper_size, left_margin, top_margin):
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_pdf = temp_file.name
        c = canvas.Canvas(temp_pdf, pagesize=(paper_size[0], paper_size[1]))

        labels_generated = 0

        barcode_height = 0.5 * inch
        barcode_width = 2 * inch

        adjusted_row_height = 1 * inch
        row_spacing = 0
        column_width = 2.625 * inch
        column_spacing = 0.1325 * inch

        x_positions = [left_margin, column_width + column_spacing + left_margin, 2 * column_width + 2 * column_spacing + left_margin]
        y_position = paper_size[1] - top_margin

        while labels_generated < barcode_quantity:
            for x_position in x_positions:
                if labels_generated >= barcode_quantity:
                    break
                image_path = os.path.join(output_folder, f"barcode_{barcode_value}")
                c.drawImage(f"{image_path}.png", x_position, y_position - barcode_height, width=barcode_width, height=barcode_height)

                c.setFont("Times-Roman", 10)
                barcode_text = barcode_value
                text_width = c.stringWidth(barcode_text, "Times-Roman", 12)
                text_x_position = x_position + (barcode_width - text_width) / 2
                c.drawString(text_x_position, y_position - barcode_height - 0.15 * inch, barcode_text)

                c.setFont("Helvetica", 8)
                additional_text_width = c.stringWidth(additional_text, "Helvetica", 8)
                additional_text_x_position = x_position + (barcode_width - text_width) / 2 + text_width + 0.2 * inch - 13
                c.drawString(additional_text_x_position, y_position - barcode_height - 0.15 * inch, additional_text)

                labels_generated += 1

            y_position -= row_spacing + adjusted_row_height

        c.save()

        return temp_pdf

def generate_barcode_image(barcode_value, output_folder):
    Barcode.default_writer_options['write_text'] = False
    code128 = barcode.get_barcode_class('code128')
    barcode_img = code128(barcode_value, writer=ImageWriter())

    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    barcode_img.save(os.path.join(output_folder, f"barcode_{barcode_value}"))

def process_input_data(input_data):
    data = []
    lines = input_data.strip().split('\n')
        
    # Check if the first line contains header keywords
    if any(keyword in lines[0] for keyword in ["SKU", "Barcode", "Quantity"]):
        lines = lines[1:]
    
    for line in lines:
        parts = line.split()
        if len(parts) == 3:
            additional_text = parts[0]
            barcode_value = parts[1]
            barcode_quantity = int(parts[2])
            data.append((additional_text, barcode_value, barcode_quantity))
            generate_barcode_image(barcode_value, output_folder)
        else:
            logging.warning("Incorrect data format: %s", line)
    return data

def merge_pdfs(output_pdf, pdfs_to_merge):
    pdf_merger = PdfMerger()

    for pdf in pdfs_to_merge:
        pdf_merger.append(pdf)

    with open(output_pdf, "wb") as merged_output:
        pdf_merger.write(merged_output)

    pdf_merger.close()

def main(input_data):
    try:
        data = process_input_data(input_data)
        pdfs_to_merge = []

        for item in data:
            additional_text, barcode_value, barcode_quantity = item
            full_pages = math.ceil(barcode_quantity / 30)

            for _ in range(full_pages):
                temp_pdf = generate_labels_per_page(additional_text, barcode_value, 30, letter, 0.18 * inch, 0.5 * inch)
                pdfs_to_merge.append(temp_pdf)

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H_%M_%S")
        output_pdf = f"output/barcode_output/30_stickers_{timestamp}.pdf"
        merge_pdfs(output_pdf, pdfs_to_merge)
        logging.info("PDF merging completed successfully.")
        return output_pdf

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return None

if __name__ == "__main__":
    input_data = """SKU123 123456789012 10
SKU124 123456789013 5
SKU125 123456789014 15"""
    main(input_data)
