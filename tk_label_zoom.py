import io
import multiprocessing
import os
import re
import tempfile
from flask import Flask, request, send_file, render_template,current_app, Blueprint
import fitz  # PyMuPDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

tk_label_zoom_bp = Blueprint('tk_label', __name__)

pdfmetrics.registerFont(TTFont('SimSun', 'simsun.ttc'))

def parse_packing_slip(page_text):
    # Extract common fields
    tracking_number = re.search(r'Tracking\s*number:\s*(\d{22})', page_text)
    in_transit_by = re.search(r'In transit by:\s*(\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)', page_text)
    order_id = re.search(r'Order ID:\s*(\d+)', page_text)
    qty_total = re.search(r'Qty Total:\s*(\d+)', page_text)
    seller_note = re.search(r'Seller Note:\s*([^\n]+)', page_text)

    # Extract the product block
    try:
        block = re.search(r'Product Name\s+SKU\s+Seller SKU\s+Qty(.*?)Qty Total:', page_text, re.DOTALL).group(1)
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        
        items = []
        current_item = []
        
        for line in lines:
            current_item.append(line)
            if line.isdigit():  # When we hit a quantity number
                if len(current_item) >= 4:
                    product_name = " ".join(current_item[:-3])
                    color = current_item[-3]
                    seller_sku = current_item[-2]
                    qty = current_item[-1]
                    items.append({
                        "product_name": product_name,
                        "color": color,
                        "seller_sku": seller_sku,
                        "qty": qty
                    })
                current_item = []

    except:
        items = []

    return {
        "tracking_number": tracking_number.group(1) if tracking_number else "",
        "in_transit_by": in_transit_by.group(1) if in_transit_by else "",
        "order_id": order_id.group(1) if order_id else "",
        "items": items,
        "total_qty": qty_total.group(1) if qty_total else "",
        "seller_note": seller_note.group(1) if seller_note else ""
    }

def wrap_text(c, text, width_inches):
    max_width = width_inches * inch
    words = text.split()
    lines = []
    current_line = []
    
    for word in words:
        current_line.append(word)
        line_width = c.stringWidth(' '.join(current_line), "Helvetica-Bold", 10)
        if line_width > max_width:
            if len(current_line) > 1:
                current_line.pop()
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    
    if current_line:
        lines.append(' '.join(current_line))
    return lines
def extract_and_create_slip(page_text):
    info = parse_packing_slip(page_text)
    if not info or not info['items']:
        return None
        
    # Use original slip if more than 5 items
    if len(info['items']) > 4:
        return None

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(4 * inch, 6 * inch))
    
    # Set margins
    margin = 0.2 * inch
    y_pos = 5.8 * inch  # Start from top with margin
    
    # Header info with larger font
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y_pos, f"Order ID: {info['order_id']}")
    y_pos -= 20
    c.drawString(margin, y_pos, f"Tracking #: {info['tracking_number']}")
    y_pos -= 25

    # Items section with improved spacing
    for item in info['items']:
        # Product name with larger font
        c.setFont("Helvetica-Bold", 14)
        wrapped_lines = wrap_text(c, item['product_name'], 3.6)
        for line in wrapped_lines[:2]:
            c.drawString(margin, y_pos, line)
            y_pos -= 18
        
        # Item details with larger font and more spacing
        c.setFont("Helvetica-Bold", 16)
        c.drawString(margin, y_pos, f"Color: {item['color']}")
        y_pos -= 20
# Draw SKU text with underline
        text = f"SKU: {item['seller_sku']} x {item['qty']}"
        c.drawString(margin, y_pos, text)
        c.line(margin, y_pos - 2, margin + c.stringWidth(text, "Helvetica-Bold", 16), y_pos - 2)
        y_pos -= 25
    
    # Footer info with larger font
    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y_pos, f"Total Qty: {info['total_qty']}")
    y_pos -= 20
    
    if info.get("seller_note"):
        c.setFont("SimSun", 14)
        note_text = f"Note: {info['seller_note']}"
        wrapped_note = wrap_text(c, note_text, 3.6)
        for line in wrapped_note[:1]:
            c.drawString(margin, y_pos, line)
    
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.read()

@tk_label_zoom_bp.route('/tk_label_zoom', methods=['GET', 'POST'])
def tk_label_zoom():
    if request.method == 'POST':
        uploaded_files = request.files.getlist('pdf')
        new_doc = fitz.open()
        
        # Initialize tracking variables
        total_pages = 0
        total_shipping_labels = 0
        total_items = 0
        final_page_count = 0

        for uploaded_file in uploaded_files:
            if uploaded_file and uploaded_file.filename.endswith('.pdf'):
                file_data = uploaded_file.read()
                original_pdf = fitz.open(stream=file_data, filetype='pdf')
                packing_texts = [original_pdf[i].get_text() for i in range(len(original_pdf)) if i % 2 == 1]
                
                # Update metrics
                total_shipping_labels += len(original_pdf) // 2
                
                # Count items from packing slips
                for text in packing_texts:
                    info = parse_packing_slip(text)
                    if info and info['items']:
                        total_items += sum(int(item['qty']) for item in info['items'])

                with multiprocessing.Pool() as pool:
                    new_slips = pool.map(extract_and_create_slip, packing_texts)

                slip_index = 0
                for i in range(len(original_pdf)):
                    if i % 2 == 0:
                        new_doc.insert_pdf(original_pdf, from_page=i, to_page=i)
                        final_page_count += 1
                    else:
                        if new_slips[slip_index]:
                            slip_pdf = fitz.open(stream=new_slips[slip_index], filetype='pdf')
                            new_doc.insert_pdf(slip_pdf)
                            final_page_count += 1
                        else:
                            new_doc.insert_pdf(original_pdf, from_page=i, to_page=i)
                            final_page_count += 1
                        slip_index += 1

        fd, tmp_path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        new_doc.save(tmp_path)
        new_doc.close()

        # Return both the file and the metrics
        response = send_file(tmp_path, as_attachment=True, download_name='combined_slips.pdf', mimetype='application/pdf')
        response.headers['X-Total-Pages'] = str(final_page_count)
        response.headers['X-Total-Labels'] = str(total_shipping_labels)
        response.headers['X-Total-Items'] = str(total_items)
        return response
            
    return render_template('tk_label_zoom.html')

