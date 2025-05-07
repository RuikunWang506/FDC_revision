from flask import (
    Blueprint,
    render_template,
    request,
    send_file,
    flash,
    redirect,
    url_for,
    session,
    current_app
)
import io
import uuid
from pypdf import PdfReader, PdfWriter
from pdf2docx import Converter
from openpyxl import Workbook
import zipfile
import tempfile
import os

pdf_toolkit_bp = Blueprint('pdf_toolkit', __name__)

@pdf_toolkit_bp.route('/split_pdf', methods=['GET', 'POST'])
def split_pdf():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file or file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if not file.filename.lower().endswith('.pdf'):
            flash('File must be a PDF', 'error')
            return redirect(request.url)
        
        try:
            reader = PdfReader(file)
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    page_buffer = io.BytesIO()
                    writer.write(page_buffer)
                    page_buffer.seek(0)
                    zip_file.writestr(f'page_{i+1}.pdf', page_buffer.read())
            
            zip_buffer.seek(0)
            return send_file(
                zip_buffer,
                as_attachment=True,
                download_name='split_pages.zip',
                mimetype='application/zip'
            )
            
        except Exception as e:
            current_app.logger.error(f"Error splitting PDF: {e}", exc_info=True)
            flash(f'Error splitting PDF: {str(e)}', 'error')
            return redirect(request.url)
    
    return render_template('online_toolkit.html', current_user=current_user, active_tool='split_pdf')

@pdf_toolkit_bp.route('/compress_pdf', methods=['GET', 'POST'])
def compress_pdf():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file or file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if not file.filename.lower().endswith('.pdf'):
            flash('File must be a PDF', 'error')
            return redirect(request.url)
        
        try:
            reader = PdfReader(file)
            writer = PdfWriter()
            
            for page in reader.pages:
                page.compress_content_streams()
                writer.add_page(page)
            
            buffer = io.BytesIO()
            writer.write(buffer)
            buffer.seek(0)
            
            return send_file(
                buffer,
                as_attachment=True,
                download_name='compressed.pdf',
                mimetype='application/pdf'
            )
            
        except Exception as e:
            current_app.logger.error(f"Error compressing PDF: {e}", exc_info=True)
            flash(f'Error compressing PDF: {str(e)}', 'error')
            return redirect(request.url)
    
    return render_template('online_toolkit.html', current_user=current_user, active_tool='compress_pdf')

@pdf_toolkit_bp.route('/pdf_to_word', methods=['GET', 'POST'])
def pdf_to_word():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file or file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if not file.filename.lower().endswith('.pdf'):
            flash('File must be a PDF', 'error')
            return redirect(request.url)
        
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_pdf:
                file.save(temp_pdf.name)
                temp_docx = tempfile.NamedTemporaryFile(suffix='.docx', delete=False)
                
                cv = Converter(temp_pdf.name)
                cv.convert(temp_docx.name)
                cv.close()
                
                with open(temp_docx.name, 'rb') as f:
                    buffer = io.BytesIO(f.read())
                
                os.unlink(temp_pdf.name)
                os.unlink(temp_docx.name)
                
                buffer.seek(0)
                return send_file(
                    buffer,
                    as_attachment=True,
                    download_name='converted.docx',
                    mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                )
                
        except Exception as e:
            current_app.logger.error(f"Error converting PDF to Word: {e}", exc_info=True)
            flash(f'Error converting PDF to Word: {str(e)}', 'error')
            return redirect(request.url)
    
    return render_template('online_toolkit.html', current_user=current_user, active_tool='pdf_to_word')


@pdf_toolkit_bp.route('/pdf_to_excel', methods=['GET', 'POST'])
def pdf_to_excel():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if not file or file.filename == '':
            flash('No file selected', 'error')
            return redirect(request.url)
        
        if not file.filename.lower().endswith('.pdf'):
            flash('File must be a PDF', 'error')
            return redirect(request.url)
        
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_pdf:
                file.save(temp_pdf.name)
                temp_xlsx = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
                
                reader = PdfReader(temp_pdf.name)
                wb = Workbook()
                ws = wb.active
                
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        lines = text.split('\n')
                        for line in lines:
                            ws.append([line])
                
                wb.save(temp_xlsx.name)
                
                with open(temp_xlsx.name, 'rb') as f:
                    buffer = io.BytesIO(f.read())
                
                os.unlink(temp_pdf.name)
                os.unlink(temp_xlsx.name)
                
                buffer.seek(0)
                return send_file(
                    buffer,
                    as_attachment=True,
                    download_name='converted.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                )
                
        except Exception as e:
            current_app.logger.error(f"Error converting PDF to Excel: {e}", exc_info=True)
            flash(f'Error converting PDF to Excel: {str(e)}', 'error')
            return redirect(request.url)
    
    return render_template('online_toolkit.html', current_user=current_user, active_tool='pdf_to_excel')

@pdf_toolkit_bp.route('/online_toolkit', methods=['GET'])
def online_toolkit():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    
    current_user = session['username']
    return render_template('online_toolkit.html', current_user=current_user)

@pdf_toolkit_bp.route('/pdf_combine', methods=['GET', 'POST'])
def pdf_toolkit():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']

    if request.method == 'POST':
        files = request.files.getlist('pdf_files')
        current_app.logger.debug(
            "[PDF Toolkit] User %s uploaded %d files: %s",
            current_user,
            len(files),
            [f.filename for f in files]
        )

        if not files or files[0].filename == '':
            flash('No files selected', 'error')
            return redirect(request.url)

        for f in files:
            if not f.filename.lower().endswith('.pdf'):
                flash('All files must be PDFs', 'error')
                return redirect(request.url)

        writer = PdfWriter()
        good_count = 0

        for idx, f in enumerate(files, start=1):
            try:
                reader = PdfReader(f.stream or f)
                for page in reader.pages:
                    writer.add_page(page)
                current_app.logger.debug(
                    "[PDF Toolkit] Appended #%d: %s (%d pages)",
                    idx,
                    f.filename,
                    len(reader.pages)
                )
                good_count += 1

            except Exception as e:
                current_app.logger.error(
                    "[PDF Toolkit] Skipping %s: %s", f.filename, e, exc_info=True
                )
                flash(f"Warning: {f.filename} skipped: {e}", "warning")

        if good_count == 0:
            flash("No valid PDF files to merge.", "error")
            return redirect(request.url)

        try:
            buffer = io.BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            merged_name = f"merged_{uuid.uuid4().hex[:8]}.pdf"
            current_app.logger.info(
                "[PDF Toolkit] User %s combined %d PDFs → %s",
                current_user,
                good_count,
                merged_name
            )

            return send_file(
                buffer,
                as_attachment=True,
                download_name=merged_name,
                mimetype='application/pdf'
            )

        except Exception:
            current_app.logger.error(
                "[PDF Toolkit] Error writing merged PDF for %s",
                current_user,
                exc_info=True
            )
            flash('Error generating merged PDF.', 'error')
            return redirect(request.url)

    return render_template('online_toolkit.html', current_user=current_user, active_tool='pdf_combine')
