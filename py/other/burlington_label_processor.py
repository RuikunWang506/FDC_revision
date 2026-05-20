"""Center-crop US Letter-style PDFs to a 4×6 inch label and extract label fields."""

import base64
import io
import re
from collections import defaultdict
from pathlib import Path

import pyodbc
from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session, url_for
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import DictionaryObject, NameObject, RectangleObject, StreamObject
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from werkzeug.utils import secure_filename

from product_detail_module.product_detail import fetch_ocean_data, search_product_data
from py.order.order_search import find_order_number_by_po
from py.utilities.setting import Config
from py.other.burlington_label_processor_storage import (
    delete_job,
    job_file_path,
    list_jobs,
    load_job_meta,
    save_job,
)
from py.other.label_status import mark_burlington_labels
from py.utilities.setting import login_required

burlington_label_processor_bp = Blueprint("burlington_label_processor", __name__)
sql_server_config = Config.SQL_SERVER_CONFIG

# 72 PDF points per inch
INCH_PT = 72.0
CROP_W_PT = 4 * INCH_PT
CROP_H_PT = 6 * INCH_PT
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

SUMMARY_HEADERS = ("PO #", "Order", "Pages", "Styles", "Total Qty")
SUMMARY_COL_WEIGHTS = (0.26, 0.14, 0.10, 0.34, 0.16)
SUMMARY_MARGIN_PT = 10.0
SUMMARY_TITLE_PT = 11.0


def _crop_single_page(page, page_num: int) -> PageObject:
    mb = page.mediabox
    w = float(mb.width)
    h = float(mb.height)
    if w + 0.5 < CROP_W_PT or h + 0.5 < CROP_H_PT:
        raise ValueError(
            f"Page {page_num} is {_points_to_inches_str(w, h)}; "
            f"needs at least 4×6 in ({CROP_W_PT:.0f}×{CROP_H_PT:.0f} pt) to crop."
        )
    llx = float(mb.left)
    lly = float(mb.bottom)
    dx = (w - CROP_W_PT) / 2.0
    dy = (h - CROP_H_PT) / 2.0
    nllx = llx + dx
    nlly = lly + dy
    page.add_transformation(Transformation().translate(tx=-nllx, ty=-nlly))
    box = RectangleObject((0, 0, CROP_W_PT, CROP_H_PT))
    page.mediabox = box
    page.cropbox = box
    for box_name in ("/ArtBox", "/BleedBox", "/TrimBox"):
        if box_name in page:
            page[NameObject(box_name)] = box
    return page


def _crop_pdf_to_pages(pdf_bytes: bytes) -> list[PageObject]:
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if len(reader.pages) == 0:
        raise ValueError("PDF has no pages.")
    return [_crop_single_page(page, i + 1) for i, page in enumerate(reader.pages)]


def _make_black_separator_page() -> PageObject:
    w, h = int(CROP_W_PT), int(CROP_H_PT)
    page = PageObject.create_blank_page(width=w, height=h)
    stream = StreamObject()
    stream.set_data(f"0 0 0 rg 0 0 {w} {h} re f".encode("latin-1"))
    page[NameObject("/Contents")] = stream
    page[NameObject("/Resources")] = DictionaryObject()
    return page


def _po_key(parsed_page: dict) -> str:
    return parsed_page.get("po") or ""


def _dotted_border_overlay_page() -> PageObject:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(CROP_W_PT, CROP_H_PT))
    c.setDash(4, 4)
    c.setLineWidth(2.5)
    c.setStrokeColor(colors.HexColor("#c53030"))
    inset = 12
    c.rect(
        inset,
        inset,
        CROP_W_PT - (2 * inset),
        CROP_H_PT - (2 * inset),
        stroke=1,
        fill=0,
    )
    c.save()
    return PdfReader(buf).pages[0]


def _apply_dotted_border(page: PageObject) -> PageObject:
    """Overlay dashed border on a normalized 4×6 page (origin 0,0)."""
    page.merge_page(_dotted_border_overlay_page())
    return page


