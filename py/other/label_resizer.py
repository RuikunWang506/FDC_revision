"""Center-crop US Letter-style PDFs to a 4×6 inch label for each page."""

import io
import re
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file, session
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from werkzeug.utils import secure_filename

from py.utilities.setting import login_required

label_resizer_bp = Blueprint("label_resizer", __name__)

# 72 PDF points per inch
INCH_PT = 72.0
CROP_W_PT = 4 * INCH_PT
CROP_H_PT = 6 * INCH_PT
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def _crop_pdf_center_4x6(pdf_bytes: bytes) -> bytes:
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if len(reader.pages) == 0:
        raise ValueError("PDF has no pages.")
    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        mb = page.mediabox
        w = float(mb.width)
        h = float(mb.height)
        if w + 0.5 < CROP_W_PT or h + 0.5 < CROP_H_PT:
            raise ValueError(
                f"Page {i + 1} is {_points_to_inches_str(w, h)}; "
                f"needs at least 4×6 in ({CROP_W_PT:.0f}×{CROP_H_PT:.0f} pt) to crop."
            )
        llx = float(mb.left)
        lly = float(mb.bottom)
        dx = (w - CROP_W_PT) / 2.0
        dy = (h - CROP_H_PT) / 2.0
        nllx = llx + dx
        nlly = lly + dy
        nurx = nllx + CROP_W_PT
        nury = nlly + CROP_H_PT
        box = RectangleObject((nllx, nlly, nurx, nury))
        page.mediabox = box
        page.cropbox = box
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


@label_resizer_bp.route("/label_resizer")
@login_required
def label_resizer_page():
    current_user = session.get("username", "")
    return render_template(
        "label_resizer.html",
        current_user=current_user,
    )


@label_resizer_bp.route("/label_resizer/process", methods=["POST"])
@login_required
def label_resizer_process():
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
        out_bytes = _crop_pdf_center_4x6(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Could not process PDF: {e}"}), 400

    stem = re.sub(r"[^\w\-]+", "_", _stem_from_upload(upload.filename))
    if not stem:
        stem = "label"
    download_name = f"{stem}_4x6.pdf"
    return send_file(
        io.BytesIO(out_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )
