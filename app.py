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


_load_nssm_env_file()

from py.utilities.logging_setup import configure_logging, log_event
from py.utilities.setting import Config
from routes import register_routes
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
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        app.logger.info(
            "--------------------------------------------------------------------"
            "system restarted (dev reloader child)"
            "--------------------------------------------------------------------"
        )

    # Configure reloader to exclude log file and output directories
    from werkzeug._reloader import ReloaderLoop
    
    # Get absolute paths for exclusions
    exclude_dirs = [
        os.path.abspath('log.txt'),
        os.path.abspath('output'),
        os.path.abspath('static/file backup'),
        os.path.abspath('__pycache__'),
    ]
    
    # Patch the reloader to exclude these paths
    original_trigger_reload = ReloaderLoop.trigger_reload
    def patched_trigger_reload(self, filename):
        # Don't trigger reload for excluded files/directories
        abs_filename = os.path.abspath(filename) if filename else ''
        for exclude in exclude_dirs:
            if abs_filename.startswith(exclude):
                return
        return original_trigger_reload(self, filename)
    
    ReloaderLoop.trigger_reload = patched_trigger_reload
    
    app.run(
        host='0.0.0.0',
        port=5555,
        debug=True,
        request_handler=CustomRequestHandler,
        use_reloader=False
    )