def _nonstandard_qty_page_numbers(parsed_pages: list[dict]) -> set[int]:
    """
    Pages where this SKU's carton qty appears fewer than 2 times in the batch
    (e.g. one label with 8 among many with 20).
    """
    sku_qty_counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    page_rows: list[tuple[int, str, int]] = []

    for pr in parsed_pages:
        if not pr.get("ok"):
            continue
        items = pr.get("items") or []
        if not items:
            continue
        page_num = int(pr["page"])
        for item in items:
            sku = _sku_key(item["style"], item["color"])
            qty = int(item["qty"])
            sku_qty_counts[sku][qty] += 1
            page_rows.append((page_num, sku, qty))

    flagged: set[int] = set()
    for page_num, sku, qty in page_rows:
        if sku_qty_counts[sku][qty] < 2:
            flagged.add(page_num)
    return flagged


def _mark_nonstandard_pages(parsed_pages: list[dict], flagged: set[int]) -> None:
    for pr in parsed_pages:
        pr["nonstandard_qty"] = int(pr.get("page") or 0) in flagged


def _insert_po_separator_pages(
    cropped_pages: list[PageObject],
    parsed_pages: list[dict],
    highlight_pages: set[int] | None = None,
) -> tuple[list[PageObject], int]:
    """Insert a black 4×6 page before each page where the PO changes."""
    if len(cropped_pages) != len(parsed_pages):
        raise ValueError("Page count mismatch between cropped PDF and parse results.")
    highlight_pages = highlight_pages or set()
    output: list[PageObject] = []
    separator_count = 0
    prev_po_key: str | None = None
    for i, page in enumerate(cropped_pages):
        po_key = _po_key(parsed_pages[i])
        if i > 0 and po_key != prev_po_key:
            output.append(_make_black_separator_page())
            separator_count += 1
        page_out = page
        if (i + 1) in highlight_pages:
            page_out = _apply_dotted_border(page)
        output.append(page_out)
        prev_po_key = po_key
    return output, separator_count


def _overview_rows_for_summary(po_overview: list[dict]) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for entry in po_overview:
        po = entry.get("po") or ""
        if not po or po == "(no PO)":
            continue
        rows.append(
            (
                str(po),
                str(entry.get("order_id") or "—"),
                str(entry.get("page_count") or ""),
                str(entry.get("styles") or "—"),
                str(entry.get("total_qty") or ""),
            )
        )
    return rows


def _fit_cell_text(
    c: canvas.Canvas,
    text: str,
    max_width: float,
    font_name: str,
    font_size: float,
) -> str:
    c.setFont(font_name, font_size)
    if c.stringWidth(text, font_name, font_size) <= max_width:
        return text
    trimmed = text
    while trimmed and c.stringWidth(trimmed + "…", font_name, font_size) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else "…"


def _summary_layout(font_size: float, row_count: int) -> tuple[float, float, float, float]:
    title_h = SUMMARY_TITLE_PT + 4
    header_h = font_size * 1.55
    row_h = font_size * 1.38
    table_h = header_h + row_count * row_h
    return title_h, header_h, row_h, table_h


def _pick_summary_font_and_chunks(
    rows: list[tuple[str, ...]],
) -> tuple[float, list[list[tuple[str, ...]]]]:
    if not rows:
        return 8.0, [[]]
    usable_h = CROP_H_PT - (2 * SUMMARY_MARGIN_PT)
    for font_size in (9.0, 8.0, 7.0, 6.0, 5.5, 5.0):
        _, _, _, table_h = _summary_layout(font_size, len(rows))
        total_h = SUMMARY_TITLE_PT + 6 + table_h
        if total_h <= usable_h:
            return font_size, [rows]
    mid = (len(rows) + 1) // 2
    return 7.0, [rows[:mid], rows[mid:]]


