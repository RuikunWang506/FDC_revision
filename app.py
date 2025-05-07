from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.serving import WSGIRequestHandler

from logging_setup import configure_logging
from config import Config
from routes import register_routes
from auth import log_event

class CustomRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        # Skip everything from the local host
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return

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
app.secret_key = __import__("os").urandom(24)
app.config.from_object(Config)
app.config['SERVER_NAME'] = 'www.fdcef.com'
app.config['PREFERRED_URL_SCHEME'] = 'https'

configure_logging(app)
register_routes(app)

@app.template_filter('zip')
def zip_filter(a, b):
    return zip(a, b)
app.jinja_env.filters['zip'] = zip_filter

if __name__ == "__main__":
    app.logger.info(
        "--------------------------------------------------------------------"
        "system restarted"
        "--------------------------------------------------------------------"
    )
    app.run(
        host='0.0.0.0',
        port=5555,
        debug=True,
        request_handler=CustomRequestHandler
    )
