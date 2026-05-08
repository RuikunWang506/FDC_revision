from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler
import os
import sys


def _load_nssm_env_file():
    """Apply KEY=VALUE lines from nssm-fdcef-environment.env before Config is imported.

    NSSM does not read that file; this lets the same file drive Turnstile and other
    vars for the fdcef service. Variables already in os.environ (e.g. set in NSSM) win.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nssm-fdcef-environment.env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if not key:
                    continue
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def _windows_process_ancestor_exes(max_depth=10):
    """Return normcased full paths of this process's ancestors (parent, grandparent, ...).

    Used so NSSM is detected even when env vars are missing: the outer Python process
    is a direct child of nssm.exe, and the Werkzeug reloader's inner child is parented
    by another python.exe whose parent is still nssm.exe.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    pid_to_parent = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snap, ctypes.byref(pe)):
            return []
        while True:
            pid_to_parent[pe.th32ProcessID] = pe.th32ParentProcessID
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    finally:
        kernel32.CloseHandle(snap)

    def _image_path(pid):
        if pid <= 0:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            kernel32.CloseHandle(handle)

    out = []
    cur = os.getpid()
    for _ in range(max_depth):
        parent = pid_to_parent.get(cur)
        if parent is None or parent == cur:
            break
        path = _image_path(parent)
        if not path:
            break
        out.append(os.path.normcase(path))
        cur = parent
    return out


def _running_as_windows_service():
    """Best-effort detection for NSSM/Windows service context."""
    service_markers = (
        "NSSM_SERVICE_NAME",
        "NSSM_APPLICATION",
        "NSSM_EXE",
        "SERVICE_NAME",
    )
    if any(os.environ.get(name) for name in service_markers):
        return True
    if str(os.environ.get("SESSIONNAME", "")).strip().lower() == "services":
        return True
    service_parent_names = frozenset(("nssm.exe", "services.exe"))
    for exe in _windows_process_ancestor_exes():
        if os.path.basename(exe).lower() in service_parent_names:
            return True
    return False


_load_nssm_env_file()

from py.utilities.logging_setup import configure_logging, log_event
from py.utilities.setting import Config
from routes import register_routes
from py.utilities.navigation_audit import register_navigation_audit
from py.utilities.auth import log_event



class CustomRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        # Skip everything from the local host
        if self.client_address[0] in ("127.0.0.1", "::1", "173.198.50.202"):
            return True

        # Skip static files (adjust prefix if needed)
        if self.path.startswith("/static"):
            return

        entry = (
            f'{self.client_address[0]} - - '
            f'[{self.log_date_time_string()}] '
            f'"{self.command} {self.path} {self.request_version}" '
            f'{code} {size}'
        )
        log_event("INFO", entry)

    def log_error(self, format, *args):
        """
        You might still want to see bad‐request errors even from localhost,
        so we don't filter those here by default.
        """
        entry = (
            f'{self.client_address[0]} - - '
            f'[{self.log_date_time_string()}] '
            f'{format % args}'
        )
        log_event("ERROR", entry)


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.secret_key = 'FDCkey2026-may-1-turnstile-update'
app.config.from_object(Config)

# Session configuration for both local and production
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP for localhost
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Only set production cookie / scheme. Do NOT pin SERVER_NAME: that forces generated
# external URLs to www even when staff open the app via the LAN IP (e.g. http://192.168.254.30:port),
# which slows everything by sending traffic through Cloudflare. Emails use Config.PUBLIC_APP_ORIGIN.
if app.config.get('ENV') == 'production':
    app.config['PREFERRED_URL_SCHEME'] = 'https'
    app.config['SESSION_COOKIE_SECURE'] = True

configure_logging(app)
register_routes(app)
register_navigation_audit(app)

try:
    from py.Toolkits.V7listcreator import workbook_exports_fail_orphaned_running

    workbook_exports_fail_orphaned_running(app)
except Exception:
    pass

try:
    log_event(
        'info',
        f'🟣 SYSTEM Flask app process started pid={os.getpid()} cwd={os.getcwd()} argv={sys.argv!r}',
    )
except Exception:
    pass

@app.template_filter('zip')
def zip_filter(a, b):
    return zip(a, b)
app.jinja_env.filters['zip'] = zip_filter

if __name__ == "__main__":
    # Optional: set NSSM AppParameters to "... app.py --no-reloader" to force no reloader
    # even if detection fails. Interactive dev: run without that flag (default debug on).
    _explicit_no_reloader = "--no-reloader" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--no-reloader"]

    _as_service = _running_as_windows_service()
    if _as_service or _explicit_no_reloader:
        # Override machine-wide FLASK_DEBUG for this process so the service never enables
        # the Werkzeug reloader (including the inner reloader child).
        os.environ["FLASK_DEBUG"] = "0"

    # Debug + auto-reload on code changes (dev). Default to off when started as a service.
    _default_debug = "0" if (_as_service or _explicit_no_reloader) else "1"
    _raw = str(os.environ.get("FLASK_DEBUG", _default_debug)).strip().lower()
    _dev_debug = _raw not in ("0", "false", "no", "")

    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        app.logger.info(
            "--------------------------------------------------------------------"
            "system restarted (dev reloader child)"
            "--------------------------------------------------------------------"
        )

    if _dev_debug:
        # Exclude noisy paths so log/output churn does not restart the server.
        try:
            from werkzeug._reloader import ReloaderLoop
        except ImportError:  # pragma: no cover
            ReloaderLoop = None  # type: ignore

        if ReloaderLoop is not None:
            _root = os.path.dirname(os.path.abspath(__file__))
            exclude_dirs = [
                os.path.abspath(os.path.join(_root, "log.txt")),
                os.path.abspath(os.path.join(_root, "output")),
                os.path.abspath(os.path.join(_root, "static", "file backup")),
                os.path.abspath(os.path.join(_root, "__pycache__")),
                os.path.normcase(Config.LOG_FILE) if getattr(Config, "LOG_FILE", None) else "",
            ]
            exclude_dirs = [p for p in exclude_dirs if p]

            _original_trigger_reload = ReloaderLoop.trigger_reload

            def _patched_trigger_reload(self, filename):  # type: ignore[misc]
                abs_filename = os.path.normcase(os.path.abspath(filename)) if filename else ""
                for ex in exclude_dirs:
                    if ex and abs_filename.startswith(os.path.normcase(ex)):
                        return
                return _original_trigger_reload(self, filename)

            ReloaderLoop.trigger_reload = _patched_trigger_reload  # type: ignore[method-assign]

    app.run(
        host='0.0.0.0',
        port=5555,
        debug=_dev_debug,
        request_handler=CustomRequestHandler,
        use_reloader=_dev_debug,
    )
