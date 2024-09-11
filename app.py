from flask import Flask
from logging_setup import configure_logging
from config import Config
from routes import register_routes  # Import your routes after binding the dashboard
import os
import sys
import traceback
import ctypes




# Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config.from_object(Config)


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
    app.logger.info("autobots, rolling out!")
    traceback.print_exc()
    try:
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        app.run(host='0.0.0.0', port=5555, debug=False)
    except Exception as e:
        app.logger.error("Error starting Flask application: %s", e)
