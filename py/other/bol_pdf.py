"""US Letter Bill of Lading PDF with fillable AcroForm fields."""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

import pyodbc
from reportlab.lib.colors import white
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from py.order.order_search import (
    fetch_order_notes,
    fetch_order_ship_to_lines,
    parse_boxes_from_notes,
    sql_server_config,
)

BOL_DATA_ROWS = 5

COMPANY_HEADER_BY_DIGITS = {
    5: {
        "title": "FASHION DISTRIBUTION CENTER INC.",
        "address": "827 S LAWSON STREET, CITY OF INDUSTRY, CA 91748",
        "tel_fax": "TEL: 213-748-9999  FAX: 213-748-9977",
    },
    4: {
        "title": "E-FASHION INC.",
        "address": "827 S LAWSON STREET, CITY OF INDUSTRY, CA 91748",
        "tel_fax": "TEL: 213-398-9999  FAX: 213-398-9977",
    },
}

SHIPPER_BY_DIGITS = {
    5: {
        "name": "Fashion Distribution Center Inc",
        "address": "827 S LAWSON STREET",
        "city": "CITY OF INDUSTRY, CA 91748",
        "phone": "213-748-9999",
    },
    4: {
        "name": "E-fashion Inc",
        "address": "827 S LAWSON STREET",
        "city": "CITY OF INDUSTRY, CA 91748",
        "phone": "213-398-9999",
    },
}

PAGE_W, PAGE_H = letter
MARGIN = 0.4 * inch
BORDER_W = 2.5
TABLE_GRID_W = 1
HEADER_RULE_GAP = 28  # space below company rule before BILL OF LADING title

FONT_COMPANY_TITLE = 15
FONT_COMPANY_LINE = 11
FONT_TITLE = 17
FONT_SUBTITLE = 11
FONT_LABEL = 11
FONT_TABLE_HDR = 9
FONT_TOTAL = 11
FONT_FOOTER = 10
FONT_FIELD = 11

EMPTY_ROW = {"po": "", "description": "", "qty": "", "cases": "", "weight": "", "pallet": ""}
_UCL_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "static" / "forms" / "UCL_Bill_of_Lading_23.pdf"
)
UCL_SHIPPER_CUSTOMER_ID = "5005"


def _blank_if_na(raw) -> str:
    s = str(raw or "").strip()
    if not s or s.upper() in ("N/A", "NA", "NONE", "NULL"):
        return ""
    return s


def _format_phone(raw) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"{digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    s = str(raw or "").strip()
    return s if s else ""


