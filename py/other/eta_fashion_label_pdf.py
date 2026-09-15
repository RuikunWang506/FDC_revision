"""ETA Fashion (00951) shipping labels: US Letter 4-in-1 or landscape 6×4 one per page."""

from __future__ import annotations

import io
from pathlib import Path

import pyodbc
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from py.order.order_search import sql_server_config

ETA_FASHION_CUSTOMER_ID = "00951"

PAGE_W, PAGE_H = letter  # 8.5 × 11 portrait
LABEL_W = PAGE_W
LABEL_H = 2.75 * inch
LABELS_PER_PAGE = 4

# Label 4×6 printer stock: landscape 6×4 (horizontal), one label per page
PAGE_4X6_W = 6 * inch
PAGE_4X6_H = 4 * inch

MARGIN_L = 0.28 * inch
MARGIN_R = 0.22 * inch
MARGIN_T = 0.10 * inch
MARGIN_B = 0.10 * inch
GAP_LOGO_TEXT = 0.10 * inch

FONT_COMPANY_1 = 30
FONT_COMPANY_2 = 24
FONT_FIELD = 18
FONT_VALUE = 19

COMPANY_LINE_1 = "COMERCIAL ETATEX C.A"
COMPANY_LINE_2 = "Quito \u2013 Ecuador"
VENDOR_VALUE = "FDC"


def _database_for_order(order_number: str) -> str | None:
    order_number = str(order_number).strip()
    if len(order_number) == 5:
        return "omsdata_FDC"
    if len(order_number) == 4:
        return "oms_EF"
    return None


