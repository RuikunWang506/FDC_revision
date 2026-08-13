import os
import math
import sys
import tempfile
import threading
import barcode
from barcode import Code128
from barcode.writer import ImageWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
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

# DYMO 1933084 / 30334 multi-purpose: 2-1/4" x 1-1/4" on LabelWriter 450 Turbo
DYMO_LABEL_WIDTH = 2.25 * inch
DYMO_LABEL_HEIGHT = 1.25 * inch
DYMO_PRINTER_DEFAULT = "DYMO LabelWriter 450 Turbo"
DYMO_MAX_LABELS = 5000

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
            additional_text = parts[0].strip().upper()
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


def generate_dymo_barcode_image(barcode_value):
    """Code128 barcode PNG tuned for a 2.25\" Dymo label (no human-readable digits)."""
    from io import BytesIO
    from PIL import Image

    Barcode.default_writer_options['write_text'] = False
    code128 = barcode.get_barcode_class('code128')
    barcode_img = code128(str(barcode_value), writer=ImageWriter())

    fp = BytesIO()
    barcode_img.write(fp, options={
        'module_width': 0.5,
        'module_height': 12.0,
        'quiet_zone': 1.2,
        'write_text': False,
        'font_size': 0,
        'text_distance': 1,
        'dpi': 300,
    })
    fp.seek(0)
    return Image.open(fp).convert("RGB")


def _dymo_barcode_reader(pil_image):
    from io import BytesIO

    buf = BytesIO()
    pil_image.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _fit_centered_text(c, text, font_name, max_width, max_size, min_size=7):
    size = max_size
    while size > min_size and c.stringWidth(text, font_name, size) > max_width:
        size -= 0.5
    return size


def generate_dymo_labels_pdf(input_data):
    """One 2.25\" x 1.25\" page per sticker: barcode, digits, then SKU."""
    from io import BytesIO

    data = process_input_data(input_data)
    if not data:
        raise ValueError("No valid SKU / barcode / quantity rows to print.")

    total = sum(item[2] for item in data)
    if total > DYMO_MAX_LABELS:
        raise ValueError(f"Too many labels ({total}). Maximum is {DYMO_MAX_LABELS}.")

    bio = BytesIO()
    c = canvas.Canvas(bio, pagesize=(DYMO_LABEL_WIDTH, DYMO_LABEL_HEIGHT))

    margin_x = 0.07 * inch
    margin_top = 0.05 * inch
    text_max_width = DYMO_LABEL_WIDTH - (2 * margin_x)
    barcode_w = text_max_width
    # Previous barcode band was 0.80"; shorten by 1/3.
    barcode_h = 0.80 * inch * (2.0 / 3.0)
    barcode_x = margin_x
    barcode_y = DYMO_LABEL_HEIGHT - margin_top - barcode_h
    barcode_num_y = 0.38 * inch
    sku_y = 0.09 * inch

    for additional_text, barcode_value, barcode_quantity, _pil in data:
        dymo_image = generate_dymo_barcode_image(barcode_value)
        img_reader = _dymo_barcode_reader(dymo_image)
        sku = (additional_text or "").strip().upper()
        barcode_text = str(barcode_value or "").strip()
        sku_font = _fit_centered_text(c, sku, "Helvetica-Bold", text_max_width, 16, 8)
        num_font = _fit_centered_text(c, barcode_text, "Helvetica-Bold", text_max_width, 14, 8)

        for _ in range(barcode_quantity):
            c.drawImage(
                img_reader,
                barcode_x,
                barcode_y,
                width=barcode_w,
                height=barcode_h,
                preserveAspectRatio=False,
            )
            c.setFont("Helvetica-Bold", num_font)
            num_w = c.stringWidth(barcode_text, "Helvetica-Bold", num_font)
            c.drawString((DYMO_LABEL_WIDTH - num_w) / 2.0, barcode_num_y, barcode_text)
            c.setFont("Helvetica-Bold", sku_font)
            sku_w = c.stringWidth(sku, "Helvetica-Bold", sku_font)
            c.drawString((DYMO_LABEL_WIDTH - sku_w) / 2.0, sku_y, sku)
            c.showPage()

    c.save()
    bio.seek(0)
    return bio.getvalue(), total


