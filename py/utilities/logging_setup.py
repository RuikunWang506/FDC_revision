import logging
import os
from py.utilities.setting import Config

import datetime
from flask import Blueprint, request, render_template, jsonify, redirect, url_for, session, current_app, Response
from py.utilities.setting import login_required
import re

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


@log_viewer_bp.route('/log')
@login_required
def display():
    current_user = session['username']
    search_term = request.args.get('search', '').strip()

    try:
        with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
            lines = file.readlines()
        
        # If there's a search term, filter logs across all pages
        if search_term:
            filtered_lines = []
            for line in lines:
                if search_term.lower() in line.lower():
                    filtered_lines.append(line)
            
            formatted_lines = format_logs(filtered_lines)
            reversed_lines = list(reversed(formatted_lines))
            
            # Return search results with pagination
            per_page = 100
            page = request.args.get('page', 1, type=int)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_log_lines = reversed_lines[start:end]
            
            # Calculate total pages for search results
            total_pages = len(reversed_lines) // per_page + (1 if len(reversed_lines) % per_page > 0 else 0)
            
            return render_template('log_viewer.html', 
                                log_lines=paginated_log_lines, 
                                page=page, 
                                total_pages=total_pages,
                                current_user=current_user,
                                search_term=search_term), 200
        else:
            # Original pagination logic for all logs
            formatted_lines = format_logs(lines)
            reversed_lines = list(reversed(formatted_lines))
            
            per_page = 100
            page = request.args.get('page', 1, type=int)
            start = (page - 1) * per_page
            end = start + per_page
            paginated_log_lines = reversed_lines[start:end]
            
            total_pages = len(reversed_lines) // per_page + (1 if len(reversed_lines) % per_page > 0 else 0)
            
            return render_template('log_viewer.html', 
                                log_lines=paginated_log_lines, 
                                page=page, 
                                total_pages=total_pages,
                                current_user=current_user,
                                search_term=''), 200

    except Exception as e:
        current_app.logger.error('Error accessing log file: %s', e)
        return jsonify(error=str(e)), 500


@log_viewer_bp.route('/log/search', methods=['POST'])
@login_required
def search_logs():
    search_term = request.form.get('search_term', '').strip()
    if not search_term:
        return redirect(url_for('.display'))
    
    return redirect(url_for('.display', search=search_term, page=1))


@log_viewer_bp.route('/log/live')
@login_required
def live_logs():
    """Stream live log events using Server-Sent Events"""
    def generate():
        # Get initial log content
        try:
            with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                lines = file.readlines()
            
            # Send last 50 lines as initial data
            recent_lines = lines[-50:] if len(lines) > 50 else lines
            for line in recent_lines:
                formatted_lines = format_logs([line])
                if formatted_lines:
                    log_class, stripped_line = formatted_lines[0]
                    yield f"data: {stripped_line}\n\n"
        except Exception as e:
            yield f"data: Error reading log file: {str(e)}\n\n"

        # Keep connection alive and check for new log entries
        last_size = os.path.getsize(Config.LOG_FILE) if os.path.exists(Config.LOG_FILE) else 0
        
        while True:
            try:
                current_size = os.path.getsize(Config.LOG_FILE) if os.path.exists(Config.LOG_FILE) else 0
                
                if current_size > last_size:
                    # New content added
                    with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                        file.seek(last_size)
                        new_content = file.read()
                    
                    if new_content:
                        new_lines = new_content.strip().split('\n')
                        for line in new_lines:
                            if line.strip():
                                formatted_lines = format_logs([line])
                                if formatted_lines:
                                    log_class, stripped_line = formatted_lines[0]
                                    yield f"data: {stripped_line}\n\n"
                    
                    last_size = current_size
                
                # Send heartbeat every 30 seconds to keep connection alive
                import time
                time.sleep(1)
                # Yield empty data every 30 seconds to keep connection alive
                yield f"data: \n\n"
                
            except Exception as e:
                yield f"data: Error in live stream: {str(e)}\n\n"
                break

    return Response(generate(), mimetype='text/event-stream')