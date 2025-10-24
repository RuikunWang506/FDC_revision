"""
DAM Sync Routes - Flask Blueprint for DAM synchronization
Provides API endpoints to trigger and monitor DAM sync operations
"""

from flask import Blueprint, jsonify, request, render_template
from threading import Thread
import os
import sys
import io
from contextlib import redirect_stdout

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from py.utilities.dam_sync_service_v2 import DAMSyncServiceV2

dam_sync_bp = Blueprint('dam_sync', __name__, url_prefix='/dam')

# Max log lines to retain in memory (prevent unbounded growth)
MAX_LOG_LINES = 10000

# Global sync status
sync_status = {
    'is_running': False,
    'last_run': None,
    'last_stats': None,
    'error': None,
    'logs': [],  # Store recent log messages
    'current_progress': None  # Current progress info
}

def run_sync_background(force_rebuild_index=False, include_fetch=False):
    """Run sync in background thread"""
    global sync_status
    
    try:
        sync_status['is_running'] = True
        sync_status['error'] = None
        sync_status['logs'] = []  # Clear previous logs
        sync_status['current_progress'] = None
        
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
                    bufsize=1,
                    cwd=os.getcwd()
                )
                def reader(stream, prefix=""):
                    try:
                        for line in iter(stream.readline, ''):
                            if line:
                                log_fn((prefix + line.rstrip()))
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
                        capture_print(f"Smart Auto-Filter exited with code {rc}")
                except Exception as fetch_err:
                    capture_print(f"Error running Smart Auto-Filter: {fetch_err}")

            sync_service = DAMSyncServiceV2()
            sync_service.run_full_sync(force_rebuild_index=force_rebuild_index)
            
            sync_status['last_stats'] = sync_service.stats
            sync_status['last_run'] = str(sync_service.stats.get('end_time'))
        finally:
            # Restore original print
            builtins.print = original_print
        
    except Exception as e:
        sync_status['error'] = str(e)
        sync_status['logs'].append(f"ERROR: {str(e)}")
        # Use original_print if available, otherwise regular print
        try:
            original_print(f"Error in background sync: {e}")
        except:
            print(f"Error in background sync: {e}")
    finally:
        sync_status['is_running'] = False

@dam_sync_bp.route('/')
def dam_sync_home():
    """DAM sync control panel"""
    return render_template('dam_sync.html', status=sync_status)

@dam_sync_bp.route('/status')
def get_sync_status():
    """Get current sync status"""
    return jsonify(sync_status)

@dam_sync_bp.route('/start', methods=['POST'])
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
    
    # Start sync in background thread
    thread = Thread(target=run_sync_background, args=(force_rebuild_index, include_fetch))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'message': 'Sync started in background'
    })

@dam_sync_bp.route('/generate-index', methods=['POST'])
def generate_index():
    """Generate index files only (no sync)"""
    try:
        data = request.get_json() or {}
        source = data.get('source', 'both')  # 'catalog', 'big_picture', or 'both'
        
        sync_service = DAMSyncServiceV2()
        results = {}
        
        if source in ['catalog', 'both']:
            files = sync_service.generate_index(
                sync_service.catalog_dir,
                str(sync_service.catalog_index),
                "*.jpg"
            )
            results['catalog'] = {
                'count': len(files),
                'path': str(sync_service.catalog_index)
            }
        
        if source in ['big_picture', 'both']:
            files = sync_service.generate_index(
                sync_service.big_picture_dir,
                str(sync_service.big_picture_index),
                "*.jpg"
            )
            results['big_picture'] = {
                'count': len(files),
                'path': str(sync_service.big_picture_index)
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
def check_scheduled_sync():
    """Check if scheduled sync should run and execute if needed"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Sync is already running'
        }), 400
    
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
def fetch_from_dropbox():
    """Run only the Dropbox fetch (Smart Auto-Filter) in background"""
    if sync_status['is_running']:
        return jsonify({
            'success': False,
            'message': 'Another operation is already running'
        }), 400

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
                sync_status['error'] = f"Smart Auto-Filter exited with code {rc}"
        except Exception as e:
            sync_status['error'] = str(e)
            print(f"Error during fetch: {e}")
        finally:
            sync_status['is_running'] = False

    thread = Thread(target=_run_fetch_only)
    thread.daemon = True
    thread.start()

    return jsonify({'success': True, 'message': 'Dropbox fetch started'})
