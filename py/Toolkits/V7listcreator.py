from py.utilities.setting import login_required
from py.utilities.csrf import get_csrf_token, rewrite_html_csrf_meta
from flask import Blueprint, render_template, request, jsonify, current_app, send_file, send_from_directory, redirect, url_for, flash, session, Response
from werkzeug.utils import secure_filename
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, PatternFill, Border, Side
from openpyxl.drawing.image import Image as OpenPyXLImage
from openpyxl.utils import get_column_letter

from io import BytesIO
import json
import os
import re
import subprocess
import threading
import time
import uuid
import pyodbc
import pandas as pd
from py.utilities.setting import Config
from py.utilities.setting import resize_image
from py.utilities.dam_utils import get_canonical_assets_for_skus, get_asset_url
from py.utilities.ocean_qty import apply_ocean_qty_to_rows, PLOG_OCEAN_JOIN, ocean_qty_sql_scalar
import py.utilities.models
import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote, unquote
import mysql.connector
from mysql.connector import Error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from py.other.bag_style_viewer import (
    fetch_description_options,
    fetch_style_codes_for_filters,
    _normalize_description_list,
)

v7_listcreator_bp = Blueprint('v7_listcreator', __name__, template_folder='templates')


@v7_listcreator_bp.context_processor
def _v7_listcreator_template_globals():
    """Expose server Dropbox root so the browser can turn pasted full paths into dropbox\\relative paths."""
    return {
        'v7_local_dropbox_dir': getattr(Config, 'LOCAL_DROPBOX_DIR', '') or '',
    }


# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG
db_config = Config.DB_CONFIG

 

selected_images = []  # This should be populated based on your application logic
formatted_date = datetime.datetime.now().strftime("%Y%m%d")  # Example date formatting

# Full_catalog embeds 1800px images per card — often doubles CPU/time vs Export_600 alone and can exceed
# reverse-proxy limits (e.g. Cloudflare 524). Omit automatically above these thresholds.
WORKBOOK_FULL_CATALOG_MAX_STYLES = 35
WORKBOOK_FULL_CATALOG_MAX_ROWS = 80

# Async workbook exports: rolling file history per user (see export_workbook queue).
WORKBOOK_EXPORT_HISTORY_MAX = 3
# Mark "running" jobs failed if no heartbeat for this long (hangs, stuck threads).
WORKBOOK_EXPORT_STALE_AFTER_SEC = int(os.environ.get("WORKBOOK_EXPORT_STALE_AFTER_SEC", "3600"))
_workbook_manifest_lock = threading.Lock()


# Staged HTML render for large/Dropbox lists: description gate first, then background full page (cached ~7 days).
LISTCREATOR_HTML_CACHE_DAYS = 7
_listcreator_html_lock = threading.Lock()
LISTCREATOR_SNAPSHOT_LABEL_MAX = 120
PACIFIC_TZ = ZoneInfo('America/Los_Angeles')

# Friendly names for snapshot job owner filter + badges (keys: login or storage slug, lowercased).
LISTCREATOR_JOB_OWNER_DISPLAY = {
    'xj': 'Elaine',
    'yeti': 'Tristen',
}

def _listcreator_job_owner_display(job_username: str, storage_slug: str) -> str:
    """Public label for a job row / filter (mapped name, else login, else folder slug)."""
    u = (job_username or '').strip()
    s = (storage_slug or '').strip()
    for key, label in LISTCREATOR_JOB_OWNER_DISPLAY.items():
        if u.lower() == key or s.lower() == key:
            return label
    if u:
        return u
    if s:
        return s
    return ''


def _listcreator_job_owner_tooltip(job_username: str, storage_slug: str) -> str:
    """Hover detail: raw login and disk folder for support."""
    u = (job_username or '').strip()
    s = (storage_slug or '').strip()
    parts = []
    if u:
        parts.append(f'Login: {u}')
    if s:
        parts.append(f'Folder: {s}')
    return ' · '.join(parts) if parts else ''


def _listcreator_my_jobs_filter_label(current_user: str, me_slug: str) -> str:
    cu = (current_user or '').strip().lower()
    sl = (me_slug or '').strip().lower()
    if cu in LISTCREATOR_JOB_OWNER_DISPLAY:
        return f"My jobs — {LISTCREATOR_JOB_OWNER_DISPLAY[cu]}"
    if sl in LISTCREATOR_JOB_OWNER_DISPLAY:
        return f"My jobs — {LISTCREATOR_JOB_OWNER_DISPLAY[sl]}"
    return 'My jobs'


def _pst_now_parts():
    """Wall time in Pacific (PST/PDT) with human label for UI."""
    dt = datetime.datetime.now(PACIFIC_TZ)
    return {
        'created_at_iso': dt.isoformat(timespec='seconds'),
        'created_at_pt_display': dt.strftime('%Y-%m-%d %I:%M %p %Z'),
    }


def _sanitize_snapshot_label(raw):
    if not raw or not str(raw).strip():
        return ''
    s = re.sub(r'[\s]+', ' ', str(raw).strip())
    return s[:LISTCREATOR_SNAPSHOT_LABEL_MAX]


def _listcreator_input_source_looks_like_dropbox_path(list_input):
    """True when the user's original submission is a Dropbox/folder-style path vs a SKU line."""
    if not list_input or not str(list_input).strip():
        return False
    s = str(list_input).strip().lower()
    if 'dropbox' in s:
        return True
    x = str(list_input).strip()
    return '\\' in x or bool(re.match(r'^[a-z]:\\', x)) or x.startswith('\\\\')


def _listcreator_input_is_single_dropbox_path(list_input):
    """True when the whole line is one Dropbox path (folder names may contain spaces)."""
    s = (list_input or '').strip()
    if not s or ',' in s or ';' in s:
        return False
    if re.match(r'^dropbox\\', s, re.IGNORECASE):
        return True
    if re.match(r'^[a-z]:\\', s, re.IGNORECASE) and re.search(r'\\dropbox\\', s, re.IGNORECASE):
        return True
    if s.startswith('\\\\') and 'dropbox' in s.lower():
        return True
    return False


def _token_looks_like_style(token):
    """Catalog style code: letters + 4 digits, optional size suffix (e.g. L1343, TBDBN3602)."""
    s = (token or '').strip()
    if not s:
        return False
    return bool(
        re.match(r'^[A-Za-z]{1,2}\d{4}([-]?[smlSML]*)?$', s, re.IGNORECASE)
        or re.match(r'^[A-Za-z]{3,}\d{4}([-]?[smlSML]*)?$', s, re.IGNORECASE)
    )


def _extract_container_pur_num_from_token(token):
    """Container PO from fdcef URL or plain token (e.g. F26193)."""
    t = (token or '').strip()
    if not t:
        return None
    low = t.lower()
    if 'container_search' in low or 'container_id=' in low:
        m = re.search(r'[?&]container_id=([^&#\s]+)', t, re.IGNORECASE)
        if m:
            return unquote(m.group(1)).strip().upper()
    if re.match(r'^https?://', low, re.IGNORECASE):
        return None
    s = t.upper()
    if _token_looks_like_style(s):
        return None
    if _listcreator_input_source_looks_like_dropbox_path(t):
        return None
    # Container PO: one leading letter then digits only (e.g. F26193, T3305).
    if re.match(r'^[A-Z]\d+$', s):
        return s
    return None


def _styles_from_container_pur_nums(pur_nums):
    """
    Resolve container PO numbers (plog.pur_num) to unique style codes.
    Returns (pur_num -> [styles], error_message).
    """
    unique = []
    seen = set()
    for p in pur_nums or []:
        u = (p or '').strip().upper()
        if u and u not in seen:
            seen.add(u)
            unique.append(u)
    if not unique:
        return {}, None

    by_pur = {p: [] for p in unique}
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};'
            f'DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};'
            f'PWD={sql_server_config["password"]}'
        )
        cursor = conn.cursor()
        placeholders = ','.join('?' * len(unique))
        query = f"""
            SELECT DISTINCT pl.pur_num,
                LEFT(pl.prod_cd, CHARINDEX(' ', pl.prod_cd + ' ') - 1) AS style
            FROM plog pl
            WHERE pl.pur_num IN ({placeholders})
            AND (pl.hdl_chg > 0 OR pl.log_qty > 0)
            AND pl.prod_cd IS NOT NULL AND LTRIM(RTRIM(pl.prod_cd)) <> ''
        """
        cursor.execute(query, unique)
        for row in cursor.fetchall():
            pur = (row[0] or '').strip().upper()
            style = (row[1] or '').strip().upper()
            if pur and style and style not in by_pur.setdefault(pur, []):
                by_pur[pur].append(style)
        cursor.close()
        conn.close()
    except Exception as e:
        current_app.logger.error('listcreator container style lookup failed: %s', e, exc_info=True)
        return None, 'Could not load styles from container database. Try again or enter styles manually.'

    return by_pur, None


def _expand_listcreator_mixed_input(list_input):
    """
    Expand container IDs/URLs into styles and merge with explicit style tokens.
    A single Dropbox path is returned unchanged. Returns (expanded_input, error_message).
    """
    raw = (list_input or '').strip()
    if not raw:
        return raw, None

    # Folder names may contain spaces — never tokenize a lone dropbox\... path on whitespace.
    if _listcreator_input_is_single_dropbox_path(raw):
        return raw, None

    tokens = [t.strip() for t in re.split(r'[\s,\n\r;]+', raw) if t.strip()]
    if not tokens:
        return raw, None

    if len(tokens) == 1 and _listcreator_input_source_looks_like_dropbox_path(tokens[0]):
        return raw, None

    dropbox_parts = []
    style_parts = []
    container_ids = []

    for tok in tokens:
        cid = _extract_container_pur_num_from_token(tok)
        if cid:
            if cid not in container_ids:
                container_ids.append(cid)
        elif _listcreator_input_source_looks_like_dropbox_path(tok):
            dropbox_parts.append(tok)
        elif _token_looks_like_style(tok):
            style_parts.append(tok.strip().upper())
        else:
            style_parts.append(tok.strip().upper())

    if dropbox_parts and (container_ids or style_parts):
        return None, (
            'Use either one Dropbox folder path by itself, '
            'or a comma-separated list of styles and/or container numbers (e.g. T3305, F26193).'
        )
    if len(dropbox_parts) > 1:
        return None, 'Only one Dropbox folder path is allowed.'
    if dropbox_parts:
        return dropbox_parts[0], None

    if not container_ids:
        seen = set()
        unique = []
        for s in style_parts:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return ','.join(unique) if unique else raw, None

    by_pur, db_err = _styles_from_container_pur_nums(container_ids)
    if db_err:
        return None, db_err

    all_styles = list(style_parts)
    missing = []
    for cid in container_ids:
        styles = (by_pur or {}).get(cid) or []
        if not styles:
            # e.g. TBDBN3602 was misread as a container PO — keep it as an explicit style.
            if _token_looks_like_style(cid):
                all_styles.append(cid.strip().upper())
            else:
                missing.append(cid)
        else:
            all_styles.extend(styles)

    if missing:
        return None, f'No styles found for container(s): {", ".join(missing)}'

    seen = set()
    unique = []
    for s in all_styles:
        su = s.upper()
        if su not in seen:
            seen.add(su)
            unique.append(su)

    expanded = ','.join(unique)
    current_app.logger.info(
        'listcreator expanded input: %d container(s), %d explicit style(s) -> %d unique style(s)',
        len(container_ids),
        len(style_parts),
        len(unique),
    )
    return expanded, None


def _list_input_is_dropbox_subpath_folder(list_input):
    """True only for `dropbox\\...` synced paths used to write aaaa.xlsx into that folder."""
    if not list_input or not str(list_input).strip():
        return False
    return bool(re.search(r'dropbox\\(.+)', str(list_input).strip(), re.IGNORECASE))


def _resolve_v7_export_dropbox_dir(list_input_raw):
    """
    Resolved Dropbox synced folder path under LOCAL_DROPBOX_DIR for writing aaaa.xlsx.
    Returns absolute path string, or None if invalid/missing/not a directory.
    """
    if not list_input_raw or not str(list_input_raw).strip():
        return None
    m = re.search(r'dropbox\\(.+)', str(list_input_raw).strip(), re.IGNORECASE)
    if not m:
        return None
    relative = m.group(1).strip().rstrip('\\/')
    root = getattr(Config, 'LOCAL_DROPBOX_DIR', None) or ''
    if not root:
        current_app.logger.error('LOCAL_DROPBOX_DIR missing')
        return None
    try:
        base = Path(os.path.abspath(str(root))).resolve()
        candidate = Path(os.path.join(str(base), relative.replace('/', os.sep)))
        dest = candidate.resolve(strict=False)
        dest.relative_to(base)
    except (ValueError, OSError):
        current_app.logger.warning('rejecting Dropbox export path (escape or invalid): %r', list_input_raw)
        return None
    if not dest.is_dir():
        current_app.logger.info('Dropbox export folder not present: %s', dest)
        return None
    return str(dest)


def _v7_count_images_in_folder(path: Path) -> int:
    """Non-recursive count of .jpg / .png files (same rule as list_from_dropbox / count_dropbox_files)."""
    n = 0
    try:
        for name in os.listdir(path):
            if name.lower().endswith(('.jpg', '.png')):
                n += 1
    except OSError:
        return 0
    return n


def _v7_safe_resolve_dropbox_browse_path(relative_raw: str):
    """Resolve a subpath under LOCAL_DROPBOX_DIR. Returns (Path | None, error_code str)."""
    root = getattr(Config, 'LOCAL_DROPBOX_DIR', None) or ''
    if not root:
        return None, 'not_configured'
    try:
        base = Path(os.path.abspath(str(root))).resolve()
    except OSError:
        return None, 'not_configured'
    rel = (relative_raw or '').strip().replace('/', '\\')
    parts = [p for p in rel.split('\\') if p and p != '.']
    if any(p == '..' for p in parts):
        return None, 'invalid'
    try:
        candidate = base.joinpath(*parts) if parts else base
        dest = candidate.resolve(strict=False)
        dest.relative_to(base)
    except (ValueError, OSError):
        return None, 'invalid'
    if not dest.exists():
        return None, 'not_found'
    if not dest.is_dir():
        return None, 'not_dir'
    return dest, 'ok'


def _v7_rel_path_under_dropbox(base: Path, dest: Path) -> str:
    try:
        rel = dest.relative_to(base)
    except ValueError:
        return ''
    return str(rel).replace('/', '\\')


def _v7_subprocess_no_window_flags():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


_V7_DROPBOX_SERVICE_NAME = 'dropbox'
_V7_DROPBOX_RESTART_TASK = 'FDC Restart Dropbox'
_V7_DROPBOX_USER_START_TASK = 'FDC Start Dropbox User'
_V7_NSSM_EXE_CANDIDATES = (
    r'C:\nssm\nssm.exe',
    r'C:\NSSM\nssm.exe',
)


def _v7_nssm_exe_path():
    for p in _V7_NSSM_EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ''