def build_dymo_preview(input_data):
    """Unique SKUs with barcode PNG (base64) for the design popup."""
    import base64
    from io import BytesIO

    data = process_input_data(input_data)
    if not data:
        raise ValueError("No valid SKU / barcode / quantity rows to preview.")

    items = []
    total = 0
    for sku, barcode_value, qty, _pil in data:
        image = generate_dymo_barcode_image(barcode_value)
        buf = BytesIO()
        image.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        items.append({
            "sku": str(sku or "").upper(),
            "barcode": barcode_value,
            "quantity": int(qty),
            "barcode_png": f"data:image/png;base64,{b64}",
        })
        total += int(qty)
    return items, total


def resolve_dymo_printer_name():
    """Match DYMO_LABEL_PRINTER env (default LabelWriter 450 Turbo) to an installed printer."""
    configured = os.getenv("DYMO_LABEL_PRINTER", DYMO_PRINTER_DEFAULT).strip()
    if sys.platform != "win32":
        return configured

    try:
        import win32print
    except ImportError as exc:
        raise RuntimeError("pywin32 is required to print to the Dymo.") from exc

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    installed = [entry[2] for entry in win32print.EnumPrinters(flags)]
    if not installed:
        raise RuntimeError("No Windows printers were found on this server.")

    if configured in installed:
        return configured

    configured_l = configured.lower()
    for name in installed:
        if name.lower() == configured_l:
            return name

    for name in installed:
        lower = name.lower()
        if "dymo" in lower and "450" in lower:
            return name

    for name in installed:
        if "dymo" in name.lower():
            return name

    sample = ", ".join(sorted(installed)[:8])
    raise RuntimeError(
        f'Printer "{configured}" was not found. Installed printers include: {sample}'
    )


def _find_dymo_1933084_paper(printer_name):
    """Return (paper_id, width_tenths_mm, length_tenths_mm, form_name) for 2.25\" x 1.25\"."""
    import win32con
    import win32print

    papers = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERS) or []
    names = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERNAMES) or []
    sizes = win32print.DeviceCapabilities(printer_name, "", win32con.DC_PAPERSIZE) or []

    def _size_xy(sz):
        if isinstance(sz, dict):
            return int(sz.get("x") or 0), int(sz.get("y") or 0)
        if isinstance(sz, (tuple, list)) and len(sz) >= 2:
            return int(sz[0]), int(sz[1])
        return 0, 0

    ranked = []
    for index, paper_id in enumerate(papers):
        form_name = names[index] if index < len(names) else ""
        width, length = _size_xy(sizes[index] if index < len(sizes) else None)
        lower = str(form_name).lower()
        # 571 x 317 tenths-mm = 2.25" x 1.25"
        size_match = (
            abs(width - 571) <= 8 and abs(length - 317) <= 8
        ) or (
            abs(width - 317) <= 8 and abs(length - 571) <= 8
        )
        if "30334" in lower:
            rank = 0
        elif "1933084" in lower or "11354" in lower:
            rank = 1
        elif "2-1/4" in lower and "1-1/4" in lower:
            rank = 2
        elif size_match:
            rank = 3
        else:
            continue
        ranked.append((rank, int(paper_id), width, length, str(form_name)))

    if not ranked:
        return None
    ranked.sort()
    _rank, paper_id, width, length, form_name = ranked[0]
    return paper_id, width, length, form_name


