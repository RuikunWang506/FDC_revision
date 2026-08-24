"""Presentable Master Audit PDF from a dashboard snapshot."""
from __future__ import annotations

import base64
import io
import os
import re
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

NAVY = colors.HexColor("#15306b")
BLUE = colors.HexColor("#1d4fbb")
LIGHT = colors.HexColor("#eef3fb")
ALT = colors.HexColor("#f7f9fd")
LINE = colors.HexColor("#d5def0")
MUTED = colors.HexColor("#5d6a85")
WHITE = colors.white
_FONT = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONTS_READY = False


def _ensure_fonts() -> None:
    global _FONT, _FONT_BOLD, _FONTS_READY
    if _FONTS_READY:
        return
    _FONTS_READY = True
    candidates = [
        (r"C:\Windows\Fonts\msyh.ttc", True),
        (r"C:\Windows\Fonts\simhei.ttf", False),
        (r"C:\Windows\Fonts\arialuni.ttf", False),
    ]
    for path, is_ttc in candidates:
        if not os.path.isfile(path):
            continue
        try:
            if is_ttc:
                pdfmetrics.registerFont(TTFont("AuditSans", path, subfontIndex=0))
                try:
                    pdfmetrics.registerFont(TTFont("AuditSans-Bold", path, subfontIndex=1))
                except Exception:
                    pdfmetrics.registerFont(TTFont("AuditSans-Bold", path, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont("AuditSans", path))
                pdfmetrics.registerFont(TTFont("AuditSans-Bold", path))
            _FONT = "AuditSans"
            _FONT_BOLD = "AuditSans-Bold"
            return
        except Exception:
            continue

_DATA_URL_RE = re.compile(r"^data:image/(?:png|jpeg|jpg);base64,", re.I)


def _money(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"${number:,.2f}"


def _int(value: Any) -> str:
    try:
        number = int(float(value or 0))
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}"


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _styles() -> dict[str, ParagraphStyle]:
    _ensure_fonts()
    base = getSampleStyleSheet()
    return {
        "hero": ParagraphStyle(
            "auditHero",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=20,
            textColor=NAVY,
            spaceAfter=2,
            leading=24,
        ),
        "sub": ParagraphStyle(
            "auditSub",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=9,
            textColor=MUTED,
            spaceAfter=10,
            leading=12,
        ),
        "section": ParagraphStyle(
            "auditSection",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=11,
            textColor=NAVY,
            spaceBefore=12,
            spaceAfter=6,
            leading=14,
        ),
        "hint": ParagraphStyle(
            "auditHint",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=8,
            textColor=MUTED,
            spaceAfter=6,
        ),
        "kpi_label": ParagraphStyle(
            "auditKpiLabel",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=7,
            textColor=MUTED,
            alignment=TA_LEFT,
            leading=9,
        ),
        "kpi_value": ParagraphStyle(
            "auditKpiValue",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=13,
            textColor=NAVY,
            alignment=TA_LEFT,
            leading=16,
        ),
        "cell": ParagraphStyle(
            "auditCell",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=8,
            textColor=NAVY,
            leading=10,
        ),
        "cell_right": ParagraphStyle(
            "auditCellRight",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=8,
            textColor=NAVY,
            alignment=TA_RIGHT,
            leading=10,
        ),
        "th": ParagraphStyle(
            "auditTh",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=8,
            textColor=NAVY,
            leading=10,
        ),
        "th_right": ParagraphStyle(
            "auditThRight",
            parent=base["Normal"],
            fontName=_FONT_BOLD,
            fontSize=8,
            textColor=NAVY,
            alignment=TA_RIGHT,
            leading=10,
        ),
        "caption": ParagraphStyle(
            "auditCaption",
            parent=base["Normal"],
            fontName=_FONT,
            fontSize=8,
            textColor=MUTED,
            alignment=TA_CENTER,
            spaceBefore=2,
            spaceAfter=8,
        ),
    }


def _decode_image(data_url: str, max_w: float, max_h: float) -> Image | None:
    raw = _text(data_url)
    if not raw or not _DATA_URL_RE.match(raw) or len(raw) > 2_000_000:
        return None
    try:
        payload = raw.split(",", 1)[1]
        blob = base64.b64decode(payload, validate=False)
        if len(blob) > 1_500_000:
            return None
        bio = io.BytesIO(blob)
        try:
            from PIL import Image as PILImage

            with PILImage.open(bio) as im:
                width, height = im.size
            bio.seek(0)
        except Exception:
            width, height = 800, 400
            bio.seek(0)
        if width <= 0 or height <= 0:
            return None
        scale = min(max_w / width, max_h / height)
        return Image(bio, width=width * scale, height=height * scale)
    except Exception:
        return None


def _table(data: list[list], col_widths: list[float] | None = None) -> Table:
    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
        ("TEXTCOLOR", (0, 0), (-1, 0), NAVY),
        ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ALT]),
    ]
    if len(data) > 1:
        style.append(("BACKGROUND", (0, -1), (-1, -1), LIGHT))
        style.append(("FONTNAME", (0, -1), (-1, -1), _FONT_BOLD))
    table.setStyle(TableStyle(style))
    return table