def _v7_dropbox_restart_task_configured():
    if os.name != 'nt':
        return False
    try:
        r = subprocess.run(
            ['schtasks', '/Query', '/TN', _V7_DROPBOX_RESTART_TASK],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_v7_subprocess_no_window_flags(),
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _v7_dropbox_user_start_task_configured():
    if os.name != 'nt':
        return False
    try:
        r = subprocess.run(
            ['schtasks', '/Query', '/TN', _V7_DROPBOX_USER_START_TASK],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_v7_subprocess_no_window_flags(),
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _v7_dropbox_windows_user():
    """Windows account that owns LOCAL_DROPBOX_DIR (tray/sync client user)."""
    root = getattr(Config, 'LOCAL_DROPBOX_DIR', None) or ''
    m = re.match(r'^[a-zA-Z]:\\users\\([^\\]+)\\', str(root), re.IGNORECASE)
    if m:
        return m.group(1)
    return os.environ.get('DROPBOX_WINDOWS_USER', 'stream')


def _v7_dropbox_process_rows():
    """Dropbox.exe rows from tasklist: pid, session_id (0 = SYSTEM service)."""
    if os.name != 'nt':
        return []
    try:
        r = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq Dropbox.exe', '/FO', 'CSV', '/NH'],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_v7_subprocess_no_window_flags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    rows = []
    for line in (r.stdout or '').splitlines():
        ln = line.strip()
        if not ln or ln.upper().startswith('INFO:'):
            continue
        if 'dropbox.exe' not in ln.lower():
            continue
        parts = [p.strip('"') for p in ln.split('","')]
        if parts and parts[0].startswith('"'):
            parts[0] = parts[0].lstrip('"')
        if parts and parts[-1].endswith('"'):
            parts[-1] = parts[-1].rstrip('"')
        if len(parts) < 4:
            continue
        pid = parts[1]
        try:
            session_id = int(parts[3])
        except (TypeError, ValueError):
            session_id = 0
        if pid.isdigit():
            rows.append({'pid': pid, 'session_id': session_id})
    return rows


def _v7_dropbox_access_denied(err_text: str) -> bool:
    t = (err_text or '').lower()
    return (
        'access is denied' in t
        or "can't open service" in t
        or 'openervice' in t
    )


def _v7_dropbox_restart_bat_path():
    try:
        root = Path(__file__).resolve().parents[2]
    except IndexError:
        return ''
    p = root / 'auto_run' / 'restart_dropbox_service.bat'
    return str(p) if p.is_file() else ''


def _v7_process_runs_as_local_system():
    if os.name != 'nt':
        return False
    if (os.environ.get('USERNAME') or '').upper() == 'SYSTEM':
        return True
    if str(os.environ.get('SESSIONNAME', '')).strip().lower() == 'services':
        return True
    for marker in ('NSSM_SERVICE_NAME', 'NSSM_APPLICATION', 'SERVICE_NAME'):
        if os.environ.get(marker):
            return True
    return False


def _v7_dropbox_run_restart_bat_direct():
    """Run restart batch (only works when this process can control services)."""
    bat = _v7_dropbox_restart_bat_path()
    if not bat:
        return 'restart_dropbox_service.bat not found.'
    flags = _v7_subprocess_no_window_flags()
    try:
        r = subprocess.run(
            ['cmd', '/c', bat],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=flags,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return str(e)
    if r.returncode != 0:
        return (r.stderr or r.stdout or 'restart_dropbox_service.bat failed').strip()
    return None


def _v7_dropbox_task_already_running_message(err_text: str) -> bool:
    t = (err_text or '').lower()
    return 'currently running' in t


def _v7_dropbox_wait_for_restart_task_idle(timeout_sec=120):
    """Poll until FDC Restart Dropbox is not Running (best-effort)."""
    flags = _v7_subprocess_no_window_flags()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ['schtasks', '/Query', '/TN', _V7_DROPBOX_RESTART_TASK, '/FO', 'LIST', '/V'],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=flags,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            break
        if r.returncode != 0:
            break
        text = r.stdout or ''
        if re.search(r'^\s*Status:\s*Running\s*$', text, re.MULTILINE | re.IGNORECASE):
            time.sleep(2)
            continue
        break


def _v7_dropbox_trigger_restart_task():
    """Run the SYSTEM scheduled task (Setup-DropboxRestartTask.ps1). Returns error str or None."""
    flags = _v7_subprocess_no_window_flags()
    try:
        r = subprocess.run(
            ['schtasks', '/Run', '/TN', _V7_DROPBOX_RESTART_TASK],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=flags,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return str(e)
    out = (r.stdout or '') + '\n' + (r.stderr or '')
    if r.returncode != 0:
        err = out.strip() or 'schtasks /Run failed'
        if _v7_dropbox_task_already_running_message(err):
            _v7_dropbox_wait_for_restart_task_idle()
            return None
        if _v7_dropbox_access_denied(err) and _v7_process_runs_as_local_system():
            direct_err = _v7_dropbox_run_restart_bat_direct()
            if not direct_err:
                return None
            if direct_err:
                current_app.logger.warning('schtasks denied; direct bat: %s', direct_err)
        return err
    _v7_dropbox_wait_for_restart_task_idle(timeout_sec=90)
    return None


def _v7_run_sc_queryex(service_name: str):
    """Parse `sc queryex` for Windows service state and PID."""
    if os.name != 'nt':
        return None
    try:
        r = subprocess.run(
            ['sc', 'queryex', service_name],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_v7_subprocess_no_window_flags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    text = r.stdout or ''
    state = None
    pid = None
    for line in text.splitlines():
        ln = line.strip()
        if ln.startswith('STATE'):
            # e.g. STATE : 4  RUNNING
            m = re.search(r'\d+\s+(\S+)', ln)
            if m:
                state = m.group(1).upper()
        elif ln.upper().startswith('PID'):
            m = re.search(r':\s*(\d+)', ln)
            if m and int(m.group(1)) > 0:
                pid = m.group(1)
    return {'state': state or 'UNKNOWN', 'pid': pid}


def _v7_dropbox_service_state():
    svc = _v7_run_sc_queryex(_V7_DROPBOX_SERVICE_NAME)
    return svc.get('state') if svc else None


def _v7_dropbox_wait_for_service_state(target_states, timeout_sec=90):
    """Wait until service reaches one of target_states (e.g. STOPPED, RUNNING)."""
    targets = {s.upper() for s in target_states}
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        state = _v7_dropbox_service_state()
        if state and state.upper() in targets:
            return state
        time.sleep(1.5)
    return _v7_dropbox_service_state()


def _v7_dropbox_start_user_sync_client():
    """Launch Dropbox.exe in the logged-in Windows user session."""
    flags = _v7_subprocess_no_window_flags()
    if _v7_dropbox_user_start_task_configured():
        try:
            r = subprocess.run(
                ['schtasks', '/Run', '/TN', _V7_DROPBOX_USER_START_TASK],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=flags,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or 'schtasks user start failed').strip()
        return True, None
    exes = _v7_dropbox_desktop_exe_candidates()
    if not exes:
        return False, 'Dropbox.exe not found and user start task is not configured.'
    try:
        subprocess.Popen(
            ['cmd', '/c', 'start', '', exes[0]],
            creationflags=flags,
            close_fds=True,
        )
    except OSError as e:
        return False, str(e)
    return True, None


def _v7_dropbox_controlled_service_restart():
    """
    Stop SYSTEM service, kill all Dropbox.exe, start user-session sync client.
    Returns (ok: bool, error: str|None).
    """
    flags = _v7_subprocess_no_window_flags()
    name = _V7_DROPBOX_SERVICE_NAME

    state = _v7_dropbox_service_state()
    if state == 'STOP_PENDING':
        _v7_dropbox_wait_for_service_state(['STOPPED'], timeout_sec=90)
        state = _v7_dropbox_service_state()
    elif state == 'START_PENDING':
        _v7_dropbox_wait_for_service_state(['RUNNING', 'STOPPED'], timeout_sec=90)
        state = _v7_dropbox_service_state()

    if state == 'RUNNING':
        subprocess.run(
            ['net', 'stop', name],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=flags,
        )
        final = _v7_dropbox_wait_for_service_state(['STOPPED'], timeout_sec=90)
        if final and final.upper() != 'STOPPED':
            return False, f'Dropbox service did not stop (still {final}).'

    _v7_dropbox_kill_orphan_processes()
    time.sleep(2)

    ok, err = _v7_dropbox_start_user_sync_client()
    if not ok:
        return False, err or 'Could not start Dropbox user sync client.'
    time.sleep(3)
    st = _v7_dropbox_desktop_status()
    if st.get('sync_client_running'):
        return True, None
    return False, (
        f'Dropbox sync client did not appear in {_v7_dropbox_windows_user()} session — '
        'is that user logged in?'
    )


def _v7_dropbox_process_pids():
    return [row['pid'] for row in _v7_dropbox_process_rows()]


def _v7_dropbox_desktop_exe_candidates():
    """Known Dropbox desktop client locations on Windows."""
    if os.name != 'nt':
        return []
    candidates = []
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    # NSSM dropbox service on this server uses the x86 Client path.
    candidates.extend([
        os.path.join(pf86, 'Dropbox', 'Client', 'Dropbox.exe'),
        os.path.join(pf, 'Dropbox', 'Client', 'Dropbox.exe'),
    ])
    localappdata = os.environ.get('LOCALAPPDATA', '')
    if localappdata:
        candidates.append(os.path.join(localappdata, 'Dropbox', 'client', 'Dropbox.exe'))
        inst = os.path.join(localappdata, 'Dropbox', 'instance')
        if os.path.isdir(inst):
            try:
                for name in os.listdir(inst):
                    p = os.path.join(inst, name, 'Dropbox.exe')
                    if os.path.isfile(p):
                        candidates.append(p)
            except OSError:
                pass
    seen = set()
    out = []
    for p in candidates:
        key = os.path.normcase(os.path.abspath(p))
        if key in seen or not os.path.isfile(p):
            continue
        seen.add(key)
        out.append(p)
    return out


def _v7_dropbox_desktop_process():
    """Return (running: bool, pid: str|None) — prefers Windows service PID."""
    svc = _v7_run_sc_queryex(_V7_DROPBOX_SERVICE_NAME)
    if svc and svc.get('state') == 'RUNNING' and svc.get('pid'):
        return True, svc['pid']
    pids = _v7_dropbox_process_pids()
    if pids:
        return True, pids[0]
    return False, None


def _v7_dropbox_desktop_status():
    """Status of the Dropbox sync app and configured LOCAL_DROPBOX_DIR."""
    root = getattr(Config, 'LOCAL_DROPBOX_DIR', None) or ''
    folder_exists = False
    folder_readable = False
    if root:
        try:
            base = Path(os.path.abspath(str(root))).resolve()
            folder_exists = base.is_dir()
            if folder_exists:
                os.listdir(base)
                folder_readable = True
        except OSError:
            folder_exists = bool(root and os.path.isdir(root))
    svc = _v7_run_sc_queryex(_V7_DROPBOX_SERVICE_NAME)
    service_configured = svc is not None
    service_state = svc['state'] if svc else None
    service_pid = svc['pid'] if svc else None
    process_rows = _v7_dropbox_process_rows()
    user_rows = [row for row in process_rows if row['session_id'] > 0]
    system_rows = [row for row in process_rows if row['session_id'] == 0]
    user_pids = [row['pid'] for row in user_rows]
    system_pids = [row['pid'] for row in system_rows]
    process_pids = [row['pid'] for row in process_rows]
    service_running = service_state == 'RUNNING'
    sync_client_running = bool(user_pids)
    running = sync_client_running or service_running or bool(system_pids)
    pid = (user_pids[0] if user_pids else None) or service_pid or (system_pids[0] if system_pids else None)
    win_user = _v7_dropbox_windows_user()
    exes = _v7_dropbox_desktop_exe_candidates()
    return {
        'platform_supported': os.name == 'nt',
        'running': running,
        'sync_client_running': sync_client_running,
        'pid': pid,
        'windows_user': win_user,
        'service_name': _V7_DROPBOX_SERVICE_NAME,
        'service_configured': service_configured,
        'service_state': service_state,
        'service_pid': service_pid,
        'restart_task_configured': _v7_dropbox_restart_task_configured(),
        'restart_task_name': _V7_DROPBOX_RESTART_TASK,
        'user_start_task_configured': _v7_dropbox_user_start_task_configured(),
        'user_start_task_name': _V7_DROPBOX_USER_START_TASK,
        'process_count': len(process_pids),
        'process_pids': process_pids[:12],
        'user_session_count': len(user_pids),
        'user_session_pids': user_pids[:12],
        'system_session_count': len(system_pids),
        'exe_found': bool(exes),
        'exe_path': exes[0] if exes else '',
        'folder_path': str(root) if root else '',
        'folder_exists': folder_exists,
        'folder_readable': folder_readable,
    }


def _v7_dropbox_kill_orphan_processes():
    """Stop stray Dropbox.exe when the Windows service is not running."""
    flags = _v7_subprocess_no_window_flags()
    try:
        subprocess.run(
            ['taskkill', '/IM', 'Dropbox.exe', '/F'],
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=flags,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        current_app.logger.warning('taskkill Dropbox.exe failed: %s', e)
        return False
    return True


def _v7_dropbox_wait_for_running(timeout_sec=25):
    """Poll until Dropbox sync client runs in a user session (tray app), not only SYSTEM."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        st = _v7_dropbox_desktop_status()
        if st.get('sync_client_running'):
            return st
        time.sleep(1.5)
    return _v7_dropbox_desktop_status()


def _v7_dropbox_service_start_or_restart(*, restart: bool):
    """Use Windows service `dropbox` (NSSM) with stop-wait-start (no nssm restart races)."""
    status = _v7_dropbox_desktop_status()
    if not status.get('service_configured'):
        return None
    service_state = status.get('service_state')

    def _finish(action, message):
        new_status = _v7_dropbox_wait_for_running(timeout_sec=50)
        if not new_status.get('sync_client_running'):
            hint = (
                f' Dropbox sync client did not start in {_v7_dropbox_windows_user()} session — '
                'user must be logged in. Re-run Setup-DropboxRestartTask.ps1 as Admin.'
            )
            if new_status.get('system_session_count', 0) and not new_status.get('user_session_count'):
                hint = (
                    f' Only SYSTEM-session Dropbox is running (not {_v7_dropbox_windows_user()} tray sync).'
                    + hint
                )
            return {
                'ok': False,
                'error': 'Dropbox sync client is not running in the user session.' + hint,
                'status': new_status,
            }
        return {
            'ok': True,
            'action': action,
            'message': message,
            'status': new_status,
        }

    def _soft_ok(message):
        """Restart failed but Dropbox still appears up — reload folders anyway."""
        return {
            'ok': True,
            'action': 'reload_only',
            'message': message,
            'status': _v7_dropbox_desktop_status(),
        }

    setup_hint = (
        ' Re-run as Admin: cd C:\\FDC_FLASK_APP\\auto_run; .\\Setup-DropboxRestartTask.ps1'
    )

    if _v7_dropbox_restart_task_configured():
        err = _v7_dropbox_trigger_restart_task()
        if not err:
            action = 'restarted' if restart and status.get('running') else 'started'
            msg = {
                'started': f'Started Dropbox sync in {_v7_dropbox_windows_user()} Windows session.',
                'restarted': f'Restarted Dropbox sync in {_v7_dropbox_windows_user()} session — check tray icon.',
            }.get(action, 'Dropbox restart task triggered.')
            result = _finish(action, msg)
            if result.get('ok'):
                return result
            if _v7_dropbox_desktop_status().get('sync_client_running'):
                return _soft_ok(
                    'Restart task ran; Dropbox sync client still running — folder list refreshed.'
                )
        elif not _v7_dropbox_access_denied(err):
            current_app.logger.warning('dropbox restart task failed: %s', err)

    if not restart and service_state == 'RUNNING' and status.get('process_count', 0) > 0:
        return _finish('already_running', 'Dropbox Windows service is already running.')

    try:
        ok, err = _v7_dropbox_controlled_service_restart()
        if ok:
            action = 'restarted' if restart and status.get('running') else 'started'
            msg = {
                'started': f'Started Dropbox sync in {_v7_dropbox_windows_user()} Windows session.',
                'restarted': f'Restarted Dropbox sync in {_v7_dropbox_windows_user()} session — check tray icon.',
            }.get(action, 'Dropbox service updated.')
            return _finish(action, msg)

        if err and _v7_dropbox_desktop_status().get('sync_client_running'):
            return _soft_ok(
                'Could not restart cleanly. Dropbox sync client still running — folder list refreshed.'
            )

        if err and _v7_dropbox_access_denied(err) and not _v7_dropbox_restart_task_configured():
            err = err + setup_hint
        elif err and not _v7_dropbox_restart_task_configured():
            err = err + setup_hint

        current_app.logger.warning('dropbox service control failed: %s', err)
        return {
            'ok': False,
            'error': err or 'Could not restart Dropbox Windows service.',
            'status': _v7_dropbox_desktop_status(),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        current_app.logger.error('dropbox service control exception: %s', e)
        if _v7_dropbox_desktop_status().get('sync_client_running'):
            return _soft_ok(
                f'Restart error ({e}) but Dropbox sync client still running — folder list refreshed.'
            )
        return {
            'ok': False,
            'error': f'Could not control Dropbox service: {e}' + setup_hint,
            'status': _v7_dropbox_desktop_status(),
        }


def _v7_dropbox_desktop_start_or_restart(*, restart: bool):
    """
    Start/restart Dropbox via Windows service (NSSM). Falls back to launching exe.
    Returns (payload dict, http_status).
    """
    status = _v7_dropbox_desktop_status()
    if os.name != 'nt':
        return {
            'ok': False,
            'error': 'Dropbox desktop control is only available on Windows servers.',
            'status': status,
        }, 503

    if status.get('service_configured'):
        if status.get('running') and not restart:
            return {
                'ok': True,
                'action': 'already_running',
                'message': 'Dropbox Windows service is already running.',
                'status': status,
            }, 200
        svc_result = _v7_dropbox_service_start_or_restart(restart=restart)
        if svc_result is not None:
            code = 200 if svc_result.get('ok') else 500
            return svc_result, code

    exes = _v7_dropbox_desktop_exe_candidates()
    if not exes:
        return {
            'ok': False,
            'error': 'Dropbox.exe was not found and no dropbox Windows service is configured.',
            'status': status,
        }, 503
    exe = exes[0]
    flags = _v7_subprocess_no_window_flags()
    action = 'started'
    if status['running']:
        if not restart:
            return {
                'ok': True,
                'action': 'already_running',
                'message': 'Dropbox sync app is already running.',
                'status': _v7_dropbox_desktop_status(),
            }, 200
        action = 'restarted'
        _v7_dropbox_kill_orphan_processes()
        time.sleep(3)
    try:
        subprocess.Popen(
            ['cmd', '/c', 'start', '', exe],
            creationflags=flags,
            close_fds=True,
        )
    except OSError as e:
        current_app.logger.error('Failed to launch Dropbox.exe: %s', e)
        return {
            'ok': False,
            'error': f'Could not launch Dropbox: {e}',
            'status': _v7_dropbox_desktop_status(),
        }, 500
    new_status = _v7_dropbox_wait_for_running()
    if not new_status.get('sync_client_running'):
        return {
            'ok': False,
            'error': (
                f'Tried to launch Dropbox.exe but no sync client appeared in '
                f'{_v7_dropbox_windows_user()} session.'
            ),
            'status': new_status,
        }, 500
    msg = {
        'started': 'Started Dropbox sync app.',
        'restarted': 'Restarted Dropbox sync app — it should fetch new files shortly.',
    }.get(action, 'Dropbox sync app updated.')
    return {
        'ok': True,
        'action': action,
        'message': msg,
        'status': new_status,
    }, 200


def _slug_for_job_id(display_label: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '_', (display_label or '').strip())
    s = re.sub(r'_+', '_', s).strip('_')
    return s[:72] if s else 'snapshot'


def _listcreator_safe_job_file_id(snapshot_label_slug: str) -> str:
    """Filesystem + URL-safe id: slug + short suffix (collision-resistant)."""
    base = snapshot_label_slug.strip('_') or 'snapshot'
    suf = uuid.uuid4().hex[:12]
    full = f'{base}_{suf}'
    if len(full) > 118:
        full = base[:80].rstrip('_') + '_' + suf
    return full


def _listcreator_job_id_available(app, slug, job_id):
    jp = os.path.join(_listcreator_html_base(app, slug), 'jobs', f'{job_id}.json')
    hp = _listcreator_html_snap_path(app, slug, job_id)
    return not os.path.isfile(jp) and not os.path.isfile(hp)


def _allocate_listcreator_job_id(app, slug, sanitized_label_slug: str) -> str:
    for _ in range(48):
        jid = _listcreator_safe_job_file_id(sanitized_label_slug)
        if _listcreator_job_id_available(app, slug, jid):
            return jid
    return uuid.uuid4().hex


def _valid_listcreator_job_id(job_id):
    if not job_id or len(job_id) > 120:
        return False
    if re.match(r'^[a-f0-9]{32}$', job_id):
        return True
    return bool(re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]{6,117}$', str(job_id)))


def _listcreator_html_user_slug(username):
    s = secure_filename((username or '').strip()) or 'user'
    return s[:120]


def _listcreator_requested_storage_slug(current_user):
    """Disk folder under listcreator_html for this request (?owner_slug= for shared snapshots)."""
    raw = (request.args.get('owner_slug') or request.args.get('owner') or '').strip()
    if not raw:
        return _listcreator_html_user_slug(current_user)
    cleaned = secure_filename(raw)[:120]
    return cleaned if cleaned else _listcreator_html_user_slug(current_user)


def _listcreator_html_root(app):
    return os.path.join(app.instance_path, 'listcreator_html')


def _listcreator_html_storage_slugs_with_jobs(app):
    root = _listcreator_html_root(app)
    if not os.path.isdir(root):
        return []
    slugs = []
    for name in os.listdir(root):
        jp = os.path.join(root, name, 'jobs')
        if os.path.isdir(jp):
            slugs.append(name)
    return sorted(slugs)


def _listcreator_html_merge_all_user_jobs(app):
    merged = []
    for slug in _listcreator_html_storage_slugs_with_jobs(app):
        for j in _listcreator_html_list_all_jobs(app, slug):
            row = dict(j)
            row['storage_slug'] = slug
            merged.append(row)
    merged.sort(key=lambda x: _parse_job_created_at(x.get('created_at')), reverse=True)
    return merged


def _listcreator_cached_html_public_url(rid, storage_slug):
    """Relative URL to open a snapshot; always includes owner_slug for correct on-disk folder."""
    base = url_for('v7_listcreator.listcreator_cached_html', rid=rid)
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}owner_slug={quote(storage_slug, safe="")}'


def _listcreator_html_job_wait_public_url(rid, storage_slug):
    base = url_for('v7_listcreator.html_job_wait', rid=rid)
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}owner_slug={quote(storage_slug, safe="")}'


def _listcreator_html_base(app, slug):
    base = os.path.join(app.instance_path, 'listcreator_html', slug)
    os.makedirs(os.path.join(base, 'jobs'), exist_ok=True)
    os.makedirs(os.path.join(base, 'html'), exist_ok=True)
    return base


def _listcreator_html_job_manifest_path(app, slug, rid):
    return os.path.join(_listcreator_html_base(app, slug), 'jobs', f'{rid}.json')


def _listcreator_html_snap_path(app, slug, rid):
    return os.path.join(_listcreator_html_base(app, slug), 'html', f'{rid}.html')


def _listcreator_html_prune_old(app, slug):
    """Remove HTML snapshots and manifests older than LISTCREATOR_HTML_CACHE_DAYS."""
    base = _listcreator_html_base(app, slug)
    jobs_dir = os.path.join(base, 'jobs')
    html_dir = os.path.join(base, 'html')
    cutoff = datetime.datetime.utcnow() - timedelta(days=LISTCREATOR_HTML_CACHE_DAYS)
    for jd in (jobs_dir,):
        if not os.path.isdir(jd):
            continue
        for name in os.listdir(jd):
            if not name.endswith('.json'):
                continue
            path = os.path.join(jd, name)
            try:
                with open(path, encoding='utf-8') as f:
                    meta = json.load(f)
                created_raw = meta.get('created_at', '')
                try:
                    created = datetime.datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
                    if created.tzinfo:
                        created = created.replace(tzinfo=None)
                except ValueError:
                    created = datetime.datetime.utcfromtimestamp(os.path.getmtime(path))
                rid = meta.get('id') or name.replace('.json', '')
                if created >= cutoff.replace(tzinfo=None):
                    continue
                hn = os.path.join(html_dir, f'{rid}.html')
                if os.path.isfile(hn):
                    try:
                        os.remove(hn)
                    except OSError:
                        pass
                os.remove(path)
            except (OSError, json.JSONDecodeError):
                continue


def _listcreator_html_load_job(app, slug, rid):
    path = _listcreator_html_job_manifest_path(app, slug, rid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _parse_job_created_at(iso_raw):
    if not iso_raw:
        return datetime.datetime.utcfromtimestamp(0)
    s = iso_raw.strip().replace('Z', '+00:00')
    try:
        dt = datetime.datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return datetime.datetime.utcfromtimestamp(0)


def _listcreator_html_list_all_jobs(app, slug):
    """All HTML snapshot manifests for user, newest first."""
    jobs_dir = os.path.join(_listcreator_html_base(app, slug), 'jobs')
    if not os.path.isdir(jobs_dir):
        return []
    out = []
    for name in os.listdir(jobs_dir):
        if not name.endswith('.json'):
            continue
        path = os.path.join(jobs_dir, name)
        try:
            with open(path, encoding='utf-8') as f:
                meta = json.load(f)
            rid = meta.get('id') or name.replace('.json', '')
            li_full = meta.get('list_input') or ''
            out.append({
                'id': rid,
                'status': meta.get('status'),
                'created_at': meta.get('created_at'),
                'created_at_pt_display': meta.get('created_at_pt_display'),
                'snapshot_name': meta.get('snapshot_name'),
                'snapshot_generated_at_pt': meta.get('snapshot_generated_at_pt'),
                'snapshot_generated_at_iso': meta.get('snapshot_generated_at_iso'),
                'list_input': li_full,
                'resolved_styles_full': meta.get('resolved_styles_full') or '',
                'error': meta.get('error'),
                'job_username': (meta.get('username') or '').strip(),
            })
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda j: _parse_job_created_at(j.get('created_at')), reverse=True)
    return out


def _listcreator_html_fail_job(app, slug, rid, message):
    with _listcreator_html_lock:
        meta = _listcreator_html_load_job(app, slug, rid)
        if not meta:
            return
        meta['status'] = 'failed'
        meta['error'] = message
        p = _listcreator_html_job_manifest_path(app, slug, rid)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=0)
        os.replace(tmp, p)
        _listcreator_html_prune_old(app, slug)


def _listcreator_html_delete_job_disk(app, slug, rid, meta=None):
    """Remove cached HTML artifact(s) and the job manifest for this snapshot id."""
    if meta is None:
        meta = _listcreator_html_load_job(app, slug, rid) or {}
    base = _listcreator_html_base(app, slug)
    html_dir = os.path.join(base, 'html')
    jp = _listcreator_html_job_manifest_path(app, slug, rid)
    candidates = []
    if meta.get('html_file'):
        candidates.append(os.path.join(html_dir, meta['html_file']))
    candidates.append(_listcreator_html_snap_path(app, slug, rid))
    seen = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    if os.path.isfile(jp):
        try:
            os.remove(jp)
        except OSError:
            return False
    return True


def _workbook_user_slug(username):
    s = secure_filename((username or '').strip()) or 'user'
    return s[:120]


def _workbook_user_dir(app, slug):
    base = os.path.join(app.instance_path, 'workbook_exports', slug)
    os.makedirs(base, exist_ok=True)
    return base


def _workbook_manifest_path(app, slug):
    return os.path.join(_workbook_user_dir(app, slug), 'manifest.json')


def _workbook_load_manifest(app, slug):
    path = _workbook_manifest_path(app, slug)
    if not os.path.isfile(path):
        return {'edition_counter': 0, 'jobs': []}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {'edition_counter': 0, 'jobs': []}


def _workbook_save_manifest(app, slug, data):
    path = _workbook_manifest_path(app, slug)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=0)
    os.replace(tmp, path)


def _parse_workbook_job_created_at(iso_s):
    if not iso_s:
        return None
    s = str(iso_s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        return None


def _workbook_sweep_stale_running_jobs(app, slug):
    """Fail running jobs older than WORKBOOK_EXPORT_STALE_AFTER_SEC (hang / zombie UI)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with _workbook_manifest_lock:
        manifest = _workbook_load_manifest(app, slug)
        jobs = manifest.get("jobs") or []
        changed = False
        for j in jobs:
            if j.get("status") != "running":
                continue
            created = _parse_workbook_job_created_at(j.get("created_at"))
            if created is None:
                continue
            if (now - created).total_seconds() <= WORKBOOK_EXPORT_STALE_AFTER_SEC:
                continue
            j["status"] = "failed"
            j["error"] = (
                "Export did not finish within the time limit. The job may have hung (database or images), "
                "or the server was busy. Try again with fewer styles, uncheck Full catalog, or check server logs."
            )
            changed = True
        if changed:
            _workbook_save_manifest(app, slug, manifest)


def workbook_exports_fail_orphaned_running(app):
    """On process start: mark all 'running' jobs failed (background threads never survive restart)."""
    root = os.path.join(app.instance_path, "workbook_exports")
    if not os.path.isdir(root):
        return
    msg = (
        "Server restarted before this export finished. Please queue the workbook again."
    )
    for slug in os.listdir(root):
        user_dir = os.path.join(root, slug)
        if not os.path.isdir(user_dir):
            continue
        with _workbook_manifest_lock:
            manifest = _workbook_load_manifest(app, slug)
            jobs = manifest.get("jobs") or []
            changed = False
            for j in jobs:
                if j.get("status") != "running":
                    continue
                j["status"] = "failed"
                j["error"] = msg
                changed = True
            if changed:
                _workbook_save_manifest(app, slug, manifest)
                app.logger.info(
                    "workbook export: marked interrupted running job(s) failed slug=%s",
                    slug,
                )


def _workbook_delete_disk_file(app, slug, job):
    fn = job.get('file')
    if not fn:
        return
    fp = os.path.join(_workbook_user_dir(app, slug), fn)
    if os.path.isfile(fp):
        try:
            os.remove(fp)
        except OSError:
            pass


def _workbook_fail_job(app, slug, job_id, message):
    with _workbook_manifest_lock:
        manifest = _workbook_load_manifest(app, slug)
        for j in manifest.get('jobs', []):
            if j.get('id') == job_id:
                j['status'] = 'failed'
                j['error'] = message
                break
        _workbook_save_manifest(app, slug, manifest)


def _workbook_jobs_public(jobs):
    out = []
    for j in jobs or []:
        out.append({
            'id': j.get('id'),
            'status': j.get('status'),
            'edition': j.get('edition'),
            'created_at': j.get('created_at'),
            'download_name': j.get('download_name'),
            'error': j.get('error'),
        })
    return out


def _workbook_export_worker(app, slug, job_id, payload, current_user):
    with app.app_context():
        try:
            current_app.logger.info(
                "workbook export worker started job_id=%s user=%s",
                job_id,
                current_user,
            )
            download_name, bio = _build_workbook_from_request_data(payload, current_user)
            user_dir = _workbook_user_dir(app, slug)
            disk_name = f'{job_id}.xlsx'
            dest = os.path.join(user_dir, disk_name)
            with open(dest, 'wb') as f:
                f.write(bio.getvalue())
            edition_for_slack = None
            with _workbook_manifest_lock:
                manifest = _workbook_load_manifest(app, slug)
                for j in manifest.get('jobs', []):
                    if j.get('id') == job_id:
                        j['status'] = 'ready'
                        j['download_name'] = download_name
                        j['file'] = disk_name
                        j['error'] = None
                        edition_for_slack = j.get('edition')
                        break
                _workbook_save_manifest(app, slug, manifest)
            current_app.logger.info(
                'workbook export ready job_id=%s user=%s',
                job_id,
                current_user,
            )
            try:
                from py.utilities.slack_notify import notify_v7_list_creator_optional, public_origin

                origin = public_origin()
                dl = f'{origin}/export_workbook/download/{job_id}' if origin else f'/export_workbook/download/{job_id}'
                notify_v7_list_creator_optional(
                    '[V7 List Creator] "All styles with any stock" workbook export ready\n'
                    f'User: {current_user}\n'
                    f'Edition: #{edition_for_slack if edition_for_slack is not None else "?"}\n'
                    f'File: {download_name}\n'
                    f'Download (login required): {dl}\n'
                )
            except Exception:
                current_app.logger.debug('slack v7 workbook notify skipped', exc_info=True)
        except ValueError as e:
            _workbook_fail_job(app, slug, job_id, str(e))
        except Exception:
            current_app.logger.exception('workbook export failed job_id=%s', job_id)
            _workbook_fail_job(app, slug, job_id, 'Internal Server Error')


def _build_workbook_from_request_data(request_data, current_user):
    """
    Build the list creator workbook. Returns (download_filename, BytesIO).
    Raises ValueError for recoverable input/data errors.
    """
    export_data = request_data.get('exportData') or []
    list_input_raw = (request_data.get('list_input') or '').strip()
    removed_columns = request_data.get('removedColumns', {})
    want_full_catalog = request_data.get('includeFullCatalog', True)

    if not export_data:
        export_data = build_export_data_from_list_input(list_input_raw, current_user)

    if not export_data:
        raise ValueError(
            'No inventory rows found (nothing with available or ocean qty > 0 in OMS).'
        )

    styles = []
    for item in export_data:
        st = (item.get('style') or '').strip()
        if st:
            styles.append(st)
    combined_list = list_from_origin(','.join(styles), current_user)
    raw_rows = []
    for chunk in _chunk_list(combined_list, 120):
        raw_rows.extend(merge_inventory_rows_cross_database(get_sql_data(chunk)))
    raw_rows = merge_inventory_rows_cross_database(raw_rows)
    if not raw_rows:
        raise ValueError('No inventory data')

    df = pd.DataFrame(raw_rows)
    if len(df.columns) == 17:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'sales_cost', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt']
    elif len(df.columns) == 18:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'sales_cost', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt', 'in_oms_status']
    else:
        current_app.logger.error(f'export_workbook unexpected columns: {len(df.columns)}')
        raise RuntimeError('Unexpected data shape')

    df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce').fillna(0).astype(int)
    df['ocean'] = pd.to_numeric(df['ocean'], errors='coerce').fillna(0).astype(int)
    df['selling_price'] = pd.to_numeric(df['selling_price'], errors='coerce').fillna(0)
    df['sales_cost'] = pd.to_numeric(df['sales_cost'], errors='coerce').fillna(0)

    avail = df[(df['quantity'] > 0) | (df['ocean'] > 0)]

    workbook = Workbook()
    ws1 = workbook.active
    ws1.title = "dbinfo"
    ws1.append(["SKU", "Style", "Price", "Cost", "Stock", "Ocean"])
    if not avail.empty:
        dbinfo_agg = avail.groupby('SKU', as_index=False).agg(
            SKU_no_color=('SKU_no_color', 'first'),
            selling_price=('selling_price', 'first'),
            sales_cost=('sales_cost', 'first'),
            stock=('quantity', 'sum'),
            ocean=('ocean', 'sum'),
        )
        for _, row in dbinfo_agg.iterrows():
            ws1.append([
                row['SKU'],
                row['SKU_no_color'],
                row['selling_price'],
                row['sales_cost'],
                int(row['stock']),
                int(row['ocean']),
            ])
    else:
        ws1.append(["—", "No SKUs with available or ocean qty > 0", "", "", "", ""])
    for col_letter, w in [('A', 18), ('B', 14), ('C', 12), ('D', 12), ('E', 10), ('F', 10)]:
        ws1.column_dimensions[col_letter].width = w

    ws2 = workbook.create_sheet("style_totals")
    ws2.append(["Style", "Total quantity"])
    if not avail.empty:
        agg = avail.groupby('SKU_no_color', as_index=False).agg(
            total_qty=('quantity', 'sum'),
            ocean_sum=('ocean', 'sum')
        )
        agg['combined'] = agg['total_qty'] + agg['ocean_sum']
        for _, row in agg.iterrows():
            ws2.append([row['SKU_no_color'], int(row['combined'])])
    else:
        ws2.append(["—", "No qualifying styles"])
    ws2.column_dimensions['A'].width = 18
    ws2.column_dimensions['B'].width = 18

    eligible = set()
    if not avail.empty:
        for nc in avail['SKU_no_color'].dropna():
            eligible.add(normalize_style_for_grouping(str(nc).strip()))

    filtered_export = []
    for item in export_data:
        st = (item.get('style') or '').strip()
        if normalize_style_for_grouping(st) not in eligible:
            continue
        rows = item.get('rows') or []
        new_rows = []
        for r in rows:
            q = int(float(r.get('quantity') or 0))
            o = int(float(r.get('ocean') or 0))
            if q > 0 or o > 0:
                new_rows.append(r)
        if not new_rows:
            continue
        filtered_export.append({**item, 'rows': new_rows})

    if filtered_export:
        ws3 = workbook.create_sheet("Export_600")
        # Same visual sizing as Export List (export_list): resize source width 800px, embed 200×150 in column A merges.
        _populate_export_list_sheet(ws3, filtered_export, 800, removed_columns, embed_width=200, embed_height=150)

    n_export_styles = len(export_data)
    total_card_rows = sum(len(item.get('rows') or []) for item in export_data)
    too_heavy_for_full_catalog = (
        n_export_styles > WORKBOOK_FULL_CATALOG_MAX_STYLES
        or total_card_rows > WORKBOOK_FULL_CATALOG_MAX_ROWS
    )
    include_full_catalog = bool(want_full_catalog) and not too_heavy_for_full_catalog

    if want_full_catalog and too_heavy_for_full_catalog:
        current_app.logger.info(
            "export_workbook: omitting Full_catalog (%s styles, %s rows; limits %s/%s)",
            n_export_styles,
            total_card_rows,
            WORKBOOK_FULL_CATALOG_MAX_STYLES,
            WORKBOOK_FULL_CATALOG_MAX_ROWS,
        )

    if include_full_catalog:
        ws_fc = workbook.create_sheet("Full_catalog")
        _populate_export_list_sheet(ws_fc, export_data, 1800, removed_columns, embed_width=1800, embed_height=1350)
    elif want_full_catalog and too_heavy_for_full_catalog:
        ws_note = workbook.create_sheet("Notes")
        ws_note.append(["Full_catalog sheet omitted (timeout guard)"])
        note_body = (
            f"This export has {n_export_styles} styles and {total_card_rows} inventory rows. "
            f"The Full_catalog sheet (1800px images) is skipped when there are more than "
            f"{WORKBOOK_FULL_CATALOG_MAX_STYLES} styles or {WORKBOOK_FULL_CATALOG_MAX_ROWS} rows "
            "so the server can finish within typical gateway timeouts (Cloudflare 524). "
            "Sheets dbinfo, style_totals, and Export_600 are still included. "
            'Uncheck "Include Full Catalog" for a faster job, use a shorter style list for a full workbook, '
            "or ask IT to raise origin/proxy time limits."
        )
        ws_note.append([note_body])
        ws_note.column_dimensions["A"].width = 100

    formatted_date = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"list_creator_workbook_{formatted_date}.xlsx"
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return filename, output


def list_from_dropbox(list_input, current_user):
    """Processes a dropbox path and returns list of styles and image mappings."""
    match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        current_app.logger.info(f"***{current_user}*** processing regular input: @@@__{relative_path}")
        
        if not os.path.exists(dropbox_path):
            return {"error": "Directory not found. Please check your URL."}
        
        file_list = set()
        sku_to_image = {}
        
        for filename in os.listdir(dropbox_path):
            if filename.lower().endswith(('.jpg', '.png')):
                # Standard (L1343) or extended (TBDBN3602) prefix + 4 digits
                base_match = re.match(r'([A-Za-z]{1,2}\d{4}(?:-\d+)?)', filename)
                if not base_match:
                    base_match = re.match(r'([A-Za-z]{3,}\d{4}(?:-\d+)?)', filename)
                if base_match:
                    base_sku = base_match.group(1).upper()
                    # Check if it's a size suffix pattern after the base SKU
                    remaining = filename[len(base_sku):]
                    size_suffix = re.search(r'[-]?[smlSML](?:\.|$)', remaining)
                    if size_suffix:
                        # Handle size suffix cases (both with and without dash)
                        dash_style = f"{base_sku}-{size_suffix.group().strip('.-')}"
                        no_dash_style = f"{base_sku}{size_suffix.group().strip('.-')}"
                        file_list.add(dash_style)
                        file_list.add(no_dash_style)
                        sku_to_image[dash_style] = os.path.join(dropbox_path, filename)
                        sku_to_image[no_dash_style] = os.path.join(dropbox_path, filename)
                    else:
                        # For any other variations, just use the base SKU
                        file_list.add(base_sku)
                        sku_to_image[base_sku] = os.path.join(dropbox_path, filename)
        
        if not file_list:
            return {"error": "No images found in the specified directory. Please check your URL."}
        
        file_list = sorted(list(file_list))
        clean_list_for_memo = str(file_list).replace("'","")
        current_app.logger.info(f"list_from_dropbox called with input: {clean_list_for_memo}")
        
        return file_list, sku_to_image

    return list_input, {}


def list_from_origin(list_input, current_user):
    # Convert input string to list of SKUs
    current_app.logger.info(f"***{current_user}*** processing regular input:@@@__ {list_input}")

    # Split by commas and clean up each SKU
    raw_styles = [sku.strip().upper() for sku in list_input.split(',') if sku.strip()]
    style_list = []
    
    for style in raw_styles:
        # Check if the style has a suffix (like M, L, etc.)
        base_match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
        
        if base_match and base_match.group(3):  # If it has a suffix
            base_style = base_match.group(1)    # The base part (e.g., "L1343")
            has_dash = base_match.group(2) == '-'  # Whether it has a dash
            suffix = base_match.group(3)        # The suffix part (e.g., "M")
            
            # For styles with suffix (L1343M or L1343-M), add both formats
            if has_dash:
                # If input had dash (L1343-M), use this as canonical form
                style_list.append(style)
                # Also add no-dash version for search
                no_dash_style = base_style + suffix
                style_list.append(no_dash_style)
            else:
                # If input had no dash (L1343M), use this as canonical form
                style_list.append(style)
                # Also add dash version for search
                dash_style = base_style + '-' + suffix
                style_list.append(dash_style)
        else:
            # For base styles without suffix (L1343), only add the exact style
            style_list.append(style)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_style_list = [x for x in style_list if not (x in seen or seen.add(x))]
    return unique_style_list

def normalize_style(style):
    """Remove dash and convert to uppercase for comparison"""
    return re.sub(r'-', '', style).upper()


def _phase1_resolve_list_input(list_input, current_user):
    """Parse Dropbox path or SKU list → style_list and thumbnails map; duplicates/missing-styles metadata."""
    error_message = None
    dropbox_images = {}
    missing_styles = []
    duplicate_items = []
    clean_list_for_memo = None
    original_input_styles = [s.strip().upper() for s in (list_input or '').split(',') if s.strip()]

    if 'dropbox' in (list_input or '').lower():
        match = re.search(r'dropbox\\(.+)', list_input, re.IGNORECASE)
        if match:
            relative_path = match.group(1)
            full_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
            if os.path.exists(full_path):
                base_sku_to_files = {}
                for filename in os.listdir(full_path):
                    if filename.lower().endswith(('.jpg', '.png')):
                        base_match = re.match(r'([A-Za-z]{1,2}\d{4})', filename)
                        if base_match:
                            base_sku = base_match.group(1).upper()
                            base_sku_to_files.setdefault(base_sku, []).append(filename)
                for base_sku, files in base_sku_to_files.items():
                    if len(files) > 1:
                        duplicate_items.append({'base_sku': base_sku, 'files': files})

        result = list_from_dropbox(list_input, current_user)
        if isinstance(result, dict) and 'error' in result:
            return {'error_message': result['error'], 'style_list': [], 'dropbox_images': {}, 'duplicate_items': [], 'missing_styles': [], 'clean_list_for_memo': None}
        style_list, dropbox_images = result
        current_app.logger.info(f"***{current_user}*** processing dropbox input: {list_input}")
        clean_list_for_memo = ','.join(style_list).replace("'", "")
    else:
        style_list = list_from_origin(list_input, current_user)
        normalized_style_list = [re.sub(r'-', '', s).upper() for s in style_list]
        for orig_style in original_input_styles:
            normalized_orig = re.sub(r'-', '', orig_style).upper()
            if normalized_orig not in normalized_style_list:
                missing_styles.append(orig_style)

    return {
        'error_message': error_message,
        'style_list': style_list,
        'dropbox_images': dropbox_images,
        'duplicate_items': duplicate_items,
        'missing_styles': missing_styles,
        'clean_list_for_memo': clean_list_for_memo,
        'original_input_styles': original_input_styles,
    }


@v7_listcreator_bp.route('/v7_listcreator', methods=['GET', 'POST'])
@v7_listcreator_bp.route("/v7_listcreator/<string:user_input>", methods=["GET"])
@login_required
def index(user_input=None):
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))

    current_user = session['username']
    error_message = None
    dropbox_images = {}
    missing_descriptions = []
    image_urls = {}
    missing_styles = []
    duplicate_styles = []
    duplicate_items = []
    clean_list_for_memo = None
    
    bag_description_options = fetch_description_options()

    # Handle GET request with query parameter — prefill form only; user must add snapshot and POST
    if request.method == 'GET' and (
        request.args.get('list_input')
        or request.args.get('description')
        or request.args.getlist('description')
    ):
        list_input = (request.args.get('list_input') or '').strip()
        filter_descriptions = request.args.getlist('description')
        if not filter_descriptions:
            filter_descriptions = _normalize_description_list(request.args.get('description'))
        filter_stock = request.args.get('stock_filter') or None
        filter_color = request.args.get('color_filter') or None
        if not list_input and filter_descriptions:
            codes = fetch_style_codes_for_filters(filter_descriptions, filter_stock, filter_color)
            list_input = ','.join(codes)
        session['original_input'] = list_input
        pst = _pst_now_parts()
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            list_input=list_input,
            snapshot_name=(request.args.get('snapshot_name') or '').strip(),
            filter_descriptions=filter_descriptions,
            filter_stock=filter_stock or '',
            filter_color=filter_color or '',
            bag_description_options=bag_description_options,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message=None,
        )

    # Handle POST request
    if request.method == 'POST':
        list_input = (request.form.get('list_input') or '').strip()
        snapshot_name = (request.form.get('snapshot_name') or '').strip()
        session['original_input'] = list_input
        if not list_input:
            pst = _pst_now_parts()
            return render_template(
                'V7_Listcreator.html',
                current_user=current_user,
                list_input='',
                snapshot_name=snapshot_name,
                bag_description_options=bag_description_options,
                filter_descriptions=[],
                filter_stock='',
                filter_color='',
                snapshot_time_hint=pst['created_at_pt_display'],
                snapshot_time_hint_iso=pst['created_at_iso'],
                error_message='Enter a SKU list or Dropbox path before running.',
            )
        return process_list_input(list_input, current_user, snapshot_name=snapshot_name, enforce_snapshot_label=True)

    pst = _pst_now_parts()
    return render_template(
        'V7_Listcreator.html',
        current_user=current_user,
        error_message=error_message,
        bag_description_options=bag_description_options,
        filter_descriptions=[],
        filter_stock='',
        filter_color='',
        snapshot_time_hint=pst['created_at_pt_display'],
        snapshot_time_hint_iso=pst['created_at_iso'],
    )


def _quick_filter_request_payload():
    """Read descriptions/stock/color from JSON POST, form POST, or GET query."""
    payload = {}
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        if not payload:
            payload = request.form.to_dict(flat=False)
    descriptions = payload.get('descriptions') if isinstance(payload, dict) else None
    if descriptions is None:
        descriptions = payload.get('description') if isinstance(payload, dict) else None
    if descriptions is None:
        descriptions = request.args.getlist('description')
    if isinstance(descriptions, str):
        descriptions = _normalize_description_list(descriptions)
    elif not descriptions:
        descriptions = _normalize_description_list(request.args.get('description'))
    else:
        descriptions = _normalize_description_list(descriptions)

    stock_filter = None
    color_filter = None
    if isinstance(payload, dict):
        raw_stock = payload.get('stock_filter')
        if isinstance(raw_stock, list):
            raw_stock = raw_stock[0] if raw_stock else None
        stock_filter = (raw_stock or '').strip() or None
        raw_color = payload.get('color_filter')
        if isinstance(raw_color, list):
            raw_color = raw_color[0] if raw_color else None
        color_filter = (raw_color or '').strip() or None
    if stock_filter is None:
        stock_filter = request.args.get('stock_filter') or None
    if color_filter is None:
        color_filter = request.args.get('color_filter') or None
    return descriptions, stock_filter, color_filter


@v7_listcreator_bp.route('/v7_listcreator/quick_filter_styles', methods=['GET', 'POST'])
@login_required
def quick_filter_styles():
    if 'username' not in session:
        return jsonify({'success': False, 'message': 'Login required'}), 401
    descriptions, stock_filter, color_filter = _quick_filter_request_payload()
    if not descriptions:
        return jsonify({'success': False, 'message': 'Choose at least one description'}), 400
    try:
        styles = fetch_style_codes_for_filters(descriptions, stock_filter, color_filter)
    except Exception:
        current_app.logger.exception('quick_filter_styles failed')
        return jsonify({
            'success': False,
            'message': 'Could not load styles. Try fewer descriptions or a narrower stock filter.',
        }), 500
    return jsonify({
        'success': True,
        'styles': styles,
        'count': len(styles),
        'list_input': ','.join(styles),
        'descriptions': descriptions,
    })


def process_list_input(list_input, current_user, snapshot_name='', enforce_snapshot_label=False):
    """All lists use the snapshot pipeline: descriptions gate when needed, then background HTML snapshot."""
    pst = _pst_now_parts()
    if not list_input or not str(list_input).strip():
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message=None,
        )

    expanded_input, expand_err = _expand_listcreator_mixed_input(list_input)
    if expand_err:
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            list_input=list_input,
            snapshot_name=snapshot_name,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message=expand_err,
        )
    list_input = expanded_input

    snap_lbl = _sanitize_snapshot_label(snapshot_name)

    phase = _phase1_resolve_list_input(list_input, current_user)
    if phase.get('error_message'):
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            list_input=list_input,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message=phase['error_message'],
            snapshot_name=snapshot_name if snapshot_name is not None else '',
        )

    style_list = phase['style_list']
    if not style_list:
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message='No styles resolved from input.',
            list_input=list_input,
            snapshot_name=snapshot_name if snapshot_name is not None else '',
        )

    if enforce_snapshot_label and len(snap_lbl) < 2:
        return render_template(
            'V7_Listcreator.html',
            current_user=current_user,
            list_input=list_input,
            snapshot_name=snapshot_name,
            snapshot_time_hint=pst['created_at_pt_display'],
            snapshot_time_hint_iso=pst['created_at_iso'],
            error_message='Enter a snapshot name (at least 2 characters). It labels your saved HTML snapshot; times use Pacific (Los Angeles).',
        )

    bulk = get_descriptions_bulk(style_list)
    descriptions = {s: _description_lookup_from_bulk(s, bulk) for s in style_list}
    seen_m = set()
    missing_descriptions = []
    for s in style_list:
        raw = descriptions.get(s, '') or ''
        norm = raw.strip().upper()
        if norm in ('', 'N/A', 'NA') and s not in seen_m:
            seen_m.add(s)
            missing_descriptions.append(s)

    if missing_descriptions:
        image_urls = get_images_for_selection(missing_descriptions)
        miss_set = set(missing_descriptions)
        drop_filter = {k: v for k, v in phase['dropbox_images'].items() if k in miss_set}
        session['original_input'] = list_input
        session['v7_lc_staged'] = True
        session['v7_lc_snapshot_name'] = snap_lbl
        pst_gate = _pst_now_parts()
        return render_template(
            'V7_Listcreator_descriptions_gate.html',
            current_user=current_user,
            list_input=list_input,
            snapshot_name=snap_lbl,
            snapshot_time_hint=pst_gate['created_at_pt_display'],
            snapshot_time_hint_iso=pst_gate['created_at_iso'],
            missing_descriptions=missing_descriptions,
            image_urls=image_urls,
            dropbox_images=drop_filter,
        )

    session.pop('v7_lc_staged', None)
    app = current_app._get_current_object()
    rid = _enqueue_listcreator_html_job(app, list_input, current_user, snap_lbl)
    return redirect(url_for('v7_listcreator.html_job_wait', rid=rid))

@v7_listcreator_bp.route('/update_descriptions', methods=['POST'])
@login_required
def update_descriptions():
    descriptions = {}
    original_input = (request.form.get('list_input') or '').strip() or session.get('original_input', '')

    # Collect all style descriptions from form
    for key, value in request.form.items():
        if key.startswith('description_'):
            style_index = key.split('_')[1]
            style = request.form.get(f'style_{style_index}', '')
            raw_val = (value or '').strip()
            if style and raw_val:
                descriptions[style] = raw_val.upper()
                update_description_in_db(style, raw_val.upper())

    # Staged gate flow, or bake HTML snapshot embedding rebuild_html_snapshot hidden fields —
    # without this, Submit from a saved snapshot only updated DB because v7_lc_staged was popped at job queue time.
    rebuild_from_snapshot = (
        request.form.get('rebuild_html_snapshot') == '1' and bool((request.form.get('list_input') or '').strip())
    )
    staged_now = session.get('v7_lc_staged')
    enqueue_rebuild = bool(staged_now) or rebuild_from_snapshot

    if enqueue_rebuild:
        app = current_app._get_current_object()
        snap = _sanitize_snapshot_label(
            (request.form.get('snapshot_name') or '').strip()
            or (session.get('v7_lc_snapshot_name') or '')
            or ''
        )
        if len(snap) < 2:
            flash('Snapshot name required (use at least 2 characters).', 'warning')
            return redirect(url_for('v7_listcreator.index'))
        session.pop('v7_lc_staged', None)
        session.pop('v7_lc_snapshot_name', None)
        session['original_input'] = original_input
        rid = _enqueue_listcreator_html_job(app, original_input, session.get('username', ''), snap)
        return redirect(url_for('v7_listcreator.html_job_wait', rid=rid))

    return redirect(url_for('v7_listcreator.index', list_input=original_input))


