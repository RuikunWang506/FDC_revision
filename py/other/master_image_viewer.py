from flask import Blueprint, render_template, request, jsonify, session, send_file, redirect, url_for, flash, make_response, current_app
import py.utilities.models
from py.utilities.setting import Config, login_required
from py.utilities.logging_setup import log_event
import paramiko
import io
import os
import json
import copy
import zipfile
import threading
import time
import uuid
import hashlib
import glob
import stat
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse
from sqlalchemy.exc import IntegrityError
from typing import Any, Dict, Optional

from py.utilities.customer_purchase_access import (
    FEATURE_MASTER_IMAGE_VIEWER,
    batch_all_skus_skipped_message,
    customer_purchase_notice,
    filter_skus_for_customer,
    filter_skus_for_customer_detailed,
    json_forbidden_if_customer_purchase_denied,
)
from py.utilities.master_image_viewer_permissions import master_image_viewer_hide_path

master_image_viewer_bp = Blueprint('master_image_viewer', __name__)

# TK/external user password (simple hardcoded for demo)
TK_PASSWORD = 'ucandoit'

# Global task status tracking for background China FTP fetches
background_tasks = {}
background_tasks_lock = threading.Lock()

_china_sku_fetch_locks = {}
_china_sku_fetch_guard = threading.Lock()
_china_sku_active_tasks = {}

BATCH_TASK_LOG_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'china_batch_logs')
)
os.makedirs(BATCH_TASK_LOG_DIR, exist_ok=True)

from py.utilities import china_ftp_cache as _china_ftp_cache

CHINA_DIR_SEARCH_MAX = _china_ftp_cache.CHINA_DIR_SEARCH_MAX


def _china_directory_is_yeti() -> bool:
    return (session.get('username') or '').strip().lower() == 'yeti'


def _china_directory_json_forbidden():
    if not _china_directory_is_yeti():
        return jsonify({'error': 'Access denied. Only yeti can use China FTP directory.'}), 403
    return None


def _miv_session_ok() -> bool:
    return bool(session.get('username')) or bool(session.get('tk_user'))


def _miv_json_unauthorized():
    if not _miv_session_ok():
        return jsonify({'error': 'Unauthorized'}), 403
    return None


def _china_dir_lookup_saved_images(filenames: list) -> dict:
    """Map filename -> preview info for files saved in big_picture (DB + disk)."""
    names = list({n for n in filenames if n})
    if not names:
        return {}

    from py.utilities.models import ImageAsset

    db = py.utilities.models.SessionLocal()
    try:
        rows = (
            db.query(ImageAsset.id, ImageAsset.filename, ImageAsset.path)
            .filter(
                ImageAsset.filename.in_(names),
                ImageAsset.source == 'big_picture',
                ImageAsset.is_active == True,
            )
            .all()
        )
    finally:
        db.close()

    out = {}
    for row in rows:
        path = row.path or ''
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            out[row.filename] = {
                'asset_id': row.id,
                'preview_url': f'/image/{row.id}/200',
            }
    return out


def _china_dir_enrich_downloaded(items: list) -> int:
    """Mark items whose filename exists in local big_picture storage."""
    image_items = [i for i in items if i.get('is_image')]
    lookup_names = set()
    derived_by_item = {}
    for item in image_items:
        name = item.get('name') or ''
        if name:
            lookup_names.add(name)
        sku = _china_ftp_cache.guess_sku_from_path(item.get('rel_path') or '')
        if sku:
            derived = _china_ftp_cache.derive_sync_filename(item, sku)
            derived_by_item[id(item)] = derived
            lookup_names.add(derived)

    saved = _china_dir_lookup_saved_images(list(lookup_names))
    downloaded_count = 0
    for item in image_items:
        name = item.get('name') or ''
        sku = _china_ftp_cache.guess_sku_from_path(item.get('rel_path') or '')
        derived = derived_by_item.get(id(item))
        if sku and _china_ftp_cache.is_generic_sync_filename(name, sku):
            hit = saved.get(derived) if derived else None
        else:
            hit = saved.get(name)
            if not hit and derived:
                hit = saved.get(derived)
        if hit:
            item['downloaded'] = True
            item['preview_url'] = hit['preview_url']
            item['asset_id'] = hit['asset_id']
            downloaded_count += 1
        else:
            item['downloaded'] = False
    return downloaded_count


def _china_ftp_list_directory(rel_path: str = '', force_refresh: bool = False) -> dict:
    payload = _china_ftp_cache.list_directory(rel_path, force_refresh=force_refresh)
    payload['downloaded_count'] = _china_dir_enrich_downloaded(payload['items'])
    payload['index_status'] = _china_ftp_cache.index_status()
    return payload


def _china_ftp_search(query: str, limit: int = CHINA_DIR_SEARCH_MAX) -> dict:
    payload = _china_ftp_cache.search(query, limit=limit)
    payload['downloaded_count'] = _china_dir_enrich_downloaded(payload.get('items') or [])
    payload['index_status'] = _china_ftp_cache.index_status()
    return payload


def _utc_now_iso():
    return datetime.utcnow().isoformat() + 'Z'


def _safe_write_json(file_path, payload):
    tmp_path = f"{file_path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def _persist_batch_task_snapshot(task_id, task_data):
    if not task_id or not isinstance(task_data, dict):
        return

    snapshot = copy.deepcopy(task_data)
    snapshot['task_id'] = task_id
    snapshot['saved_at'] = _utc_now_iso()

    task_file = os.path.join(BATCH_TASK_LOG_DIR, f'batch_task_{task_id}.json')
    latest_file = os.path.join(BATCH_TASK_LOG_DIR, 'latest_batch_task.json')

    try:
        _safe_write_json(task_file, snapshot)
        _safe_write_json(latest_file, snapshot)
    except Exception as e:
        log_event('error', f"Failed to persist batch task snapshot {task_id}: {str(e)}")