def fetch_order_customer_id(order_number: str) -> tuple[str | None, str | None]:
    """Return normalized customer id for order, or (None, error)."""
    order_number = str(order_number).strip()
    database_name = _database_for_order(order_number)
    if not database_name:
        return None, "Invalid order number. Use a 4- or 5-digit order #."

    try:
        conn = pyodbc.connect(
            f"DRIVER={sql_server_config['driver']};SERVER={sql_server_config['server']};"
            f"DATABASE={database_name};UID={sql_server_config['username']};PWD={sql_server_config['password']}"
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT LTRIM(RTRIM(cus_id)) FROM dbo.orders WHERE ord_num = ?",
            (order_number,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return None, str(e)

    if not row or not row[0]:
        return None, f"Order {order_number} not found."
    return str(row[0]).strip(), None


def _eta_logo_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "static" / "images" / "etafashion_logo.png"


def _logo_box_size() -> float:
    """Max square logo box (1:1) that fits label height and right column."""
    by_height = LABEL_H - MARGIN_T - MARGIN_B
    by_width = 2.05 * inch
    return min(by_height, by_width)


def _draw_etafashion_logo(c, box_x: float, box_y: float, box_size: float, logo_path: Path | None) -> None:
    """Draw logo centered in a square box; preserve image aspect ratio."""
    if logo_path and logo_path.is_file():
        from reportlab.lib.utils import ImageReader

        reader = ImageReader(str(logo_path))
        iw, ih = reader.getSize()
        if iw > 0 and ih > 0:
            scale = min(box_size / iw, box_size / ih)
            w, h = iw * scale, ih * scale
            draw_x = box_x + (box_size - w) / 2
            draw_y = box_y + (box_size - h) / 2
            c.drawImage(
                reader,
                draw_x,
                draw_y,
                width=w,
                height=h,
                preserveAspectRatio=True,
                mask="auto",
            )
            return

    c.setFillColor(colors.black)
    c.rect(box_x, box_y, box_size, box_size, fill=1, stroke=0)
    font = "Helvetica-Bold"
    font_size = max(12, int(box_size * 0.14))
    text = "ETAFASHION"
    c.setFillColor(colors.white)
    c.setFont(font, font_size)
    tw = stringWidth(text, font, font_size)
    ascent = font_size * 0.72
    c.drawString(box_x + (box_size - tw) / 2, box_y + (box_size - ascent) / 2, text)
    c.setFillColor(colors.black)


def _row_baselines() -> list[tuple[float, float]]:
    """Return (y_baseline, font_size) for each text row, top to bottom."""
    gap_after = (14, 16, 18)  # pts between rows
    rows = [
        (FONT_COMPANY_1, gap_after[0]),
        (FONT_COMPANY_2, gap_after[1]),
        (FONT_FIELD, gap_after[2]),
        (FONT_FIELD, 0),
    ]
    y = LABEL_H - MARGIN_T - 6
    out: list[tuple[float, float]] = []
    for font_size, gap in rows:
        y -= font_size
        out.append((y, font_size))
        y -= gap
    return out


def _draw_underline_value(
    c,
    x: float,
    y: float,
    label: str,
    value: str,
    line_len: float,
    *,
    label_font: str = "Helvetica-Bold",
    label_size: float = FONT_FIELD,
    value_font: str = "Helvetica-Bold",
    value_size: float = FONT_VALUE,
) -> float:
    """Draw 'Label ____ value' and return x after the line."""
    c.setFont(label_font, label_size)
    c.drawString(x, y, label)
    x_line = x + stringWidth(label, label_font, label_size) + 0.10 * inch
    y_line = y - 3
    c.setLineWidth(1.1)
    c.line(x_line, y_line, x_line + line_len, y_line)
    if value:
        c.setFont(value_font, value_size)
        c.drawString(x_line + 0.08 * inch, y, value)
    return x_line + line_len


def _fit_font_size(text: str, font: str, max_width: float, max_size: float, min_size: float = 10) -> float:
    """Largest font size in [min_size, max_size] whose text fits max_width."""
    size = float(max_size)
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _draw_eta_label(c, box_num: int, total_boxes: int, logo_path: Path | None) -> None:
    """Draw one 8.5×2.75 label in current translated coordinates (origin bottom-left)."""
    x = MARGIN_L
    logo_box = _logo_box_size()
    logo_x = LABEL_W - MARGIN_R - logo_box
    logo_y = LABEL_H - MARGIN_T - logo_box
    text_right = logo_x - GAP_LOGO_TEXT

    _draw_etafashion_logo(c, logo_x, logo_y, logo_box, logo_path)

    baselines = _row_baselines()
    y1, _ = baselines[0]
    y2, _ = baselines[1]
    y_vendor, _ = baselines[2]
    y_box, _ = baselines[3]

    c.setFont("Helvetica-Bold", FONT_COMPANY_1)
    c.drawString(x, y1, COMPANY_LINE_1)

    c.setFont("Helvetica-Bold", FONT_COMPANY_2)
    c.drawString(x, y2, COMPANY_LINE_2)

    vendor_line_len = max(text_right - x - 0.2 * inch, 3.0 * inch)
    _draw_underline_value(
        c,
        x,
        y_vendor,
        "Vendor",
        VENDOR_VALUE,
        vendor_line_len,
        label_size=FONT_FIELD,
        value_size=FONT_VALUE,
    )

    field_font = "Helvetica-Bold"
    c.setFont(field_font, FONT_FIELD)
    c.drawString(x, y_box, "Box Number")
    x_after = x + stringWidth("Box Number", field_font, FONT_FIELD) + 0.10 * inch
    num_line = 0.85 * inch
    y_line = y_box - 3
    c.setLineWidth(1.1)
    c.line(x_after, y_line, x_after + num_line, y_line)
    c.setFont(field_font, FONT_VALUE)
    c.drawString(x_after + 0.08 * inch, y_box, str(box_num))

    x_of = x_after + num_line + 0.18 * inch
    c.setFont(field_font, FONT_FIELD)
    c.drawString(x_of, y_box, "Of")
    x_after_of = x_of + stringWidth("Of", field_font, FONT_FIELD) + 0.10 * inch
    c.line(x_after_of, y_line, x_after_of + num_line, y_line)
    c.setFont(field_font, FONT_VALUE)
    c.drawString(x_after_of + 0.08 * inch, y_box, str(total_boxes))


def _draw_box_number_of(c, x: float, y: float, box_num: int, total_boxes: int, right: float, field_size: float, value_size: float) -> None:
    """Draw 'Box Number __N__ Of __T__' fitted into [x, right]."""
    font = "Helvetica-Bold"
    gap = 0.10 * inch
    mid_gap = 0.14 * inch
    label_box_w = stringWidth("Box Number", font, field_size)
    label_of_w = stringWidth("Of", font, field_size)
    usable = right - x - label_box_w - label_of_w - gap * 2 - mid_gap
    num_line = max(usable / 2.0, 0.55 * inch)

    x_num = x + label_box_w + gap
    x_of = x_num + num_line + mid_gap
    x_tot = x_of + label_of_w + gap
    y_line = y - 3

    c.setFillColor(colors.black)
    c.setFont(font, field_size)
    c.drawString(x, y, "Box Number")
    c.setLineWidth(1.1)
    c.line(x_num, y_line, x_num + num_line, y_line)
    c.setFont(font, value_size)
    c.drawString(x_num + 0.06 * inch, y, str(box_num))

    c.setFont(font, field_size)
    c.drawString(x_of, y, "Of")
    c.line(x_tot, y_line, min(x_tot + num_line, right), y_line)
    c.setFont(font, value_size)
    c.drawString(x_tot + 0.06 * inch, y, str(total_boxes))


def _draw_eta_label_4x6(c, box_num: int, total_boxes: int, logo_path: Path | None) -> None:
    """Draw one landscape 6×4 label that fills the page (not a scaled letter strip)."""
    page_w, page_h = PAGE_4X6_W, PAGE_4X6_H
    ml, mr, mt, mb = 0.24 * inch, 0.18 * inch, 0.16 * inch, 0.16 * inch
    x = ml
    header_w = page_w - ml - mr

    company_size = _fit_font_size(COMPANY_LINE_1, "Helvetica-Bold", header_w, 28, 16)
    y_company = page_h - mt - company_size
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", company_size)
    c.drawString(x, y_company, COMPANY_LINE_1)

    city_size = _fit_font_size(COMPANY_LINE_2, "Helvetica-Bold", header_w, min(20.0, company_size - 4), 12)
    y_city = y_company - 0.12 * inch - city_size
    c.setFont("Helvetica-Bold", city_size)
    c.drawString(x, y_city, COMPANY_LINE_2)

    box_size = 18
    box_value = 22
    y_box = mb + 0.08 * inch
    _draw_box_number_of(c, x, y_box, box_num, total_boxes, page_w - mr, box_size, box_value)

    band_top = y_city - 0.16 * inch
    band_bottom = y_box + box_value + 0.14 * inch
    band_h = max(band_top - band_bottom, 1.3 * inch)

    logo_box = min(band_h, 2.35 * inch)
    logo_x = page_w - mr - logo_box
    logo_y = band_bottom + (band_h - logo_box) / 2
    _draw_etafashion_logo(c, logo_x, logo_y, logo_box, logo_path)

    field_right = logo_x - 0.14 * inch
    field_size = 17
    value_size = 21
    vendor_y = band_bottom + band_h * 0.42
    vendor_label_w = stringWidth("Vendor", "Helvetica-Bold", field_size)
    vendor_line = max(field_right - x - vendor_label_w - 0.12 * inch, 1.2 * inch)
    _draw_underline_value(
        c,
        x,
        vendor_y,
        "Vendor",
        VENDOR_VALUE,
        vendor_line,
        label_size=field_size,
        value_size=value_size,
    )


def _label_origin_y(slot: int) -> float:
    """Slot 0 = top label on sheet."""
    return PAGE_H - (slot + 1) * LABEL_H


def build_eta_fashion_label_pdf_letter(total_boxes: int) -> bytes:
    """One label per box; four 8.5×2.75 labels per US Letter page."""
    total = int(total_boxes)
    if total < 1:
        raise ValueError("Total boxes must be at least 1.")

    logo_path = _eta_logo_path()
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))

    for box_num in range(1, total + 1):
        idx = box_num - 1
        if idx > 0 and idx % LABELS_PER_PAGE == 0:
            pdf.showPage()

        slot = idx % LABELS_PER_PAGE
        origin_y = _label_origin_y(slot)
        pdf.saveState()
        pdf.translate(0, origin_y)
        _draw_eta_label(pdf, box_num, total, logo_path)
        pdf.restoreState()

    pdf.save()
    buf.seek(0)
    return buf.getvalue()


def build_eta_fashion_label_pdf_4x6(total_boxes: int) -> bytes:
    """One label per box on landscape 6×4 pages (horizontal 4×6 stock)."""
    total = int(total_boxes)
    if total < 1:
        raise ValueError("Total boxes must be at least 1.")

    logo_path = _eta_logo_path()
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(PAGE_4X6_W, PAGE_4X6_H))

    for box_num in range(1, total + 1):
        if box_num > 1:
            pdf.showPage()
        _draw_eta_label_4x6(pdf, box_num, total, logo_path)

    pdf.save()
    buf.seek(0)
    return buf.getvalue()


def build_eta_fashion_label_pdf(total_boxes: int) -> bytes:
    """Alias for US Letter 4-in-1 layout."""
    return build_eta_fashion_label_pdf_letter(total_boxes)


def ensure_eta_logo_asset() -> None:
    """Create static/images/etafashion_logo.png if missing (black tile + white text)."""
    path = _eta_logo_path()
    if path.is_file():
        return
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return

    size = 200
    img = Image.new("RGB", (size, size), "black")
    draw = ImageDraw.Draw(img)
    text = "ETAFASHION"
    try:
        font = ImageFont.truetype("arialbd.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2), text, fill="white", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