def _set_dymo_paper(printer_name, paper_id, paper_width, paper_length, form_name):
    """Select 1933084/30334 stock (2.25 x 1.25) on the Dymo driver."""
    import win32con
    import win32print

    handle = win32print.OpenPrinter(printer_name, {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
    try:
        info = win32print.GetPrinter(handle, 2)
        devmode = info.get("pDevMode")
        if not devmode:
            raise RuntimeError("Dymo printer did not return a DEVMODE.")
        devmode.PaperSize = int(paper_id)
        if paper_width:
            devmode.PaperWidth = int(paper_width)
        if paper_length:
            devmode.PaperLength = int(paper_length)
        if form_name:
            devmode.FormName = form_name
        devmode.Orientation = getattr(win32con, "DMORIENT_PORTRAIT", 1)
        fields = int(getattr(devmode, "Fields", 0) or 0)
        fields |= (
            win32con.DM_PAPERSIZE
            | win32con.DM_PAPERWIDTH
            | win32con.DM_PAPERLENGTH
            | win32con.DM_ORIENTATION
            | win32con.DM_FORMNAME
        )
        devmode.Fields = fields
        info["pDevMode"] = devmode
        win32print.SetPrinter(handle, 2, info, 0)
    finally:
        win32print.ClosePrinter(handle)


def _print_dymo_pdf_via_gdi(path, printer_name, logger):
    try:
        import pypdfium2 as pdfium
        import win32ui
        from PIL import ImageWin
    except ImportError:
        return False

    paper = _find_dymo_1933084_paper(printer_name)
    if paper:
        paper_id, paper_width, paper_length, form_name = paper
        try:
            _set_dymo_paper(
                printer_name, paper_id, paper_width, paper_length, form_name
            )
            logger.info(
                "Selected Dymo stock %s (id=%s, %s x %s tenths-mm)",
                form_name,
                paper_id,
                paper_width,
                paper_length,
            )
        except Exception as exc:
            logger.warning("Could not select Dymo 1933084/30334 paper size: %s", exc)
    else:
        logger.warning("No 2.25 x 1.25 Dymo form found; printing with the driver default.")

    dpi = max(72, min(600, int(os.getenv("DYMO_LABEL_PRINT_DPI", "300"))))
    scale = dpi / 72.0
    doc = pdfium.PdfDocument(path)
    page_count = len(doc)
    try:
        if page_count < 1:
            return False

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        hdc.SetMapMode(1)  # MM_TEXT
        printable_w = hdc.GetDeviceCaps(8)
        printable_h = hdc.GetDeviceCaps(10)
        first = doc[0]
        pdf_w = float(first.get_width())
        pdf_h = float(first.get_height())
        pdf_landscape = pdf_w >= pdf_h
        page_landscape = printable_w >= printable_h
        # Default 0: PDF is already 2.25" wide x 1.25" tall, matching 30334 stock.
        # Previous auto-90 ran because the driver was still on 30252 address labels.
        rotation = 0
        explicit = os.getenv("DYMO_LABEL_PRINT_ROTATION", "").strip()
        if explicit.isdigit():
            rotation = int(explicit) % 360
        elif pdf_landscape and not page_landscape:
            rotation = 90
        elif not pdf_landscape and page_landscape:
            rotation = 270

        logger.info(
            "Dymo printable %sx%s px (%.2f x %.2f in), pdf %.0fx%.0f, rotation=%s",
            printable_w,
            printable_h,
            printable_w / max(hdc.GetDeviceCaps(88), 1),
            printable_h / max(hdc.GetDeviceCaps(90), 1),
            pdf_w,
            pdf_h,
            rotation,
        )

        hdc.StartDoc("Loris Dymo 1933084")
        try:
            for index in range(page_count):
                page = doc[index]
                bitmap = page.render(scale=scale, rotation=rotation)
                try:
                    pil_image = bitmap.to_pil()
                finally:
                    bitmap.close()

                img_w, img_h = pil_image.size
                fit = min(printable_w / img_w, printable_h / img_h)
                dest_w = max(1, int(img_w * fit))
                dest_h = max(1, int(img_h * fit))
                x0 = (printable_w - dest_w) // 2
                y0 = (printable_h - dest_h) // 2

                hdc.StartPage()
                dib = ImageWin.Dib(pil_image)
                dib.draw(hdc.GetHandleOutput(), (x0, y0, x0 + dest_w, y0 + dest_h))
                hdc.EndPage()
        finally:
            hdc.EndDoc()
            hdc.DeleteDC()
    finally:
        doc.close()

    logger.info(
        "Printed Dymo labels via Win32 GDI to %s (%d label(s) at %d DPI, rotation=%s)",
        printer_name,
        page_count,
        dpi,
        rotation,
    )
    return True


def print_dymo_pdf(pdf_bytes, printer_name, logger):
    """Send a 2.25\" x 1.25\" label PDF to the Dymo on this Windows host."""
    if sys.platform != "win32":
        raise RuntimeError("Direct Dymo printing is only supported on the Windows server.")

    printer_name = resolve_dymo_printer_name() if not printer_name else printer_name

    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="fdc_dymo_")
    os.close(fd)
    abs_path = os.path.abspath(path)
    try:
        with open(path, "wb") as handle:
            handle.write(pdf_bytes)

        def _cleanup():
            try:
                os.remove(path)
            except OSError:
                pass

        try:
            if _print_dymo_pdf_via_gdi(abs_path, printer_name, logger):
                threading.Timer(120.0, _cleanup).start()
                return printer_name
        except Exception as exc:
            logger.warning("Dymo GDI print failed, falling back: %s", exc)

        from py.Toolkits.Label_Maker import _print_pdf_to_windows_printer

        _print_pdf_to_windows_printer(pdf_bytes, printer_name, logger)
        _cleanup()
        return printer_name
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

if __name__ == "__main__":
    input_data = """test1	013000006057	44
tef34	013000001243	18"""
    main(input_data)