def _load_batch_task_snapshot(task_id):
    if not task_id:
        return None

    task_file = os.path.join(BATCH_TASK_LOG_DIR, f'batch_task_{task_id}.json')
    if not os.path.exists(task_file):
        return None

    try:
        with open(task_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log_event('error', f"Failed to load batch task snapshot {task_id}: {str(e)}")
        return None


def _load_latest_batch_snapshot():
    latest_file = os.path.join(BATCH_TASK_LOG_DIR, 'latest_batch_task.json')
    if not os.path.exists(latest_file):
        return None

    try:
        with open(latest_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log_event('error', f"Failed to load latest batch task snapshot: {str(e)}")
        return None


def _parse_batch_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _batch_snapshot_newest_ts(
    payload: Dict[str, Any], file_mtime_fallback: Optional[datetime]
) -> Optional[datetime]:
    best: Optional[datetime] = None
    for key in ("finished_at", "updated_at", "saved_at", "started_at"):
        dt = _parse_batch_iso_datetime(str(payload.get(key) or ""))
        if dt is not None and (best is None or dt > best):
            best = dt
    if best is not None:
        return best
    return file_mtime_fallback


def _batch_history_style_label(full: dict) -> str:
    """Short style label for batch history sidebar rows."""
    current_style = full.get("current_style") or {}
    sku = str(current_style.get("sku") or full.get("current_sku") or "").strip().upper()
    total = int(full.get("total") or 0)
    current_index = int(full.get("current_index") or 0)
    if sku:
        if total > 0 and current_index > 0:
            return f"{sku} ({current_index}/{total})"
        return sku
    skus = full.get("skus") or []
    if len(skus) == 1:
        return str(skus[0]).strip().upper()
    results = full.get("results") or []
    if len(results) == 1:
        only_sku = str((results[0] or {}).get("sku") or "").strip().upper()
        if only_sku:
            return only_sku
    if total == 1 and skus:
        return str(skus[0]).strip().upper()
    if total > 1:
        return f"{total} styles"
    return ""


def _list_batch_fetch_history(*, days: int, limit: Optional[int] = None) -> list:
    """Running + finished batch jobs from disk snapshots (recent) merged with in-memory tasks."""
    cutoff = datetime.utcnow() - timedelta(days=max(1, min(int(days), 90)))
    by_id: Dict[str, Dict[str, Any]] = {}
    pattern = os.path.join(BATCH_TASK_LOG_DIR, "batch_task_*.json")
    for path in glob.glob(pattern):
        try:
            mtime = datetime.utcfromtimestamp(os.path.getmtime(path))
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        tid = str(data.get("task_id") or "").strip()
        if not tid:
            base = os.path.basename(path)
            if base.startswith("batch_task_") and base.endswith(".json"):
                tid = base[len("batch_task_") : -len(".json")]
        if not tid:
            continue
        newest = _batch_snapshot_newest_ts(data, mtime)
        if newest is None or newest < cutoff:
            continue
        by_id[tid] = copy.deepcopy(data)

    with batch_tasks_lock:
        mem_items = [(str(tid), copy.deepcopy(task)) for tid, task in batch_tasks.items()]
    for tid, task in mem_items:
        newest = _batch_snapshot_newest_ts(task, None)
        if newest is None:
            newest = datetime.utcnow()
        running_like = _batch_top_status_running_like(str(task.get("status") or ""))
        if running_like or newest >= cutoff:
            by_id[tid] = task

    decorated: list = []
    for tid, full in by_id.items():
        full = copy.deepcopy(full)
        full["task_id"] = tid
        _annotate_batch_runtime_flags(tid, full)
        summ = full.get("summary") or {}
        newest_sort = _batch_snapshot_newest_ts(full, None) or datetime.min
        decorated.append(
            (
                newest_sort,
                {
                    "task_id": tid,
                    "status": full.get("status"),
                    "is_running": full.get("is_running"),
                    "interrupted": full.get("interrupted"),
                    "worker_alive": full.get("worker_alive"),
                    "started_at": full.get("started_at"),
                    "finished_at": full.get("finished_at"),
                    "updated_at": full.get("updated_at"),
                    "total": full.get("total"),
                    "completed": full.get("completed"),
                    "summary": {
                        "synced": summ.get("synced"),
                        "failed": summ.get("failed"),
                        "already_synced": summ.get("already_synced"),
                    },
                    "error": ((full.get("error") or "")[:500] if full.get("status") == "error" else None),
                    "style_label": _batch_history_style_label(full),
                },
            )
        )
    decorated.sort(key=lambda x: x[0], reverse=True)
    rows = [row for _, row in decorated]
    if limit is not None:
        rows = rows[: max(1, min(int(limit), 50))]
    return rows


def _audit_image_search(query_value: str) -> None:
    """Log image lookup with the user's actual input and caller context."""
    raw_input = (
        request.args.get("input")
        or request.args.get("q")
        or query_value
    ).strip()
    via = (request.args.get("via") or request.args.get("source") or "").strip()
    if not via and request.referrer:
        try:
            via = urlparse(request.referrer).path or ""
        except Exception:
            via = ""
    via_part = f" via={via}" if via else ""

    username = session.get("username")
    tk_user = session.get("tk_user", False)
    if username:
        log_event(
            "info",
            f"***{username.upper()}***__@@@searched images@@@ input={raw_input}{via_part}",
        )
    elif tk_user:
        log_event(
            "info",
            f"***TK_USER***__@@@searched images@@@ input={raw_input}{via_part}",
        )


def find_images_by_sku(sku):
    """Find all images related to a SKU using the DAM system, filesystem, and remote China FTP"""
    if not sku:
        return {'error': 'SKU is required'}
    
    sku_upper = sku.upper()
    
    try:
        db = py.utilities.models.SessionLocal()
        
        # Get all active assets matching the SKU pattern (like product_image does)
        assets = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.sku.ilike(f'%{sku_upper}%'),
            py.utilities.models.ImageAsset.is_active == True
        ).order_by(py.utilities.models.ImageAsset.sku, py.utilities.models.ImageAsset.version).all()
        
        # Group assets by source - use actual sources from DAM
        grouped_images = {
            'dam_dropbox': [],
            'dam_catalog': [],  # This includes catalog images from item_lookup
            'dam_big_picture': [],  # This includes product images from product_image
            'filesystem_missing': [],  # Images found in filesystem but not in DAM
            'dam_other': [],
            'china_ftp': []
        }
        
        import os
        dimensions_updated = False
        for asset in assets:
            # Skip if file does not exist (only for local paths, not remote China FTP paths)
            if hasattr(asset, 'path') and asset.path:
                # Don't skip remote paths (Windows UNC paths like D:\... from China FTP server)
                # Only skip if it looks like a local path and doesn't exist
                is_remote = asset.path.startswith('\\\\') or (len(asset.path) > 1 and asset.path[1] == ':' and asset.source == 'big_picture')
                if not is_remote and not os.path.exists(asset.path):
                    continue

            if (
                getattr(asset, 'source', None) == 'big_picture'
                and (asset.width is None or asset.height is None)
                and asset.path
                and os.path.exists(asset.path)
            ):
                w, h = _read_local_image_dimensions(asset.path)
                if w and h:
                    asset.width = w
                    asset.height = h
                    dimensions_updated = True
                    if not getattr(asset, 'file_size', None):
                        try:
                            asset.file_size = os.path.getsize(asset.path)
                        except OSError:
                            pass
            # Extract directory information for better organization
            directory = None
            relative_path = None
            if hasattr(asset, 'path') and asset.path:
                directory = os.path.dirname(asset.path)
                # Get relative path from common base directories
                common_bases = [
                    r'C:\Users\Stream\Dropbox\Pictures-China (1)',
                    r'C:\users\stream\Dropbox\Pictures-China (1)',
                    r'\\192.168.1.8\public'
                ]
                for base in common_bases:
                    if asset.path.startswith(base):
                        relative_path = os.path.relpath(asset.path, base)
                        break
                if not relative_path and directory:
                    relative_path = os.path.basename(directory)

            # Use China FTP streaming endpoint when big_picture path is remote/non-local.
            use_china_ftp_endpoint = False
            if getattr(asset, 'source', None) == 'big_picture' and getattr(asset, 'path', None):
                path_value = asset.path
                is_windows_like = path_value.startswith('\\\\') or (len(path_value) > 1 and path_value[1] == ':')
                if is_windows_like and not os.path.exists(path_value):
                    use_china_ftp_endpoint = True
            
            asset_info = {
                'id': asset.id,
                'sku': asset.sku,
                'version': asset.version,
                'filename': asset.filename,
                'source': asset.source,
                'width': asset.width,
                'height': asset.height,
                'is_canonical': asset.is_canonical,
                'created_at': asset.created_at.isoformat() if asset.created_at else None,
                'url': f'/china_ftp_image/{asset.id}/800' if use_china_ftp_endpoint else f'/image/{asset.id}/800',
                'full_url': f'/china_ftp_image/{asset.id}/2000' if use_china_ftp_endpoint else f'/image/{asset.id}/2000',
                'original_url': f'/china_ftp_image/{asset.id}/0' if use_china_ftp_endpoint else f'/image/{asset.id}/0',
                'directory': directory,
                'relative_path': relative_path,
                'file_path': asset.path if hasattr(asset, 'path') else None,
                'file_size': asset.file_size if hasattr(asset, 'file_size') else None
            }
            source = asset.source.lower() if asset.source else 'unknown'
            if source == 'dropbox':
                grouped_images['dam_dropbox'].append(asset_info)
            elif source == 'catalog' or source == 'catalog_original':
                grouped_images['dam_catalog'].append(asset_info)
            elif source == 'big_picture':
                grouped_images['dam_big_picture'].append(asset_info)
            else:
                grouped_images['dam_other'].append(asset_info)

        if dimensions_updated:
            db.commit()
        
        # Calculate totals
        total_count = len(assets)
        grouped_images['total_count'] = total_count
        grouped_images['sku'] = sku_upper
        
        return grouped_images
        
    except Exception as e:
        return {'error': f'Database error: {str(e)}'}
    finally:
        db.close()

# TK password entry (popup form POST)
@master_image_viewer_bp.route('/tk_password', methods=['POST'])
def tk_password():
    password = request.form.get('password')
    if password == TK_PASSWORD:
        session['tk_user'] = True
        session['internal_user'] = False
        flash('Access granted for TK user.', 'info')
        return redirect(url_for('master_image_viewer.master_image_viewer'))
    else:
        flash('Incorrect password. Please try again.', 'danger')
        return redirect(url_for('master_image_viewer.master_image_viewer'))


def _china_directory_page_context():
    from py.other.clientview import is_client_portal_only_user

    current_user = session.get('username')
    show_navbar = bool(current_user) and not is_client_portal_only_user(current_user)
    return {
        'authenticated': True,
        'current_user': current_user,
        'show_navbar': show_navbar,
        'initial_query': request.args.get('q', ''),
        'index_status': _china_ftp_cache.index_status(),
        'rebuild_status': _china_index_rebuild_status(),
    }


@master_image_viewer_bp.route('/master_image_viewer/chinadirectory', methods=['GET'])
@login_required
def china_directory():
    if not _china_directory_is_yeti():
        flash('Access denied. Only yeti can view China FTP directory.', 'warning')
        return redirect(url_for('master_image_viewer.master_image_viewer'))
    ctx = _china_directory_page_context()
    return render_template('china_directory.html', **ctx)


@master_image_viewer_bp.route('/api/china_directory/browse')
@login_required
def api_china_directory_browse():
    denied = _china_directory_json_forbidden()
    if denied:
        return denied
    rel_path = request.args.get('path', '')
    force_refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
    try:
        payload = _china_ftp_list_directory(rel_path, force_refresh=force_refresh)
        return jsonify(payload)
    except ValueError:
        return jsonify({'error': 'Invalid path'}), 400
    except Exception as e:
        log_event('error', f"China directory browse error: {str(e)}")
        return jsonify({'error': f'Browse failed: {str(e)}'}), 500


_china_index_rebuild_lock = threading.Lock()
_china_index_rebuild_state = {
    'running': False,
    'started_at': None,
    'finished_at': None,
    'result': None,
    'error': None,
    'progress': None,
}


def _china_index_rebuild_progress_update(progress: dict) -> None:
    with _china_index_rebuild_lock:
        _china_index_rebuild_state['progress'] = dict(progress)


def _china_index_rebuild_status() -> dict:
    with _china_index_rebuild_lock:
        status = dict(_china_index_rebuild_state)
    status['index'] = _china_ftp_cache.index_status()
    if status.get('running') and not status.get('progress') and status.get('started_at'):
        estimated = _china_ftp_cache.estimate_walk_progress(status['started_at'])
        if estimated:
            status['progress'] = estimated
    return status


def _background_rebuild_china_index(triggered_by: str = 'unknown'):
    global _china_index_rebuild_state
    try:
        year_label = _china_ftp_cache.current_year_label()
        log_event('info', f'China FTP index rebuild started by {triggered_by} (scope: {year_label})')

        def _log(msg: str) -> None:
            log_event('info', f'China FTP index: {msg}')

        result = _china_ftp_cache.build_full_index(_log, _china_index_rebuild_progress_update)
        with _china_index_rebuild_lock:
            _china_index_rebuild_state['result'] = result
            _china_index_rebuild_state['error'] = None
        log_event(
            'info',
            f"China FTP index rebuild done: entries={result.get('entry_count')} "
            f"folders={result.get('folder_count')} built_at={result.get('built_at')}",
        )
    except Exception as exc:
        with _china_index_rebuild_lock:
            _china_index_rebuild_state['error'] = str(exc)
            _china_index_rebuild_state['result'] = None
        log_event('error', f'China FTP index rebuild failed: {exc}')
    finally:
        with _china_index_rebuild_lock:
            _china_index_rebuild_state['running'] = False
            _china_index_rebuild_state['finished_at'] = datetime.utcnow().isoformat() + 'Z'
            _china_index_rebuild_state['progress'] = None


@master_image_viewer_bp.route('/api/china_directory/index_status')
@login_required
def api_china_directory_index_status():
    denied = _china_directory_json_forbidden()
    if denied:
        return denied
    return jsonify(_china_index_rebuild_status())


@master_image_viewer_bp.route('/api/china_directory/rebuild_index', methods=['POST'])
@login_required
def api_china_directory_rebuild_index():
    denied = _china_directory_json_forbidden()
    if denied:
        return denied

    with _china_index_rebuild_lock:
        if _china_index_rebuild_state['running']:
            return jsonify({
                'success': False,
                'message': 'Index rebuild already in progress',
                **_china_index_rebuild_status(),
            }), 409

        _china_index_rebuild_state['running'] = True
        _china_index_rebuild_state['started_at'] = datetime.utcnow().isoformat() + 'Z'
        _china_index_rebuild_state['finished_at'] = None
        _china_index_rebuild_state['result'] = None
        _china_index_rebuild_state['error'] = None
        _china_index_rebuild_state['progress'] = None

    triggered_by = (session.get('username') or 'unknown').strip()
    thread = threading.Thread(
        target=_background_rebuild_china_index,
        kwargs={'triggered_by': triggered_by},
        daemon=True,
    )
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Index rebuild started in background',
        **_china_index_rebuild_status(),
    })


