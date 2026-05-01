"""
DAM Sync Routes - Flask Blueprint for DAM synchronization
Provides API endpoints to trigger and monitor DAM sync operations
"""

from flask import Blueprint, jsonify, request, render_template, redirect, url_for, session, current_app
from functools import wraps
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function
from threading import Thread, Lock
import json
import os
import re
import subprocess
import sys
import io
from contextlib import redirect_stdout
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2
from py.utilities.logging_setup import log_event

dam_sync_bp = Blueprint('dam_sync', __name__, url_prefix='/dam')

# Serialized writes to dedicated audit file (parallel to hourly_sync.log / log.txt troubleshooting).
_dam_cron_audit_lock = Lock()


def _dam_cron_audit_path():
    try:
        from py.utilities.setting import Config
        log_file = getattr(Config, 'LOG_FILE', None) or ''
        base = os.path.dirname(log_file) if log_file else os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..'))
        return os.path.join(base, 'dam_cron_audit.log')
    except Exception:
        return os.path.join(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')), 'dam_cron_audit.log')


def dam_cron_audit(event, **kv):
    """Append one NDJSON-friendly line so you can grep who called /dam/cron/*."""
    try:
        row = {'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'event': event}
        row.update(kv)
        line = json.dumps(row, ensure_ascii=False, default=str) + '\n'
        ap = _dam_cron_audit_path()
        dname = os.path.dirname(ap)
        if dname and not os.path.exists(dname):
            os.makedirs(dname, exist_ok=True)
        with _dam_cron_audit_lock:
            with open(ap, 'a', encoding='utf-8') as fh:
                fh.write(line)
    except Exception:
        pass


def _cron_request_client():
    """Best-effort client + proxy forwarding context (never log secrets)."""
    try:
        return {
            'remote_addr': getattr(request, 'remote_addr', None),
            'xff': (request.headers.get('X-Forwarded-For') or '')[:512],
            'user_agent': (request.headers.get('User-Agent') or '')[:512],
            'forwarded': (request.headers.get('Forwarded') or '')[:512],
            'host': (request.headers.get('Host') or '')[:255],
            'fwd_proto': (request.headers.get('X-Forwarded-Proto') or '')[:64],
            'referer': (request.headers.get('Referer') or '')[:512],
            'method': getattr(request, 'method', ''),
            'path': getattr(request, 'path', ''),
            'content_length': getattr(request, 'content_length', None),
            'secret_header_present': bool((request.headers.get('X-DAM-Cron-Secret') or '').strip()),
        }
    except Exception:
        return {}

# Windows Task Scheduler: primary unattended sync (every 24 hours). Legacy name kept for migration queries.
DAILY_SYNC_TASK_NAME = 'FDC DAM Daily Sync'
LEGACY_HOURLY_TASK_NAME = 'FDC DAM Hourly Sync'
WEEKLY_TASK_NAME = 'FDC DAM Weekly Rebuild'

# Max log lines to retain in memory (prevent unbounded growth)
MAX_LOG_LINES = 10000


def _parse_windows_next_run_line(raw_line):
    raw_line = (raw_line or '').strip()
    if not raw_line or raw_line.lower() in ('n/a', 'none'):
        return None
    for fmt in ('%m/%d/%Y %I:%M:%S %p', '%m/%d/%Y %H:%M:%S'):
        try:
            return datetime.strptime(raw_line, fmt)
        except ValueError:
            continue
    return None


def _schtasks_next_run(task_name):
    """Query schtasks for Next Run Time (Windows). Returns dict with next_ts_ms or error."""
    if sys.platform != 'win32':
        return {'error': 'not_windows'}

    try:
        r = subprocess.run(
            ['schtasks', '/Query', '/TN', task_name, '/FO', 'LIST', '/V'],
            capture_output=True,
            timeout=25,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return {'error': str(e)}

    if r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip()
        return {'error': err or 'schtasks failed'}

    text = r.stdout or ''
    m = re.search(r'Next Run Time:\s*(.+)', text)
    if not m:
        return {'error': 'could_not_find_next_run_time'}

    raw = m.group(1).strip()
    dt = _parse_windows_next_run_line(raw)
    if dt is None:
        return {'raw': raw, 'next_ts_ms': None, 'next_run_display': raw}

    try:
        ts_ms = int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return {'raw': raw, 'next_ts_ms': None, 'next_run_display': raw}

    return {
        'next_ts_ms': ts_ms,
        'next_run_display': dt.strftime('%Y-%m-%d %H:%M:%S'),
        'raw': raw,
    }

# Global sync status
sync_status = {
    'is_running': False,
    'last_run': None,
    'last_stats': None,
    'error': None,
    'logs': [],  # Store recent log messages
    'current_progress': None  # Current progress info
}

def run_sync_background(force_rebuild_index=False, include_fetch=False, include_cleanup=False, log_source='manual'):
    """Run sync in background thread.

    log_source: 'manual' (UI /dam/start) or 'cron' (Task Scheduler /dam/cron/start).
    Cron runs write to Config.LOG_FILE so they appear on the /log page.
    """
    global sync_status
    
    try:
        sync_status['is_running'] = True
        sync_status['error'] = None
        sync_status['logs'] = []  # Clear previous logs
        sync_status['current_progress'] = None

        if log_source == 'cron':
            try:
                log_event(
                    'info',
                    f'🟢 INFO DAM scheduled sync START (cron) fetch={include_fetch} '
                    f'rebuild_index={force_rebuild_index} cleanup={include_cleanup}',
                )
            except Exception:
                pass
        
        # Log DAM sync access with username
        try:
            from flask import current_app, session
            username = session.get('username', 'Unknown')
            current_app.logger.info(f"***{username}***___@@@Accessed@@@__DAM Sync Service (manual sync started)")
        except Exception:
            pass

        # Save original print function
        import builtins
        original_print = builtins.print

        # Create a custom print function that captures output
        def capture_print(*args, **kwargs):
            message = ' '.join(str(arg) for arg in args)
            sync_status['logs'].append(message)
            # Keep only last MAX_LOG_LINES lines
            if len(sync_status['logs']) > MAX_LOG_LINES:
                sync_status['logs'] = sync_status['logs'][-MAX_LOG_LINES:]
            # Use original print to avoid recursion
            original_print(message)

        # Monkey-patch the print function in the sync service
        builtins.print = capture_print

        try:
            # Helper to stream subprocess output line-by-line
            def stream_process(cmd, log_fn):
                import subprocess, threading, sys as _sys
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    cwd=os.getcwd()
                )
                def reader(stream, prefix=""):
                    try:
                        for line in iter(stream.readline, ''):
                            if line:
                                log_fn((prefix + line.rstrip()))
                    except UnicodeDecodeError:
                        # Handle any remaining encoding issues
                        pass
                    finally:
                        try:
                            stream.close()
                        except Exception:
                            pass
                t_out = threading.Thread(target=reader, args=(proc.stdout, ""), daemon=True)
                t_err = threading.Thread(target=reader, args=(proc.stderr, "[stderr] "), daemon=True)
                t_out.start(); t_err.start()
                returncode = proc.wait()
                t_out.join(timeout=1)
                t_err.join(timeout=1)
                return returncode

            # Optional: fetch new images from Dropbox first
            if include_fetch:
                capture_print("\n=== Fetching new images from Dropbox (Smart Auto-Filter) ===")
                import sys as _sys
                try:
                    rc = stream_process([_sys.executable, 'py/standalone/smart_auto_filter.py'], capture_print)
                    if rc != 0:
                        # If the subprocess returned non-zero, inspect recent output for a completion marker.
                        recent = '\n'.join(sync_status.get('logs', [])[-200:])
                        success_markers = [
                            'Full scan complete',
                            '✅ Full scan complete',
                            'Processing Complete',
                            '✅ Processing Complete!',
                            'SCAN_COMPLETE|',
                        ]
                        if any(marker in recent for marker in success_markers):
                            capture_print(f"Smart Auto-Filter exited with code {rc} but reported completion in output — treating as SUCCESS")
                            try:
                                log_event('warning', f"Smart Auto-Filter returned code {rc} but output contains completion marker. Recent output:\n{recent}")
                            except Exception:
                                pass
                        else:
                            capture_print(f"Smart Auto-Filter exited with code {rc}")
                            try:
                                log_event('error', f"Smart Auto-Filter exited with code {rc}. Recent output:\n{recent}")
                            except Exception:
                                pass
                except Exception as fetch_err:
                    capture_print(f"Error running Smart Auto-Filter: {fetch_err}")

            sync_service = DAMSyncServiceV2()
            sync_service.run_full_sync(force_rebuild_index=force_rebuild_index)
            
            sync_status['last_stats'] = sync_service.stats
            sync_status['last_run'] = str(sync_service.stats.get('end_time'))
            
            # Optional: cleanup broken links after sync
            if include_cleanup:
                capture_print("\n=== Cleaning up broken links ===")
                try:
                    import py.utilities.models
                    db = py.utilities.models.SessionLocal()
                    try:
                        assets = db.query(py.utilities.models.ImageAsset).filter(
                            py.utilities.models.ImageAsset.is_active == True
                        ).all()
                        
                        capture_print(f"Checking {len(assets)} active image records...")
                        log_event('info', f'DAM cleanup started as part of sync: checking {len(assets)} active records')
                        
                        cleaned = 0
                        for asset in assets:
                            if hasattr(asset, 'path') and asset.path:
                                if not os.path.exists(asset.path):
                                    asset.is_active = False
                                    cleaned += 1
                        
                        db.commit()
                        capture_print(f"✅ Cleanup complete: {cleaned} broken links marked inactive")
                        log_event('info', f'DAM cleanup completed: {cleaned} files marked inactive out of {len(assets)} checked')
                    finally:
                        db.close()
                except Exception as cleanup_err:
                    capture_print(f"⚠️ Cleanup error: {cleanup_err}")
                    log_event('error', f'DAM cleanup failed during sync: {str(cleanup_err)}')

            if log_source == 'cron':
                try:
                    st = sync_status.get('last_stats') or {}
                    log_event(
                        'info',
                        '🟢 SUCCESS DAM scheduled sync DONE (cron) '
                        f"processed={st.get('total_processed')} added={st.get('total_added')} "
                        f"updated={st.get('total_updated')} skipped={st.get('total_skipped')} "
                        f"errors={st.get('total_errors')} last_run={sync_status.get('last_run')}",
                    )
                    dam_cron_audit(
                        'cron_background_finished_ok',
                        processed=st.get('total_processed'),
                        added=st.get('total_added'),
                        updated=st.get('total_updated'),
                        skipped=st.get('total_skipped'),
                        errors=st.get('total_errors'),
                        last_run=sync_status.get('last_run'),
                    )
                except Exception:
                    pass

        finally:
            # Restore original print
            builtins.print = original_print
        
    except Exception as e:
        sync_status['error'] = str(e)
        sync_status['logs'].append(f"ERROR: {str(e)}")
        if log_source == 'cron':
            try:
                log_event('error', f'🔴 ERROR DAM scheduled sync FAILED (cron): {e}')
                dam_cron_audit('cron_background_finished_error', error=str(e)[:2000])
            except Exception:
                pass
        # Use original_print if available, otherwise regular print
        try:
            original_print(f"Error in background sync: {e}")
        except:
            print(f"Error in background sync: {e}")
    finally:
        sync_status['is_running'] = False


def _dam_cron_secret():
    """Shared secret for Task Scheduler / unattended sync.

    Order: env DAM_CRON_SECRET, then repo file auto_run/dam_cron_secret.local.txt (same file curl.bat reads).
    """
    s = (os.environ.get('DAM_CRON_SECRET') or '').strip()
    if s:
        return s
    try:
        _repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        p = os.path.join(_repo, 'auto_run', 'dam_cron_secret.local.txt')
        if os.path.isfile(p):
            with open(p, encoding='utf-8') as f:
                return (f.readline() or '').strip()
    except Exception:
        pass
    return ''


def _cron_authorized():
    if not _dam_cron_secret():
        return False
    return request.headers.get('X-DAM-Cron-Secret') == _dam_cron_secret()


def _reconcile_false_errors():
    """If a non-zero exit was recorded but the captured logs show a completion marker,
    treat it as success for UI presentation and persist a warning.
    This helps when the child prints completion but the process returns a strange code
    due to pipe/encoding/host issues.
    """
    try:
        err = sync_status.get('error')
        if not err:
            return

        # Only consider subprocess exit code messages
        if 'exited with code' not in str(err):
            return

        recent = '\n'.join(sync_status.get('logs', [])[-400:])
        markers = ['Full scan complete', '✅ Full scan complete', 'Processing Complete', '✅ Processing Complete!', 'SCAN_COMPLETE|FULL', 'SCAN_COMPLETE|']
        if any(m in recent for m in markers):
            # Persist a warning and clear the transient error for UI
            try:
                log_event('warning', f"Clearing false-positive subprocess error: {err}. Recent output indicates completion.\n{recent}")
            except Exception:
                pass
            # Append a UI-visible note
            sync_status['logs'].append(f"[warning] Cleared subprocess exit error because output contains completion marker.")
            sync_status['error'] = None
    except Exception:
        # Don't let reconciliation raise
        pass

@dam_sync_bp.route('/')
@login_required
def dam_sync_home():
    """DAM sync control panel"""
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    current_user = session['username']
    return render_template('dam_sync.html', status=sync_status, current_user=current_user)


@dam_sync_bp.route('/scheduler-next', methods=['GET'])
@login_required
def scheduler_next():
    """Next schtasks run times for daily (24h) + weekly DAM automation (Windows Task Scheduler)."""
    if 'username' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    daily = {'task': DAILY_SYNC_TASK_NAME, **_schtasks_next_run(DAILY_SYNC_TASK_NAME)}
    # If the server was never re-provisioned, fall back to legacy hourly task name for display.
    if daily.get('error') or daily.get('next_ts_ms') is None:
        legacy = {'task': LEGACY_HOURLY_TASK_NAME, **_schtasks_next_run(LEGACY_HOURLY_TASK_NAME)}
        if not legacy.get('error') and legacy.get('next_ts_ms') is not None:
            daily = legacy
    weekly = {'task': WEEKLY_TASK_NAME, **_schtasks_next_run(WEEKLY_TASK_NAME)}
    return jsonify({
        'success': True,
        'platform': sys.platform,
        'daily': daily,
        'weekly': weekly,
        # Deprecated alias for old frontends
        'hourly': daily,
    })


@dam_sync_bp.route('/cron/start', methods=['POST'])
def dam_cron_start():
    """
    Unattended sync for Task Scheduler / curl — NOT browser login.
    Requires header: X-DAM-Cron-Secret: <same value as server env DAM_CRON_SECRET>.

    POST JSON body matches /dam/start:
      force_rebuild_index, include_fetch, include_cleanup (all optional booleans).
    """
    client = _cron_request_client()

    if not _dam_cron_secret():
        dam_cron_audit('cron_start_rejected', outcome='cron_disabled_missing_secret_env', **client)
        return jsonify({
            'success': False,
            'error': 'Cron automation disabled: set environment variable DAM_CRON_SECRET on this server.',
        }), 503
    if not _cron_authorized():
        dam_cron_audit('cron_start_rejected', outcome='unauthorized_bad_or_missing_secret', **client)
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    if sync_status['is_running']:
        dam_cron_audit('cron_start_rejected', outcome='sync_already_running', **client)
        return jsonify({'success': False, 'message': 'Sync is already running'}), 400

    data = request.get_json() or {}
    force_rebuild_index = data.get('force_rebuild_index', False)
    include_fetch = data.get('include_fetch', True)
    include_cleanup = data.get('include_cleanup', True)

    dam_cron_audit(
        'cron_start_accepted',
        fetch=include_fetch,
        rebuild_index=force_rebuild_index,
        cleanup=include_cleanup,
        **client,
    )

    try:
        current_app.logger.info(
            '***cron*** scheduled DAM sync start (fetch=%s rebuild=%s cleanup=%s)',
            include_fetch,
            force_rebuild_index,
            include_cleanup,
        )
    except Exception:
        pass

    thread = Thread(
        target=lambda: run_sync_background(
            force_rebuild_index,
            include_fetch,
            include_cleanup,
            log_source='cron',
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({'success': True, 'message': 'Sync started in background'})


@dam_sync_bp.route('/cron/status', methods=['GET'])
def dam_cron_status():
    """Minimal status for monitoring scripts (same X-DAM-Cron-Secret header)."""
    client = _cron_request_client()

    if not _dam_cron_secret():
        dam_cron_audit('cron_status_rejected', outcome='cron_disabled_missing_secret_env', **client)
        return jsonify({'configured': False, 'error': 'DAM_CRON_SECRET not set'}), 503
    if not _cron_authorized():
        dam_cron_audit('cron_status_rejected', outcome='unauthorized_bad_or_missing_secret', **client)
        return jsonify({'error': 'Unauthorized'}), 401

    _reconcile_false_errors()
    return jsonify({
        'configured': True,
        'is_running': sync_status.get('is_running'),
        'last_run': sync_status.get('last_run'),
        'error': sync_status.get('error'),
        'log_lines': len(sync_status.get('logs') or []),
    })


@dam_sync_bp.route('/status')
@login_required
def get_sync_status():
    """Get current sync status"""
    # Reconcile transient false-positive subprocess errors before returning
    _reconcile_false_errors()
    return jsonify(sync_status)

@dam_sync_bp.route('/start', methods=['POST'])
@login_required
def start_sync():
    """Start manual DAM sync"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Sync is already running'
        }), 400
    
    data = request.get_json() or {}
    force_rebuild_index = data.get('force_rebuild_index', False)
    include_fetch = data.get('include_fetch', False)
    include_cleanup = data.get('include_cleanup', False)
    
    # Log DAM sync access with username
    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service")

    # Start sync in background thread
    thread = Thread(target=run_sync_background, args=(force_rebuild_index, include_fetch, include_cleanup))
    thread.daemon = True
    thread.start()

    return jsonify({
        'success': True,
        'message': 'Sync started in background'
    })

@dam_sync_bp.route('/generate-index', methods=['POST'])
@login_required
def generate_index():
    """Scan catalog/big_picture folders for JPEG counts only (no JSON files; no DB sync)."""
    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service (folder JPEG scan)")
    try:
        data = request.get_json() or {}
        source = data.get('source', 'both')  # 'catalog', 'big_picture', or 'both'
        
        sync_service = DAMSyncServiceV2()
        results = {}
        
        if source in ['catalog', 'both']:
            files = sync_service.collect_jpg_paths_relative(sync_service.catalog_dir)
            results['catalog'] = {
                'count': len(files),
                'root': sync_service.catalog_dir,
            }
        
        if source in ['big_picture', 'both']:
            files = sync_service.collect_jpg_paths_relative(sync_service.big_picture_dir)
            results['big_picture'] = {
                'count': len(files),
                'root': sync_service.big_picture_dir,
            }
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@dam_sync_bp.route('/scheduled-check', methods=['POST'])
@login_required
def check_scheduled_sync():
    """Check if scheduled sync should run and execute if needed"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Sync is already running'
        }), 400
    
    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service (scheduled check)")
    try:
        sync_service = DAMSyncServiceV2()
        
        if sync_service.should_run_scheduled_sync():
            # Start sync in background
            thread = Thread(target=lambda: sync_service.run_scheduled_sync())
            thread.daemon = True
            thread.start()
            
            return jsonify({
                'success': True,
                'message': 'Scheduled sync started',
                'should_run': True
            })
        else:
            return jsonify({
                'success': True,
                'message': 'No sync needed (last sync < 3 days ago)',
                'should_run': False
            })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@dam_sync_bp.route('/fetch', methods=['POST'])
@login_required
def fetch_from_dropbox():
    """Run only the Dropbox fetch (Smart Auto-Filter) in background"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Another operation is already running'
        }), 400

    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service (fetch)")
    def _run_fetch_only():
        global sync_status
        sync_status['is_running'] = True
        sync_status['error'] = None
        sync_status['logs'] = []
        try:
            import sys as _sys, subprocess, threading
            def _log(line):
                sync_status['logs'].append(line)
                if len(sync_status['logs']) > MAX_LOG_LINES:
                    sync_status['logs'] = sync_status['logs'][-MAX_LOG_LINES:]
                try:
                    print(line)
                except Exception:
                    pass
            _log("\n=== Fetching new images from Dropbox (Smart Auto-Filter) ===")
            proc = subprocess.Popen([
                _sys.executable,
                'py/standalone/smart_auto_filter.py'
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=os.getcwd())
            def reader(stream, prefix=""):
                try:
                    for line in iter(stream.readline, ''):
                        if line:
                            _log(prefix + line.rstrip())
                except Exception:
                    pass

                    # Start sync in background thread
                    thread = Thread(target=run_sync_background)
                    thread.daemon = True
                    thread.start()

                    return jsonify({'success': True, 'message': 'DAM sync started'})
                except Exception as e:
                    sync_status['is_running'] = False
                    sync_status['error'] = str(e)
                    return jsonify({'success': False, 'error': str(e)}), 500
                else:
                    sync_status['error'] = f"Smart Auto-Filter exited with code {rc}"
                    # Persist the failure and recent output to the persistent app log for post-mortem
                    try:
                        log_event('error', f"Smart Auto-Filter exited with code {rc}. Recent output:\n{recent}")
                    except Exception as _log_err:
                        # Best-effort logging, don't let logging failures crash the thread
                        print(f"Failed to write persistent log for Smart Auto-Filter error: {_log_err}")
            # Attempt to reconcile false-positive errors (if the child printed completion but returned a strange code)
            try:
                _reconcile_false_errors()
            except Exception:
                pass
        except Exception as e:
            sync_status['error'] = str(e)
            print(f"Error during fetch: {e}")
        finally:
            sync_status['is_running'] = False

    thread = Thread(target=_run_fetch_only)
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Dropbox fetch started'})
    

@dam_sync_bp.route('/fetch-full', methods=['POST'])
@login_required
def fetch_from_dropbox_full():
    """Run Smart Auto-Filter in full-scan mode (process all files) in background"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Another operation is already running'
        }), 400

    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service (fetch full)")
    def _run_full_fetch():
        global sync_status
        sync_status['is_running'] = True
        sync_status['error'] = None
        sync_status['logs'] = []
        try:
            import sys as _sys, subprocess, threading
            def _log(line):
                sync_status['logs'].append(line)
                if len(sync_status['logs']) > MAX_LOG_LINES:
                    sync_status['logs'] = sync_status['logs'][-MAX_LOG_LINES:]
                try:
                    print(line)
                except Exception:
                    pass
            _log("\n=== Fetching ALL images from Dropbox (Smart Auto-Filter FULL) ===")
            proc = subprocess.Popen([
                _sys.executable,
                'py/standalone/smart_auto_filter.py',
                '--full'
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=os.getcwd())
            def reader(stream, prefix=""):
                try:
                    for line in iter(stream.readline, ''):
                        if line:
                            _log(prefix + line.rstrip())
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass
            t_out = threading.Thread(target=reader, args=(proc.stdout, ""), daemon=True)
            t_err = threading.Thread(target=reader, args=(proc.stderr, "[stderr] "), daemon=True)
            t_out.start(); t_err.start()
            rc = proc.wait()
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            if rc != 0:
                # Inspect recent output for a full-scan completion marker; if present, treat as success
                recent = '\n'.join(sync_status.get('logs', [])[-200:])
                full_markers = ['Full scan complete', '✅ Full scan complete', 'SCAN_COMPLETE|FULL']
                if any(marker in recent for marker in full_markers):
                    try:
                        log_event('warning', f"Smart Auto-Filter (full) exited with code {rc} but reported completion. Recent output:\n{recent}")
                    except Exception:
                        pass
                    _log(f"Smart Auto-Filter (full) exited with code {rc} but reported completion — treating as SUCCESS")
                else:
                    sync_status['error'] = f"Smart Auto-Filter (full) exited with code {rc}"
                    # Persist the failure and recent output to the persistent app log for post-mortem
                    try:
                        log_event('error', f"Smart Auto-Filter (full) exited with code {rc}. Recent output:\n{recent}")
                    except Exception as _log_err:
                        print(f"Failed to write persistent log for Smart Auto-Filter full error: {_log_err}")
        except Exception as e:
            sync_status['error'] = str(e)
            print(f"Error during full fetch: {e}")
        finally:
            sync_status['is_running'] = False

    thread = Thread(target=_run_full_fetch)
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Dropbox full fetch started'})

@dam_sync_bp.route('/cleanup', methods=['POST'])
@login_required
def cleanup_broken_links():
    """Scan DAM image records and mark missing files as inactive."""
    from flask import current_app, session
    username = session.get('username', 'Unknown')
    current_app.logger.info(f"***{username}***___@@@manually started@@@__DAM Sync Service (cleanup)")
    log_event('info', 'Starting DAM cleanup process')
    
    import py.utilities.models
    import os
    db = py.utilities.models.SessionLocal()
    try:
        assets = db.query(py.utilities.models.ImageAsset).filter(
            py.utilities.models.ImageAsset.is_active == True
        ).all()
        
        log_event('info', f'Found {len(assets)} active DAM image records to check')
        
        cleaned = 0
        for asset in assets:
            if hasattr(asset, 'path') and asset.path:
                if not os.path.exists(asset.path):
                    asset.is_active = False
                    cleaned += 1
                    log_event('info', f'Marked missing file as inactive: {asset.path}')
        
        db.commit()
        log_event('info', f'DAM cleanup completed: {cleaned} files marked inactive out of {len(assets)} checked')
        
        return jsonify({'success': True, 'cleaned': cleaned, 'total_checked': len(assets)})
    except Exception as e:
        log_event('error', f'DAM cleanup failed: {str(e)}')
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db.close()
    
