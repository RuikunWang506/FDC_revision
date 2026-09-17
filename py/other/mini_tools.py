"""Mini Tools — staff document utilities (PDF, convert, compress, barcode, Excel, OCR)."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Blueprint, jsonify, render_template, request, send_file, session
from werkzeug.utils import secure_filename

from py.utilities.decorators import login_required
from py.utilities.logging_setup import log_event
from py.utilities.mini_tools_ai import (
    AI_TASKS,
    ai_status_payload,
    pick_vision,
    run_text_task,
    vision_ocr,
)
from py.utilities.mini_tools_ocr import ocr_image_bytes, ocr_status_payload, tesseract_available

mini_tools_bp = Blueprint("mini_tools", __name__)

MAX_CONVERT_BYTES = 25 * 1024 * 1024
MAX_CONVERT_FILES = 5
MAX_OCR_BYTES = 15 * 1024 * 1024
CONVERT_TIMEOUT_SEC = 120

CONVERT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".csv",
    ".txt",
    ".html",
    ".htm",
}

LIBREOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def gotenberg_url() -> str:
    return (os.getenv("GOTENBERG_URL") or "").strip().rstrip("/")


def find_soffice() -> str:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for path in LIBREOFFICE_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def convert_engine() -> Tuple[Optional[str], str]:
    """Return (engine_name, location) or (None, '')."""
    url = gotenberg_url()
    if url:
        return "gotenberg", url
    soffice = find_soffice()
    if soffice:
        return "libreoffice", soffice
    return None, ""


def allowed_convert_name(filename: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    return ext in CONVERT_EXTENSIONS


def _safe_stem(filename: str) -> str:
    stem = Path(secure_filename(filename or "document")).stem or "document"
    return stem[:80]


def convert_with_gotenberg(files: List[Tuple[str, bytes]], base_url: str) -> bytes:
    payload = []
    for name, data in files:
        payload.append(("files", (name, data)))
    form = {"merge": "true"} if len(files) > 1 else None
    resp = requests.post(
        f"{base_url}/forms/libreoffice/convert",
        files=payload,
        data=form,
        timeout=CONVERT_TIMEOUT_SEC,
    )
    if resp.status_code >= 400:
        snippet = (resp.text or "")[:240]
        raise RuntimeError(f"Gotenberg returned {resp.status_code}: {snippet}")
    if not resp.content:
        raise RuntimeError("Gotenberg returned an empty PDF.")
    return resp.content


def convert_with_soffice(files: List[Tuple[str, bytes]], soffice: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="fdc_mini_tools_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        pdf_parts: List[Path] = []
        for index, (name, data) in enumerate(files):
            ext = Path(name).suffix.lower() or ".txt"
            src = Path(tmp) / f"in_{index}{ext}"
            src.write_bytes(data)
            proc = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    "--nolockcheck",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(out_dir),
                    str(src),
                ],
                capture_output=True,
                text=True,
                timeout=CONVERT_TIMEOUT_SEC,
                check=False,
            )
            expected = out_dir / f"{src.stem}.pdf"
            if proc.returncode != 0 and not expected.is_file():
                err = (proc.stderr or proc.stdout or "LibreOffice convert failed").strip()
                raise RuntimeError(err[:240])
            if not expected.is_file():
                raise RuntimeError(f"LibreOffice did not produce a PDF for {name}.")
            pdf_parts.append(expected)

        if len(pdf_parts) == 1:
            return pdf_parts[0].read_bytes()

        try:
            from pypdf import PdfWriter
        except ImportError as exc:
            raise RuntimeError("pypdf is required to merge converted PDFs.") from exc
        writer = PdfWriter()
        for part in pdf_parts:
            writer.append(str(part))
        dest = out_dir / "merged.pdf"
        with dest.open("wb") as handle:
            writer.write(handle)
        return dest.read_bytes()


def convert_documents(files: List[Tuple[str, bytes]]) -> bytes:
    engine, location = convert_engine()
    if engine == "gotenberg":
        return convert_with_gotenberg(files, location)
    if engine == "libreoffice":
        return convert_with_soffice(files, location)
    raise RuntimeError(
        "Document conversion is not configured. Set GOTENBERG_URL or install LibreOffice."
    )


@mini_tools_bp.route("/mini_tools", methods=["GET"])
@login_required
def mini_tools_page():
    engine, _location = convert_engine()
    ocr = ocr_status_payload()
    ai = ai_status_payload()
    return render_template(
        "mini_tools.html",
        current_user=session.get("username"),
        convert_available=bool(engine),
        convert_engine=engine or "",
        ocr_available=bool(ocr.get("available") or ai.get("vision")),
        ai_available=bool(ai.get("available")),
        ai_status=ai,
    )


@mini_tools_bp.route("/mini_tools/api/status", methods=["GET"])
@login_required
def mini_tools_status():
    return jsonify(
        ok=True,
        convert=convert_status_payload(),
        ocr=ocr_status_payload(),
        ai=ai_status_payload(),
    )


@mini_tools_bp.route("/mini_tools/api/convert", methods=["POST"])
@login_required
def mini_tools_convert():
    uploads = request.files.getlist("files") or request.files.getlist("file")
    if not uploads:
        return jsonify(ok=False, error="Choose a Word, Excel, or PowerPoint file to convert."), 400
    if len(uploads) > MAX_CONVERT_FILES:
        return jsonify(ok=False, error=f"Maximum {MAX_CONVERT_FILES} files per conversion."), 400

    prepared: List[Tuple[str, bytes]] = []
    total = 0
    for item in uploads:
        filename = (item.filename or "").strip()
        if not filename:
            continue
        if not allowed_convert_name(filename):
            return jsonify(
                ok=False,
                error=f"Unsupported type for {filename}. Use Word, Excel, PowerPoint, or OpenDocument.",
            ), 400
        data = item.read()
        total += len(data)
        if total > MAX_CONVERT_BYTES:
            return jsonify(ok=False, error="Combined upload is over 25 MB."), 400
        if not data:
            return jsonify(ok=False, error=f"{filename} is empty."), 400
        safe_name = _safe_stem(filename) + Path(filename).suffix.lower()
        prepared.append((safe_name, data))

    if not prepared:
        return jsonify(ok=False, error="Choose a Word, Excel, or PowerPoint file to convert."), 400

    engine, _location = convert_engine()
    if not engine:
        return jsonify(
            ok=False,
            error="Document conversion is not configured. Set GOTENBERG_URL or install LibreOffice.",
        ), 503

    user = session.get("username") or "?"
    names = ", ".join(name for name, _data in prepared)
    try:
        pdf_bytes = convert_documents(prepared)
    except subprocess.TimeoutExpired:
        log_event("WARNING", f"MINI_TOOLS convert timeout user={user} files={names}")
        return jsonify(ok=False, error="Conversion timed out. Try a smaller file."), 504
    except Exception as exc:
        log_event("WARNING", f"MINI_TOOLS convert failed user={user} files={names} err={exc}")
        return jsonify(ok=False, error=str(exc)[:300]), 502

    log_event("INFO", f"MINI_TOOLS convert user={user} engine={engine} files={names}")
    download_name = _safe_stem(prepared[0][0]) + ".pdf"
    if len(prepared) > 1:
        download_name = "converted.pdf"
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )


@mini_tools_bp.route("/mini_tools/api/ocr", methods=["POST"])
@login_required
def mini_tools_ocr():
    upload = request.files.get("file") or (request.files.getlist("files") or [None])[0]
    if upload is None or not (upload.filename or "").strip():
        return jsonify(ok=False, error="Choose a photo to read."), 400
    data = upload.read()
    if not data:
        return jsonify(ok=False, error="That image is empty."), 400
    if len(data) > MAX_OCR_BYTES:
        return jsonify(ok=False, error="Image is over 15 MB."), 400
    lang = (request.form.get("lang") or request.args.get("lang") or "eng").strip()
    engine = (request.form.get("engine") or "auto").strip().lower()
    if engine not in {"auto", "vision", "tesseract"}:
        engine = "auto"
    user = session.get("username") or "?"
    errors: List[str] = []
    used = ""
    result: Dict[str, Any] = {}

    want_vision = engine in {"auto", "vision"} and bool(pick_vision()[1])
    if want_vision:
        try:
            result = vision_ocr(data, lang=lang)
            used = "ollama-vision"
            if not (result.get("text") or "").strip():
                result = {}
                used = ""
        except Exception as exc:
            errors.append(f"vision: {exc}")
            if engine == "vision":
                log_event("WARNING", f"MINI_TOOLS ocr vision failed user={user} err={exc}")
                return jsonify(ok=False, error=str(exc)[:300]), 502

    if engine == "tesseract" or (engine == "auto" and not (result.get("text") or "").strip()):
        if not tesseract_available():
            if result.get("text"):
                used = used or "ollama-vision"
            else:
                return jsonify(
                    ok=False,
                    error=errors[0] if errors else "Tesseract is not installed on this server.",
                ), 503
        else:
            try:
                tess = ocr_image_bytes(data, lang=lang)
                if tess.get("text") and (engine == "tesseract" or not result.get("text")):
                    result = tess
                    used = "tesseract"
            except Exception as exc:
                errors.append(f"tesseract: {exc}")
                if not result.get("text"):
                    log_event("WARNING", f"MINI_TOOLS ocr failed user={user} err={exc}")
                    return jsonify(ok=False, error=str(exc)[:300]), 502

    if not result.get("text"):
        return jsonify(ok=False, error="; ".join(errors) or "No text found."), 502

    log_event(
        "INFO",
        f"MINI_TOOLS ocr user={user} engine={used} chars={len(result.get('text') or '')}",
    )
    return jsonify(
        ok=True,
        text=result.get("text") or "",
        confidence=result.get("confidence"),
        engine=used or result.get("engine") or engine,
        model=result.get("model") or "",
        host=result.get("host") or "",
        lang=result.get("lang") or lang,
    )


@mini_tools_bp.route("/mini_tools/api/ai", methods=["POST"])
@login_required
def mini_tools_ai():
    task = (request.form.get("task") or "").strip()
    if task not in AI_TASKS:
        return jsonify(ok=False, error="Choose a task (translate, extract, draft, cleanup)."), 400
    text = (request.form.get("text") or "").strip()
    upload = request.files.get("file")
    image = upload.read() if upload and upload.filename else b""
    if image and len(image) > MAX_OCR_BYTES:
        return jsonify(ok=False, error="Image is over 15 MB."), 400
    user = session.get("username") or "?"
    try:
        result = run_text_task(task, text, image_bytes=image or None)
    except Exception as exc:
        log_event("WARNING", f"MINI_TOOLS ai failed user={user} task={task} err={exc}")
        return jsonify(ok=False, error=str(exc)[:300]), 502
    log_event("INFO", f"MINI_TOOLS ai user={user} task={task} model={result.get('model')}")
    return jsonify(ok=True, **{k: v for k, v in result.items() if k != "source" or v})


def convert_status_payload() -> Dict[str, Any]:
    engine, _location = convert_engine()
    return {"available": bool(engine), "engine": engine or ""}