@master_image_viewer_bp.route('/api/china_directory/search')
@login_required
def api_china_directory_search():
    denied = _china_directory_json_forbidden()
    if denied:
        return denied
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Query parameter q is required'}), 400
    try:
        payload = _china_ftp_search(query)
        return jsonify(payload)
    except Exception as e:
        log_event('error', f"China directory search error for {query}: {str(e)}")
        return jsonify({'error': f'Search failed: {str(e)}'}), 500


# Main page for master image viewer
@master_image_viewer_bp.route("/master_image_viewer/<string:sku>", methods=["GET"])
@master_image_viewer_bp.route('/master_image_viewer', methods=['GET'])
def master_image_viewer(sku=None):
    # Internal user: must be logged in
    current_user = session.get('username')
    internal_user = bool(current_user)
    # If TK/external user, restrict session
    tk_user = session.get('tk_user', False)
    if not internal_user and not tk_user:
        # Show password popup via template logic
        return render_template(
            'master_image_viewer.html',
            initial_sku=sku,
            hide_image_path=False,
        )
    # If TK user, restrict access to only this page
    if tk_user:
        session['internal_user'] = False
    else:
        session['internal_user'] = True
    from py.other.clientview import is_client_portal_only_user

    notice = customer_purchase_notice(current_user, FEATURE_MASTER_IMAGE_VIEWER)
    show_navbar = bool(current_user) and not is_client_portal_only_user(current_user)
    hide_path = master_image_viewer_hide_path(current_user)
    return render_template(
        'master_image_viewer.html',
        initial_sku=sku,
        current_user=current_user,
        customer_purchase_notice=notice,
        show_navbar=show_navbar,
        hide_image_path=hide_path,
    )
# Image serving is now handled by the DAM system at /image/{asset_id}/{size}

# --- ZIP download endpoint for TK/external users ---
import tempfile
import requests

@master_image_viewer_bp.route('/download_zip', methods=['POST'])
def download_zip():
    # Allow both internal and TK/external users
    data = request.get_json(force=True, silent=True)
    if not (session.get('tk_user') or session.get('username')):
        current_app.logger.error("Unauthorized access attempt to /download_zip")
        return make_response('Unauthorized', 403)
    images = data.get('images', []) if data else []
    if not images:
        current_app.logger.error("No images provided in /download_zip payload")
        return make_response('No images provided', 400)
    # Download images to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        file_paths = []
        for idx, img in enumerate(images):
            # Use file_path if available (works for both internal and TK users)
            if img.get('file_path') and os.path.exists(img['file_path']):
                fpath = img['file_path']
                fname = img['filename']
                dest_path = os.path.join(tmpdir, fname)
                try:
                    with open(fpath, 'rb') as src, open(dest_path, 'wb') as dst:
                        dst.write(src.read())
                    file_paths.append(dest_path)
                except Exception as e:
                    current_app.logger.error(f"Exception copying {fpath}: {str(e)}")
                    continue
            # Fallback: fetch via HTTP (for China FTP or other remote images)
            elif img.get('original_url') and (img['original_url'].startswith('/image/') or img['original_url'].startswith('/china_ftp_image')):
                full_url = request.host_url.rstrip('/') + img['original_url']
                try:
                    resp = requests.get(full_url, stream=True)
                    if resp.status_code == 200:
                        fname = img['filename']
                        fpath = os.path.join(tmpdir, fname)
                        with open(fpath, 'wb') as f:
                            for chunk in resp.iter_content(4096):
                                f.write(chunk)
                        file_paths.append(fpath)
                    else:
                        current_app.logger.error(f"Failed to download {full_url}: status {resp.status_code}")
                except Exception as e:
                    current_app.logger.error(f"Exception downloading {full_url}: {str(e)}")
                    continue
            else:
                current_app.logger.error(f"Invalid image entry: {img}")
                continue
        # Create zip
        zip_path = os.path.join(tmpdir, 'FDC_images.zip')
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for fpath in file_paths:
                zipf.write(fpath, arcname=os.path.basename(fpath))
        # Send zip file (read into memory to avoid Windows temp file lock)
        with open(zip_path, 'rb') as f:
            zip_bytes = f.read()
        return send_file(
            io.BytesIO(zip_bytes),
            mimetype='application/zip',
            as_attachment=True,
            download_name='FDC_images.zip'
        )

@master_image_viewer_bp.route('/api/search_images')
def search_images():
    """API endpoint to search for images by SKU"""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400

    username = session.get('username')
    denied = json_forbidden_if_customer_purchase_denied(
        username, sku, FEATURE_MASTER_IMAGE_VIEWER
    )
    if denied:
        return denied
    
    _audit_image_search(sku)

    try:
        results = find_images_by_sku(sku)
        return jsonify(results)
    except Exception as e:
        log_event('error', f"Master Image Viewer API Error for SKU {sku}: {str(e)}")
        return jsonify({'error': f'Search failed: {str(e)}'}), 500

@master_image_viewer_bp.route('/api/fetch_china_ftp')
def fetch_china_ftp():
    """API endpoint to fetch China FTP images for a SKU"""
    sku = request.args.get('sku', '').strip()
    
    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400

    username = session.get('username')
    denied = json_forbidden_if_customer_purchase_denied(
        username, sku, FEATURE_MASTER_IMAGE_VIEWER
    )
    if denied:
        return denied
    
    # Log the fetch - use actual username if logged in, otherwise tk_user
    tk_user = session.get('tk_user', False)
    
    if username:
        log_event('info', f"***{username.upper()}***__@@@fetched China FTP images for SKU@@@__ {sku}")
    elif tk_user:
        log_event('info', f"***TK_USER***__@@@fetched China FTP images for SKU@@@__ {sku}")
    
    sku_upper = sku.upper()
    
    try:
        china_ftp_images, source = _list_china_ftp_images_for_sku(sku_upper)
        for asset_info in china_ftp_images:
            from urllib.parse import quote
            encoded_path = quote(asset_info['file_path'])
            asset_info['url'] = f'/china_ftp_image?path={encoded_path}&size=800'
            asset_info['full_url'] = f'/china_ftp_image?path={encoded_path}&size=2000'
            asset_info['original_url'] = f'/china_ftp_image?path={encoded_path}&size=0'
        return jsonify({
            'china_ftp': china_ftp_images,
            'source': source,
            'year': _china_ftp_cache.current_year_label(),
            'sync_filter': _china_ftp_cache.sync_fetch_filter_info(),
        })
    except Exception as e:
        log_event('error', f"Fetch China FTP API Error for SKU {sku}: {str(e)}")
        return jsonify({'error': f'Fetch failed: {str(e)}'}), 500


CHINA_FTP_SYNC_WORKERS = _china_ftp_cache.CHINA_FTP_DOWNLOAD_WORKERS
CHINA_FTP_CONNECT_FAILURE_ABORT = _china_ftp_cache.CHINA_FTP_CONNECT_FAILURE_ABORT


def _acquire_china_sku_fetch_lock(sku_upper: str):
    """Return a held lock for this SKU, or None if a fetch is already running."""
    with _china_sku_fetch_guard:
        lock = _china_sku_fetch_locks.get(sku_upper)
        if lock is None:
            lock = threading.Lock()
            _china_sku_fetch_locks[sku_upper] = lock
        if not lock.acquire(blocking=False):
            return None
        return lock


def _release_china_sku_fetch_lock(sku_upper: str, lock) -> None:
    if lock is None:
        return
    with _china_sku_fetch_guard:
        _china_sku_active_tasks.pop(sku_upper, None)
    try:
        lock.release()
    except RuntimeError:
        pass


def _load_existing_big_picture_filenames(sku_upper: str) -> dict:
    db = py.utilities.models.SessionLocal()
    try:
        existing_assets = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.sku == sku_upper,
            py.utilities.models.ImageAsset.source == 'big_picture',
            py.utilities.models.ImageAsset.is_active == True
        ).all()
        return {asset.filename: True for asset in existing_assets}
    finally:
        db.close()


def _list_china_ftp_images_for_sku(sku_upper: str) -> tuple:
    """List China FTP images for fetch/sync: current year, white-bg folders only."""
    entries, source = _china_ftp_cache.find_sku_files(
        sku_upper,
        year_only=True,
        sync_only=True,
    )
    existing_big_picture = _load_existing_big_picture_filenames(sku_upper)
    images = []
    for entry in entries:
        filename = _china_ftp_cache.derive_sync_filename(entry, sku_upper)
        asset_info = _china_ftp_cache.sku_entry_to_asset_info(
            entry,
            sku_upper,
            already_synced=filename in existing_big_picture,
        )
        images.append(asset_info)
    return images, source


def _read_local_image_dimensions(local_path):
    """Return (width, height) from a local image file, or (None, None) on failure."""
    try:
        from PIL import Image
        with Image.open(local_path) as img:
            return img.size
    except Exception as e:
        log_event('warning', f"Could not read image dimensions for {local_path}: {e}")
        return None, None


def _china_ftp_sftp_get(remote_path, local_path):
    _china_ftp_cache.download_file(remote_path, local_path)


def _sync_one_china_ftp_image(image, sku_folder):
    """Download one China FTP file and register it in big_picture. Returns (filename, status, conn_error)."""
    from py.utilities.models import ImageAsset

    filename = image['filename']
    remote_path = image.get('file_path')
    remote_name = image.get('remote_filename') or filename
    local_path = os.path.join(sku_folder, filename)

    if not _china_ftp_cache.is_sync_fetch_file(remote_name):
        return filename, 'skipped', False

    try:
        _china_ftp_sftp_get(remote_path, local_path)

        if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            log_event('error', f"Failed to download China FTP image {filename} - file missing or empty after download")
            return filename, 'failed', False

        width, height = _read_local_image_dimensions(local_path)
        file_size = os.path.getsize(local_path)

        db_session = py.utilities.models.SessionLocal()
        try:
            existing = db_session.query(ImageAsset).filter(
                ImageAsset.sku == image['sku'],
                ImageAsset.filename == image['filename'],
                ImageAsset.source == 'big_picture'
            ).first()

            if not existing:
                sha256_hash = hashlib.sha256()
                with open(local_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''):
                        sha256_hash.update(chunk)

                new_asset = ImageAsset(
                    sku=image['sku'],
                    source='big_picture',
                    filename=image['filename'],
                    path=local_path,
                    sha256=sha256_hash.hexdigest(),
                    version=1,
                    width=width,
                    height=height,
                    file_size=file_size,
                    is_canonical=False,
                    is_active=True
                )
                db_session.add(new_asset)
                db_session.commit()
            else:
                updated = False
                if 'china_ftp_downloads' in (existing.path or '').replace('/', '\\'):
                    existing.path = local_path
                    updated = True
                if width and height and (not existing.width or not existing.height):
                    existing.width = width
                    existing.height = height
                    updated = True
                if file_size and not existing.file_size:
                    existing.file_size = file_size
                    updated = True
                if updated:
                    db_session.commit()
            return filename, 'synced', False
        except IntegrityError:
            db_session.rollback()
            return filename, 'synced', False
        except Exception as e:
            log_event('error', f"Failed to sync image {filename} to database: {str(e)}")
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except Exception:
                    pass
            return filename, 'failed', False
        finally:
            db_session.close()
    except Exception as e:
        conn_error = _china_ftp_cache.is_connection_error(e)
        log_event('error', f"Failed to download China FTP image {filename}: {str(e)}")
        return filename, 'failed', conn_error


def _china_ftp_sync_images(images_to_sync, sku_folder, task_updater=None):
    """Download and register China FTP images with pooled parallel SFTP workers."""
    workers = max(1, int(_china_ftp_cache.CHINA_FTP_DOWNLOAD_WORKERS or 1))
    total = len(images_to_sync)
    if total == 0:
        return 0, 0, False, None

    state_lock = threading.Lock()
    synced_count = 0
    failed_count = 0
    consecutive_conn_failures = 0
    aborted = False
    abort_reason = None
    completed_count = 0
    in_flight: Dict[int, str] = {}
    in_flight_started: Dict[int, float] = {}
    stop_heartbeat = threading.Event()

    def _pulse_progress(force_elapsed=None):
        if not callable(task_updater):
            return
        with state_lock:
            active = list(in_flight.items())
            synced = synced_count
            failed = failed_count
            done = completed_count
            is_aborted = aborted
            reason = abort_reason
        if active:
            active.sort(key=lambda item: item[0])
            primary_idx, primary_name = active[0]
            started_at = in_flight_started.get(primary_idx, time.time())
            elapsed = force_elapsed if force_elapsed is not None else int(time.time() - started_at)
            extra = len(active) - 1
            filename = primary_name
            if extra > 0:
                filename = f"{primary_name} (+{extra} more)"
            sync_index = done
        else:
            elapsed = force_elapsed or 0
            filename = None
            sync_index = done

        payload = {
            'status': 'syncing',
            'current_sync_index': sync_index,
            'current_filename': filename,
            'sync_phase': 'downloading' if active else 'done',
            'synced': synced,
            'failed': failed,
            'download_workers': workers,
            'download_in_flight': len(active),
        }
        if elapsed:
            payload['download_elapsed_sec'] = elapsed
        if is_aborted:
            payload['aborted'] = True
            payload['abort_reason'] = reason
        task_updater(payload)

    def _heartbeat():
        while not stop_heartbeat.wait(5):
            with state_lock:
                if not in_flight or aborted:
                    continue
            _pulse_progress()

    def _record_result(idx, filename, result, conn_error, elapsed_sec):
        nonlocal synced_count, failed_count, consecutive_conn_failures
        nonlocal aborted, abort_reason, completed_count
        with state_lock:
            completed_count += 1
            if result == 'synced':
                synced_count += 1
                consecutive_conn_failures = 0
            elif result == 'skipped':
                pass
            else:
                failed_count += 1
                if conn_error:
                    consecutive_conn_failures += 1
                    if consecutive_conn_failures >= CHINA_FTP_CONNECT_FAILURE_ABORT:
                        aborted = True
                        abort_reason = (
                            f'Aborted after {consecutive_conn_failures} consecutive connection failures'
                        )
                        log_event('error', f'China FTP sync aborting: {abort_reason}')
                else:
                    consecutive_conn_failures = 0
            done = completed_count
            synced = synced_count
            failed = failed_count
            is_aborted = aborted
            reason = abort_reason

        if callable(task_updater):
            task_updater({
                'current_sync_index': done,
                'current_filename': filename,
                'sync_phase': 'done' if result == 'synced' else 'failed',
                'synced': synced,
                'failed': failed,
                'aborted': is_aborted,
                'abort_reason': reason,
                'download_elapsed_sec': elapsed_sec,
                'download_workers': workers,
            })

    def _process_one(idx, image):
        with state_lock:
            if aborted:
                return idx, image.get('filename', 'unknown'), 'failed', True, 0
        filename = image.get('filename', 'unknown')
        with state_lock:
            in_flight[idx] = filename
            in_flight_started[idx] = time.time()
        started = time.time()
        try:
            filename, result, conn_error = _sync_one_china_ftp_image(image, sku_folder)
            elapsed = int(time.time() - started)
            return idx, filename, result, conn_error, elapsed
        except Exception as exc:
            elapsed = int(time.time() - started)
            conn_error = _china_ftp_cache.is_connection_error(exc)
            log_event('error', f'China FTP sync error for {filename}: {exc}')
            return idx, filename, 'failed', conn_error, elapsed
        finally:
            with state_lock:
                in_flight.pop(idx, None)
                in_flight_started.pop(idx, None)

    heartbeat = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat.start()
    _pulse_progress(0)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_process_one, idx, image): idx
                for idx, image in enumerate(images_to_sync, start=1)
            }
            for future in as_completed(futures):
                with state_lock:
                    if aborted and completed_count >= synced_count + failed_count:
                        pass
                try:
                    idx, filename, result, conn_error, elapsed = future.result()
                except Exception as exc:
                    idx = futures[future]
                    filename = images_to_sync[idx - 1].get('filename', 'unknown')
                    result = 'failed'
                    conn_error = _china_ftp_cache.is_connection_error(exc)
                    elapsed = 0
                    log_event('error', f'China FTP worker failed for {filename}: {exc}')

                _record_result(idx, filename, result, conn_error, elapsed)

                with state_lock:
                    should_abort = aborted
                if should_abort:
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=1)

    with state_lock:
        if aborted:
            remaining = total - completed_count
            if remaining > 0:
                failed_count += remaining
        synced = synced_count
        failed = failed_count
        is_aborted = aborted
        reason = abort_reason

    return synced, failed, is_aborted, reason


def background_china_ftp_fetch(task_id, sku, username=None, tk_user=False, progress_callback=None):
    """Background task to fetch and sync China FTP images."""

    def _notify_progress():
        if not callable(progress_callback):
            return
        try:
            with background_tasks_lock:
                snapshot = dict(background_tasks.get(task_id, {}))
            progress_callback(snapshot)
        except Exception as cb_error:
            log_event('error', f"Progress callback error for task {task_id}: {str(cb_error)}")

    def _update_task(fields):
        with background_tasks_lock:
            task = background_tasks.setdefault(task_id, {})
            task.update(fields)
            task['updated_at'] = _utc_now_iso()
        _notify_progress()

    try:
        _update_task({
            'status': 'fetching',
            'current': 0,
            'total': 0,
            'to_sync': 0,
            'current_sync_index': 0,
            'current_filename': None,
            'synced': 0,
            'failed': 0,
            'already_synced': 0,
            'error': None,
            'started_at': _utc_now_iso(),
            'finished_at': None
        })

        sku_upper = sku.upper()
        sku_lock = _acquire_china_sku_fetch_lock(sku_upper)
        if sku_lock is None:
            with _china_sku_fetch_guard:
                existing_task_id = _china_sku_active_tasks.get(sku_upper)
            _update_task({
                'status': 'skipped',
                'error': f'Fetch already running for SKU {sku_upper}',
                'existing_task_id': existing_task_id,
                'finished_at': _utc_now_iso(),
            })
            return

        with _china_sku_fetch_guard:
            _china_sku_active_tasks[sku_upper] = task_id

        try:
            year_label = _china_ftp_cache.current_year_label()
            try:
                china_ftp_images, list_source = _list_china_ftp_images_for_sku(sku_upper)
            except Exception as e:
                log_event('error', f"China FTP fetch error for SKU {sku_upper}: {str(e)}")
                _update_task({'status': 'error', 'error': str(e), 'finished_at': _utc_now_iso()})
                return

            _update_task({
                'total': len(china_ftp_images),
                'current': len(china_ftp_images),
                'list_source': list_source,
                'year_scope': year_label,
                'sync_filter': _china_ftp_cache.sync_fetch_filter_info(),
            })

            images_to_sync = [img for img in china_ftp_images if not img['already_synced']]
            already_synced_count = len(china_ftp_images) - len(images_to_sync)

            _update_task({
                'status': 'syncing',
                'to_sync': len(images_to_sync),
                'already_synced': already_synced_count,
                'synced': 0,
                'failed': 0,
                'current_sync_index': 0,
                'current_filename': None,
            })

            big_picture_base = Config.IMAGE_DAM_BIG_PICTURE_DIR
            sku_folder = os.path.join(big_picture_base, sku_upper)
            os.makedirs(sku_folder, exist_ok=True)

            synced_count, failed_count, aborted, abort_reason = _china_ftp_sync_images(
                images_to_sync,
                sku_folder,
                task_updater=_update_task,
            )

            final_status = 'completed'
            if aborted:
                final_status = 'error'
            _update_task({
                'status': final_status,
                'synced': synced_count,
                'failed': failed_count,
                'aborted': aborted,
                'abort_reason': abort_reason,
                'finished_at': _utc_now_iso(),
            })

            if username:
                log_event(
                    'info',
                    f"***{username.upper()}***__@@@completed background China FTP fetch for SKU@@@__ "
                    f"{sku_upper} ({year_label}: Synced: {synced_count}, Failed: {failed_count})",
                )
            elif tk_user:
                log_event(
                    'info',
                    f"***TK_USER***__@@@completed background China FTP fetch for SKU@@@__ "
                    f"{sku_upper} ({year_label}: Synced: {synced_count}, Failed: {failed_count})",
                )
        finally:
            _release_china_sku_fetch_lock(sku_upper, sku_lock)

    except Exception as e:
        _update_task({'status': 'error', 'error': str(e), 'finished_at': _utc_now_iso()})
        log_event('error', f"Background China FTP fetch error for task {task_id}: {str(e)}")


@master_image_viewer_bp.route('/api/fetch_china_ftp_async', methods=['POST'])
def fetch_china_ftp_async():
    """Start a single-SKU China FTP fetch tracked as a batch job."""
    sku = request.args.get('sku', '').strip()

    if not sku:
        return jsonify({'error': 'SKU parameter is required'}), 400

    username = session.get('username')
    raw_skus = [sku.strip()]
    allowed_skus, skipped = filter_skus_for_customer_detailed(
        username, raw_skus, FEATURE_MASTER_IMAGE_VIEWER,
    )
    if not allowed_skus:
        msg = batch_all_skus_skipped_message(skipped)
        return jsonify(
            {
                'error': msg,
                'success': False,
                'skipped': skipped,
            }
        ), 403

    tk_user = session.get('tk_user', False)
    task_id = str(uuid.uuid4())[:8]
    app_obj = current_app._get_current_object()

    thread = threading.Thread(
        target=background_batch_china_ftp_fetch,
        args=(task_id, allowed_skus, username, tk_user, app_obj, skipped),
    )
    thread.daemon = True
    thread.start()

    return jsonify(
        {
            'task_id': task_id,
            'sku': allowed_skus[0],
            'batch': True,
            'started_at': _utc_now_iso(),
            'skipped': skipped,
            'started_count': len(allowed_skus),
            'skipped_count': len(skipped),
        }
    )


@master_image_viewer_bp.route('/api/china_ftp_task_status')
def china_ftp_task_status():
    """Get the status of a background China FTP fetch task"""
    task_id = request.args.get('task_id', '').strip()
    
    if not task_id:
        return jsonify({'error': 'task_id parameter is required'}), 400
    
    with background_tasks_lock:
        status = background_tasks.get(task_id)
    
    if not status:
        return jsonify({'error': 'Task not found'}), 404
    
    return jsonify(status)


# Global storage for batch tasks
batch_tasks = {}
batch_tasks_lock = threading.Lock()
BATCH_LIVE_LOG_MAX = 150
_batch_persist_last_at: Dict[str, float] = {}


def _persist_batch_task_throttled(task_id: str, *, force: bool = False) -> None:
    """Persist batch snapshot at most every 2s unless forced."""
    if not task_id:
        return
    now = time.time()
    if not force:
        last = _batch_persist_last_at.get(task_id, 0.0)
        if (now - last) < 2.0:
            return
    _batch_persist_last_at[task_id] = now
    _persist_batch_task_by_id(task_id)


def _append_batch_live_log(
    task_id: str,
    message: str,
    *,
    level: str = 'info',
    key: Optional[str] = None,
) -> None:
    """Append a timestamped line to the batch task live log (deduped by optional key)."""
    if not task_id or not message:
        return
    entry = {
        'ts': _utc_now_iso(),
        'level': level,
        'message': message,
    }
    with batch_tasks_lock:
        task = batch_tasks.get(task_id)
        if not task:
            return
        if key and task.get('_live_log_last_key') == key:
            return
        if key:
            task['_live_log_last_key'] = key
        log = task.setdefault('live_log', [])
        log.append(entry)
        if len(log) > BATCH_LIVE_LOG_MAX:
            task['live_log'] = log[-BATCH_LIVE_LOG_MAX:]
        task['updated_at'] = _utc_now_iso()


def _normalized_batch_sku_list(skus) -> list:
    """Uppercase non-empty SKUs in order (for persistence + rerun)."""
    out: list = []
    for s in skus or []:
        u = str(s).strip().upper()
        if u:
            out.append(u)
    return out


def _slack_notify_batch_china_completed(
    *,
    task_id: str,
    username: Optional[str],
    tk_user: bool,
    task_dict: Dict[str, Any],
    app,
) -> None:
    """Same Slack destination as V7 List Creator (#v7-list_creator_bot webhook)."""
    try:
        from flask import url_for
        from py.utilities.slack_notify import notify_v7_list_creator_optional, public_origin

        summ = task_dict.get("summary") or {}
        synced = int(summ.get("synced") or 0)
        failed = int(summ.get("failed") or 0)
        already = int(summ.get("already_synced") or 0)
        who = (username or "").strip() or ("TK_USER" if tk_user else "unknown")
        style_list = _normalized_batch_sku_list(task_dict.get("skus"))
        if not style_list:
            raw_results = task_dict.get("results") or []
            style_list = _normalized_batch_sku_list(
                [r.get("sku") for r in raw_results if isinstance(r, dict)]
            )
        n_styles = len(style_list) if style_list else int(task_dict.get("total") or 0)
        slack_styles_max = 3400
        styles_display = ", ".join(style_list) if style_list else "(none recorded)"
        if len(styles_display) > slack_styles_max:
            styles_display = styles_display[: slack_styles_max - 20] + "\n… (truncated)"
        chunks = [
            "[Master Image Viewer] China batch FTP fetch completed",
            f"User: {who}",
            f"Task id: {task_id}",
            f"Styles ({n_styles}): {styles_display}",
            f"Summary — synced: {synced}, failed: {failed}, already synced: {already}",
        ]
        with app.app_context():
            origin = public_origin()
            try:
                open_path = url_for("master_image_viewer.master_image_viewer")
                if origin and open_path:
                    chunks.append(f"Open: {origin.rstrip('/')}{open_path}")
            except Exception:
                pass
            notify_v7_list_creator_optional("\n".join(chunks) + "\n")
    except Exception as exc:
        log_event("warning", f"Slack batch-complete notify skipped: {exc}")


def _batch_top_status_running_like(status: Optional[str]) -> bool:
    """True if persisted status looks like an in-flight batch (worker may or may not still exist)."""
    s = (status or "").strip().lower()
    return s in ("running", "fetching", "syncing")


def _annotate_batch_runtime_flags(task_id: str, payload: Dict[str, Any]) -> None:
    """
    Set is_running / interrupted / worker_alive so the UI can tell:
    - live worker vs stale snapshot (e.g. after app restart)
    - completed / error terminal states
    """
    if not isinstance(payload, dict):
        return
    tid = (task_id or str(payload.get("task_id") or "")).strip()
    st = payload.get("status")
    running_like = _batch_top_status_running_like(str(st) if st is not None else "")
    with batch_tasks_lock:
        in_memory = bool(tid and tid in batch_tasks)
    payload["worker_alive"] = bool(in_memory and running_like)
    if running_like and not in_memory:
        payload["interrupted"] = True
        payload["is_running"] = False
    elif running_like and in_memory:
        payload["interrupted"] = False
        payload["is_running"] = True
    else:
        payload["interrupted"] = False
        payload["is_running"] = False


def _persist_batch_task_by_id(task_id):
    with batch_tasks_lock:
        task_snapshot = copy.deepcopy(batch_tasks.get(task_id))
    if task_snapshot:
        _persist_batch_task_snapshot(task_id, task_snapshot)


def _sort_task_candidates(candidates):
    def sort_key(item):
        task = item[1]
        return task.get('updated_at') or task.get('started_at') or ''

    return sorted(candidates, key=sort_key, reverse=True)


def background_batch_china_ftp_fetch(
    task_id, skus, username=None, tk_user=False, app_obj=None, initial_skipped=None
):
    """Background task to fetch and sync China FTP images for multiple SKUs."""
    sku_list = _normalized_batch_sku_list(skus)
    pre_skipped = list(initial_skipped or [])
    try:
        started_at = _utc_now_iso()
        pre_results = []
        for item in pre_skipped:
            sku_key = str(item.get("sku") or "").strip().upper()
            pre_results.append(
                {
                    "sku": sku_key,
                    "count": 0,
                    "synced": 0,
                    "already_synced": 0,
                    "total_found": 0,
                    "to_sync": 0,
                    "failed": 0,
                    "status": "skipped",
                    "reason": item.get("reason"),
                    "error": item.get("message"),
                    "finished_at": started_at,
                }
            )
        with batch_tasks_lock:
            batch_tasks[task_id] = {
                'status': 'running',
                'total': len(sku_list) + len(pre_skipped),
                'completed': len(pre_skipped),
                'results': pre_results,
                'skipped_prefilter': pre_skipped,
                'error': None,
                'current_index': 0,
                'current_sku': None,
                'current_stage': 'starting',
                'current_style': None,
                'live_log': [],
                'started_at': started_at,
                'updated_at': started_at,
                'finished_at': None,
                'skus': list(sku_list),
                'summary': {
                    'synced': 0,
                    'failed': 0,
                    'already_synced': 0
                }
            }
        _persist_batch_task_by_id(task_id)
        _append_batch_live_log(
            task_id,
            f"Batch started — {len(sku_list)} style(s) to fetch",
            key='batch-start',
        )

        for idx, sku in enumerate(sku_list, start=1):
            sku_upper = str(sku).strip().upper()

            with batch_tasks_lock:
                task = batch_tasks[task_id]
                task['current_index'] = idx
                task['current_sku'] = sku_upper if sku_upper else None
                task['current_stage'] = 'preparing'
                task['current_style'] = {
                    'sku': sku_upper,
                    'status': 'preparing',
                    'current': 0,
                    'total': 0,
                    'to_sync': 0,
                    'synced': 0,
                    'failed': 0,
                    'already_synced': 0
                }
                task['updated_at'] = _utc_now_iso()
            _append_batch_live_log(
                task_id,
                f"[{idx}/{len(sku_list)}] Starting {sku_upper}…",
                key=f'style-start-{sku_upper}',
            )
            _persist_batch_task_by_id(task_id)

            if not sku_upper:
                with batch_tasks_lock:
                    task = batch_tasks[task_id]
                    task['results'].append({
                        'sku': '',
                        'count': 0,
                        'synced': 0,
                        'already_synced': 0,
                        'total_found': 0,
                        'to_sync': 0,
                        'failed': 0,
                        'status': 'skipped'
                    })
                    task['completed'] += 1
                    task['current_stage'] = 'skipped'
                    task['updated_at'] = _utc_now_iso()
                _persist_batch_task_by_id(task_id)
                continue

            single_task_id = f"{task_id}_{idx}"

            def _single_progress_callback(single_status):
                with batch_tasks_lock:
                    task = batch_tasks.get(task_id)
                    if not task:
                        return
                    task['current_index'] = idx
                    task['current_sku'] = sku_upper
                    task['current_stage'] = single_status.get('status', 'running')
                    task['current_style'] = {
                        'sku': sku_upper,
                        'status': single_status.get('status', 'running'),
                        'current': int(single_status.get('current_sync_index', 0) or 0),
                        'total': int(single_status.get('total', 0) or 0),
                        'to_sync': int(single_status.get('to_sync', 0) or 0),
                        'synced': int(single_status.get('synced', 0) or 0),
                        'failed': int(single_status.get('failed', 0) or 0),
                        'already_synced': int(single_status.get('already_synced', 0) or 0),
                        'current_filename': single_status.get('current_filename'),
                        'sync_phase': single_status.get('sync_phase'),
                        'download_elapsed_sec': single_status.get('download_elapsed_sec'),
                        'download_workers': single_status.get('download_workers'),
                        'download_in_flight': single_status.get('download_in_flight'),
                    }
                    task['updated_at'] = _utc_now_iso()

                st = (single_status.get('status') or '').lower()
                total_found = int(single_status.get('total', 0) or 0)
                to_sync = int(single_status.get('to_sync', 0) or 0)
                already = int(single_status.get('already_synced', 0) or 0)
                step = int(single_status.get('current_sync_index', 0) or 0)
                synced = int(single_status.get('synced', 0) or 0)
                failed = int(single_status.get('failed', 0) or 0)
                filename = single_status.get('current_filename') or ''
                sync_phase = (single_status.get('sync_phase') or '').lower()

                if st == 'fetching':
                    _append_batch_live_log(
                        task_id,
                        f"{sku_upper} — scanning China FTP ({single_status.get('year_scope') or '2026'} white-bg)…",
                        key=f'{sku_upper}-fetching',
                    )
                elif st == 'syncing' and to_sync >= 0 and step == 0 and not filename and not sync_phase:
                    src = single_status.get('list_source') or 'index'
                    _append_batch_live_log(
                        task_id,
                        (
                            f"{sku_upper} — found {total_found} image(s) via {src}; "
                            f"{to_sync} to sync, {already} already on disk"
                        ),
                        key=f'{sku_upper}-sync-start',
                    )
                elif sync_phase == 'downloading' and filename:
                    elapsed = int(single_status.get('download_elapsed_sec') or 0)
                    if elapsed <= 0:
                        msg = f"{sku_upper} — downloading {step + 1}/{to_sync}: {filename}"
                        key = f'{sku_upper}-dl-{step + 1}-{filename}'
                    else:
                        msg = f"{sku_upper} — still downloading ({elapsed}s): {filename}"
                        key = f'{sku_upper}-dl-wait-{step + 1}-{elapsed // 10}'
                    _append_batch_live_log(task_id, msg, key=key)
                elif filename and sync_phase in ('done', 'failed'):
                    _append_batch_live_log(
                        task_id,
                        (
                            f"{sku_upper} — {step}/{to_sync} | "
                            f"synced {synced}, failed {failed} | {filename}"
                        ),
                        key=f'{sku_upper}-file-{step}-{filename}',
                    )
                elif single_status.get('aborted'):
                    _append_batch_live_log(
                        task_id,
                        f"{sku_upper} — aborted: {single_status.get('abort_reason') or 'connection errors'}",
                        level='error',
                        key=f'{sku_upper}-aborted',
                    )

                _persist_batch_task_throttled(task_id)

            try:
                background_china_ftp_fetch(
                    single_task_id,
                    sku_upper,
                    username,
                    tk_user,
                    progress_callback=_single_progress_callback
                )
                with background_tasks_lock:
                    single_status = dict(background_tasks.get(single_task_id, {}))

                single_result_status = single_status.get('status', 'completed')
                display_status = 'completed' if single_result_status == 'completed' else 'failed'

                synced = int(single_status.get('synced', 0) or 0)
                failed = int(single_status.get('failed', 0) or 0)
                already_synced = int(single_status.get('already_synced', 0) or 0)
                total_found = int(single_status.get('total', 0) or 0)
                to_sync = int(single_status.get('to_sync', 0) or 0)

                with batch_tasks_lock:
                    task = batch_tasks[task_id]
                    task['results'].append({
                        'sku': sku_upper,
                        'count': synced,
                        'synced': synced,
                        'failed': failed,
                        'already_synced': already_synced,
                        'total_found': total_found,
                        'to_sync': to_sync,
                        'status': display_status,
                        'raw_status': single_result_status,
                        'finished_at': _utc_now_iso()
                    })
                    task['completed'] += 1
                    task['summary']['synced'] += synced
                    task['summary']['failed'] += failed
                    task['summary']['already_synced'] += already_synced
                    task['current_stage'] = display_status
                    task['updated_at'] = _utc_now_iso()
                _append_batch_live_log(
                    task_id,
                    (
                        f"{sku_upper} — done ({display_status}): synced {synced}, "
                        f"failed {failed}, already {already_synced}"
                    ),
                    level='error' if display_status != 'completed' else 'info',
                    key=f'{sku_upper}-done',
                )
                _persist_batch_task_throttled(task_id, force=True)
            except Exception as e:
                with batch_tasks_lock:
                    task = batch_tasks[task_id]
                    task['results'].append({
                        'sku': sku_upper,
                        'count': 0,
                        'synced': 0,
                        'failed': 0,
                        'status': 'failed',
                        'error': str(e),
                        'finished_at': _utc_now_iso()
                    })
                    task['completed'] += 1
                    task['current_stage'] = 'failed'
                    task['updated_at'] = _utc_now_iso()
                _persist_batch_task_throttled(task_id, force=True)
            finally:
                with background_tasks_lock:
                    background_tasks.pop(single_task_id, None)

        with batch_tasks_lock:
            batch_tasks[task_id]['status'] = 'completed'
            batch_tasks[task_id]['current_stage'] = 'completed'
            batch_tasks[task_id]['finished_at'] = _utc_now_iso()
            batch_tasks[task_id]['updated_at'] = _utc_now_iso()
            summ = dict((batch_tasks.get(task_id) or {}).get('summary') or {})
        _append_batch_live_log(
            task_id,
            (
                f"Batch complete — synced {int(summ.get('synced') or 0)}, "
                f"failed {int(summ.get('failed') or 0)}, "
                f"already {int(summ.get('already_synced') or 0)}"
            ),
            key='batch-complete',
        )
        _persist_batch_task_throttled(task_id, force=True)

        snap_for_slack: Optional[Dict[str, Any]] = None
        with batch_tasks_lock:
            snap_for_slack = copy.deepcopy(batch_tasks.get(task_id))
        if app_obj and snap_for_slack:
            _slack_notify_batch_china_completed(
                task_id=task_id,
                username=username,
                tk_user=tk_user,
                task_dict=snap_for_slack,
                app=app_obj,
            )

        if username:
            log_event('info', f"***{username.upper()}***__@@@completed batch China FTP fetch@@@__ task:{task_id} styles:{len(sku_list)}")
        elif tk_user:
            log_event('info', f"***TK_USER***__@@@completed batch China FTP fetch@@@__ task:{task_id} styles:{len(sku_list)}")
    except Exception as e:
        with batch_tasks_lock:
            if task_id not in batch_tasks:
                batch_tasks[task_id] = {
                    'status': 'error',
                    'total': len(sku_list),
                    'completed': 0,
                    'results': [],
                    'error': str(e),
                    'skus': list(sku_list),
                    'started_at': _utc_now_iso(),
                    'updated_at': _utc_now_iso(),
                    'finished_at': _utc_now_iso()
                }
            else:
                batch_tasks[task_id]['status'] = 'error'
                batch_tasks[task_id]['error'] = str(e)
                batch_tasks[task_id]['finished_at'] = _utc_now_iso()
                batch_tasks[task_id]['updated_at'] = _utc_now_iso()
                batch_tasks[task_id].setdefault('skus', list(sku_list))
        _persist_batch_task_by_id(task_id)
        log_event('error', f"Batch China FTP fetch failed for task {task_id}: {str(e)}")