def _header_footer(canvas, doc, generated_at: str) -> None:
    canvas.saveState()
    width, height = letter
    canvas.setFillColor(NAVY)
    canvas.rect(0, height - 32, width, 32, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont(_FONT_BOLD, 10)
    canvas.drawString(36, height - 20, "FDC  ·  Master Audit")
    canvas.setFont(_FONT, 8)
    canvas.drawRightString(width - 36, height - 20, generated_at)
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(36, 28, width - 36, 28)
    canvas.setFillColor(MUTED)
    canvas.setFont(_FONT, 8)
    canvas.drawString(36, 16, "Internal wholesale report")
    canvas.drawRightString(width - 36, 16, f"Page {doc.page}")
    canvas.restoreState()


def build_audit_pdf(snapshot: dict[str, Any], *, prepared_by: str = "") -> bytes:
    styles = _styles()
    meta = snapshot.get("meta") or {}
    kpis = snapshot.get("kpis") or {}
    monthly = list(snapshot.get("monthly") or [])[:24]
    customers = list(snapshot.get("customers") or [])[:15]
    prefixes = list(snapshot.get("prefixes") or [])[:12]
    color_rows = list(snapshot.get("colors") or [])[:12]
    charts = snapshot.get("charts") or {}

    period = _text(meta.get("period_label"), "Selected period")
    db_label = _text(meta.get("db_label"), "Both databases")
    rank_mode = "Order count" if _text(meta.get("top_customer_mode")) == "orders" else "Total sales"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    prepared = _text(prepared_by) or _text(meta.get("prepared_by"), "FDC")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=44,
        bottomMargin=40,
        title=f"FDC Master Audit — {period}",
        author=prepared,
        subject="Wholesale order dashboard report",
    )
    story: list[Any] = []
    usable = letter[0] - 72

    story.append(Paragraph("Wholesale order dashboard", styles["hero"]))
    story.append(
        Paragraph(
            f"Period <b>{period}</b>  ·  Database <b>{db_label}</b>  ·  Prepared by <b>{prepared}</b>",
            styles["sub"],
        )
    )

    kpi_items = [
        ("Sales", _money(kpis.get("sales"))),
        ("Orders", _int(kpis.get("orders"))),
        ("Bags", _int(kpis.get("bags"))),
        ("Avg order", _money(kpis.get("aov"))),
        ("Line items", _int(kpis.get("items"))),
        ("Unique styles", _int(kpis.get("styles"))),
    ]
    kpi_boxes = []
    for label, value in kpi_items:
        kpi_boxes.append(
            Table(
                [[Paragraph(label.upper(), styles["kpi_label"])], [Paragraph(value, styles["kpi_value"])]],
            )
        )
    kpi_grid = Table(
        [kpi_boxes[0:3], kpi_boxes[3:6]],
        colWidths=[usable / 3.0] * 3,
    )
    kpi_grid.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(kpi_grid)

    story.append(Paragraph("Monthly performance", styles["section"]))
    if monthly:
        header = [
            Paragraph("Month", styles["th"]),
            Paragraph("Sales", styles["th_right"]),
            Paragraph("Orders", styles["th_right"]),
            Paragraph("Bags", styles["th_right"]),
            Paragraph("FDC", styles["th_right"]),
            Paragraph("E-Fashion", styles["th_right"]),
        ]
        rows = [header]
        tot_sales = tot_orders = tot_qty = tot_fdc = tot_efas = 0.0
        for row in monthly:
            sales = float(row.get("revenue") or 0)
            orders = float(row.get("orders") or 0)
            qty = float(row.get("qty") or 0)
            fdc = float(row.get("fdc_revenue") or 0)
            efas = float(row.get("efas_revenue") or 0)
            tot_sales += sales
            tot_orders += orders
            tot_qty += qty
            tot_fdc += fdc
            tot_efas += efas
            rows.append(
                [
                    Paragraph(_text(row.get("month")), styles["cell"]),
                    Paragraph(_money(sales), styles["cell_right"]),
                    Paragraph(_int(orders), styles["cell_right"]),
                    Paragraph(_int(qty), styles["cell_right"]),
                    Paragraph(_money(fdc), styles["cell_right"]),
                    Paragraph(_money(efas), styles["cell_right"]),
                ]
            )
        rows.append(
            [
                Paragraph("Total", styles["th"]),
                Paragraph(_money(tot_sales), styles["th_right"]),
                Paragraph(_int(tot_orders), styles["th_right"]),
                Paragraph(_int(tot_qty), styles["th_right"]),
                Paragraph(_money(tot_fdc), styles["th_right"]),
                Paragraph(_money(tot_efas), styles["th_right"]),
            ]
        )
        story.append(
            _table(rows, [90, 95, 70, 70, 95, 102])
        )
    else:
        story.append(Paragraph("No monthly rows in this period.", styles["hint"]))

    sales_img = _decode_image(charts.get("sales") or "", usable / 2 - 6, 150)
    volume_img = _decode_image(charts.get("volume") or "", usable / 2 - 6, 150)
    if sales_img or volume_img:
        story.append(Paragraph("Trend charts", styles["section"]))
        pair = Table(
            [[sales_img or "", volume_img or ""]],
            colWidths=[usable / 2.0, usable / 2.0],
        )
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(pair)
        story.append(Paragraph("Monthly sales (left)  ·  Bags and orders (right)", styles["caption"]))

    story.append(Paragraph(f"Top customers by {rank_mode.lower()}", styles["section"]))
    if customers:
        header = [
            Paragraph("Customer", styles["th"]),
            Paragraph("Sales", styles["th_right"]),
            Paragraph("Orders", styles["th_right"]),
            Paragraph("Bags", styles["th_right"]),
            Paragraph("Avg order", styles["th_right"]),
        ]
        rows = [header]
        tot_sales = tot_orders = tot_qty = 0.0
        for row in customers:
            sales = float(row.get("revenue") or 0)
            orders = float(row.get("orders") or 0)
            qty = float(row.get("qty") or 0)
            tot_sales += sales
            tot_orders += orders
            tot_qty += qty
            aov = sales / orders if orders else 0
            rows.append(
                [
                    Paragraph(_text(row.get("customer"), "—")[:42], styles["cell"]),
                    Paragraph(_money(sales), styles["cell_right"]),
                    Paragraph(_int(orders), styles["cell_right"]),
                    Paragraph(_int(qty), styles["cell_right"]),
                    Paragraph(_money(aov), styles["cell_right"]),
                ]
            )
        rows.append(
            [
                Paragraph("Listed total", styles["th"]),
                Paragraph(_money(tot_sales), styles["th_right"]),
                Paragraph(_int(tot_orders), styles["th_right"]),
                Paragraph(_int(tot_qty), styles["th_right"]),
                Paragraph("", styles["th"]),
            ]
        )
        story.append(_table(rows, [210, 90, 70, 70, 82]))
    else:
        story.append(Paragraph("No customer rows in this period.", styles["hint"]))

    cust_img = _decode_image(charts.get("customer") or "", usable, 170)
    if cust_img:
        story.append(Spacer(1, 8))
        story.append(cust_img)
        story.append(Paragraph("Top customers chart", styles["caption"]))

    mix_img = _decode_image(charts.get("prefix") or "", usable / 2 - 6, 160)
    color_img = _decode_image(charts.get("color") or "", usable / 2 - 6, 160)
    if mix_img or color_img:
        story.append(Paragraph("Style mix and color mix", styles["section"]))
        pair = Table(
            [[mix_img or "", color_img or ""]],
            colWidths=[usable / 2.0, usable / 2.0],
        )
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        story.append(pair)
        story.append(Paragraph("Style mix by SKU prefix (left)  ·  Color mix by bag count (right)", styles["caption"]))

    mix_tables = []
    if prefixes:
        header = [
            Paragraph("Style", styles["th"]),
            Paragraph("Bags", styles["th_right"]),
            Paragraph("Share", styles["th_right"]),
        ]
        rows = [header]
        for row in prefixes:
            rows.append(
                [
                    Paragraph(_text(row.get("name")), styles["cell"]),
                    Paragraph(_int(row.get("qty")), styles["cell_right"]),
                    Paragraph(_text(row.get("pct"), "0%"), styles["cell_right"]),
                ]
            )
        mix_tables.append(_table(rows, [usable / 2 - 12, 70, 60]))
    if color_rows:
        header = [
            Paragraph("Color", styles["th"]),
            Paragraph("Bags", styles["th_right"]),
            Paragraph("Share", styles["th_right"]),
        ]
        rows = [header]
        for row in color_rows:
            rows.append(
                [
                    Paragraph(_text(row.get("name")), styles["cell"]),
                    Paragraph(_int(row.get("qty")), styles["cell_right"]),
                    Paragraph(_text(row.get("pct"), "0%"), styles["cell_right"]),
                ]
            )
        mix_tables.append(_table(rows, [usable / 2 - 12, 70, 60]))
    if mix_tables:
        if len(mix_tables) == 1:
            story.append(mix_tables[0])
        else:
            wrap = Table([[mix_tables[0], mix_tables[1]]], colWidths=[usable / 2.0, usable / 2.0])
            wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (1, 0), (1, 0), 8)]))
            story.append(wrap)

    def on_page(canvas, doc_):
        _header_footer(canvas, doc_, generated_at)

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buffer.getvalue()