def _format_bol_date(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%d-%b-%y")


def _cases_for_line(qty: int, per_box: int, descrip: str) -> int:
    if per_box and per_box > 0 and qty > 0:
        full, rem = divmod(int(qty), int(per_box))
        return full + (1 if rem else 0)
    text = str(descrip or "").strip()
    m = re.match(r"^(\d+)", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 0 if qty <= 0 else 1


def _database_for_order(order_number: str) -> str | None:
    order_number = str(order_number).strip()
    if len(order_number) == 5:
        return "omsdata_FDC"
    if len(order_number) == 4:
        return "oms_EF"
    return None


def _fetch_case_weights(skus: list[str], database_name: str) -> dict[str, float]:
    if not skus:
        return {}
    cleaned = []
    seen = set()
    for sku in skus:
        key = str(sku or "").replace("\u00a0", " ").strip().upper()
        if key and key not in seen:
            seen.add(key)
            cleaned.append(key)
    if not cleaned:
        return {}

    placeholders = ",".join("?" for _ in cleaned)
    weights: dict[str, float] = {}
    try:
        conn = pyodbc.connect(
            f"DRIVER={sql_server_config['driver']};SERVER={sql_server_config['server']};"
            f"DATABASE={database_name};UID={sql_server_config['username']};PWD={sql_server_config['password']}"
        )
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT UPPER(LTRIM(RTRIM(PROD_CD))) AS sku, CASE_WT
            FROM dbo.inv
            WHERE UPPER(LTRIM(RTRIM(PROD_CD))) IN ({placeholders})
            """,
            cleaned,
        )
        for row in cur.fetchall():
            sku_key = str(row[0] or "").strip().upper()
            try:
                wt = float(row[1]) if row[1] is not None else 0.0
            except (TypeError, ValueError):
                wt = 0.0
            if sku_key and wt > 0:
                weights[sku_key] = wt
        cur.close()
        conn.close()
    except Exception:
        pass
    return weights


def fetch_order_bol_context(order_number: str) -> tuple[dict | None, str | None]:
    """Load header, ship-to, and line rows for BOL prefilling."""
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
            """
            SELECT
                c.PHONE,
                ol.COMM_LN,
                ol.UT_DESC,
                ol.PROD_CD,
                CAST(ol.ORDER_QTY AS INT) AS qty,
                CAST(ISNULL(ol.PC_UNIT, 0) AS INT) AS per_box,
                LTRIM(RTRIM(o.CUS_ID)) AS cus_id,
                o.ATTN,
                o.ORD_AMT
            FROM dbo.orders o
            INNER JOIN dbo.customer c ON o.cus_id = c.cus_id
            INNER JOIN dbo.ord_log ol ON o.ord_num = ol.ord_num
            WHERE o.ord_num = ?
            ORDER BY ol.COMM_LN
            """,
            (order_number,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        return None, str(e)

    if not rows:
        return None, f"Order {order_number} not found."

    ship_phone = _format_phone(rows[0][0])
    customer_id = _blank_if_na(rows[0][6])
    attention = _blank_if_na(rows[0][7])
    try:
        invoice_value = float(rows[0][8]) if rows[0][8] is not None else 0.0
    except (TypeError, ValueError):
        invoice_value = 0.0

    ship_to_lines, ship_err = fetch_order_ship_to_lines(order_number)
    if ship_err:
        return None, ship_err
    ship_to_address_text = "\n".join(ship_to_lines or [])
    if ship_phone:
        ship_to_lines = list(ship_to_lines) + [ship_phone]

    digit_len = len(order_number)
    shipper = SHIPPER_BY_DIGITS.get(digit_len, SHIPPER_BY_DIGITS[5])
    company = COMPANY_HEADER_BY_DIGITS.get(digit_len, COMPANY_HEADER_BY_DIGITS[5])
    shipper_text = "\n".join(
        [shipper["name"], shipper["address"], shipper["city"], shipper["phone"]]
    )
    shipper_address_text = "\n".join(
        [shipper["name"], shipper["address"], shipper["city"]]
    )

    skus = [str(r[3] or "").strip() for r in rows]
    case_weights = _fetch_case_weights(skus, database_name)

    total_qty = 0
    total_cases = 0
    total_weight = 0.0

    for row in rows:
        descrip = str(row[2] or "").strip()
        sku = str(row[3] or "").replace("\u00a0", " ").strip()
        qty = int(row[4] or 0)
        per_box = int(row[5] or 0)
        cases = _cases_for_line(qty, per_box, descrip)
        case_wt = case_weights.get(sku.upper(), 0.0)
        line_weight = round(case_wt * cases, 2) if case_wt and cases else 0.0

        total_qty += qty
        total_cases += cases
        total_weight += line_weight

    notes_boxes = parse_boxes_from_notes(fetch_order_notes(order_number))
    boxes = notes_boxes if notes_boxes else (total_cases or "")
    weight_val = round(total_weight, 2) if total_weight else ""
    declared_value = round(invoice_value, 2) if invoice_value else ""

    first_row = {
        "po": order_number,
        "description": "handbags",
        "qty": total_qty,
        "cases": total_cases,
        "weight": weight_val,
        "pallet": "",
    }
    data_rows = [first_row] + [dict(EMPTY_ROW) for _ in range(BOL_DATA_ROWS - 1)]

    return (
        {
            "order_number": order_number,
            "date": _format_bol_date(),
            "date_mdy": datetime.now().strftime("%m/%d/%Y"),
            "bol_number": order_number,
            "pickup_confirm": "",
            "company_header": company,
            "customer_id": customer_id,
            "attention": attention,
            "shipper": shipper,
            "shipper_text": shipper_text,
            "shipper_address_text": shipper_address_text,
            "shipper_phone": shipper.get("phone", ""),
            "ship_to_text": "\n".join(ship_to_lines),
            "ship_to_address_text": ship_to_address_text,
            "ship_to_phone": ship_phone,
            "data_rows": data_rows,
            "total_qty": total_qty,
            "total_cases": total_cases,
            "total_weight": weight_val,
            "boxes": boxes,
            "description_text": "handbags",
            "declared_value": declared_value,
            "total_pallet": "",
            "remark": "",
        },
        None,
    )


def _ucl_field_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ucl_boxes_label(boxes) -> str:
    raw = _ucl_field_str(boxes)
    if not raw:
        return ""
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return raw
    if n <= 0:
        return ""
    return "1 box" if n == 1 else f"{n} boxes"


def _ucl_weight_lb(weight) -> str:
    raw = _ucl_field_str(weight)
    if not raw:
        return ""
    try:
        w = float(raw)
    except (TypeError, ValueError):
        return raw if raw.lower().endswith("lb") else f"{raw} lb"
    if w <= 0:
        return ""
    if w == int(w):
        return f"{int(w)} lb"
    return f"{w:g} lb"


def _ucl_money(amount) -> str:
    raw = _ucl_field_str(amount)
    if not raw:
        return ""
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return raw
    if n <= 0:
        return ""
    if n == int(n):
        return str(int(n))
    return f"{n:.2f}"


def fill_ucl_bol_pdf(ctx: dict) -> bytes:
    """Prefill the official UCL fillable cargo receipt (all copies stay editable)."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import BooleanObject, NameObject

    template = _UCL_TEMPLATE
    if not template.is_file():
        raise FileNotFoundError(f"UCL BOL template missing: {template}")

    boxes_label = _ucl_boxes_label(ctx.get("boxes"))
    values = {
        "Text15": "",
        "Text16": _ucl_field_str(ctx.get("date_mdy")),
        "Text9": UCL_SHIPPER_CUSTOMER_ID,
        "Text10": "",
        "Text1": _ucl_field_str(ctx.get("shipper_address_text")),
        "Text2": _ucl_field_str(ctx.get("shipper_phone")),
        "Text4": _ucl_field_str(ctx.get("ship_to_address_text")),
        "Text5": _ucl_field_str(ctx.get("attention")),
        "Text3": _ucl_field_str(ctx.get("ship_to_phone")),
        "PIECESRow1": boxes_label,
        "WEIGHTRow1": _ucl_weight_lb(ctx.get("total_weight")),
        "DESCRIPTIONRow1": "handbags",
        "DATE PICKED UPRow1": _ucl_field_str(ctx.get("date_mdy")),
        "PIECESRow1_2": boxes_label,
        "fill_15": _ucl_money(ctx.get("declared_value")),
        "Text6": _ucl_money(ctx.get("declared_value")),
        "Check Box12": "/Yes",
        "Check Box14": "/Yes",
    }

    reader = PdfReader(str(template))
    writer = PdfWriter()
    writer.append(reader)
    acro = writer._root_object.get("/AcroForm")
    if acro is not None:
        acro.update({NameObject("/NeedAppearances"): BooleanObject(True)})

    for page in writer.pages:
        try:
            writer.update_page_form_field_values(page, values, auto_regenerate=True)
        except Exception:
            writer.update_page_form_field_values(page, values, auto_regenerate=False)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _textfield(c, name, x, y, width, height, value="", multiline=False):
    """Borderless fillable field inside boxed/table areas."""
    flags = "multiline" if multiline else ""
    c.acroForm.textfield(
        name=name,
        tooltip=name,
        x=x,
        y=y,
        width=width,
        height=height,
        value=str(value) if value is not None else "",
        fillColor=white,
        borderWidth=0,
        forceBorder=False,
        fieldFlags=flags,
        fontSize=FONT_FIELD,
    )


def _draw_label(c, x, y, text, size=None, bold=False):
    size = size or FONT_LABEL
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, y, text)


def _label_width(text, bold=False):
    font = "Helvetica-Bold" if bold else "Helvetica"
    return stringWidth(text, font, FONT_LABEL)


def _sig_inline_field(c, name, y, label, label_x, field_end_x, value=""):
    """Label and underline on the same row (spreadsheet-style)."""
    _draw_label(c, label_x, y, label)
    field_x = label_x + _label_width(label) + 6
    field_w = max(field_end_x - field_x, 40)
    underline_y = y - 4
    _hline(c, field_x, underline_y, field_x + field_w, thickness=TABLE_GRID_W)
    _textfield(c, name, field_x, y - 2, field_w, 14, value)


def _set_border(c, width=None):
    c.setLineWidth(width if width is not None else BORDER_W)


def _hline(c, x1, y, x2, thickness=None):
    _set_border(c, thickness if thickness is not None else BORDER_W)
    c.line(x1, y, x2, y)
    c.setLineWidth(1)


def _vline(c, x, y1, y2, thickness=None):
    _set_border(c, thickness if thickness is not None else BORDER_W)
    c.line(x, y1, x, y2)
    c.setLineWidth(1)


def _rect(c, x, y, w, h):
    _set_border(c)
    c.rect(x, y, w, h, stroke=1, fill=0)
    c.setLineWidth(1)


def _draw_company_header(c, left, width, y, header: dict) -> float:
    """Draw centered company block; return y below thick rule with clear gap."""
    c.setFont("Helvetica-Bold", FONT_COMPANY_TITLE)
    c.drawCentredString(PAGE_W / 2, y, header.get("title", ""))
    y -= 16
    c.setFont("Helvetica", FONT_COMPANY_LINE)
    c.drawCentredString(PAGE_W / 2, y, header.get("address", ""))
    y -= 14
    c.drawCentredString(PAGE_W / 2, y, header.get("tel_fax", ""))
    y -= 14
    _hline(c, left, y, left + width, thickness=3)
    return y - HEADER_RULE_GAP


def build_bol_pdf(ctx: dict) -> bytes:
    """Render a fillable US Letter Bill of Lading."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"BOL {ctx.get('bol_number', '')}")

    left = MARGIN
    right = PAGE_W - MARGIN
    width = right - left
    y = PAGE_H - MARGIN

    company = ctx.get("company_header") or COMPANY_HEADER_BY_DIGITS[5]
    y = _draw_company_header(c, left, width, y, company)

    c.setFont("Helvetica-Bold", FONT_TITLE)
    c.drawCentredString(PAGE_W / 2, y, "BILL OF LADING")
    y -= 16
    c.setFont("Helvetica", FONT_SUBTITLE)
    c.drawCentredString(PAGE_W / 2, y, "(NOT NEGOTIABLE)")
    y -= 22

    field_h = 16
    _draw_label(c, left, y, "Date:")
    _textfield(c, "bol_date", left + 40, y - 2, 105, field_h, ctx.get("date", ""))
    _draw_label(c, left + width - 168, y, "BOL # :")
    _textfield(c, "bol_number", left + width - 122, y - 2, 115, field_h, ctx.get("bol_number", ""))
    y -= 22

    _draw_label(c, left, y, "Pick up# /Confirmation #:")
    _textfield(c, "pickup_confirm", left + 148, y - 2, width - 148, field_h, ctx.get("pickup_confirm", ""))
    y -= 24

    half_w = (width - 10) / 2
    ship_right = left + half_w + 10
    _draw_label(c, left + 4, y, "Shipper:", bold=True)
    _draw_label(c, ship_right + 4, y, "Ship to:", bold=True)
    y -= 18

    box_h = 74
    box_pad = 6
    shipper_y = y - box_h
    _rect(c, left, shipper_y, half_w, box_h)
    _rect(c, ship_right, shipper_y, half_w, box_h)
    _textfield(
        c,
        "shipper_block",
        left + box_pad,
        shipper_y + box_pad,
        half_w - (2 * box_pad),
        box_h - (2 * box_pad),
        ctx.get("shipper_text", ""),
        multiline=True,
    )
    _textfield(
        c,
        "ship_to_block",
        ship_right + box_pad,
        shipper_y + box_pad,
        half_w - (2 * box_pad),
        box_h - (2 * box_pad),
        ctx.get("ship_to_text", ""),
        multiline=True,
    )
    y = shipper_y - 10

    col_headers = [
        "Customer PO (Sale Order #)",
        "Description",
        "Total Qty",
        "Total Case",
        "Weight",
        "Qty of Pallet",
    ]
    # Wider PO column so header fits; Description starts further right
    col_fracs = [0.26, 0.28, 0.11, 0.11, 0.12, 0.12]
    col_widths = [width * f for f in col_fracs]
    col_x = [left]
    for w in col_widths[:-1]:
        col_x.append(col_x[-1] + w)

    header_h = 30
    row_h = 22
    table_top = y
    table_bottom = table_top - header_h - BOL_DATA_ROWS * row_h - row_h

    _rect(c, left, table_bottom, width, table_top - table_bottom)
    header_sep_y = table_top - header_h
    _hline(c, left, header_sep_y, left + width, thickness=TABLE_GRID_W)
    for cx in col_x[1:]:
        _vline(c, cx, table_bottom, table_top, thickness=TABLE_GRID_W)

    header_y = table_top - header_h + 6
    c.setFont("Helvetica-Bold", FONT_TABLE_HDR)
    c.drawString(col_x[0] + 4, header_y + 10, "Customer PO")
    c.drawString(col_x[0] + 4, header_y, "(Sale Order #)")
    c.drawString(col_x[1] + 6, header_y + 5, "Description")
    hdr_rest = col_headers[2:]
    for i, hdr in enumerate(hdr_rest, start=2):
        c.drawString(col_x[i] + 3, header_y + 5, hdr)

    data_rows = ctx.get("data_rows") or []
    for r_idx in range(BOL_DATA_ROWS):
        row = data_rows[r_idx] if r_idx < len(data_rows) else EMPTY_ROW
        row_y_top = table_top - header_h - r_idx * row_h
        row_y = row_y_top - row_h
        _hline(c, left, row_y_top, left + width, thickness=TABLE_GRID_W)
        keys = ["po", "description", "qty", "cases", "weight", "pallet"]
        for i, key in enumerate(keys):
            val = row.get(key, "")
            if r_idx == 0 and key == "po" and not val:
                val = ctx.get("bol_number", "")
            pad_l = 6 if key == "description" else 4
            _textfield(
                c,
                f"row{r_idx + 1}_{key}",
                col_x[i] + pad_l,
                row_y + 3,
                col_widths[i] - pad_l - 3,
                row_h - 6,
                val,
                multiline=(key == "description"),
            )

    total_y_top = table_top - header_h - BOL_DATA_ROWS * row_h
    total_y = total_y_top - row_h
    _hline(c, left, total_y_top, left + width, thickness=TABLE_GRID_W)
    c.setFont("Helvetica-Bold", FONT_TOTAL)
    c.drawRightString(col_x[1] + col_widths[1] - 6, total_y + 7, "Total:")
    for col_i, (field_name, ctx_key) in enumerate(
        [
            ("total_qty", "total_qty"),
            ("total_cases", "total_cases"),
            ("total_weight", "total_weight"),
            ("total_pallet", "total_pallet"),
        ],
        start=2,
    ):
        _textfield(
            c,
            field_name,
            col_x[col_i] + 3,
            total_y + 3,
            col_widths[col_i] - 6,
            row_h - 6,
            ctx.get(ctx_key, ""),
        )

    y = table_bottom - 20
    sig_col_w = (width - 14) / 2
    sig_right_x = left + sig_col_w + 14
    row_gap = 22
    left_field_end = sig_right_x - 8
    right_field_end = left + width

    def _sig_pair(left_label, left_name, left_val, right_label, right_name, right_val):
        nonlocal y
        _sig_inline_field(c, left_name, y, left_label, left, left_field_end, left_val)
        _sig_inline_field(
            c, right_name, y, right_label, sig_right_x, right_field_end, right_val
        )
        y -= row_gap

    _sig_pair("Shipper Name:", "shipper_name", "", "Carrier:", "carrier", "")
    _sig_inline_field(
        c, "carrier_phone", y, "Carrier Phone #:", sig_right_x, right_field_end, ""
    )
    y -= row_gap

    _sig_pair("Shipper Sign:", "shipper_sign", "", "Receiver Sign:", "receiver_sign", "")
    _sig_pair("Date:", "shipper_sign_date", ctx.get("date", ""), "Date:", "receiver_date", "")

    y -= 26
    _draw_label(c, left, y, "Remark:", bold=True)
    y -= 8
    remark_h = 52 * 3
    remark_y = y - remark_h
    _rect(c, left, remark_y, width, remark_h)
    _textfield(
        c,
        "remark",
        left + 8,
        remark_y + 8,
        width - 16,
        remark_h - 16,
        ctx.get("remark", ""),
        multiline=True,
    )

    c.setFont("Helvetica", FONT_FOOTER)
    c.drawCentredString(
        PAGE_W / 2,
        MARGIN - 2,
        "*******Please make 2 copies, one is for company file, another one is for carrier.",
    )

    c.save()
    buf.seek(0)
    return buf.getvalue()