@master_image_viewer_bp.route('/api/batch_fetch_china_ftp', methods=['POST'])
def batch_fetch_china_ftp():
    """Start background batch fetch for multiple SKUs."""
    data = request.get_json(silent=True) or {}
    skus = data.get('skus', [])

    if not isinstance(skus, list) or len(skus) == 0:
        return jsonify({'error': 'No SKUs provided'}), 400

    username = session.get('username')
    raw_skus = [str(s).strip() for s in skus if str(s).strip()]
    allowed_skus, skipped = filter_skus_for_customer_detailed(
        username, raw_skus, FEATURE_MASTER_IMAGE_VIEWER,
    )
    if not allowed_skus:
        msg = batch_all_skus_skipped_message(skipped)
        return jsonify(
            {
                'error': msg,
                'success': False,
                'skipped': skipped,
            }
        ), 403
    skus = allowed_skus

    tk_user = session.get('tk_user', False)
    task_id = str(uuid.uuid4())[:8]
    app_obj = current_app._get_current_object()

    thread = threading.Thread(
        target=background_batch_china_ftp_fetch,
        args=(task_id, skus, username, tk_user, app_obj, skipped)
    )
    thread.daemon = True
    thread.start()

    return jsonify(
        {
            'task_id': task_id,
            'started_at': _utc_now_iso(),
            'skipped': skipped,
            'started_count': len(skus),
            'skipped_count': len(skipped),
        }
    )


@master_image_viewer_bp.route('/api/batch_fetch_status')
def batch_fetch_status():
    """Get status of batch fetch task."""
    task_id = request.args.get('task_id', '').strip()

    if not task_id:
        return jsonify({'error': 'task_id parameter is required'}), 400

    with batch_tasks_lock:
        status = copy.deepcopy(batch_tasks.get(task_id))

    if not status:
        status = _load_batch_task_snapshot(task_id)

    if not status:
        return jsonify({'error': 'Task not found'}), 404

    status['task_id'] = task_id
    _annotate_batch_runtime_flags(task_id, status)
    return jsonify(status)


@master_image_viewer_bp.route('/api/batch_fetch_latest')
def batch_fetch_latest():
    """Get latest running task, or the latest saved batch task snapshot."""
    with batch_tasks_lock:
        task_items = [(tid, copy.deepcopy(task)) for tid, task in batch_tasks.items()]

    running_candidates = [
        item for item in task_items
        if item[1].get('status') in ('running', 'fetching', 'syncing')
    ]

    if running_candidates:
        latest_id, latest_task = _sort_task_candidates(running_candidates)[0]
        latest_task['task_id'] = latest_id
        _annotate_batch_runtime_flags(latest_id, latest_task)
        return jsonify(latest_task)

    if task_items:
        latest_id, latest_task = _sort_task_candidates(task_items)[0]
        latest_task['task_id'] = latest_id
        _annotate_batch_runtime_flags(latest_id, latest_task)
        return jsonify(latest_task)

    latest_snapshot = _load_latest_batch_snapshot()
    if latest_snapshot:
        snap_id = str(latest_snapshot.get('task_id') or '')
        _annotate_batch_runtime_flags(snap_id, latest_snapshot)
        return jsonify(latest_snapshot)

    return jsonify({'status': 'none', 'is_running': False, 'interrupted': False, 'worker_alive': False})


@master_image_viewer_bp.route('/api/batch_fetch_history')
def batch_fetch_history():
    """List China batch FTP jobs from disk + in-memory (optional day window and limit)."""
    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 90))
    limit_raw = request.args.get('limit')
    limit = None
    if limit_raw is not None and str(limit_raw).strip() != '':
        try:
            limit = max(1, min(int(limit_raw), 50))
        except (TypeError, ValueError):
            limit = 10
    cutoff = datetime.utcnow() - timedelta(days=days)
    tasks = _list_batch_fetch_history(days=days, limit=limit)
    return jsonify(
        {
            "ok": True,
            "days": days,
            "limit": limit,
            "cutoff_utc": cutoff.isoformat() + "Z",
            "tasks": tasks,
        }
    )


@master_image_viewer_bp.route('/api/delete_images', methods=['POST'])
def delete_images():
    """API endpoint to batch delete images from catalog (database)."""
    # Only allow logged in internal users (not tk_user)
    username = session.get('username')
    tk_user = session.get('tk_user', False)

    # Allow if user has a username AND is not using tk_user password
    if not username:
        return jsonify({'error': 'Unauthorized - must be logged in'}), 403

    # If user only used ucandoit password (tk_user=True, no username), deny
    if tk_user and not username:
        return jsonify({'error': 'Unauthorized - TK users cannot delete images'}), 403

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    images = data.get('images', [])
    if not images:
        return jsonify({'error': 'No images provided'}), 400

    db = None
    try:
        db = py.utilities.models.SessionLocal()
        deleted_count = 0
        failed_images = []

        for img in images:
            asset_id = img.get('id')
            filename = img.get('filename', 'unknown')

            # Skip images without an ID (like china_ftp images)
            if not asset_id:
                failed_images.append({'filename': filename, 'reason': 'No database ID (external image)'})
                continue

            try:
                # Find the asset by ID
                asset = db.query(py.utilities.models.ImageAsset).filter_by(
                    id=asset_id,
                    is_active=True
                ).first()

                if not asset:
                    failed_images.append({'filename': filename, 'reason': 'Asset not found or already deleted'})
                    continue

                # Soft delete: mark as inactive and set deleted_at timestamp
                from datetime import datetime
                asset.is_active = False
                asset.deleted_at = datetime.utcnow()

                deleted_count += 1
                log_event('info', f"***{username.upper()}***__@@@deleted image@@@__ {filename} (ID: {asset_id})")

            except Exception as e:
                failed_images.append({'filename': filename, 'reason': str(e)})
                current_app.logger.error(f"Error deleting image {filename} (ID: {asset_id}): {e}")

        db.commit()

        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'failed_images': failed_images,
            'message': f'Successfully deleted {deleted_count} image(s)'
        }), 200

    except Exception as e:
        if db is not None:
            db.rollback()
        current_app.logger.error(f"Error in batch delete: {e}")
        return jsonify({'error': f'Batch delete failed: {str(e)}'}), 500
    finally:
        if db is not None:
            db.close()

@master_image_viewer_bp.route('/china_ftp_image/<int:asset_id>/<int:width>')
def china_ftp_image_by_id(asset_id, width):
    """Serve China FTP images by asset ID with optional resizing"""
    try:
        db = py.utilities.models.SessionLocal()
        asset = db.query(py.utilities.models.ImageAsset).filter_by(
            id=asset_id,
            is_active=True
        ).first()

        if not asset:
            return jsonify({'error': 'Asset not found'}), 404

        # Check authentication - allow public access for big_picture sources
        current_user = session.get('username')
        tk_user = session.get('tk_user', False)
        
        if asset.source != 'big_picture':
            if not current_user and not tk_user:
                return jsonify({'error': 'Unauthorized'}), 403

        remote_path = asset.path
        if not remote_path:
            return jsonify({'error': 'Image path not found'}), 404

        try:
            # SFTP connection using paramiko
            transport = paramiko.Transport(('219.135.198.130', 22))
            transport.connect(username='fdc', password='Bst@93109310')
            sftp = paramiko.SFTPClient.from_transport(transport)

            # Download to memory
            buffer = io.BytesIO()
            sftp.getfo(remote_path, buffer)
            buffer.seek(0)
            sftp.close()
            transport.close()

            # If width=0, serve original image
            if width == 0:
                filename = os.path.basename(remote_path).lower()
                if filename.endswith('.png'):
                    mimetype = 'image/png'
                elif filename.endswith('.gif'):
                    mimetype = 'image/gif'
                elif filename.endswith('.bmp'):
                    mimetype = 'image/bmp'
                elif filename.endswith('.webp'):
                    mimetype = 'image/webp'
                else:
                    mimetype = 'image/jpeg'
                
                return send_file(buffer, mimetype=mimetype)
            
            # Resize image on demand
            try:
                from PIL import Image
                img = Image.open(buffer)
                orig_width, orig_height = img.size
                new_height = int((width / orig_width) * orig_height)  # Maintain aspect ratio
                resized_img = img.resize((width, new_height), Image.LANCZOS)
                resized_buffer = io.BytesIO()
                resized_img.save(resized_buffer, format='JPEG')
                resized_buffer.seek(0)
                return send_file(resized_buffer, mimetype='image/jpeg')
            except Exception as e:
                current_app.logger.warning(f"Failed to resize China FTP image {asset_id}: {str(e)}, serving original")
                buffer.seek(0)
                return send_file(buffer, mimetype='image/jpeg')

        except Exception as e:
            current_app.logger.error(f"Failed to fetch China FTP image {asset_id}: {str(e)}")
            return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 500

    except Exception as e:
        current_app.logger.error(f"Error serving China FTP image {asset_id}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        db.close()


@master_image_viewer_bp.route('/china_ftp_image')
def china_ftp_image():
    """Serve images from remote China FTP via SFTP"""
    remote_path = request.args.get('path')
    size = request.args.get('size', '0')  # Kept for URL compatibility
    
    if not remote_path:
        current_app.logger.error("China FTP image: No path provided")
        return jsonify({'error': 'Path required'}), 400
    
    try:
        # URL decode the path
        from urllib.parse import unquote
        remote_path = unquote(remote_path)

        # SFTP connection using paramiko
        transport = paramiko.Transport(('219.135.198.130', 22))
        transport.connect(username='fdc', password='Bst@93109310')
        sftp = paramiko.SFTPClient.from_transport(transport)

        # Download to memory and stream directly (no local cache/output files).
        buffer = io.BytesIO()
        sftp.getfo(remote_path, buffer)
        buffer.seek(0)
        sftp.close()
        transport.close()

        # Determine mimetype based on file extension
        filename = os.path.basename(remote_path).lower()
        if filename.endswith('.png'):
            mimetype = 'image/png'
        elif filename.endswith('.gif'):
            mimetype = 'image/gif'
        elif filename.endswith('.bmp'):
            mimetype = 'image/bmp'
        elif filename.endswith('.webp'):
            mimetype = 'image/webp'
        else:
            mimetype = 'image/jpeg'

        return send_file(buffer, mimetype=mimetype)
    except paramiko.AuthenticationException as e:
        current_app.logger.error(f"China FTP image: Authentication failed for {remote_path}: {str(e)}")
        return jsonify({'error': f'Authentication failed: {str(e)}'}), 500
    except paramiko.SSHException as e:
        current_app.logger.error(f"China FTP image: SSH connection failed for {remote_path}: {str(e)}")
        return jsonify({'error': f'SSH connection failed: {str(e)}'}), 500
    except FileNotFoundError as e:
        current_app.logger.error(f"China FTP image: File not found {remote_path}: {str(e)}")
        return jsonify({'error': f'File not found: {str(e)}'}), 404
    except Exception as e:
        current_app.logger.error(f"China FTP image: Failed to fetch {remote_path}: {str(e)}")
        return jsonify({'error': f'Failed to fetch image: {str(e)}'}), 500


@master_image_viewer_bp.route('/api/sync_china_image', methods=['POST'])
@login_required
def sync_china_image():
    """Sync a China FTP image to big_picture folder and add to database"""
    try:
        data = request.json
        sku = data.get('sku', '').strip().upper()
        filename = data.get('filename', '').strip()
        image_url = data.get('image_url', '').strip()
        
        if not sku or not filename:
            return jsonify({'error': 'SKU and filename are required'}), 400
        
        # Extract the remote path from the image URL
        # URL format: /china_ftp_image?path=/Individual_big_picture/...&size=0
        import urllib.parse
        parsed = urllib.parse.urlparse(image_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        remote_path = query_params.get('path', [None])[0]
        
        if not remote_path:
            return jsonify({'error': 'Invalid image URL'}), 400
        
        # URL decode the path (it comes URL-encoded)
        remote_path = urllib.parse.unquote(remote_path)
        
        # Download image from China FTP
        sku_folder = os.path.join(Config.IMAGE_DAM_BIG_PICTURE_DIR, sku)
        os.makedirs(sku_folder, exist_ok=True)
        local_path = os.path.join(sku_folder, filename)
        _china_ftp_cache.download_file(remote_path, local_path)
        
        current_app.logger.info(f"Synced China FTP image {filename} to {local_path}")
        
        # Calculate SHA256 hash
        import hashlib
        sha256_hash = hashlib.sha256()
        with open(local_path, 'rb') as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        file_hash = sha256_hash.hexdigest()
        
        # Get file size
        file_size = os.path.getsize(local_path)
        
        # Add to database using ImageAsset model
        from PIL import Image
        img = Image.open(local_path)
        width, height = img.size
        
        db = py.utilities.models.SessionLocal()
        try:
            # Check if image already exists in big_picture source
            existing = db.query(py.utilities.models.ImageAsset).filter(
                py.utilities.models.ImageAsset.sku == sku,
                py.utilities.models.ImageAsset.source == 'big_picture',
                py.utilities.models.ImageAsset.filename == filename,
                py.utilities.models.ImageAsset.is_active == True
            ).first()
            
            if existing:
                current_app.logger.info(f"Image {filename} already exists in database for SKU {sku}")
                return jsonify({
                    'message': f'Image {filename} synced successfully (already in database)',
                    'sku': sku,
                    'filename': filename
                }), 200
            
            # Get current max version for this SKU in big_picture
            max_version = db.query(py.utilities.models.ImageAsset).filter(
                py.utilities.models.ImageAsset.sku == sku,
                py.utilities.models.ImageAsset.source == 'big_picture'
            ).count()
            
            # Create new asset record
            new_asset = py.utilities.models.ImageAsset(
                sku=sku,
                source='big_picture',
                filename=filename,
                path=local_path,
                sha256=file_hash,
                file_size=file_size,
                width=width,
                height=height,
                version=max_version + 1,
                is_active=True,
                is_canonical=False
            )
            
            db.add(new_asset)
            db.commit()
            
            # Get username for logging
            username = None
            if hasattr(session, 'get'):
                username = session.get('username')
            if not username:
                from flask_login import current_user
                if current_user and current_user.is_authenticated:
                    username = current_user.username
            
            log_event('image_sync', {
                'sku': sku,
                'filename': filename,
                'source': 'china_ftp',
                'target': 'big_picture',
                'path': local_path,
                'user': username or 'tk_user'
            })
            
            current_app.logger.info(f"Added {filename} to database for SKU {sku} (version {max_version + 1})")
            
            return jsonify({
                'message': f'Image {filename} synced successfully to Product Images',
                'sku': sku,
                'filename': filename,
                'version': max_version + 1
            }), 200
            
        finally:
            db.close()
            
    except paramiko.AuthenticationException as e:
        current_app.logger.error(f"Sync failed - Authentication error: {str(e)}")
        return jsonify({'error': f'Authentication failed: {str(e)}'}), 500
    except paramiko.SSHException as e:
        current_app.logger.error(f"Sync failed - SSH error: {str(e)}")
        return jsonify({'error': f'SSH connection failed: {str(e)}'}), 500
    except FileNotFoundError as e:
        current_app.logger.error(f"Sync failed - File not found: {str(e)}")
        return jsonify({'error': f'File not found: {str(e)}'}), 404
    except Exception as e:
        current_app.logger.error(f"Sync failed: {str(e)}")
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': f'Sync failed: {str(e)}'}), 500


# Image serving is now handled by the DAM system at /image/{asset_id}/{size}
 