@v7_listcreator_bp.route('/v7_listcreator/html_job/<rid>', methods=['GET'])
@login_required
def html_job_wait(rid):
    if not _valid_listcreator_job_id(rid):
        return redirect(url_for('v7_listcreator.index'))
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    meta = _listcreator_html_load_job(app, slug, rid)
    if not meta:
        return redirect(url_for('v7_listcreator.index'))
    poll_url = url_for('v7_listcreator.html_job_status', rid=rid) + '?owner_slug=' + quote(slug)
    return render_template(
        'V7_Listcreator_html_job_wait.html',
        rid=rid,
        job=meta,
        job_poll_url=poll_url,
        cache_days=LISTCREATOR_HTML_CACHE_DAYS,
    )


@v7_listcreator_bp.route('/v7_listcreator/html_job/<rid>/status', methods=['GET'])
@login_required
def html_job_status(rid):
    if not _valid_listcreator_job_id(rid):
        return jsonify({'ok': False, 'error': 'Invalid job'}), 400
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    meta = _listcreator_html_load_job(app, slug, rid)
    if not meta:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    out = {
        'ok': True,
        'status': meta.get('status'),
        'error': meta.get('error'),
        'view_url': _listcreator_cached_html_public_url(rid, slug) if meta.get('status') == 'ready' else meta.get('view_url'),
        'created_at': meta.get('created_at'),
        'created_at_pt_display': meta.get('created_at_pt_display'),
        'snapshot_generated_at_pt': meta.get('snapshot_generated_at_pt'),
        'snapshot_generated_at_iso': meta.get('snapshot_generated_at_iso'),
        'snapshot_name': meta.get('snapshot_name'),
    }
    return jsonify(out)


@v7_listcreator_bp.route('/v7_listcreator/html_job/<rid>/rename', methods=['POST'])
@login_required
def html_job_rename(rid):
    if not _valid_listcreator_job_id(rid):
        return jsonify({'ok': False, 'error': 'Invalid job'}), 400
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    payload = {}
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    raw_name = (payload.get('snapshot_name') if payload else '') or request.form.get('snapshot_name', '') or ''
    snap = _sanitize_snapshot_label(raw_name.strip())
    if len(snap) < 2:
        return jsonify({'ok': False, 'error': 'Snapshot name needs at least 2 characters'}), 400
    with _listcreator_html_lock:
        meta = _listcreator_html_load_job(app, slug, rid)
        if not meta or meta.get('username') != current_user:
            return jsonify({'ok': False, 'error': 'Not found'}), 404
        meta['snapshot_name'] = snap
        p = _listcreator_html_job_manifest_path(app, slug, rid)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=0)
        os.replace(tmp, p)
        _listcreator_html_prune_old(app, slug)
    return jsonify({'ok': True, 'snapshot_name': snap})


@v7_listcreator_bp.route('/v7_listcreator/html_job/<rid>/delete', methods=['DELETE', 'POST'])
@login_required
def html_job_delete(rid):
    if not _valid_listcreator_job_id(rid):
        return jsonify({'ok': False, 'error': 'Invalid job'}), 400
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    with _listcreator_html_lock:
        meta = _listcreator_html_load_job(app, slug, rid)
        if not meta or meta.get('username') != current_user:
            return jsonify({'ok': False, 'error': 'Not found'}), 404
        if not _listcreator_html_delete_job_disk(app, slug, rid, meta):
            return jsonify({'ok': False, 'error': 'Could not remove snapshot files'}), 500
        _listcreator_html_prune_old(app, slug)
    return jsonify({'ok': True})


@v7_listcreator_bp.route('/v7_listcreator/html_job/<rid>/redo', methods=['POST'])
@login_required
def html_job_redo(rid):
    """Queue a new HTML snapshot using the same list_input and snapshot label (refreshes cached list with latest data)."""
    if not _valid_listcreator_job_id(rid):
        return jsonify({'ok': False, 'error': 'Invalid job'}), 400
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    meta = _listcreator_html_load_job(app, slug, rid)
    if not meta or meta.get('username') != current_user:
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    list_input = (meta.get('list_input') or '').strip()
    if not list_input:
        return jsonify({'ok': False, 'error': 'No saved input for this job; start a new run from V7.'}), 400
    snap = _sanitize_snapshot_label(meta.get('snapshot_name') or '')
    if len(snap) < 2:
        return jsonify({'ok': False, 'error': 'Snapshot name missing; cannot redo.'}), 400
    new_rid = _enqueue_listcreator_html_job(app, list_input, current_user, snap)
    new_slug = _listcreator_html_user_slug(current_user)
    return jsonify({
        'ok': True,
        'rid': new_rid,
        'wait_url': _listcreator_html_job_wait_public_url(new_rid, new_slug),
    })


LISTCREATOR_STYLE_TOOLS_JS = '/static/js/v7_listcreator_style_tools.js?v=4'


def _inject_listcreator_style_tools_js(html):
    """Ensure cached snapshots run the latest remove-styles toolkit."""
    if not html:
        return html
    tag = f'<script src="{LISTCREATOR_STYLE_TOOLS_JS}"></script>\n'
    existing = re.compile(
        r'<script\b[^>]*\bsrc=["\'][^"\']*v7_listcreator_style_tools\.js(?:\?[^"\']*)?["\'][^>]*>\s*</script>',
        re.IGNORECASE,
    )
    if existing.search(html):
        return existing.sub(tag.rstrip('\n'), html, count=1)
    lower = html.lower()
    idx = lower.rfind('</body>')
    if idx >= 0:
        return html[:idx] + tag + html[idx:]
    return html + tag


@v7_listcreator_bp.route('/v7_listcreator/html_cached/<rid>', methods=['GET'])
@login_required
def listcreator_cached_html(rid):
    if not _valid_listcreator_job_id(rid):
        return jsonify({'error': 'Invalid'}), 400
    current_user = session.get('username')
    app = current_app._get_current_object()
    slug = _listcreator_requested_storage_slug(current_user)
    meta = _listcreator_html_load_job(app, slug, rid)
    if not meta:
        return jsonify({'error': 'Not found'}), 404
    if meta.get('status') != 'ready':
        return jsonify({'error': 'Not ready'}), 400
    hn = meta.get('html_file')
    if not hn:
        return jsonify({'error': 'Missing file'}), 404
    path = os.path.join(_listcreator_html_base(app, slug), 'html', hn)
    if not os.path.isfile(path):
        return jsonify({'error': 'Expired or removed'}), 404
    _listcreator_html_prune_old(app, slug)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            html = _inject_listcreator_style_tools_js(fh.read())
            html = rewrite_html_csrf_meta(html, get_csrf_token())
    except OSError:
        return jsonify({'error': 'Expired or removed'}), 404
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
    return resp


def _normalize_requested_styles(raw_styles):
    """Deduped, sanitized style tokens for catalog-image lookup."""
    wanted = []
    seen = set()
    for raw in raw_styles or []:
        s = str(raw or '').strip()
        if not s or len(s) > 80:
            continue
        if any(ch in s for ch in '<>"\'\n\r\t'):
            continue
        key = s.upper()
        if key in seen:
            continue
        seen.add(key)
        wanted.append(s)
        if len(wanted) >= 250:
            break
    return wanted


def listcreator_catalog_images_for_styles(styles):
    """Return DAM catalog thumbnails keyed by requested style (and uppercase)."""
    wanted = _normalize_requested_styles(styles)
    if not wanted:
        return {}
    found = get_images_for_selection(wanted) or {}
    by_upper = {}
    for key, imgs in found.items():
        if imgs:
            by_upper[str(key).upper()] = imgs
    missing = [s for s in wanted if s.upper() not in by_upper]
    if missing:
        from py.utilities.dam_utils import get_asset_by_sku
        for style in missing:
            try:
                assets = get_asset_by_sku(style, source_filter='catalog_original') or []
            except Exception:
                assets = []
            if assets and not isinstance(assets, list):
                assets = [assets]
            image_set = []
            for i, asset in enumerate(assets):
                if not asset:
                    continue
                image_set.append({
                    'filename': getattr(asset, 'filename', '') or '',
                    'path': get_asset_url(asset.id, width=150),
                    'fullsize_path': get_asset_url(asset.id),
                    'asset_id': asset.id,
                    'default': bool(getattr(asset, 'is_canonical', False) or i == 0),
                })
            if image_set:
                by_upper[style.upper()] = image_set
    out = {}
    for style in wanted:
        imgs = by_upper.get(style.upper())
        if not imgs:
            continue
        out[style] = imgs
        if style.upper() != style:
            out[style.upper()] = imgs
    return out


@v7_listcreator_bp.route('/v7_listcreator/style_catalog_images', methods=['GET', 'POST'])
@login_required
def style_catalog_images():
    """Live catalog images for one or more styles so cached cards can refresh without rerunning the list."""
    styles = []
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        raw = payload.get('styles')
        if raw is None:
            raw = payload.get('style')
        if isinstance(raw, str):
            styles = [raw]
        elif isinstance(raw, list):
            styles = raw
    else:
        many = (request.args.get('styles') or '').strip()
        one = (request.args.get('style') or '').strip()
        if many:
            styles = [part.strip() for part in many.split(',') if part.strip()]
        elif one:
            styles = [one]
    return jsonify({
        'success': True,
        'images': listcreator_catalog_images_for_styles(styles),
    })


def _listcreator_serialize_html_job(j, current_user, me_slug):
    rid = j['id']
    st = j.get('status')
    storage_slug = j.get('storage_slug') or me_slug
    vu = _listcreator_cached_html_public_url(rid, storage_slug) if st == 'ready' else None
    li_full = j.get('list_input') or ''
    resolved = j.get('resolved_styles_full') or ''
    job_user = (j.get('job_username') or '').strip()
    can_manage = job_user == (current_user or '').strip() or (
        not job_user and storage_slug == me_slug
    )
    owner_display = _listcreator_job_owner_display(job_user, storage_slug)
    if not owner_display:
        owner_display = '(no username)'
    owner_tooltip = _listcreator_job_owner_tooltip(job_user, storage_slug)
    return {
        'id': rid,
        'status': st,
        'created_at': j.get('created_at'),
        'created_at_pt_display': j.get('created_at_pt_display'),
        'snapshot_generated_at_pt': j.get('snapshot_generated_at_pt'),
        'snapshot_generated_at_iso': j.get('snapshot_generated_at_iso'),
        'snapshot_name': j.get('snapshot_name'),
        'list_input': li_full,
        'list_input_preview': li_full,
        'resolved_styles': resolved,
        'resolved_styles_full': resolved,
        'source_is_path': _listcreator_input_source_looks_like_dropbox_path(li_full),
        'view_url': vu,
        'wait_url': _listcreator_html_job_wait_public_url(rid, storage_slug),
        'error': j.get('error'),
        'job_username': job_user,
        'storage_slug': storage_slug,
        'owner_display': owner_display,
        'owner_tooltip': owner_tooltip,
        'can_manage': can_manage,
    }


@v7_listcreator_bp.route('/v7_listcreator/html_jobs', methods=['GET'])
@login_required
def listcreator_html_jobs_api():
    """Recent large-list HTML snapshot jobs across all storage folders, with optional user filter."""
    current_user = session.get('username')
    app = current_app._get_current_object()
    me_slug = _listcreator_html_user_slug(current_user)
    with _listcreator_html_lock:
        for s in _listcreator_html_storage_slugs_with_jobs(app):
            _listcreator_html_prune_old(app, s)
    merged = _listcreator_html_merge_all_user_jobs(app)
    users_nonempty = set()
    blank_n = 0
    for j in merged:
        u = (j.get('job_username') or '').strip()
        if u:
            users_nonempty.add(u)
        else:
            blank_n += 1
    filter_options = [
        {'value': '__all__', 'label': 'All users'},
        {'value': '__me__', 'label': _listcreator_my_jobs_filter_label(current_user, me_slug)},
    ]
    for u in sorted(users_nonempty, key=lambda x: x.lower()):
        filter_options.append({
            'value': u,
            'label': _listcreator_job_owner_display(u, u) or u,
        })
    if blank_n:
        filter_options.append({'value': '__blank__', 'label': '(No username in job)'})

    filter_user = (request.args.get('filter_user') or '__all__').strip()
    if filter_user in ('__all__', ''):
        rows = merged
    elif filter_user == '__blank__':
        rows = [j for j in merged if not (j.get('job_username') or '').strip()]
    elif filter_user == '__me__':
        cu = (current_user or '').strip()
        rows = [
            j for j in merged
            if (j.get('job_username') or '').strip() == cu or j.get('storage_slug') == me_slug
        ]
    else:
        rows = [j for j in merged if (j.get('job_username') or '').strip() == filter_user]

    jobs = [_listcreator_serialize_html_job(j, current_user, me_slug) for j in rows]
    return jsonify({
        'ok': True,
        'jobs': jobs,
        'standard_jobs': [],
        'filter_options': filter_options,
        'filter_user': filter_user,
        'me_slug': me_slug,
    })


def get_mysql_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def get_description_from_mysql(style):
    """Fetches the description for a given style from the MySQL database."""
    connection = get_mysql_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT description FROM onlinedb.description WHERE style = %s", (style,))
            description_row = cursor.fetchone()
            if description_row and description_row[0] and str(description_row[0]).strip():
                return str(description_row[0]).strip().upper()
            return "N/A"
        except Error as e:
            print(f"Error fetching description: {e}")
        finally:
            cursor.close()
            connection.close()
    return "N/A"

def get_descriptions_bulk(style_list):
    """Batch-fetch descriptions from MySQL for speed on large SKU lists."""
    out_by_key = {}
    if not style_list:
        return out_by_key
    unique_ordered = []
    seen = set()
    for st in style_list:
        u = str(st).strip()
        if u not in seen:
            seen.add(u)
            unique_ordered.append(u)
    conn = get_mysql_connection()
    if conn:
        chunk_size = 400
        try:
            cursor = conn.cursor()
            for i in range(0, len(unique_ordered), chunk_size):
                chunk = unique_ordered[i:i + chunk_size]
                placeholders = ','.join(['%s'] * len(chunk))
                q = (
                    f"SELECT TRIM(style) AS sty, TRIM(description) AS descr FROM onlinedb.description "
                    f"WHERE style IN ({placeholders})"
                )
                cursor.execute(q, tuple(chunk))
                for row in cursor.fetchall():
                    sty_raw, descr_raw = row[0], row[1]
                    if sty_raw is None:
                        continue
                    key = str(sty_raw).strip()
                    descr = descr_raw.strip() if descr_raw else ''
                    if descr:
                        udescr = descr.upper()
                        out_by_key[key] = udescr
                        out_by_key[key.upper()] = udescr
        except Error as e:
            current_app.logger.error(f"get_descriptions_bulk: {e}")
        finally:
            cursor.close()
            conn.close()
    return out_by_key


def _description_lookup_from_bulk(style, bulk_map):
    if not bulk_map:
        return 'N/A'
    s = str(style).strip()
    v = bulk_map.get(s) or bulk_map.get(s.upper())
    return v if v else 'N/A'


def update_description_in_db(style_index, description):
    """Updates the description for a given style in the MySQL database.
    If the style does not exist, a new row is created."""
    connection = get_mysql_connection()  # Assuming this function is defined elsewhere to get the DB connection

    if connection:
        try:
            cursor = connection.cursor()
            description = description.upper()

            # Prepare the SQL update query
            update_query = "UPDATE onlinedb.description SET description = %s WHERE style = %s"
            params = (description, style_index)

            # Log the actual query and parameters

            # Update the description for the specified style
            cursor.execute(update_query, params)

            # Check the number of affected rows
            if cursor.rowcount == 0:
                
                # Prepare the SQL insert query
                insert_query = "INSERT INTO onlinedb.description (style, description) VALUES (%s, %s)"
                cursor.execute(insert_query, (style_index, description))
                connection.commit()  # Commit the changes to the database
            else:
                connection.commit()  # Commit the changes to the database

        except Error as e:
            current_app.logger.error(f"Error updating description: {e}")

        finally:
            cursor.close()
            connection.close()


def _apply_plog_ocean_qty(conn, rows):
    """Replace inv_data.on_order_qty with plog-summed in-transit qty per SKU."""
    return apply_ocean_qty_to_rows(conn, rows, sku_index=0, ocean_index=5)


def get_sql_data(style_list):
    # Create two separate lists: one for exact base styles, one for styles with suffix
    base_styles = []
    suffix_styles = []
    
    for style in style_list:
        match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
        if not match:
            match = re.match(r'([A-Za-z]{3,}\d{4})([-]?)([smlSML]*)', style)
        if match and match.group(3):  # Has suffix
            suffix_styles.append(style)
        else:  # Base style only
            base_styles.append(style)
    
    # Debug logging
    
    # Modify the query to handle exact matching for base styles
    # Resolve matching PROD_CDs into an indexed temp table first, then join once. Temp columns are
    # VARCHAR (inv columns are CHAR) so comparisons stay index-friendly — NVARCHAR here was ~15x slower.
    query = """
    SET NOCOUNT ON;
    DECLARE @AltCDList NVARCHAR(MAX) = ?;
    DECLARE @ExactStyles NVARCHAR(MAX) = ?;
    IF OBJECT_ID('tempdb..#lc_pat') IS NOT NULL DROP TABLE #lc_pat;
    IF OBJECT_ID('tempdb..#lc_exact') IS NOT NULL DROP TABLE #lc_exact;
    IF OBJECT_ID('tempdb..#lc_prod') IS NOT NULL DROP TABLE #lc_prod;
    CREATE TABLE #lc_pat (v VARCHAR(200) COLLATE DATABASE_DEFAULT);
    CREATE TABLE #lc_exact (v VARCHAR(200) COLLATE DATABASE_DEFAULT);
    CREATE TABLE #lc_prod (p VARCHAR(50) COLLATE DATABASE_DEFAULT PRIMARY KEY);

    -- Styles with suffix: alt_CD LIKE 'STYLE%'
    INSERT INTO #lc_pat (v) SELECT value + '%' FROM dbo.SplitString(@AltCDList, ',') WHERE value <> '';
    -- Base styles: exact alt_CD (or PROD_CD) match
    INSERT INTO #lc_exact (v) SELECT value FROM dbo.SplitString(@ExactStyles, ',') WHERE value <> '';

    INSERT INTO #lc_prod (p)
    SELECT DISTINCT i.PROD_CD
    FROM dbo.inv AS i
    WHERE EXISTS (SELECT 1 FROM #lc_pat WHERE i.alt_CD LIKE #lc_pat.v)
       OR EXISTS (SELECT 1 FROM #lc_exact WHERE i.alt_CD = #lc_exact.v OR i.PROD_CD = #lc_exact.v);

        SELECT 
            i.PROD_CD AS SKU,
            i.ALT_CD AS SKU_no_color,
            id.in_stock AS in_stock,
            id.ORDER_QTY AS some_one_ordered,
            (id.in_stock - id.order_qty) AS quantity,
            id.on_order_qty AS ocean,
            i.RETAIL_PRS AS selling_price,
            i.SALES_COST AS sales_cost,
            i.PC_CASE AS QTY_in_box,
            i.UT_WT AS unit_weight,
            i.UNIT_COLOR AS color,
            idt.ut_len AS item_length,
            idt.ut_wi AS item_width,
            idt.UT_hi AS item_height,
            CASE 
                WHEN LT_SL_DT = 0 THEN 'NEVER'
                ELSE RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                    CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', LT_SL_DT) - 81088, '20230101')) AS VARCHAR)
            END AS last_activity,
            fcd.create_dt AS created_dt
        FROM dbo.inv AS i
        LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        LEFT JOIN inv_detl AS idt ON i.PROD_CD = idt.prod_cd
        LEFT JOIN (
            SELECT 
                prod_cd,
                RIGHT('0' + CAST(MONTH(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                RIGHT('0' + CAST(DAY(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR), 2) + '/' +
                CAST(YEAR(DATEADD(DAY, DATEDIFF(DAY, '19000101', CREATE_DT) - 81088, '20230101')) AS VARCHAR) AS create_dt
            FROM wm_skufile where wm_skufile.WHS_NUM = '01'
        ) AS fcd ON i.PROD_CD = fcd.prod_cd
        INNER JOIN #lc_prod AS lp ON lp.p = i.PROD_CD;
    """
    
    queries = []
    try:
        conn_fdc = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        conn_oms_ef = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        
        for conn, database_name in [(conn_fdc, 'omsdata_FDC'), (conn_oms_ef, 'oms_EF')]:
            cursor = conn.cursor()
            suffix_list = ",".join(suffix_styles)
            base_list = ",".join(base_styles)
          
            cursor.execute(query, suffix_list, base_list)
            rows = cursor.fetchall()
            rows = _apply_plog_ocean_qty(conn, rows)
            
            # Strip trailing spaces from each row's data
            rows_with_database_name = [
                (database_name,) + tuple(str(value).strip() if isinstance(value, str) else value for value in row)
                for row in rows
            ]
            queries.append(rows_with_database_name)
            cursor.close()
            
        combined_results = queries[0] + queries[1] if len(queries) > 1 else queries[0]
        return combined_results
    except Exception as e:
        current_app.logger.error(f"Error in get_sql_data: {str(e)}")
        
        # Add fallback logic for when the SQL query fails
        # Create placeholder data for each style in the list
        placeholder_data = []
        for style in style_list:
            placeholder_row = ('N/A', f'{style}BK', style, 0, 0, 0, 0, 999.0, 0, 0, 0, 'BK', 0, 0, 0, 'N/A', 'N/A', 'Not in OMS')
            placeholder_data.append(placeholder_row)
        
        return placeholder_data
    finally:
        conn_fdc.close()
        conn_oms_ef.close()





def create_pivot_table(data):
    if not data:
        return []
    
    # Create DataFrame without specifying columns - let pandas infer them
    df = pd.DataFrame(data)
    
    # Rename columns to match expected structure
    if len(df.columns) == 17:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'sales_cost', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt']
    elif len(df.columns) == 18:
        df.columns = ['DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered',
                     'quantity', 'ocean', 'selling_price', 'sales_cost', 'pcsBox',
                     'unit_weight', 'color', 'item_length', 'item_width', 'item_height',
                     'last_activity', 'Created_dt', 'in_oms_status']
    
    # Create a normalized style column that will be used for grouping
    # This will remove dashes from styles with suffixes for grouping purposes
    df['normalized_style'] = df['SKU_no_color'].apply(lambda x: normalize_style_for_grouping(x))
    
    # Filter rows where normalized_style is not null
    df = df[df['normalized_style'].notna()]
    
    # Sort by normalized_style
    df = df.sort_values('normalized_style')
    
    # Convert numeric columns to integers
    integer_columns = ['quantity', 'ocean', 'pcsBox',
                     'item_length', 'item_width', 'item_height']
    df[integer_columns] = df[integer_columns].fillna(0).astype(int)
    if 'sales_cost' in df.columns:
        df['sales_cost'] = pd.to_numeric(df['sales_cost'], errors='coerce').fillna(0)
    
    # Create dimensions column
    df['dimensions'] = df['item_length'].astype(str) + ' x ' + df['item_width'].astype(str) + ' x ' + df['item_height'].astype(str)
    
    # Group by normalized_style
    style_groups = df.groupby('normalized_style')
    
    style_cards = []
    for style, group in style_groups:
        most_common_price = round(group['selling_price'].mode().iloc[0], 2)
        
        # Use the first SKU_no_color as the display style
        display_style = group['SKU_no_color'].iloc[0]
        
        style_data = group.pivot_table(
            values=['quantity', 'ocean', 'pcsBox', 'dimensions', 'sales_cost'],
            index=['color'],
            aggfunc={
                'quantity': 'sum',
                'ocean': 'sum',
                'pcsBox': 'first',
                'dimensions': 'first',
                'sales_cost': 'first'
            }
        ).reset_index()
        
        style_data['style'] = display_style
        style_data['selling_price'] = most_common_price
        
        bk_rows = style_data[style_data['color'].str.startswith('BK')]
        other_rows = style_data[~style_data['color'].str.startswith('BK')]
        style_data = pd.concat([bk_rows, other_rows]).reset_index(drop=True)
        
        style_cards.append(style_data.to_dict('records'))
    
    return style_cards


def _reorder_pivot_ocr_colors(style_cards, ocr_color_map):
    """Keep catalog-OCR color order (hero first, then right-column variants)."""
    if not style_cards or not ocr_color_map:
        return style_cards
    out = []
    for group in style_cards:
        if not group:
            out.append(group)
            continue
        display_style = str(group[0].get('style') or '')
        wanted = ocr_color_map.get(normalize_style(display_style)) or ocr_color_map.get(display_style.upper())
        if not wanted:
            out.append(group)
            continue
        by_color = {}
        for row in group:
            key = str(row.get('color') or '').strip().upper()
            if key and key not in by_color:
                by_color[key] = row
        ordered = [by_color[c] for c in wanted if c in by_color]
        used = set(wanted)
        for row in group:
            key = str(row.get('color') or '').strip().upper()
            if key not in used:
                ordered.append(row)
                used.add(key)
        out.append(ordered or group)
    return out


def _build_listcreator_inventory_context(
    list_input,
    current_user,
    style_list,
    dropbox_images,
    duplicate_items,
    clean_list_for_memo,
    missing_styles,
    error_message=None,
):
    """Full SQL + DAM + pivot context for V7_Listcreator.html (used by sync path and background HTML job)."""
    from flask import session

    image_urls = get_images_for_selection(style_list)
    bulk = get_descriptions_bulk(style_list)
    descriptions = {style: _description_lookup_from_bulk(style, bulk) for style in style_list}
    data = get_sql_data(style_list)
    db_styles = set()

    if data:
        for row in data:
            if len(row) >= 18 and str(row[17] or '').strip() == 'Not in OMS':
                continue
            exact_style = row[2].upper() if row[2] else ''
            if exact_style:
                db_styles.add(exact_style)
                session[f'verified_{exact_style}'] = True
                sku_match = re.match(r'([A-Za-z]{1,2}\d{4})', row[2])
                if sku_match:
                    base_sku = sku_match.group(1).upper()
                    db_styles.add(base_sku)
                    session[f'verified_{base_sku}'] = True

    from py.utilities.listcreator_ocr_colors import (
        delete_temp_ocr_colors,
        resolve_not_in_oms_colors,
    )

    normalized_db_styles = {normalize_style(s) for s in db_styles}
    ocr_color_map = {}
    ocr_color_styles = []
    for style in style_list:
        if normalize_style(style) in normalized_db_styles:
            delete_temp_ocr_colors(style)
            continue
        if style not in missing_styles:
            missing_styles.append(style)
        ocr_colors = resolve_not_in_oms_colors(style, dropbox_images=dropbox_images)
        colors = ocr_colors if ocr_colors else ['BK']
        if ocr_colors:
            ocr_color_map[normalize_style(style)] = list(ocr_colors)
            ocr_color_styles.append(style)
        placeholder_data = [
            ('N/A', f'{style}{color}', style, 0, 0, 0, 0, 999.0, 0, 0, 0, color, 0, 0, 0, 'N/A', 'N/A', 'Not in OMS')
            for color in colors
        ]
        if data:
            data.extend(placeholder_data)
        else:
            data = placeholder_data

    seen_missing_desc = set()
    missing_descriptions = []
    for style in style_list:
        raw = descriptions.get(style, '') or ''
        norm = raw.strip().upper()
        if norm in ('', 'N/A', 'NA') and style not in seen_missing_desc:
            seen_missing_desc.add(style)
            missing_descriptions.append(style)

    style_count = {}
    for style in style_list:
        style_upper = style.upper()
        style_count[style_upper] = style_count.get(style_upper, 0) + 1
    duplicate_styles_list = [sty for sty, count in style_count.items() if count > 1]

    total_duplicates = sum(len(item['files']) - 1 for item in duplicate_items)
    final_count = (
        (list_input.count_dropbox_files() - total_duplicates)
        if hasattr(list_input, 'count_dropbox_files')
        else 0
    )

    if not data:
        return None

    pivot_data = create_pivot_table(data)
    if ocr_color_map:
        pivot_data = _reorder_pivot_ocr_colors(pivot_data, ocr_color_map)
    return {
        'current_user': current_user,
        'data': data,
        'pivot_data': pivot_data,
        'image_urls': image_urls,
        'dropbox_images': dropbox_images,
        'descriptions': descriptions,
        'error_message': error_message,
        'missing_styles': missing_styles,
        'ocr_color_styles': ocr_color_styles,
        'duplicate_styles': duplicate_styles_list,
        'your_input_was': list_input,
        'clean_list_for_memo': clean_list_for_memo,
        'duplicate_items': duplicate_items,
        'missing_descriptions': missing_descriptions,
        'total_duplicates': total_duplicates,
        'final_count': final_count,
        'list_input_is_dropbox_folder': _list_input_is_dropbox_subpath_folder(list_input),
        'bag_description_options': fetch_description_options(),
    }


def _listcreator_regenerate_snapshot_file(app, slug, rid, list_input, acting_username, slack_notify=False):
    """
    Re-render the full V7_Listcreator HTML for ``rid`` and overwrite the cached file + manifest in place.

    Parameters
    ----------
    acting_username :
        Passed as Flask ``session['username']`` while rendering (OMS/Dropbox session behavior).
    slack_notify :
        Worker sets True when a newly queued snapshot finishes; Edit-desc refresh uses False.
    """
    tmp_hp = None
    hp = None
    meta_out = {}
    resolved_styles_full = ''

    try:
        with app.test_request_context('/'):
            from flask import session as flask_session

            flask_session.permanent = True
            flask_session['username'] = acting_username

            expanded_input, expand_err = _expand_listcreator_mixed_input(list_input)
            if expand_err:
                return False, expand_err
            list_input = expanded_input or list_input

            phase = _phase1_resolve_list_input(list_input, acting_username)
            if phase.get('error_message'):
                return False, phase['error_message'] or 'Invalid input'
            style_list = phase['style_list']
            if not style_list:
                return False, 'No styles to process'
            ms = list(phase['missing_styles'])
            ctx = _build_listcreator_inventory_context(
                list_input,
                acting_username,
                style_list,
                phase['dropbox_images'],
                phase['duplicate_items'],
                phase['clean_list_for_memo'],
                ms,
                error_message=phase.get('error_message'),
            )
            if not ctx:
                return False, 'No inventory data for this list'

            memo = ctx.get('clean_list_for_memo')
            if memo:
                resolved_styles_full = str(memo).strip()
            else:
                resolved_styles_full = ','.join(style_list)

            pst_snap = _pst_now_parts()
            job_snap_row = _listcreator_html_load_job(app, slug, rid) or {}
            baked_snapshot_label = _sanitize_snapshot_label(job_snap_row.get('snapshot_name') or '')
            html = render_template(
                'V7_Listcreator.html',
                **ctx,
                list_input=list_input,
                snapshot_name=baked_snapshot_label,
                snapshot_generated_at_pt=pst_snap['created_at_pt_display'],
                snapshot_generated_at_iso=pst_snap['created_at_iso'],
                html_snapshot_job_id=rid,
            )
            hp = _listcreator_html_snap_path(app, slug, rid)
            tmp_hp = hp + '.tmp'
            view_url = _listcreator_cached_html_public_url(rid, slug)

        os.makedirs(os.path.dirname(hp) or '.', exist_ok=True)
        with open(tmp_hp, 'w', encoding='utf-8') as f:
            f.write(html)
        os.replace(tmp_hp, hp)
        tmp_hp = None

        pst_pt = pst_snap['created_at_pt_display']
        pst_iso = pst_snap['created_at_iso']
        basename = os.path.basename(hp)
        meta_out = {}

        with _listcreator_html_lock:
            meta = _listcreator_html_load_job(app, slug, rid)
            if not meta:
                return False, 'Snapshot job manifest not found'
            meta_out = meta.copy()
            meta['status'] = 'ready'
            meta['error'] = None
            meta['html_file'] = basename
            meta['view_url'] = view_url
            meta['resolved_styles_full'] = resolved_styles_full
            meta['snapshot_generated_at_pt'] = pst_pt
            meta['snapshot_generated_at_iso'] = pst_iso
            p = _listcreator_html_job_manifest_path(app, slug, rid)
            tmp_meta = p + '.tmp'
            with open(tmp_meta, 'w', encoding='utf-8') as f:
                json.dump(meta, f, indent=0)
            os.replace(tmp_meta, p)
            _listcreator_html_prune_old(app, slug)

        label = 'job ready (Slack notified)' if slack_notify else 'snapshot file rewritten in place'
        current_app.logger.info('listcreator HTML %s rid=%s user=%s', label, rid, acting_username)

        if slack_notify and meta_out:
            try:
                from py.utilities.slack_notify import notify_v7_list_creator_optional, public_origin

                origin = public_origin()
                rel = view_url if view_url else ''
                full = rel if rel.startswith('http') else (origin + rel if rel.startswith('/') else rel)
                lbl = ((meta_out.get('snapshot_name') or '').strip()) or '(no label)'
                li_raw = (list_input or '').strip()
                styles_text = (resolved_styles_full or '').strip()
                slack_max = 3400
                if len(styles_text) > slack_max:
                    styles_text = styles_text[: slack_max - 20] + '\n… (truncated)'

                chunks = [
                    '[V7 List Creator] HTML snapshot ready',
                    f'User: {acting_username}',
                    f'Snapshot: {lbl}',
                    f'Job id: {rid}',
                ]
                if li_raw and _listcreator_input_source_looks_like_dropbox_path(li_raw):
                    chunks.append(f'Dropbox / folder path (submitted): {li_raw}')
                elif li_raw:
                    chunks.append(f'Source input line: {li_raw}')
                if styles_text:
                    chunks.append(f'Styles (resolved): {styles_text}')
                if full:
                    chunks.append(f'Open snapshot: {full}')

                notify_v7_list_creator_optional('\n'.join(chunks) + '\n')
            except Exception:
                current_app.logger.debug('slack v7 snapshot notify skipped', exc_info=True)

        return True, None

    except Exception as e:
        current_app.logger.exception('listcreator snapshot rewrite failed rid=%s', rid)
        if tmp_hp and os.path.isfile(tmp_hp):
            try:
                os.remove(tmp_hp)
            except OSError:
                pass
        return False, str(e)


def _listcreator_render_html_worker(app, slug, rid, list_input, current_user):
    with app.app_context():
        try:
            ok, err = _listcreator_regenerate_snapshot_file(
                app, slug, rid, list_input, current_user, slack_notify=True
            )
            if not ok:
                _listcreator_html_fail_job(app, slug, rid, err or 'Render failed')
        except Exception as e:
            current_app.logger.exception('listcreator HTML job failed rid=%s', rid)
            _listcreator_html_fail_job(app, slug, rid, str(e))


def _enqueue_listcreator_html_job(app, list_input, current_user, snapshot_name=None):
    slug = _listcreator_html_user_slug(current_user)
    display_name = _sanitize_snapshot_label(snapshot_name or '')
    slug_core = _slug_for_job_id(display_name)
    rid = _allocate_listcreator_job_id(app, slug, slug_core)
    pst = _pst_now_parts()
    meta = {
        'id': rid,
        'snapshot_name': display_name,
        'status': 'running',
        'created_at': pst['created_at_iso'],
        'created_at_pt_display': pst['created_at_pt_display'],
        'list_input': list_input or '',
        'error': None,
        'html_file': None,
        'username': current_user,
        'view_url': None,
    }
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass
    with _listcreator_html_lock:
        _listcreator_html_prune_old(app, slug)
        p = _listcreator_html_job_manifest_path(app, slug, rid)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=0)
        os.replace(tmp, p)
    threading.Thread(
        target=_listcreator_render_html_worker,
        args=(app, slug, rid, list_input, current_user),
        daemon=True,
        name=f'listcreator-html-{rid[:8]}',
    ).start()
    return rid


def normalize_style_for_grouping(style):
    """
    Normalizes style codes for grouping purposes:
    - For base styles (L1343), returns the exact style
    - For styles with suffix (L1343M or L1343-M), returns the no-dash version (L1343M)
    """
    if not style:
        return None
        
    # Check if the style has a suffix - standard or extended prefix + 4 digits
    match = re.match(r'([A-Za-z]{1,2}\d{4})([-]?)([smlSML]*)', style)
    if not match:
        match = re.match(r'([A-Za-z]{3,}\d{4})([-]?)([smlSML]*)', style)
    if not match:
        return style
        
    base = match.group(1)
    suffix = match.group(3)
    
    if suffix:  # If it has a suffix
        return base + suffix  # Return without dash
    else:
        return base  # Return base style as is


def get_images_from_dropbox(dropbox_path):
    image_paths = []
    try:
        for filename in os.listdir(dropbox_path):
            if filename.lower().endswith(('.jpg', '.png')):
                image_paths.append(os.path.join(dropbox_path, filename))
    except Exception as e:
        current_app.logger.error(f"Error fetching images from Dropbox: {e}")
    
    return image_paths

def _catalog_assets_by_sku_patterns(patterns, chunk_size=100):
    """Batched equivalent of search_assets_by_sku_pattern(p, 'catalog_original') for many patterns.
    Returns {pattern: [assets ordered by sku, version]}."""
    from sqlalchemy import or_
    ImageAsset = py.utilities.models.ImageAsset
    unique = list(dict.fromkeys(p.upper() for p in patterns if p))
    found = {}
    db = py.utilities.models.SessionLocal()
    try:
        for i in range(0, len(unique), chunk_size):
            chunk = unique[i:i + chunk_size]
            rows = db.query(ImageAsset).filter(
                or_(*[ImageAsset.sku.ilike(f'%{p}%') for p in chunk]),
                ImageAsset.is_active == True,
                ImageAsset.source == 'catalog_original',
            ).all()
            for a in rows:
                found[a.id] = a
    finally:
        db.close()
    assets = sorted(found.values(), key=lambda a: ((a.sku or '').upper(), a.version or 0))
    return {p: [a for a in assets if p in (a.sku or '').upper()] for p in unique}


def get_images_for_selection(sku_list):
    from py.utilities.dam_utils import get_asset_url

    image_urls = {}
    patterns_by_sku = []
    for sku in sku_list:
        try:
            # Standard (L1343) or extended (TBDBN3602) prefix + 4 digits, optional dash suffix
            base_match = re.match(r'([A-Za-z]{1,2}\d{4})(?:-[A-Za-z0-9]+)?', sku)
            if not base_match:
                base_match = re.match(r'([A-Za-z]{3,}\d{4})(?:-[A-Za-z0-9]+)?', sku)
            if not base_match:
                continue
            base_sku = base_match.group(1).upper()  # Base SKU without suffix
            full_sku = sku.upper()  # Full SKU including any suffix
            # Full SKU, plus the no-dash / dash variant of a suffixed SKU
            patterns = [full_sku]
            if '-' in full_sku:
                patterns.append(full_sku.replace('-', ''))
            elif len(full_sku) > len(base_sku):
                patterns.append(f"{base_sku}-{full_sku[len(base_sku):]}")
            patterns_by_sku.append((full_sku, patterns))
        except UnicodeEncodeError:
            continue

    assets_by_pattern = _catalog_assets_by_sku_patterns(
        [p for _, patterns in patterns_by_sku for p in patterns]
    )

    for full_sku, patterns in patterns_by_sku:
        try:
            image_set = []
            matching_assets = [a for p in patterns for a in assets_by_pattern.get(p, [])]

            # Remove duplicates by asset ID
            unique_assets = {}
            for asset in matching_assets:
                unique_assets[asset.id] = asset
            matching_assets = list(unique_assets.values())

            # Sort by canonical first, then by version
            matching_assets.sort(key=lambda x: (not x.is_canonical, x.version))

            # Convert assets to image_set format
            for i, asset in enumerate(matching_assets):
                image_set.append({
                    'filename': asset.filename,
                    'path': get_asset_url(asset.id, width=150),  # Default thumbnail size
                    'fullsize_path': get_asset_url(asset.id),  # Full size for enlargement
                    'asset_id': asset.id,
                    'default': asset.is_canonical or (i == 0 and not any(a.is_canonical for a in matching_assets))
                })

            # Add SKU and its images to the dictionary
            if image_set:  # Only include SKUs that have images
                image_urls[full_sku] = sorted(image_set, key=lambda x: x['filename'])  # Sort by filename for consistency

        except UnicodeEncodeError:
            continue

    return image_urls


MERGE_ROW_COLUMNS = [
    'DatabaseName', 'SKU', 'SKU_no_color', 'in_stock', 'some_one_ordered', 'quantity', 'ocean',
    'selling_price', 'sales_cost', 'pcsBox', 'unit_weight', 'color', 'item_length', 'item_width',
    'item_height', 'last_activity', 'Created_dt',
]


def merge_inventory_rows_cross_database(raw_rows):
    """
    Sum quantity (available = in_stock - order_qty in source query) and ocean (incoming)
    per PROD_CD + color across FDC + E_FAS — same combined totals as product_detail.html.
    Drops rows where combined available and ocean are both <= 0.
    """
    if not raw_rows:
        return []
    ncol = len(raw_rows[0])
    if ncol == len(MERGE_ROW_COLUMNS):
        cols = MERGE_ROW_COLUMNS
    elif ncol == len(MERGE_ROW_COLUMNS) + 1:
        cols = MERGE_ROW_COLUMNS + ['in_oms_status']
    else:
        current_app.logger.warning(f"merge_inventory_rows_cross_database: unexpected ncol {ncol}")
        return raw_rows

    df = pd.DataFrame([tuple(r) for r in raw_rows], columns=cols[:ncol])
    df['SKU'] = df['SKU'].astype(str).str.strip()
    df['color'] = df['color'].fillna('').astype(str).str.strip()

    sum_cols = ['quantity', 'ocean', 'in_stock', 'some_one_ordered']
    first_cols = ['SKU_no_color', 'selling_price', 'sales_cost', 'pcsBox', 'unit_weight',
                  'item_length', 'item_width', 'item_height', 'last_activity', 'Created_dt']
    agg = {'quantity': 'sum', 'ocean': 'sum', 'in_stock': 'sum', 'some_one_ordered': 'sum'}
    for c in first_cols:
        agg[c] = 'first'

    merged = df.groupby(['SKU', 'color'], as_index=False).agg(agg)
    merged['DatabaseName'] = 'combined'
    merged = merged[(merged['quantity'] > 0) | (merged['ocean'] > 0)]

    out = []
    for _, row in merged.iterrows():
        rec = (
            'combined',
            row['SKU'],
            row['SKU_no_color'],
            row['in_stock'],
            row['some_one_ordered'],
            row['quantity'],
            row['ocean'],
            row['selling_price'],
            row['sales_cost'],
            row['pcsBox'],
            row['unit_weight'],
            row['color'],
            row['item_length'],
            row['item_width'],
            row['item_height'],
            row['last_activity'],
            row['Created_dt'],
        )
        if ncol == 18:
            rec = rec + ('',)
        out.append(rec)
    return out


def discover_styles_with_positive_inventory(style_prefix='', limit=4000):
    """
    DISTINCT ALT_CD values that have (available > 0 OR incoming > 0) in either OMS DB,
    matching product_detail availability logic (per-DB row, combined later via merge).
    """
    alt_codes = set()
    prefix = (style_prefix or '').strip().upper()
    conn_fdc = pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
    )
    conn_oms_ef = pyodbc.connect(
        f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_oms_ef"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
    )
    sql_base = f"""
        SELECT DISTINCT RTRIM(LTRIM(i.ALT_CD)) AS alt_cd
        FROM dbo.inv AS i
        INNER JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
        {PLOG_OCEAN_JOIN}
        WHERE (
            (ISNULL(id.in_stock, 0) - ISNULL(id.order_qty, 0)) > 0
            OR {ocean_qty_sql_scalar()} > 0
        )
    """
    try:
        for conn in (conn_fdc, conn_oms_ef):
            cursor = conn.cursor()
            if prefix:
                q = sql_base + " AND (UPPER(i.ALT_CD) LIKE ? OR UPPER(i.PROD_CD) LIKE ?)"
                cursor.execute(q, (f"{prefix}%", f"{prefix}%"))
            else:
                cursor.execute(sql_base)
            for row in cursor.fetchall():
                if row and row[0]:
                    alt_codes.add(str(row[0]).strip())
            cursor.close()
    finally:
        conn_fdc.close()
        conn_oms_ef.close()

    ordered = sorted(alt_codes)
    return ordered[:limit]


def _resolve_catalog_image_url(display_style, image_urls):
    """Pick default DAM image URL for a style (same rules as list creator cards)."""
    if not display_style or not image_urls:
        return '/static/no_image_available.jpg'
    for k, imgs in image_urls.items():
        if normalize_style(str(k)) == normalize_style(str(display_style)):
            ordered = sorted(imgs, key=lambda x: x['filename'])
            pick = next((im for im in ordered if im.get('default')), ordered[0])
            return pick['fullsize_path']
    return '/static/no_image_available.jpg'


def _chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def build_export_data_from_list_input(list_input_str, current_user=None):
    """
    Build export_list-shaped payloads from OMS + descriptions + DAM (no browser cards).
    Combines FDC + E_FAS like product_detail (sum available + incoming per SKU/color).
    Empty input or Dropbox-looking input → discover ALT_CD styles that have stock/ocean > 0.
    """
    if current_user is None:
        current_user = session.get('username', 'export')
    text = (list_input_str or '').strip()
    if text:
        expanded, expand_err = _expand_listcreator_mixed_input(text)
        if expand_err:
            raise ValueError(expand_err)
        text = expanded or text
    if not text or 'dropbox' in text.lower():
        discovered = discover_styles_with_positive_inventory('', limit=3500)
        if not discovered:
            return []
        text = ','.join(discovered)
    style_list = list_from_origin(text, current_user)
    if not style_list:
        return []
    merged_all = []
    for chunk in _chunk_list(style_list, 120):
        raw = get_sql_data(chunk)
        merged_all.extend(merge_inventory_rows_cross_database(raw))
    merged_all = merge_inventory_rows_cross_database(merged_all)
    if not merged_all:
        return []
    pivot_cards = create_pivot_table(merged_all)
    if not pivot_cards:
        return []
    image_urls = get_images_for_selection(style_list)
    export_items = []
    for style_group in pivot_cards:
        if not style_group:
            continue
        row0 = style_group[0]
        display_style = row0['style']
        desc = get_description_from_mysql(display_style)
        sp = row0.get('selling_price', 0)
        try:
            price_str = f"${float(sp):.2f}"
        except (TypeError, ValueError):
            price_str = f"${sp}"
        img_path = _resolve_catalog_image_url(display_style, image_urls)
        rows_out = []
        for row in style_group:
            q = int(row.get('quantity', 0) or 0)
            o = int(row.get('ocean', 0) or 0)
            if q <= 0 and o <= 0:
                continue
            rows_out.append({
                'color': str(row.get('color', '') or ''),
                'quantity': str(q),
                'ocean': str(o),
                'total': str(q + o),
                'pcsBox': str(int(row.get('pcsBox', 0) or 0)),
                'dimensions': str(row.get('dimensions', '') or ''),
            })
        if not rows_out:
            continue
        export_items.append({
            'style': display_style,
            'description': desc if desc else 'N/A',
            'price': price_str,
            'image': img_path,
            'rows': rows_out,
        })
    return export_items


@v7_listcreator_bp.route('/list_image/dropbox/<path:filename>')
def serve_dropbox_image(filename):
    dropbox_path = os.path.join(Config.LOCAL_DROPBOX_DIR, filename)

    if not os.path.exists(dropbox_path):
        return send_from_directory('static', 'no_image_available.jpg')

    # Thumbnails use ?size=150 in templates; without this we were sending full files (slow + heavy).
    size = request.args.get('size', 'full')
    if size == 'full' or size == '':
        return send_file(dropbox_path)
    try:
        sz = int(size) if str(size).isdigit() else 300
        if sz <= 0:
            return send_file(dropbox_path)
        buffer = resize_image(dropbox_path, sz)
        return send_file(buffer, mimetype='image/jpeg')
    except Exception as e:
        current_app.logger.error(f'Error serving dropbox image {filename}: {e}')
        return send_file(dropbox_path)


@v7_listcreator_bp.route('/v7_listcreator/dropbox_desktop_status', methods=['GET'])
@login_required
def dropbox_desktop_status():
    """JSON status for the Dropbox desktop sync app on this server."""
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for('auth.login'))
    st = _v7_dropbox_desktop_status()
    return jsonify({'ok': True, 'status': st})


@v7_listcreator_bp.route('/v7_listcreator/dropbox_desktop_refresh', methods=['POST'])
@login_required
def dropbox_desktop_refresh():
    """Check Dropbox sync app; start if stopped, restart if requested to refresh sync."""
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for('auth.login'))
    body = request.get_json(silent=True) or {}
    restart = bool(body.get('restart', True))
    payload, code = _v7_dropbox_desktop_start_or_restart(restart=restart)
    user = session.get('username') or '?'
    current_app.logger.info(
        'dropbox_desktop_refresh by %s: ok=%s action=%s',
        user,
        payload.get('ok'),
        payload.get('action'),
    )
    return jsonify(payload), code


@v7_listcreator_bp.route('/v7_listcreator/browse_dropbox', methods=['GET'])
@login_required
def browse_dropbox():
    """List subfolders under the server's LOCAL_DROPBOX_DIR with image counts (no file names)."""
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for('auth.login'))
    rel_raw = request.args.get('path', '') or request.args.get('subpath', '')
    dest, err = _v7_safe_resolve_dropbox_browse_path(rel_raw)
    if err == 'not_configured':
        return jsonify({'ok': False, 'error': 'Dropbox folder is not configured on this server.'}), 503
    if err == 'invalid':
        return jsonify({'ok': False, 'error': 'Invalid path.'}), 400
    if err == 'not_found':
        return jsonify({'ok': False, 'error': 'Folder not found.'}), 404
    if err == 'not_dir':
        return jsonify({'ok': False, 'error': 'Not a directory.'}), 400
    try:
        base = Path(os.path.abspath(str(Config.LOCAL_DROPBOX_DIR))).resolve()
    except OSError:
        return jsonify({'ok': False, 'error': 'Dropbox folder is not configured on this server.'}), 503
    current_rel = _v7_rel_path_under_dropbox(base, dest)
    crumbs = [{'name': base.name or 'Dropbox', 'rel': ''}]
    if current_rel:
        acc_parts = []
        for p in current_rel.split('\\'):
            acc_parts.append(p)
            crumbs.append({'name': p, 'rel': '\\'.join(acc_parts)})
    dirs = []
    try:
        entries = os.listdir(dest)
    except OSError as e:
        current_app.logger.warning('browse_dropbox listdir %s: %s', dest, e)
        return jsonify({'ok': False, 'error': 'Could not read this folder.'}), 500
    for name in entries:
        child = dest / name
        try:
            if child.is_dir():
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    mtime = 0.0
                last_updated_iso = datetime.datetime.fromtimestamp(
                    mtime, datetime.timezone.utc
                ).strftime('%Y-%m-%dT%H:%M:%SZ')
                dirs.append({
                    'name': name,
                    'rel': _v7_rel_path_under_dropbox(base, child),
                    'image_count': _v7_count_images_in_folder(child),
                    'last_updated_iso': last_updated_iso,
                    'last_updated_unix': int(mtime),
                })
        except OSError:
            continue
    dirs.sort(key=lambda x: (-x['last_updated_unix'], x['name'].lower()))
    for d in dirs:
        d.pop('last_updated_unix', None)
    image_here = _v7_count_images_in_folder(dest)
    if current_rel:
        dropbox_input_value = 'dropbox\\' + current_rel
        can_use_folder = True
    else:
        dropbox_input_value = ''
        can_use_folder = False
    return jsonify({
        'ok': True,
        'server_folder_label': base.name or 'Dropbox',
        'server_root_path': str(base),
        'current_rel': current_rel,
        'dropbox_input_value': dropbox_input_value,
        'can_use_folder': can_use_folder,
        'breadcrumbs': crumbs,
        'image_count_here': image_here,
        'directories': dirs,
        'dropbox_sync_status': _v7_dropbox_desktop_status(),
    })


@v7_listcreator_bp.route('/list_image/<path:filename>')
def serve_list_image(filename):
    # Use DAM system to find asset by filename
    import py.utilities.models
    
    db = py.utilities.models.SessionLocal()
    try:
        asset = db.query(py.utilities.models.ImageAsset).filter_by(
            filename=filename,
            is_active=True
        ).first()
        
        if not asset or not os.path.exists(asset.path):
            return send_from_directory('static', 'no_image_available.jpg')
        
        # Get size parameter for optional resizing
        size = request.args.get('size', 'full')
        
        if size == 'full':
            return send_file(asset.path)
        else:
            try:
                # Resize image on demand
                buffer = resize_image(asset.path, int(size) if size.isdigit() else 300)
                return send_file(buffer, mimetype='image/jpeg')
            except Exception as e:
                current_app.logger.error(f"Error serving image {filename}: {e}")
                return send_from_directory('static', 'no_image_available.jpg')
    finally:
        db.close()
    
def set_border(ws, cell_range):
    thin = Border(left=Side(style='thin'), right=Side(style='thin'),
                  top=Side(style='thin'), bottom=Side(style='thin'))
    for row in ws[cell_range]:
        for cell in row:
            cell.border = thin
def set_border(ws, cell_range):
    thin = Side(border_style="thin", color="000000")
    for row in ws[cell_range]:
        for cell in row:
            cell.border = Border(top=thin, left=thin, right=thin, bottom=thin)

@v7_listcreator_bp.app_template_filter('count_dropbox_files')
def count_dropbox_files(dropbox_path):
    match = re.search(r'dropbox\\(.+)', dropbox_path, re.IGNORECASE)
    if match:
        relative_path = match.group(1)
        full_path = os.path.join(Config.LOCAL_DROPBOX_DIR, relative_path)
        if os.path.exists(full_path):
            return len([f for f in os.listdir(full_path) if f.lower().endswith(('.jpg', '.png'))])
    return 0


@v7_listcreator_bp.route('/update_descriptions_bulk', methods=['POST'])
@login_required
def update_descriptions_bulk():
    """Bulk-update description rows from the Edit Desc modal. Optionally rewrite the cached HTML snapshot file in place (same job id)."""
    payload = request.get_json(silent=True) or {}
    updates = payload.get('updates') or []
    regenerate_rid = payload.get('regenerate_html_job_rid') or payload.get('regenerate_html_snapshot_rid')
    if isinstance(regenerate_rid, str):
        regenerate_rid = regenerate_rid.strip()

    connection = get_mysql_connection()

    if connection:
        try:
            cursor = connection.cursor()

            for update in updates:
                style = (update.get('style') or '').strip()
                desc_raw = update.get('description') or ''
                if not style or not str(desc_raw).strip():
                    continue
                description = str(desc_raw).strip().upper()

                # Try update first
                update_query = "UPDATE onlinedb.description SET description = %s WHERE style = %s"
                cursor.execute(update_query, (description, style))

                # If no rows were updated, insert new record
                if cursor.rowcount == 0:
                    insert_query = "INSERT INTO onlinedb.description (style, description) VALUES (%s, %s)"
                    cursor.execute(insert_query, (style, description))

            connection.commit()

            snapshot_refreshed = False
            refresh_error = None
            current_user = session.get('username')
            app = current_app._get_current_object()
            slug = (_listcreator_requested_storage_slug(current_user) if current_user else '') or ''

            if regenerate_rid and updates and _valid_listcreator_job_id(regenerate_rid) and current_user:
                meta_j = _listcreator_html_load_job(app, slug, regenerate_rid)
                if meta_j and meta_j.get('username') == current_user:
                    li = (meta_j.get('list_input') or '').strip()
                    if li:
                        ok_rw, rw_err = _listcreator_regenerate_snapshot_file(
                            app, slug, regenerate_rid, li, current_user, slack_notify=False
                        )
                        if ok_rw:
                            snapshot_refreshed = True
                        else:
                            refresh_error = rw_err or 'Rewrite failed'

            out = {
                'success': True,
                'message': 'Descriptions updated successfully',
                'snapshot_refreshed': snapshot_refreshed,
            }
            if refresh_error:
                out['snapshot_refresh_error'] = refresh_error
                out['message'] = (
                    'Descriptions saved, but rewriting the snapshot HTML file failed '
                    '(database is up to date). You can reopen this page or retry. '
                    f'Detail: {refresh_error}'
                )
            return jsonify(out)

        except Error as e:
            current_app.logger.error(f"Error updating descriptions: {e}")
            return jsonify({'success': False, 'message': str(e)})

        finally:
            cursor.close()
            connection.close()

    return jsonify({'success': False, 'message': 'Could not connect to database'})

@v7_listcreator_bp.route('/export_images', methods=['POST'])
def export_images():
    request_data = request.get_json()
    image_paths = request_data.get('imagePaths', [])
    image_size = request_data.get('imageSize')
    
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w') as zf:
        for style, img_buffer in _load_export_request_images(image_paths, image_size):
            zf.writestr(f"{style}.jpg", img_buffer.getvalue())
    
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'exported_images_{datetime.datetime.now().strftime("%Y%m%d")}.zip'
    )

def _resolve_listcreator_image_path(image_filename):
    """Resolve DAM / Dropbox / legacy paths for exports. Returns None if missing or placeholder (no image embedded)."""
    if not image_filename or 'no_image_available' in str(image_filename).lower():
        return None
    image_filename = str(image_filename).strip()
    image_path = None
    if os.path.isabs(image_filename) and os.path.exists(image_filename):
        image_path = image_filename
    elif '/image/' in image_filename:
        asset_id_match = re.search(r'/image/(\d+)/', image_filename)
        if asset_id_match:
            asset_id = int(asset_id_match.group(1))
            db = py.utilities.models.SessionLocal()
            try:
                asset = db.query(py.utilities.models.ImageAsset).filter_by(id=asset_id).first()
                image_path = asset.path if asset else None
            finally:
                db.close()
    elif '/list_image/' in image_filename:
        filename = re.sub(r'/list_image/', '', image_filename)
        db = py.utilities.models.SessionLocal()
        try:
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                filename=filename, is_active=True
            ).first()
            image_path = asset.path if asset else None
        finally:
            db.close()
    else:
        db = py.utilities.models.SessionLocal()
        try:
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                filename=image_filename, is_active=True
            ).first()
            image_path = asset.path if asset else None
        finally:
            db.close()

    if not image_path or not os.path.exists(image_path):
        current_app.logger.debug(f"No image for export cell: {image_filename}")
        return None
    return image_path


EXPORT_IMAGE_WORKERS = max(1, int(os.environ.get('V7_EXPORT_IMAGE_WORKERS', '8')))


def _export_parallel_map(fn, items):
    """Run fn over items on a thread pool inside the app context; results keep input order."""
    items = list(items)
    if len(items) <= 1 or EXPORT_IMAGE_WORKERS == 1:
        return [fn(x) for x in items]
    app = current_app._get_current_object()

    def run(x):
        with app.app_context():
            return fn(x)

    with ThreadPoolExecutor(max_workers=min(EXPORT_IMAGE_WORKERS, len(items))) as pool:
        return list(pool.map(run, items))


def _resolve_export_request_image_path(image_path):
    """Resolve an imagePaths[].path from the PDF / ZIP export requests to a file on disk."""
    full_path = None
    if '/image/' in image_path:
        asset_id_match = re.search(r'/image/(\d+)/', image_path)
        if asset_id_match:
            db = py.utilities.models.SessionLocal()
            try:
                asset = db.query(py.utilities.models.ImageAsset).filter_by(id=int(asset_id_match.group(1))).first()
                full_path = asset.path if asset else None
            finally:
                db.close()
    elif '/list_image/' in image_path:
        filename = re.sub(r'/list_image/', '', image_path)
        db = py.utilities.models.SessionLocal()
        try:
            asset = db.query(py.utilities.models.ImageAsset).filter_by(
                filename=filename, is_active=True
            ).first()
            full_path = asset.path if asset else None
        finally:
            db.close()
    else:
        full_path = image_path
    if full_path and os.path.exists(full_path):
        return full_path
    return None


def _load_export_request_images(image_paths, image_size):
    """Return [(style, BytesIO)] for PDF / ZIP exports, resized unless image_size is 'full'."""
    def load(item):
        full_path = _resolve_export_request_image_path(item['path'])
        if not full_path:
            return None
        if image_size != 'full':
            buf = resize_image(full_path, int(image_size))
        else:
            with open(full_path, 'rb') as img_file:
                buf = BytesIO(img_file.read())
        buf.seek(0)
        return item['style'], buf

    return [r for r in _export_parallel_map(load, image_paths) if r is not None]


def _populate_export_list_sheet(sheet, data, processing_image_size, removed_columns, embed_width=200, embed_height=150):
    """Fill one worksheet with the same layout as the standard Export List."""
    sheet.freeze_panes = 'A2'
    sheet.column_dimensions[get_column_letter(1)].width = 30
    sheet.column_dimensions[get_column_letter(2)].width = 12
    sheet.column_dimensions['B'].width = 15
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row, min_col=2, max_col=2):
        cell = row[0]
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    headers = ["Image", "Description", "Price", "Style", "Color", "Quantity"]
    if not removed_columns.get('ocean'):
        headers.append("Ocean")
    headers.append("Total")
    if not removed_columns.get('pcsBox'):
        headers.append("PCS/BOX")
    if not removed_columns.get('dimensions'):
        headers.append("Dimensions")

    sheet.append(headers)

    def prepare_image(item):
        image_fs = _resolve_listcreator_image_path(item['image'])
        if image_fs is None:
            return None
        if processing_image_size is not None:
            return resize_image(image_fs, processing_image_size)
        return image_fs

    display_images = _export_parallel_map(prepare_image, data)

    for item, display_image_src in zip(data, display_images):
        style = item['style']
        description = item['description']
        price = item['price']

        rows = item['rows']
        if not rows:
            row_data = ["", description, price, style, "", ""]
            if not removed_columns.get('ocean'):
                row_data.append("")
            row_data.append("")
            if not removed_columns.get('pcsBox'):
                row_data.append("")
            if not removed_columns.get('dimensions'):
                row_data.append("")
            sheet.append(row_data)

            if display_image_src is not None:
                img = OpenPyXLImage(display_image_src)
                img.width = embed_width
                img.height = embed_height
                sheet.add_image(img, f"A{sheet.max_row}")

            for _ in range(7):
                sheet.append([""] * len(headers))

            sheet.merge_cells(start_row=sheet.max_row - 7, start_column=1, end_row=sheet.max_row, end_column=1)
            sheet.merge_cells(start_row=sheet.max_row - 7, start_column=2, end_row=sheet.max_row, end_column=2)
            sheet.merge_cells(start_row=sheet.max_row - 7, start_column=3, end_row=sheet.max_row, end_column=3)
            for col in [2, 3]:
                for row_num in range(sheet.max_row - 7, sheet.max_row + 1):
                    sheet.cell(row=row_num, column=col).alignment = Alignment(horizontal='center', vertical='center')
        else:
            for index, row in enumerate(rows):
                quantity = int(float(row['quantity'])) if row['quantity'] else 0
                ocean = int(float(row['ocean'])) if row.get('ocean') else 0

                row_data = [
                    "",
                    description if index == 0 else "",
                    price if index == 0 else "",
                    style if index == 0 else "",
                    row['color'],
                    quantity
                ]
                if not removed_columns.get('ocean'):
                    row_data.append(ocean)
                row_data.append(quantity + ocean)
                if not removed_columns.get('pcsBox'):
                    row_data.append(int(float(row['pcsBox'])) if row.get('pcsBox') else 0)
                if not removed_columns.get('dimensions'):
                    row_data.append(row.get('dimensions', ''))
                    sheet.column_dimensions[get_column_letter(9)].width = 11

                sheet.append(row_data)

                if index == 0 and display_image_src is not None:
                    img = OpenPyXLImage(display_image_src)
                    img.width = embed_width
                    img.height = embed_height
                    sheet.add_image(img, f"A{sheet.max_row}")

            rows_mut = list(rows)
            while len(rows_mut) < 8:
                sheet.append([""] * len(headers))
                rows_mut.append({})

            merge_block = len(rows_mut)
            if merge_block > 8:
                sheet.merge_cells(start_row=sheet.max_row - merge_block + 1, start_column=1, end_row=sheet.max_row, end_column=1)
                sheet.merge_cells(start_row=sheet.max_row - merge_block + 1, start_column=2, end_row=sheet.max_row, end_column=2)
                sheet.merge_cells(start_row=sheet.max_row - merge_block + 1, start_column=3, end_row=sheet.max_row, end_column=3)
            else:
                sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=1, end_row=sheet.max_row, end_column=1)
                sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=2, end_row=sheet.max_row, end_column=2)
                sheet.merge_cells(start_row=sheet.max_row - 8 + 1, start_column=3, end_row=sheet.max_row, end_column=3)

            for col in [2, 3]:
                for row_num in range(sheet.max_row - merge_block + 1, sheet.max_row + 1):
                    sheet.cell(row=row_num, column=col).alignment = Alignment(horizontal='center', vertical='center')

    last_col = 'G'
    if not removed_columns.get('pcsBox'):
        last_col = 'H'
    if not removed_columns.get('dimensions'):
        last_col = 'J'
    if sheet.max_row >= 2:
        set_border(sheet, f'A2:{last_col}{sheet.max_row}')


@v7_listcreator_bp.route('/export_list', methods=['POST'])
def export_list():
    try:
        request_data = request.get_json()
        data = request_data.get('exportData', [])
        image_size_input = request_data.get('image_size', "800")
        processing_image_size = None if image_size_input == "full" else int(image_size_input)
        removed_columns = request_data.get('removedColumns', {})

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Exported Data"
        formatted_date = datetime.datetime.now().strftime("%Y-%m-%d")
        filename = f"exported_data_{formatted_date}.xlsx"

        _populate_export_list_sheet(sheet, data, processing_image_size, removed_columns)

        output = BytesIO()
        workbook.save(output)
        output.seek(0)

        return send_file(output, download_name=filename, as_attachment=True)

    except Exception as e:
        current_app.logger.error(f"Error in export_list: {e}")
        return jsonify({"error": "Internal Server Error"}), 500


@v7_listcreator_bp.route('/export_list_to_dropbox', methods=['POST'])
@login_required
def export_list_to_dropbox():
    """Write the same sheet as Export List to LOCAL_DROPBOX_DIR/<relative>/aaaa.xlsx when list_input is dropbox\\..."""
    try:
        request_data = request.get_json(force=True, silent=True) or {}
        list_input_raw = (request_data.get('list_input') or '').strip()
        dest_dir = _resolve_v7_export_dropbox_dir(list_input_raw)
        if not dest_dir:
            return jsonify({
                'ok': False,
                'error': 'Original run must use a Dropbox folder path (dropbox\\...) that exists on this server.',
            }), 400

        data = request_data.get('exportData', [])
        image_size_input = request_data.get('image_size', '800')
        if str(image_size_input).lower() == 'full':
            processing_image_size = None
        else:
            try:
                processing_image_size = int(image_size_input)
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'Invalid image size'}), 400
        removed_columns = request_data.get('removedColumns', {})

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Exported Data'
        _populate_export_list_sheet(sheet, data, processing_image_size, removed_columns)

        out_path = os.path.join(dest_dir, 'aaaa.xlsx')
        workbook.save(out_path)
        return jsonify({'ok': True, 'file': 'aaaa.xlsx'})
    except Exception as e:
        current_app.logger.exception('export_list_to_dropbox: %s', e)
        return jsonify({'ok': False, 'error': str(e) or 'Could not save file'}), 500


@v7_listcreator_bp.route('/export_workbook', methods=['POST'])
@v7_listcreator_bp.route('/export_avail_db', methods=['POST'])
@login_required
def export_workbook():
    """Queue workbook build on a background thread; download via GET .../download/<job_id> when ready."""
    try:
        request_data = request.get_json()
        if request_data is None:
            return jsonify({"error": "Invalid JSON body"}), 400

        current_user = session.get('username', 'export')
        app = current_app._get_current_object()
        slug = _workbook_user_slug(current_user)
        job_id, edition, jobs = _workbook_queue_job(app, slug, dict(request_data), current_user)
        return jsonify({
            'ok': True,
            'jobId': job_id,
            'edition': edition,
            'jobs': _workbook_jobs_public(jobs),
        })
    except Exception as e:
        current_app.logger.exception(f'Error queueing export_workbook: {e}')
        return jsonify({"error": "Could not queue export"}), 500


def _workbook_queue_job(app, slug, payload, current_user):
    """Add a running job to the slug's manifest (keeping the last WORKBOOK_EXPORT_HISTORY_MAX)
    and start the export thread. Returns (job_id, edition, jobs)."""
    _workbook_sweep_stale_running_jobs(app, slug)
    os.makedirs(os.path.join(app.instance_path, 'workbook_exports'), exist_ok=True)

    job_id = uuid.uuid4().hex
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'

    with _workbook_manifest_lock:
        manifest = _workbook_load_manifest(app, slug)
        jobs = manifest.get('jobs') or []
        while len(jobs) >= WORKBOOK_EXPORT_HISTORY_MAX:
            dropped = jobs.pop()
            _workbook_delete_disk_file(app, slug, dropped)
        edition = int(manifest.get('edition_counter', 0)) + 1
        manifest['edition_counter'] = edition
        jobs.insert(0, {
            'id': job_id,
            'status': 'running',
            'edition': edition,
            'created_at': now,
            'download_name': None,
            'error': None,
            'file': None,
        })
        manifest['jobs'] = jobs
        _workbook_save_manifest(app, slug, manifest)

    threading.Thread(
        target=_workbook_export_worker,
        args=(app, slug, job_id, payload, current_user),
        daemon=True,
        name=f'workbook-export-{job_id[:8]}',
    ).start()
    return job_id, edition, jobs


# secure_filename() strips leading underscores, so no username can map to this slug.
WEEKLY_WORKBOOK_SLUG = '_weekly_auto'
WEEKLY_WORKBOOK_USER = 'weekly-auto (Sunday 8 AM)'
WEEKLY_WORKBOOK_TZ = os.environ.get('V7_WEEKLY_WORKBOOK_TZ', 'America/Los_Angeles')
WEEKLY_WORKBOOK_HOUR = int(os.environ.get('V7_WEEKLY_WORKBOOK_HOUR', '8'))
_weekly_workbook_started = False


def _weekly_workbook_tick(app):
    now = datetime.datetime.now(ZoneInfo(WEEKLY_WORKBOOK_TZ))
    if now.weekday() != 6 or now.hour < WEEKLY_WORKBOOK_HOUR:
        return
    user_dir = _workbook_user_dir(app, WEEKLY_WORKBOOK_SLUG)
    claim = os.path.join(user_dir, f'claim-{now.date().isoformat()}')
    try:
        # O_EXCL claim: one run per Sunday even across restarts or a dev-reloader parent+child.
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return
    for name in os.listdir(user_dir):
        if name.startswith('claim-') and name != os.path.basename(claim):
            try:
                os.remove(os.path.join(user_dir, name))
            except OSError:
                pass
    payload = {'exportData': [], 'list_input': '', 'includeFullCatalog': True, 'removedColumns': {}}
    with app.app_context():
        job_id, edition, _ = _workbook_queue_job(app, WEEKLY_WORKBOOK_SLUG, payload, WEEKLY_WORKBOOK_USER)
        app.logger.info('weekly workbook export queued job_id=%s edition=%s', job_id, edition)


def start_weekly_workbook_scheduler(app):
    """Queue the "All styles with any stock" workbook every Sunday at WEEKLY_WORKBOOK_HOUR (local TZ)."""
    global _weekly_workbook_started
    if _weekly_workbook_started or os.environ.get('V7_WEEKLY_WORKBOOK_DISABLED', '').strip() in ('1', 'true'):
        return
    _weekly_workbook_started = True

    def _loop():
        while True:
            try:
                _weekly_workbook_tick(app)
            except Exception:
                app.logger.exception('weekly workbook scheduler tick failed')
            time.sleep(300)

    threading.Thread(target=_loop, daemon=True, name='v7-weekly-workbook').start()


@v7_listcreator_bp.route('/export_workbook/jobs', methods=['GET'])
@login_required
def export_workbook_jobs():
    current_user = session.get('username', 'export')
    app = current_app._get_current_object()
    slug = _workbook_user_slug(current_user)
    _workbook_sweep_stale_running_jobs(app, slug)
    _workbook_sweep_stale_running_jobs(app, WEEKLY_WORKBOOK_SLUG)
    manifest = _workbook_load_manifest(app, slug)
    weekly = _workbook_load_manifest(app, WEEKLY_WORKBOOK_SLUG)
    return jsonify({
        'jobs': _workbook_jobs_public(manifest.get('jobs') or []),
        'weekly_jobs': _workbook_jobs_public(weekly.get('jobs') or []),
    })


@v7_listcreator_bp.route('/export_workbook/download/<job_id>', methods=['GET'])
@login_required
def export_workbook_download(job_id):
    if not job_id or not re.match(r'^[a-f0-9]{32}$', job_id):
        return jsonify({'error': 'Invalid job'}), 400
    current_user = session.get('username', 'export')
    app = current_app._get_current_object()
    slug = _workbook_user_slug(current_user)
    manifest = _workbook_load_manifest(app, slug)
    job = next((j for j in manifest.get('jobs', []) if j.get('id') == job_id), None)
    if not job:
        slug = WEEKLY_WORKBOOK_SLUG
        manifest = _workbook_load_manifest(app, slug)
        job = next((j for j in manifest.get('jobs', []) if j.get('id') == job_id), None)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    if job.get('status') != 'ready':
        return jsonify({'error': 'File not ready'}), 400
    fn = job.get('file')
    dl = job.get('download_name') or 'list_creator_workbook.xlsx'
    if not fn:
        return jsonify({'error': 'Missing file'}), 404
    path = os.path.join(_workbook_user_dir(app, slug), fn)
    if not os.path.isfile(path):
        return jsonify({'error': 'File missing'}), 404
    return send_file(path, download_name=dl, as_attachment=True)


@v7_listcreator_bp.route('/export_pdf', methods=['POST'])
def export_pdf():
    request_data = request.get_json()
    image_paths = request_data.get('imagePaths', [])
    image_size = request_data.get('imageSize')
    
    output_buffer = BytesIO()
    loaded = _load_export_request_images(image_paths, image_size)
    image_data_list = [buf for _, buf in loaded]
    selected_images = [f"{style}.jpg" for style, _ in loaded]

    generate_catalog_pdf(image_data_list, output_buffer, image_size, selected_images)
    
    output_buffer.seek(0)
    return send_file(
        output_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'catalog_{datetime.datetime.now().strftime("%Y%m%d")}.pdf'
    )

def generate_catalog_pdf(image_data_list, output_buffer, image_size='full', selected_images=[]):
        c = canvas.Canvas(output_buffer, pagesize=landscape(letter))
        c.setTitle("Catalog Pictures")
        
        # Cover page
        c.setFont("Helvetica-Bold", 36)
        c.drawString(100, 550, "Catalog Pictures")
        c.setFont("Helvetica-Bold", 24)
        c.drawString(100, 500, "Product Catalog")
        c.setFont("Helvetica", 16)
        c.drawString(100, 450, f"Created Date: {datetime.datetime.now().strftime('%Y-%m-%d')}")
        c.drawString(100, 400, "Exclusive Access – Please Refrain from Sharing outside of your company")
        resolution_text = f"Image Resolution: {image_size}px" if image_size != 'full' else "Image Resolution: Original Size"
        c.drawString(100, 350, resolution_text)
        c.showPage()
        
        # Process each image and add style text
        for img_data, image_name in zip(image_data_list, selected_images):
            img = ImageReader(BytesIO(img_data.getvalue() if hasattr(img_data, 'getvalue') else img_data))
            img_width, img_height = img.getSize()
            aspect = img_height / float(img_width)
            
            max_width = 700
            width = max_width
            height = width * aspect
            
            x = (landscape(letter)[0] - width) / 2
            y = (landscape(letter)[1] - height) / 2 + 50
            
            c.drawImage(img, x, y, width=width, height=height)
            
            style_name = os.path.splitext(image_name)[0]
            c.setFont("Helvetica-Bold", 20)
            text_width = c.stringWidth(style_name, "Helvetica-Bold", 20)
            text_x = (landscape(letter)[0] - text_width) / 2
            text_y = y - 30
            c.drawString(text_x, text_y, style_name)
            
            c.showPage()
        
        c.save()


def get_sku_price(sku):
    """Query the MySQL database for the list price of a specific SKU."""
    connection = get_mysql_connection()
    if connection is None:
        return None
    
    try:
        cursor = connection.cursor(dictionary=True)
        query = "SELECT SKU, listprice FROM onlinedb.loris WHERE SKU = %s"
        cursor.execute(query, (sku,))
        result = cursor.fetchone()
        return result
    except Error as e:
        print(f"Error querying MySQL database: {e}")
        return None
    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()

def get_sku_sold_price(sku):
    """Query the SQL Server database for the sold price of a specific SKU using fuzzy matching."""
    try:
        connection = pyodbc.connect(f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={sql_server_config["database_fdc"]};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}')
        cursor = connection.cursor()
        # Use LIKE operator for fuzzy matching
        query = """
            SELECT TOP 1 ol.prod_cd, ol.unit_prs
            FROM ord_log ol
            JOIN customer c ON ol.cus_id = c.cus_id
            WHERE ol.prod_cd LIKE ? AND ol.cus_id = '00868'
            ORDER BY ol.olg_dt DESC
        """
        # Add wildcard for fuzzy matching
        search_pattern = f"%{sku}%"
        cursor.execute(query, (search_pattern,))
        row = cursor.fetchone()
        if row:
            return {"prod_cd": row[0], "unit_prs": row[1]}
        return None
    except pyodbc.Error as e:
        print(f"Error querying SQL Server database: {e}")
        return None
    finally:
        if 'connection' in locals() and connection:
            cursor.close()
            connection.close()
                    
@v7_listcreator_bp.route('/export_lori_format', methods=['POST'])
def export_lori_format():
    try:
        request_data = request.get_json()
        data = request_data.get('exportData', [])
        image_size_input = request_data.get('image_size', "800")
        processing_image_size = None if image_size_input == "full" else int(image_size_input)
        
        workbook = Workbook()
        # Create OTB sheet
        sheet = workbook.create_sheet("OTB", 0)
        sheet.append([])
        sheet.append([])
        # Add SUM formulas to cells N2, O2, and P2
        sheet['N2'] = "=SUM(N3:N523)"  # Sum of Available Units
        sheet['O2'] = "=SUM(O3:O525)"  # Sum of Ext Cost
        # Leave first two rows empty
        sheet['O2'].number_format = r'$#,##0.0_);[Red]($#,##0)'


        
        # Add headers on row 3 - Added new columns X, Y, Z for SKU, List Price, Sold Price
        headers = [
            "UPC or Item Code", "Image", "Vendor", "Description", "Cost", "Retail", "GM$", "", 
            "Sale Retail", "Event GM$", "Event GP%", "Proj Store Count", "Color", "Available Units",
            "Ext Retail", "Earliest Ship Date", "Terms", "Notes", "Case Pack", "Case Cost",
            "", "", "", "SKU", "List Price", "Sold Price"  # Added new columns
        ]
        sheet.append(headers)
        
        blue_fill = PatternFill(start_color="B8CCE4", end_color="B8CCE4", fill_type="solid")
        green_fill = PatternFill(start_color="C4D79B", end_color="C4D79B", fill_type="solid")
        tan_fill = PatternFill(start_color="F0DEAA", end_color="F0DEAA", fill_type="solid")
        
        # Apply colors to A3:D3 (blue)
        for col in range(1, 5):  # A to D
            sheet.cell(row=3, column=col).fill = blue_fill
            
        # Apply colors to E3:K3 (green)
        for col in range(5, 12):  # E to K
            sheet.cell(row=3, column=col).fill = green_fill
            
        # Apply colors to L3:O3 (tan)
        for col in range(12, 16):  # L to O
            sheet.cell(row=3, column=col).fill = tan_fill
            
        # Apply colors to P3:T3 (blue)
        for col in range(16, 21):  # P to T
            sheet.cell(row=3, column=col).fill = blue_fill

        # Set column widths
        sheet.column_dimensions['A'].width = 15  # UPC/Item Code
        sheet.column_dimensions['B'].width = 25  # Image
        sheet.column_dimensions['C'].width = 10  # Vendor
        sheet.column_dimensions['D'].width = 25  # Description
        sheet.column_dimensions['E'].width = 10  # Cost
        sheet.column_dimensions['F'].width = 10  # Retail
        sheet.column_dimensions['M'].width = 10  # Color
        sheet.column_dimensions['N'].width = 15  # Available Units
        sheet.column_dimensions['X'].width = 15  # SKU
        sheet.column_dimensions['Y'].width = 15  # List Price
        sheet.column_dimensions['Z'].width = 15  # Sold Price
        sheet.column_dimensions['R'].width = 30  # notes
        sheet.column_dimensions['o'].width = 15  # Case Pack
        
        # Set row height and apply text wrapping to header row
        sheet.row_dimensions[3].height = 39
        for col in range(1, len(headers) + 1):
            cell = sheet.cell(row=3, column=col)
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        
        # Current row counter (starting after headers)
        current_row = 4
        
        # Process each item
        for item in data:
            style = item['style']
            description = item['description']
            price_str = item['price'].replace('$', '').strip()
            price = float(price_str) if price_str and price_str != 'N/A' else 0
            image_filename = item['image']
            image_path = _resolve_listcreator_image_path(image_filename)
            if not image_path:
                current_app.logger.error(f"Image not found: {image_filename}")
                image_path = os.path.join('static', 'no_image_available.jpg')
                
            # Resize image if needed
            if processing_image_size is not None:
                image_buffer = resize_image(image_path, processing_image_size)
                img = OpenPyXLImage(image_buffer)
            else:
                img = OpenPyXLImage(image_path)
                
            # Set image dimensions
            img.width = 180
            img.height = 135
            
            # Get rows data or create empty if none
            rows = item['rows']
            if not rows:
                rows = [{"color": "N/A", "quantity": "0"}]
                
            # Ensure we have exactly 8 rows per style (pad with empty rows if needed)
            while len(rows) < 8:
                rows.append({"color": "", "quantity": "0"})
                
            # Process each row (up to 8)
            # Process each row (up to 8)
            for i, row in enumerate(rows[:8]):
                color = row.get('color', '')
                quantity = int(float(row.get('quantity', 0)))
                
                # Calculate extended retail
                ext_retail = quantity * price
                
                # Get SKU with color
                full_sku = f"{style}" if color else style
                
                # Get list price and sold price from MySQL and SQL Server
                list_price_info = get_sku_price(style)
                list_price = f"${list_price_info['listprice']}" if list_price_info and 'listprice' in list_price_info else "Not found"
                
                sold_price_info = get_sku_sold_price(style)
                sold_price = f"${sold_price_info['unit_prs']:.2f}" if sold_price_info and 'unit_prs' in sold_price_info else "No recent sales"
                
                # Determine if we should add "ordered before" to notes
                notes = ""
                if i == 0 and sold_price_info and 'unit_prs' in sold_price_info:
                    notes = "ordered before"
                
                # Create row data with raw values for numeric fields
                row_data = [
                    style if i == 0 else "",  # UPC/Item Code
                    "",  # Image placeholder
                    "FDC",  # Vendor
                    description if i == 0 else "",  # Description - fill if color is not empty
                    price if color else "",  # Cost - fill if color is not empty
                    "",  # Retail - leave empty as requested
                    f"=F{current_row}-E{current_row}" if color else "",  # GM$ formula: F(row#)-E(row#)
                    f"=G{current_row}/F{current_row}",  # Empty column
                    "",  # Sale Retail
                    "",  # Event GM$
                    f"=J{current_row}/I{current_row}",  # Event GP%
                    "",  # Proj Store Count
                    color,  # Color
                    quantity if color else "",  # Available Units
                    f"=N{current_row}*G{current_row}",  # Ext Cost - fill if color is not empty
                    "Fill it out",  # Earliest Ship Date
                    "Net 90",  # Terms
                    notes,  # Notes - "ordered before" if first row and has sold price
                    "",  # Case Pack
                    0,  # Case Cost
                    "",  # Empty column
                    "",  # Empty column
                    "",  # Empty column
                    full_sku,  # SKU with color
                    list_price,  # List Price
                    sold_price  # Sold Price
                ]
                
                # Add row to sheet
                sheet.append(row_data)
                
                # Add image to first row of each style
                if i == 0:
                    sheet.add_image(img, f"B{current_row}")
                
                # Apply cell formatting
                for col_idx, cell_value in enumerate(row_data, 1):
                    cell = sheet.cell(row=current_row, column=col_idx)
                    
                    # Center align all cells
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    
                    # Apply specific formatting based on column
                    if col_idx == 5 or col_idx == 6:  # Cost and Retail (E and F)
                        if isinstance(cell_value, (int, float)) and cell_value > 0:
                            cell.number_format = '"$ "#,##0.00'
                    
                    if col_idx == 7:  # GM$ (G)
                        cell.number_format = r'$#,##0.00_);[Red]($#,##0.00)'
                    elif col_idx == 15:  # Ext Retail (O)
                        cell.number_format = r'$#,##0.0_);[Red]($#,##0)'
                    
                    elif col_idx == 20:  # Case Cost (T)
                        if isinstance(cell_value, (int, float)):
                            cell.number_format = '"$"#,##0.00'
                
                current_row += 1
                
        # Apply borders to all cells
        thin_border = Border(
            left=Side(style='thin'), 
            right=Side(style='thin'), 
            top=Side(style='thin'), 
            bottom=Side(style='thin')
        )
        
        for row in sheet.iter_rows(min_row=3, max_row=sheet.max_row):
            for cell in row:
                cell.border = thin_border
                
        # Create output file
        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        
        return send_file(
            output, 
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"Lori_OTB_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
        )
        
    except Exception as e:
        current_app.logger.error(f"Error in export_lori_format: {e}")
        return jsonify({"error": str(e)}), 500

