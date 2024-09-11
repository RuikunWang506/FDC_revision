import logging
import os

def configure_logging(app):
    # Ensure the log directory exists or create it
    log_dir = os.path.dirname(app.config['LOG_FILE'])
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = app.config['LOG_FILE']
    log_level = logging.INFO

    # Remove existing handlers from the app logger
    for handler in app.logger.handlers[:]:
        app.logger.removeHandler(handler)

    # Create a new logger for the Flask app
    logger = app.logger
    logger.setLevel(log_level)

    # Create file and console handlers
    file_handler = logging.FileHandler(log_file)
    console_handler = logging.StreamHandler()

    file_handler.setLevel(log_level)
    console_handler.setLevel(log_level)

    # Standard log formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Custom filter to ignore HTTP GET and POST requests
    class IgnoreHTTPRequestFilter(logging.Filter):
        def filter(self, record):
            return not ('GET /' in record.getMessage() or 'POST /' in record.getMessage())

    # Add the filter to handlers
    file_handler.addFilter(IgnoreHTTPRequestFilter())
    console_handler.addFilter(IgnoreHTTPRequestFilter())

    # Add handlers to the logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # Silence the default Flask request logger (werkzeug)
    werkzeug_log = logging.getLogger('werkzeug')
    werkzeug_log.setLevel(logging.ERROR)

