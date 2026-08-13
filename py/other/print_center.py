"""Print Center — upload PDF/photo and send it to a server-side Windows printer."""

from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, current_app, jsonify, render_template, request, session
from werkzeug.utils import secure_filename

from py.utilities.setting import login_required

print_center_bp = Blueprint("print_center", __name__)

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

WAREHOUSE_PRINTER_DEFAULT = "warehouse printer"
FRONT_OFFICE_PRINTER_DEFAULT = "front office big"

PRINTER_TARGETS = {
    "warehouse": {
        "id": "warehouse",
        "label": "仓库 Label 打印机",
        "label_en": "Warehouse label printer",
        "env_key": "WAREHOUSE_LABEL_PRINTER",
        "default": WAREHOUSE_PRINTER_DEFAULT,
        "hint": "4×6 / shipping labels → warehouse printer",
    },
    "dymo": {
        "id": "dymo",
        "label": "Dymo LabelWriter",
        "label_en": "Dymo LabelWriter",
        "env_key": "DYMO_LABEL_PRINTER",
        "default": "DYMO LabelWriter 450 Turbo",
        "hint": "2.25×1.25 stickers → Dymo 450 Turbo",
    },
    "front_office": {
        "id": "front_office",
        "label": "Front Office Big",
        "label_en": "Front Office Big",
        "env_key": "FRONT_OFFICE_PRINTER",
        "default": FRONT_OFFICE_PRINTER_DEFAULT,
        "hint": "Letter / office docs → front office big",
    },
}


def _configured_printer_name(target_id: str) -> str:
    meta = PRINTER_TARGETS[target_id]
    return os.getenv(meta["env_key"], meta["default"]).strip() or meta["default"]


def _resolve_printer(target_id: str) -> str:
    """Return the installed Windows printer name for a print target."""
    configured = _configured_printer_name(target_id)
    if target_id == "dymo":
        from py.loris.loris_sticker import resolve_dymo_printer_name

        return resolve_dymo_printer_name()

    if sys_platform_is_win32():
        try:
            import win32print
        except ImportError as exc:
            raise RuntimeError("pywin32 is required for direct printing.") from exc

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        installed = [entry[2] for entry in win32print.EnumPrinters(flags)]
        if configured in installed:
            return configured
        configured_l = configured.lower()
        for name in installed:
            if name.lower() == configured_l:
                return name
        needles = []
        if target_id == "front_office":
            needles = ["front office", "front-office"]
        elif target_id == "warehouse":
            needles = ["warehouse"]
        for needle in needles:
            for name in installed:
                if needle in name.lower():
                    return name
        sample = ", ".join(sorted(installed)[:8])
        raise RuntimeError(
            f'Printer "{configured}" was not found. Installed printers include: {sample}'
        )

    return configured


def sys_platform_is_win32() -> bool:
    import sys

    return sys.platform == "win32"


def _list_installed_printers() -> List[str]:
    if not sys_platform_is_win32():
        return []
    try:
        import win32print
    except ImportError:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return sorted({entry[2] for entry in win32print.EnumPrinters(flags)})


def _printer_status_payload() -> List[Dict[str, Any]]:
    installed = set(_list_installed_printers())
    rows = []
    for target_id, meta in PRINTER_TARGETS.items():
        configured = _configured_printer_name(target_id)
        resolved = None
        available = False
        error = None
        try:
            resolved = _resolve_printer(target_id)
            available = resolved in installed if installed else False
        except Exception as exc:
            error = str(exc)
        rows.append(
            {
                "id": target_id,
                "label": meta["label"],
                "hint": meta["hint"],
                "configured": configured,
                "resolved": resolved or configured,
                "available": available,
                "error": error,
            }
        )
    return rows


# Native stock orientation (before user toggle).
DEFAULT_ORIENTATION = {
    "warehouse": "landscape",  # 6×4
    "dymo": "landscape",  # 2.25×1.25
    "front_office": "portrait",  # letter
}


def _normalize_orientation(value: Optional[str], target_id: str) -> str:
    raw = (value or "").strip().lower()
    if raw in ("portrait", "landscape"):
        return raw
    return DEFAULT_ORIENTATION.get(target_id, "portrait")


def _base_page_size_pts(target_id: str) -> Tuple[float, float]:
    """Native (width_pt, height_pt) before orientation swap."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch

    if target_id == "warehouse":
        return (6 * inch, 4 * inch)
    if target_id == "dymo":
        return (2.25 * inch, 1.25 * inch)
    return letter


def _page_size_for_target(target_id: str, orientation: str) -> Tuple[float, float]:
    """Return (width_pt, height_pt) for the selected printer + orientation."""
    w, h = _base_page_size_pts(target_id)
    default = DEFAULT_ORIENTATION.get(target_id, "portrait")
    orientation = _normalize_orientation(orientation, target_id)
    # Base sizes are already in default orientation; swap when user picks the other.
    if orientation == default:
        return (w, h)
    return (h, w)


def _image_to_pdf(image_bytes: bytes, target_id: str, orientation: str) -> bytes:
    """Fit a photo onto one PDF page sized for the selected printer + orientation."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()
    except Exception as exc:
        raise ValueError("Could not read image file.") from exc

    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    page_w, page_h = _page_size_for_target(target_id, orientation)
    margin = 6.0
    max_w = max(1.0, page_w - 2 * margin)
    max_h = max(1.0, page_h - 2 * margin)

    iw, ih = img.size
    scale = min(max_w / float(iw), max_h / float(ih))
    draw_w = iw * scale
    draw_h = ih * scale
    x = (page_w - draw_w) / 2.0
    y = (page_h - draw_h) / 2.0

    bio = io.BytesIO()
    c = canvas.Canvas(bio, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
    c.showPage()
    c.save()
    return bio.getvalue()


def _pdf_to_oriented_pages(pdf_bytes: bytes, target_id: str, orientation: str) -> bytes:
    """
    Fit each source PDF page onto the selected paper size/orientation.
    Matches image print behavior so portrait/landscape always apply.
    """
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import RectangleObject

    page_w, page_h = _page_size_for_target(target_id, orientation)
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if not reader.pages:
        raise ValueError("PDF has no pages.")

    writer = PdfWriter()
    margin = 6.0
    max_w = max(1.0, page_w - 2 * margin)
    max_h = max(1.0, page_h - 2 * margin)

    for src in reader.pages:
        mb = src.mediabox
        src_w = float(mb.width)
        src_h = float(mb.height)
        if src_w <= 0 or src_h <= 0:
            continue
        scale = min(max_w / src_w, max_h / src_h)
        draw_w = src_w * scale
        draw_h = src_h * scale
        tx = (page_w - draw_w) / 2.0
        ty = (page_h - draw_h) / 2.0

        blank = writer.add_blank_page(width=page_w, height=page_h)
        # Scale then translate into the centered print area.
        blank.merge_transformed_page(
            src,
            Transformation().scale(scale, scale).translate(tx, ty),
        )
        box = RectangleObject((0, 0, page_w, page_h))
        blank.mediabox = box
        blank.cropbox = box

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(b"\xff\xd8\xff"):
        return True  # JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data.startswith(b"BM"):
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return True  # TIFF
    return False


def _read_upload(
    target_id: str, orientation: str
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (pdf_bytes, filename, error). Images/PDFs are fitted to target paper + orientation."""
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").strip():
        return None, None, "Choose a PDF or photo (JPG/PNG) to print."

    filename = secure_filename(upload.filename) or "upload.bin"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None, None, "Only PDF and photo files (JPG, JPEG, PNG, WEBP) are supported."

    data = upload.read()
    if not data:
        return None, None, "The uploaded file is empty."
    if len(data) > MAX_UPLOAD_BYTES:
        return None, None, f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."

    orientation = _normalize_orientation(orientation, target_id)

    if ext == ".pdf" or data.startswith(b"%PDF"):
        if not data.startswith(b"%PDF"):
            return None, None, "File does not look like a PDF."
        try:
            pdf_bytes = _pdf_to_oriented_pages(data, target_id, orientation)
        except Exception as exc:
            current_app.logger.exception("PDF orientation fit failed for %s", filename)
            return None, None, f"Could not prepare PDF for printer: {exc}"
        return pdf_bytes, filename, None

    if ext in IMAGE_EXTENSIONS or _looks_like_image(data):
        try:
            pdf_bytes = _image_to_pdf(data, target_id, orientation)
        except ValueError as exc:
            return None, None, str(exc)
        except Exception as exc:
            current_app.logger.exception("Image→PDF failed for %s", filename)
            return None, None, f"Could not convert image to printable PDF: {exc}"
        return pdf_bytes, filename, None

    return None, None, "Unsupported file type."


def _send_pdf(target_id: str, pdf_bytes: bytes, filename: str) -> str:
    """Send PDF bytes to the resolved Windows printer queue."""
    printer = _resolve_printer(target_id)
    from py.Toolkits.Label_Maker import _print_pdf_to_windows_printer

    _print_pdf_to_windows_printer(pdf_bytes, printer, current_app.logger)
    return printer


@print_center_bp.route("/print_center")
@login_required
def print_center_page():
    return render_template(
        "print_center.html",
        current_user=session.get("username"),
        printers=_printer_status_payload(),
        loris_url="/lori_automation/loris_fedex",
    )


@print_center_bp.route("/print_center/status", methods=["GET"])
@login_required
def print_center_status():
    return jsonify({"ok": True, "printers": _printer_status_payload()})


@print_center_bp.route("/print_center/print", methods=["POST"])
@login_required
def print_center_print():
    target = (request.form.get("target") or "").strip().lower()
    if target not in PRINTER_TARGETS:
        return jsonify({"ok": False, "error": "Choose a printer: warehouse, dymo, or front_office."}), 400

    orientation = _normalize_orientation(request.form.get("orientation"), target)
    pdf_bytes, filename, err = _read_upload(target, orientation)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    copies_raw = (request.form.get("copies") or "1").strip()
    try:
        copies = max(1, min(20, int(copies_raw)))
    except (TypeError, ValueError):
        copies = 1

    user = session.get("username") or "?"
    try:
        printer_used = None
        for i in range(copies):
            printer_used = _send_pdf(target, pdf_bytes, filename or "upload.pdf")
            current_app.logger.info(
                "***%s***__@@@print_center@@@__ file=%s target=%s orientation=%s printer=%s copy=%s/%s",
                user,
                filename,
                target,
                orientation,
                printer_used,
                i + 1,
                copies,
            )
        return jsonify(
            {
                "ok": True,
                "printer": printer_used,
                "target": target,
                "orientation": orientation,
                "filename": filename,
                "copies": copies,
                "message": f'Sent "{filename}" ({orientation}) to {printer_used}'
                + (f" ×{copies}" if copies > 1 else ""),
            }
        )
    except Exception as exc:
        current_app.logger.exception("Print Center failed target=%s file=%s", target, filename)
        return jsonify({"ok": False, "error": str(exc)}), 500
