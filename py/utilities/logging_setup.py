import logging
import os
from py.utilities.setting import Config

import datetime
from flask import Blueprint, request, render_template, jsonify, redirect, url_for, session, current_app, Response
from py.utilities.setting import login_required
import re
import json

log_viewer_bp = Blueprint('log_viewer', __name__)

def log_event(level, message):
    """Write a simple timestamped log line to the configured LOG_FILE.

    This helper is lightweight and used by modules that want a simple
    append-only log (legacy behavior). It also forwards the message to
    the configured 'changes_logger' logger so entries appear in the
    structured logging system when configured.
    """
    try:
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        line = f"{timestamp} {level.upper()} {message}\n"
        with open(Config.LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)

        # Also send to structured logger if available
        logger = logging.getLogger('changes_logger')
        if level.lower() == 'info':
            logger.info(message)
        elif level.lower() == 'warning' or level.lower() == 'warn':
            logger.warning(message)
        elif level.lower() == 'error':
            logger.error(message)
        else:
            logger.debug(message)
    except Exception:
        # If logging fails, don't raise — this is best-effort
        pass

def configure_logging(app):
    # Ensure the log directory exists or create it
    log_dir = os.path.dirname(app.config['LOG_FILE'])
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = app.config['LOG_FILE']
    log_level = logging.INFO

    # Get the root logger and remove all handlers
    root_logger = logging.getLogger()
    root_logger.handlers = []

    # Remove existing handlers from the app logger
    for handler in app.logger.handlers[:]:
        app.logger.removeHandler(handler)

    # Create a new logger for the Flask app
    logger = app.logger
    logger.setLevel(log_level)
    logger.propagate = False

    # Create file and console handlers
    if not logger.handlers:
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
                message = record.getMessage()
                # Only filter messages that look like actual HTTP request logs
                # Pattern: IP - - [timestamp] "METHOD /path HTTP/version" status size
                return not (' - - [' in message and ('"GET /' in message or '"POST /' in message))

        # Add the filter to handlers
        file_handler.addFilter(IgnoreHTTPRequestFilter())
        console_handler.addFilter(IgnoreHTTPRequestFilter())

        # Add handlers to the logger
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    # Silence the default Flask request logger (werkzeug)
    werkzeug_log = logging.getLogger('werkzeug')
    werkzeug_log.setLevel(logging.ERROR)

    # Create a logger for changes and updates (use main log file)
    changes_logger = logging.getLogger('changes_logger')
    changes_logger.setLevel(logging.INFO)

    # Use the main log file for changes logging
    changes_log_file = Config.LOG_FILE
    changes_file_handler = logging.FileHandler(changes_log_file)
    changes_file_handler.setLevel(logging.INFO)

    # Create a formatter
    changes_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    changes_file_handler.setFormatter(changes_formatter)

    # Add the handler to the logger
    changes_logger.addHandler(changes_file_handler)


def format_logs(lines):
    formatted = []
    for line in lines:
        # Determine log class based on new format
        if '🔴' in line or 'SERVER_ERROR' in line or 'CLIENT_ERROR' in line:
            log_class = 'error'
        elif '🟢' in line or 'SUCCESS' in line:
            log_class = 'success'
        elif '🟡' in line or 'OTHER' in line:
            log_class = 'warning'
        elif 'ERROR' in line:
            log_class = 'error'
        elif 'INFO' in line:
            log_class = 'info'
        else:
            log_class = ''

        # Strip newline characters
        stripped_line = line.strip()

        # Highlight ***text*** in blue and remove ***
        stripped_line = re.sub(
            r'\*\*\*(.*?)\*\*\*',  # Match text between ***
            lambda m: f'<span class="highlight-blue">{m.group(1).upper()}</span>',  # Apply highlight and exclude ***
            stripped_line
        )

        # Highlight @@@text@@@ in purple and remove @@@
        stripped_line = re.sub(
            r'@@@(.*?)@@@',  # Match text between @@@
            r'<span class="highlight-purple">\1</span>',  # Apply highlight and exclude @@@
            stripped_line
        )

        # Highlight and capitalize the word after __
        stripped_line = re.sub(
            r'__(.*?)__\s*(.*)',  # Match the second occurrence of __ and capture everything after it
            lambda m: f'__{m.group(1)}__ <span class="highlight-red">{m.group(2).upper()}</span>',
            stripped_line
        )

        # Append the formatted line as a two-element tuple
        formatted.append((log_class, stripped_line))
    
    return formatted