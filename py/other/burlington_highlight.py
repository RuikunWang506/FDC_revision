from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify, abort
import os
import pandas as pd
from openpyxl import load_workbook

from py.utilities.setting import Config


burlington_highlight_bp = Blueprint('burlington_highlight', __name__)
EXCLUDED_FOLDERS = {
    os.path.normcase(os.path.normpath(r"C:\Users\stream\Dropbox\Ruikun\Search"))
}


def _is_excluded_path(path_value):
    normalized_path = os.path.normcase(os.path.normpath(path_value))
    for excluded in EXCLUDED_FOLDERS:
        if normalized_path == excluded:
            return True
        if normalized_path.startswith(excluded + os.sep):
            return True
    return False


def _get_burlington_root():
    return getattr(Config, 'BURLINGTON_HIGHLIGHT', r"C:\users\stream\dropbox\ruikun")


def _trim_path_from_ruikun(file_path):
    normalized_for_search = file_path.replace('/', '\\')
    marker = '\\ruikun\\'
    lowered = normalized_for_search.lower()
    idx = lowered.find(marker)
    if idx != -1:
        suffix = normalized_for_search[idx + len(marker):]
        return f"Z:\\ruikun\\{suffix}"
    return f"Z:\\ruikun\\{os.path.basename(normalized_for_search)}"


def _xlsx_match_info(file_path, normalized_query):
    row_matches = []
    workbook = load_workbook(filename=file_path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                for value in row:
                    if value is not None and normalized_query in str(value).lower():
                        row_matches.append({'sheet': sheet.title, 'row': row_number})
                        break
    finally:
        workbook.close()
    return {
        'matched': len(row_matches) > 0,
        'row_matches': row_matches,
    }


def _xls_match_info(file_path, normalized_query):
    row_matches = []
    sheets = pd.read_excel(file_path, sheet_name=None, header=None, dtype=str)
    for sheet_name, df in sheets.items():
        if df.empty:
            continue
        df_as_text = df.fillna('').astype(str)
        for row_index, row in df_as_text.iterrows():
            for value in row.values:
                if normalized_query in value.lower():
                    row_matches.append({'sheet': sheet_name, 'row': int(row_index) + 1})
                    break
    return {
        'matched': len(row_matches) > 0,
        'row_matches': row_matches,
    }


def _text_file_match_info(file_path, normalized_query):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as handle:
        content = handle.read()
    return {
        'matched': normalized_query in content.lower(),
        'row_matches': [],
    }


def _file_match_info(file_path, normalized_query):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.xlsx':
        return _xlsx_match_info(file_path, normalized_query)
    if ext == '.xls':
        return _xls_match_info(file_path, normalized_query)
    return _text_file_match_info(file_path, normalized_query)


def _search_files_by_content(base_dir, search_text):
    """Return files whose content includes the provided search text (case-insensitive)."""
    matches = []
    normalized_query = search_text.lower()
    normalized_base_dir = os.path.normcase(os.path.normpath(base_dir))

    for root, dirs, files in os.walk(base_dir):
        normalized_root = os.path.normcase(os.path.normpath(root))
        if _is_excluded_path(normalized_root):
            continue

        dirs[:] = [d for d in dirs if not _is_excluded_path(os.path.join(root, d))]

        for filename in files:
            file_path = os.path.join(root, filename)
            try:
                if _is_excluded_path(file_path):
                    continue
                match_info = _file_match_info(file_path, normalized_query)
                if match_info['matched']:
                    if not normalized_root.startswith(normalized_base_dir):
                        continue
                    matches.append({
                        'file_path': file_path,
                        'display_path': _trim_path_from_ruikun(file_path),
                        'relative_path': os.path.relpath(file_path, base_dir),
                        'file_name': filename,
                        'row_matches': match_info['row_matches'],
                    })
            except Exception:
                # Skip unreadable or unsupported files.
                continue

    return matches


@burlington_highlight_bp.route('/burlington_highlight', methods=['GET', 'POST'])
def burlington_highlight():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for('auth.login'))

    current_user = session['username']
    query = ''
    results = []
    error_message = None
    search_root = _get_burlington_root()

    if request.method == 'POST':
        query = (request.form.get('query') or '').strip()

        if query:
            base_dir = search_root
            if not os.path.isdir(base_dir):
                error_message = f'Configured directory does not exist: {base_dir}'
            else:
                results = _search_files_by_content(base_dir, query)

    return render_template(
        'burlington_highlight.html',
        current_user=current_user,
        query=query,
        results=results,
        error_message=error_message,
        search_root=search_root,
    )


@burlington_highlight_bp.route('/burlington_highlight/open_local', methods=['POST'])
def open_burlington_file_local():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    relative_path = (request.form.get('path') or '').strip()
    if not relative_path:
        return jsonify({'success': False, 'message': 'Missing path'}), 400

    base_dir = os.path.normpath(_get_burlington_root())
    requested_path = os.path.normpath(os.path.join(base_dir, relative_path))

    # Prevent path traversal outside the configured root.
    try:
        if os.path.commonpath([base_dir, requested_path]) != base_dir:
            return jsonify({'success': False, 'message': 'Forbidden path'}), 403
    except ValueError:
        return jsonify({'success': False, 'message': 'Forbidden path'}), 403

    if _is_excluded_path(requested_path):
        return jsonify({'success': False, 'message': 'Excluded path'}), 403

    extension = os.path.splitext(requested_path)[1].lower()
    if extension not in {'.xlsx', '.xls'}:
        return jsonify({'success': False, 'message': 'Only Excel files are supported'}), 400

    if not os.path.isfile(requested_path):
        return jsonify({'success': False, 'message': 'File not found'}), 404

    try:
        os.startfile(requested_path)
        return jsonify({'success': True})
    except Exception as exc:
        return jsonify({'success': False, 'message': str(exc)}), 500
