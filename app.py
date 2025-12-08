from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler
import os

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
app.secret_key = 'your-fixed-secret-key-for-persistent-sessions-fdc-flask-app-2025'
app.config.from_object(Config)

# Session configuration for both local and production
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP for localhost
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Only set SERVER_NAME for production
if app.config.get('ENV') == 'production':
    app.config['SERVER_NAME'] = 'www.fdcef.com' 
    app.config['PREFERRED_URL_SCHEME'] = 'https'
    app.config['SESSION_COOKIE_SECURE'] = True

configure_logging(app)
register_routes(app)

@app.template_filter('zip')
def zip_filter(a, b):
    return zip(a, b)
app.jinja_env.filters['zip'] = zip_filter

if __name__ == "__main__":
    # Only log restart in the reloader subprocess
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        app.logger.info(
            "--------------------------------------------------------------------"
            "system restarted"
            "--------------------------------------------------------------------"
        )
    
    # Configure reloader to exclude log file and output directories
    import sys
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
