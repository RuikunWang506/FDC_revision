from flask import Flask
from logging_setup import configure_logging
from config import Config
from routes import register_routes  # Import your routes after binding the dashboard
from werkzeug.middleware.proxy_fix import ProxyFix

import os

# Flask app
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
app.secret_key = os.urandom(24)
app.config.from_object(Config)
app.config['SERVER_NAME'] = 'www.fdcef.com'
app.config['PREFERRED_URL_SCHEME'] = 'https'

# Configure logging
configure_logging(app)
# Register routes
register_routes(app)
# Register the zip filter
@app.template_filter('zip')
def zip_filter(a, b):
    return zip(a, b)

app.jinja_env.filters['zip'] = zip_filter

if __name__ == "__main__":
    try:
        app.logger.info("--------------------------------------------------------------------system restarted--------------------------------------------------------------------")
        app.run(host='0.0.0.0', port=5555, debug=True)
    except Exception as e:
        app.logger.error("Error starting Flask application: %s", e)