def _draw_summary_table_page(
    c: canvas.Canvas,
    rows: list[tuple[str, ...]],
    *,
    font_size: float,
    page_index: int,
    page_total: int,
) -> None:
    w, h = CROP_W_PT, CROP_H_PT
    margin = SUMMARY_MARGIN_PT
    table_w = w - (2 * margin)
    col_widths = [table_w * wt for wt in SUMMARY_COL_WEIGHTS]
    font = "Helvetica"
    font_bold = "Helvetica-Bold"

    title = "PO Summary"
    if page_total > 1:
        title += f" ({page_index}/{page_total})"
    c.setFont(font_bold, SUMMARY_TITLE_PT)
    c.drawCentredString(w / 2, h - margin - SUMMARY_TITLE_PT, title)

    title_h, header_h, row_h, _ = _summary_layout(font_size, len(rows))
    y_top = h - margin - title_h - 4
    y = y_top - header_h

    x = margin
    c.setFillColor(colors.HexColor("#e8eef5"))
    c.rect(margin, y, table_w, header_h, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont(font_bold, font_size)
    for i, label in enumerate(SUMMARY_HEADERS):
        cell = _fit_cell_text(c, label, col_widths[i] - 4, font_bold, font_size)
        c.drawString(x + 2, y + (header_h - font_size) / 2, cell)
        x += col_widths[i]

    c.setFont(font, font_size)
    for row_idx, row in enumerate(rows):
        y -= row_h
        if row_idx % 2 == 1:
            c.setFillColor(colors.HexColor("#f6f8fa"))
            c.rect(margin, y, table_w, row_h, fill=1, stroke=0)
        c.setFillColor(colors.black)
        c.setStrokeColor(colors.HexColor("#cbd5e0"))
        c.rect(margin, y, table_w, row_h, fill=0, stroke=1)
        x = margin
        for i, value in enumerate(row):
            cell = _fit_cell_text(c, value, col_widths[i] - 4, font, font_size)
            c.drawString(x + 2, y + (row_h - font_size) / 2, cell)
            x += col_widths[i]


def _render_summary_label_pdf_bytes(
    chunks: list[list[tuple[str, ...]]],
    font_size: float,
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(CROP_W_PT, CROP_H_PT))
    total = len(chunks)
    drawn = 0
    for idx, chunk in enumerate(chunks, start=1):
        if not chunk:
            continue
        if drawn:
            c.showPage()
        _draw_summary_table_page(
            c, chunk, font_size=font_size, page_index=idx, page_total=total
        )
        drawn += 1
    c.save()
    return buf.getvalue()


def _pdf_bytes_to_pages(pdf_bytes: bytes) -> list[PageObject]:
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    return list(reader.pages)


def _append_po_summary_label_pages(
    pages: list[PageObject],
    po_overview: list[dict],
) -> tuple[list[PageObject], int]:
    rows = _overview_rows_for_summary(po_overview)
    if not rows:
        return pages, 0
    font_size, chunks = _pick_summary_font_and_chunks(rows)
    chunks = [c for c in chunks if c]
    if not chunks:
        return pages, 0
    summary_bytes = _render_summary_label_pdf_bytes(chunks, font_size)
    summary_pages = _pdf_bytes_to_pages(summary_bytes)
    return pages + summary_pages, len(summary_pages)


def _pages_to_pdf_bytes(pages: list[PageObject]) -> bytes:
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    out_buf = io.BytesIO()
    writer.write(out_buf)
    return out_buf.getvalue()


def _points_to_inches_str(w_pt: float, h_pt: float) -> str:
    return f"{w_pt / INCH_PT:.2f}×{h_pt / INCH_PT:.2f} in"


def _stem_from_upload(filename: str) -> str:
    name = secure_filename(filename or "label")
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return Path(name).stem


def _extract_texts_from_pages(pages: list[PageObject]) -> list[str]:
    texts: list[str] = []
    for page in pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    return texts


def _normalize_color_token(token: str) -> str:
    """Extract a short color code; tolerate OCR noise after the real code (e.g. BKIIII…)."""
    token = (token or "").strip().upper()
    if not token:
        return "—"
    if len(token) <= 6 and token.isalpha():
        return token
    prefix2 = token[:2]
    if len(token) >= 3 and prefix2.isalpha():
        rest = token[2:]
        if len(rest) >= 2 and (
            len(set(rest)) <= 3 or rest.count("I") >= len(rest) * 0.4
        ):
            return prefix2
    m = re.match(r"^([A-Z]{2,6})", token)
    return m.group(1) if m else token[:6]


def _parse_style_line(line: str) -> dict | None:
    """
    Parse a label line item: style, color (may be noisy), optional Size/QTY column, qty.
    Examples: 'N2612 BK QTY 24', 'N2541 BKIIIIIIIIA0 QTY 24'
    """
    line = line.strip()
    if not line or re.search(r"Style\s+Color", line, re.I):
        return None
    if re.match(r"^(Store|For\s*:|Bill|PO\s*#|\()", line, re.I):
        return None

    m = re.match(
        r"^([A-Z][A-Z0-9\-]{2,})\s+(.+?)\s+(\d+)\s*$",
        line,
        re.I,
    )
    if not m:
        return None

    style = m.group(1).upper()
    middle = m.group(2).strip()
    qty = int(m.group(3))
    parts = [p for p in middle.split() if p.upper() != "QTY"]
    color_token = parts[0] if parts else middle.split()[0] if middle.split() else ""
    color = _normalize_color_token(color_token)

    return {"style": style, "color": color, "qty": qty}


def _parse_label_items(lines: list[str]) -> list[dict]:
    """Parse Style / Color / Qty rows from label body lines."""
    items: list[dict] = []
    header_idx = None
    for i, line in enumerate(lines):
        if re.search(r"Style\s+Color\s+Size\s+Qty", line, re.I):
            header_idx = i
            break
    if header_idx is not None:
        for line in lines[header_idx + 1 :]:
            if re.match(r"^(Store|For\s*:|Bill|PO\s*#|\()", line, re.I):
                break
            parsed = _parse_style_line(line)
            if parsed:
                items.append(parsed)
    if not items:
        for line in lines:
            parsed = _parse_style_line(line)
            if parsed:
                items.append(parsed)
    return items


def _parse_label_page_text(text: str, page_num: int) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    joined = "\n".join(lines)

    po = None
    po_m = re.search(r"PO\s*#?\s*:?\s*(\d+)", joined, re.I)
    if po_m:
        po = po_m.group(1)

    bill_of_lading = None
    bol_m = re.search(
        r"Bill[-\s]?of[-\s]?Lading\s*:?\s*([A-Z0-9]+)",
        joined,
        re.I,
    )
    if bol_m:
        bill_of_lading = bol_m.group(1).upper()
    else:
        for i, line in enumerate(lines):
            if re.search(r"Bill[-\s]?of[-\s]?Lading", line, re.I):
                for nxt in lines[i + 1 : i + 4]:
                    if re.fullmatch(r"[A-Z]{2}\d+", nxt, re.I):
                        bill_of_lading = nxt.upper()
                        break
                if bill_of_lading:
                    break

    items = _parse_label_items(lines)
    ok = bool(po or bill_of_lading or items)
    error = None if ok else "Could not find PO, Bill of Lading, or line items on this page."

    return {
        "page": page_num,
        "ok": ok,
        "error": error,
        "po": po,
        "bill_of_lading": bill_of_lading,
        "items": items,
    }


def _sku_key(style: str, color: str) -> str:
    return f"{style}-{color}"


def _canonical_sku_for_lookup(sku: str) -> str:
    """Normalize to dashed PROD_CD form (e.g. N2612 BK -> N2612-BK)."""
    s = (sku or "").replace("\u00a0", " ").strip().upper()
    if not s:
        return s
    if "-" in s:
        return s
    parts = s.split(None, 1)
    if len(parts) == 2 and parts[1]:
        return f"{parts[0]}-{parts[1]}"
    return s


def _fetch_sku_inventory_for_label_skus(skus: list[str]) -> dict[str, dict[str, int]]:
    """Per-SKU inventory using product detail search (PROD_CD with dash)."""
    styles_to_skus: dict[str, set[str]] = defaultdict(set)
    for sku in skus:
        canon = _canonical_sku_for_lookup(sku)
        if not canon or "-" not in canon:
            continue
        style_part = canon.split("-", 1)[0]
        styles_to_skus[style_part].add(canon.lower())

    inventory: dict[str, dict[str, int]] = {}
    for style, wanted in styles_to_skus.items():
        try:
            groups = search_product_data(style.lower()) or []
        except Exception as e:
            current_app.logger.error(
                "Inventory search failed for style %s: %s", style, e
            )
            continue
        for group in groups:
            for color in group.get("colors") or []:
                prod_sku = str(color.get("sku") or "").strip()
                lookup_key = _canonical_sku_for_lookup(prod_sku).lower()
                if lookup_key not in wanted:
                    continue
                combined = (color.get("totals") or {}).get("combined") or {}
                inventory[lookup_key] = {
                    "in_warehouse": int(combined.get("available", 0) or 0),
                    "sold_reserved": int(combined.get("sold", 0) or 0),
                }
    return inventory


def _database_name_for_order(order_number: str) -> str | None:
    if len(order_number) == 5:
        return "omsdata_FDC"
    if len(order_number) == 4:
        return "oms_EF"
    return None


def _fetch_order_enrichment(order_number: str) -> dict:
    """Invoice and notes for an order."""
    database_name = _database_name_for_order(order_number)
    empty = {"invoice_number": "not invoiced", "notes": ""}
    if not database_name:
        return empty

    invoice_number = "not invoiced"
    notes = ""
    conn = None
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT CASE
                WHEN INVS_NUM IS NULL OR INVS_NUM = 0 THEN 'not invoiced'
                ELSE CAST(INVS_NUM AS VARCHAR)
            END
            FROM dbo.orders
            WHERE ord_num = ?
            """,
            (order_number,),
        )
        inv_row = cursor.fetchone()
        if inv_row and inv_row[0]:
            invoice_number = str(inv_row[0]).strip() or "not invoiced"
        cursor.close()
    except Exception as e:
        current_app.logger.error("Error fetching order header for %s: %s", order_number, e)
    finally:
        if conn:
            conn.close()

    try:
        conn_notes = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )
        cur2 = conn_notes.cursor()
        cur2.execute(
            """
            SELECT TOP 1 NOTES
            FROM dbo.ntfile
            WHERE NT_NUM = ?
              AND NOTES IS NOT NULL
              AND LTRIM(RTRIM(NOTES)) <> ''
            ORDER BY LEN(NOTES) DESC
            """,
            (order_number,),
        )
        note_row = cur2.fetchone()
        notes = str(note_row[0]).strip() if note_row and note_row[0] else ""
        cur2.close()
        conn_notes.close()
    except Exception as e:
        current_app.logger.error("Error fetching order notes for %s: %s", order_number, e)

    return {
        "invoice_number": invoice_number,
        "notes": notes,
    }


def _fetch_style_warehouse_total(style: str) -> int:
    style = (style or "").strip()
    if not style:
        return 0
    try:
        groups = search_product_data(style) or []
        total = 0
        for group in groups:
            total += int(group.get("totals", {}).get("combined", {}).get("available", 0) or 0)
        return total
    except Exception as e:
        current_app.logger.error("Error fetching warehouse total for style %s: %s", style, e)
        return 0


def _fetch_style_ocean_total(style: str) -> int:
    style = (style or "").strip()
    if not style:
        return 0
    try:
        return sum(int(row.get("quantity", 0) or 0) for row in fetch_ocean_data(style))
    except Exception as e:
        current_app.logger.error("Error fetching ocean total for style %s: %s", style, e)
        return 0


def _enrich_po_detail(po_detail: list[dict]) -> None:
    all_skus: list[str] = []
    for row in po_detail:
        for sku_row in row.get("skus") or []:
            canon = _canonical_sku_for_lookup(str(sku_row.get("sku") or ""))
            if canon:
                sku_row["sku"] = canon
                all_skus.append(canon)

    inv_map = _fetch_sku_inventory_for_label_skus(all_skus) if all_skus else {}

    for row in po_detail:
        for sku_row in row.get("skus") or []:
            key = _canonical_sku_for_lookup(str(sku_row.get("sku") or "")).lower()
            inv = inv_map.get(key, {})
            sku_row["in_warehouse"] = int(inv.get("in_warehouse", 0) or 0)
            sku_row["sold_reserved"] = int(inv.get("sold_reserved", 0) or 0)


def _enrich_po_overview(po_overview: list[dict]) -> None:
    order_cache: dict[str, dict] = {}
    warehouse_cache: dict[str, int] = {}
    ocean_cache: dict[str, int] = {}

    for row in po_overview:
        styles_raw = row.get("styles") or ""
        style_list = [
            s.strip()
            for s in styles_raw.split(",")
            if s.strip() and s.strip() != "—"
        ]
        row["style_list"] = style_list
        row["order_notes"] = ""
        row["invoice_status"] = None
        row["invoice_number"] = None
        row["warehouse_qty_total"] = 0
        row["ocean_qty_total"] = 0

        order_id = row.get("order_id")
        if order_id:
            if order_id not in order_cache:
                order_cache[order_id] = _fetch_order_enrichment(order_id)
            meta = order_cache[order_id]
            inv = meta.get("invoice_number") or "not invoiced"
            if inv and inv != "not invoiced":
                row["invoice_status"] = "invoiced"
                row["invoice_number"] = inv
            else:
                row["invoice_status"] = "not_invoiced"
            row["order_notes"] = meta.get("notes") or ""

        wh_total = 0
        ocean_total = 0
        for style in style_list:
            if style not in warehouse_cache:
                warehouse_cache[style] = _fetch_style_warehouse_total(style)
            if style not in ocean_cache:
                ocean_cache[style] = _fetch_style_ocean_total(style)
            wh_total += warehouse_cache[style]
            ocean_total += ocean_cache[style]
        row["warehouse_qty_total"] = wh_total
        row["ocean_qty_total"] = ocean_total


def _aggregate_po_summaries(page_results: list[dict]) -> tuple[list[dict], list[dict]]:
    by_po: dict[str, dict] = defaultdict(
        lambda: {"pages": 0, "skus": defaultdict(int), "bols": set(), "styles": set()}
    )
    for pr in page_results:
        if not pr.get("ok"):
            continue
        po = pr.get("po") or "(no PO)"
        bucket = by_po[po]
        bucket["pages"] += 1
        if pr.get("bill_of_lading"):
            bucket["bols"].add(pr["bill_of_lading"])
        for item in pr.get("items") or []:
            sku = _sku_key(item["style"], item["color"])
            bucket["skus"][sku] += item["qty"]
            bucket["styles"].add(item["style"])

    po_detail: list[dict] = []
    po_overview: list[dict] = []
    order_cache: dict[str, str | None] = {}

    for po in sorted(by_po.keys(), key=lambda p: (p == "(no PO)", p)):
        data = by_po[po]
        skus = [
            {"sku": sku, "qty": qty}
            for sku, qty in sorted(data["skus"].items())
        ]
        total_qty = sum(data["skus"].values())
        order_id = None
        if po != "(no PO)":
            if po not in order_cache:
                order_cache[po] = find_order_number_by_po(po)
            order_id = order_cache[po]

        entry = {
            "po": po,
            "page_count": data["pages"],
            "skus": skus,
            "total_qty": total_qty,
            "bill_of_lading": ", ".join(sorted(data["bols"])) if data["bols"] else None,
            "order_id": order_id,
        }
        po_detail.append(entry)
        styles_sorted = sorted(data["styles"])
        po_overview.append(
            {
                "po": po,
                "page_count": data["pages"],
                "unique_sku_count": len(data["skus"]),
                "styles": ", ".join(styles_sorted) if styles_sorted else "—",
                "total_qty": total_qty,
                "order_id": order_id,
            }
        )
    return po_detail, po_overview


def _parse_page_texts(texts: list[str]) -> dict:
    pages = [_parse_label_page_text(t, i + 1) for i, t in enumerate(texts)]
    po_detail, po_overview = _aggregate_po_summaries(pages)
    _enrich_po_detail(po_detail)
    _enrich_po_overview(po_overview)
    parsed_count = sum(1 for p in pages if p["ok"])
    return {
        "pages": pages,
        "parsed_count": parsed_count,
        "page_count": len(pages),
        "po_detail": po_detail,
        "po_overview": po_overview,
    }


@burlington_label_processor_bp.route("/burlington_label_processor")
@login_required
def burlington_label_processor_page():
    current_user = session.get("username", "")
    return render_template(
        "burlington_label_processor.html",
        current_user=current_user,
    )


@burlington_label_processor_bp.route("/burlington_label_processor/process", methods=["POST"])
@login_required
def burlington_label_processor_process():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    upload = request.files["file"]
    if not upload or not upload.filename:
        return jsonify({"error": "No file selected."}), 400
    if not upload.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a PDF file."}), 400
    data = upload.read()
    if not data:
        return jsonify({"error": "The file is empty."}), 400
    if len(data) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "File too large (max 25 MB)."}), 400
    try:
        cropped_pages = _crop_pdf_to_pages(data)
        texts = _extract_texts_from_pages(cropped_pages)
        parse_result = _parse_page_texts(texts)
        nonstandard_pages = _nonstandard_qty_page_numbers(parse_result["pages"])
        _mark_nonstandard_pages(parse_result["pages"], nonstandard_pages)
        final_pages, separator_count = _insert_po_separator_pages(
            cropped_pages, parse_result["pages"], nonstandard_pages
        )
        parse_result["nonstandard_qty_count"] = len(nonstandard_pages)
        final_pages, summary_label_count = _append_po_summary_label_pages(
            final_pages, parse_result.get("po_overview") or []
        )
        out_bytes = _pages_to_pdf_bytes(final_pages)
        parse_result["separator_count"] = separator_count
        parse_result["summary_label_count"] = summary_label_count
        parse_result["output_page_count"] = len(final_pages)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Could not process PDF: {e}"}), 400

    stem = re.sub(r"[^\w\-]+", "_", _stem_from_upload(upload.filename))
    if not stem:
        stem = "label"
    download_name = f"{stem}_4x6.pdf"
    order_lookup_base = url_for("order_lookup.order_lookup", _external=True).rstrip("/")
    product_detail_base = url_for("product_detail.product_detail", _external=True).rstrip("/")

    try:
        mark_burlington_labels(
            parse_result.get("po_overview") or [],
            session.get("username"),
        )
    except Exception:
        current_app.logger.exception("Failed to record Burlington label status")

    job_id = None
    try:
        job_id = save_job(
            original_bytes=data,
            processed_bytes=out_bytes,
            original_filename=upload.filename or "upload.pdf",
            download_name=download_name,
            username=session.get("username") or "",
            parse_result=parse_result,
        )
    except Exception:
        current_app.logger.exception("Failed to save burlington label processor job to output")

    return jsonify(
        {
            "download_name": download_name,
            "pdf_base64": base64.b64encode(out_bytes).decode("ascii"),
            "order_lookup_base": order_lookup_base,
            "product_detail_base": product_detail_base,
            "job_id": job_id,
            **parse_result,
        }
    )


@burlington_label_processor_bp.route("/burlington_label_processor/history")
@login_required
def burlington_label_processor_history():
    return jsonify({"jobs": list_jobs()})


@burlington_label_processor_bp.route("/burlington_label_processor/history/<string:job_id>")
@login_required
def burlington_label_processor_history_detail(job_id):
    meta = load_job_meta(job_id)
    if not meta:
        return jsonify({"error": "Job not found or expired."}), 404
    order_lookup_base = url_for("order_lookup.order_lookup", _external=True).rstrip("/")
    product_detail_base = url_for("product_detail.product_detail", _external=True).rstrip("/")
    parse_result = meta.pop("parse_result", {})
    po_overview = parse_result.get("po_overview") or []
    po_detail = parse_result.get("po_detail") or []
    if po_overview and "invoice_status" not in (po_overview[0] or {}):
        _enrich_po_overview(po_overview)
    first_skus = (po_detail[0].get("skus") or []) if po_detail else []
    if first_skus and "in_warehouse" not in first_skus[0]:
        _enrich_po_detail(po_detail)
    return jsonify(
        {
            "job": meta,
            "job_id": job_id,
            "order_lookup_base": order_lookup_base,
            "product_detail_base": product_detail_base,
            **parse_result,
        }
    )


@burlington_label_processor_bp.route("/burlington_label_processor/history/<string:job_id>/original")
@login_required
def burlington_label_processor_history_original(job_id):
    path = job_file_path(job_id, "original")
    if not path:
        return jsonify({"error": "Original file not found."}), 404
    meta = load_job_meta(job_id) or {}
    name = secure_filename(meta.get("original_filename") or "original.pdf")
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=name)


@burlington_label_processor_bp.route("/burlington_label_processor/history/<string:job_id>/processed")
@login_required
def burlington_label_processor_history_processed(job_id):
    path = job_file_path(job_id, "processed")
    if not path:
        return jsonify({"error": "Processed file not found."}), 404
    meta = load_job_meta(job_id) or {}
    name = secure_filename(meta.get("download_name") or "label_4x6.pdf")
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=name)


@burlington_label_processor_bp.route("/burlington_label_processor/history/<string:job_id>", methods=["DELETE"])
@login_required
def burlington_label_processor_history_delete(job_id):
    if delete_job(job_id):
        return jsonify({"ok": True})
    return jsonify({"error": "Job not found or already removed."}), 404
