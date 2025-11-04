from flask import (request, render_template, jsonify, redirect, url_for, session, flash, send_from_directory,send_file, Response, make_response,render_template_string)
from werkzeug.utils import secure_filename
# Standard library imports
import os
import re
import time
import json
import mysql.connector
from mysql.connector import Error
# External library imports
import pandas as pd
from slack_sdk import WebClient
from user_agents import parse
from googleapiclient.discovery import build
from google.oauth2 import service_account
# Application-specific imports
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection
from py.other.product_image import register_product_image_routes
from py.utilities.dam_routes import register_dam_routes
from py.Toolkits.Label_Maker import register_label_maker_routes
from py.Toolkits.view_all_routes import register_view_all_routes
from py.search.location_service import location_service_bp
from py.search.Location_massive_audit import location_massive_audit_bp
from py.search.container import container_bp
from py.search.item_lookup import item_lookup_bp
from py.other.mobile_lite import mobile_lite_bp
import py.Toolkits.Label_Maker, py.order.order_search, py.utilities.SQL_connector
from py.order.macy_special import process_orders
from datetime import timedelta
from py.utilities.setting import login_required  # Import the login_required decorator
import py.utilities.models
import zipfile
from py.utilities.setting import resize_image  # Import the master resizing function
from PIL import Image
from pathlib import Path

from py.Toolkits.V6listcreator import v6_listcreator_bp
from py.loris.lori_automation import lori_automation_bp
from py.other.customer import customer_bp
from py.order.order_processor import order_processor_bp
from py.utilities.auth import auth_bp
from py.utilities.logging_setup import log_event
from py.other.warehousemap import warehousemap_bp
from py.other.master_audit import master_audit_bp
from py.order.order_search import order_lookup_bp
from py.order.macy_special import macy_bp
from py.utilities.mysql_connector import mysql_connector_bp
from py.utilities.image_upload_routes import image_upload_bp
from py.other.master_image_viewer import master_image_viewer_bp
from py.dam.dam_sync_routes import dam_sync_bp
from py.other.bag_style_viewer import bag_style_viewer_bp
from py.Toolkits.boat import boat_service_bp
from py.utilities.monitor import send_discord_notification



def register_routes(app):

    register_product_image_routes(app)
    register_dam_routes(app)
    register_label_maker_routes(app)
    register_view_all_routes(app)
    app.register_blueprint(location_service_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(item_lookup_bp)
    app.register_blueprint(mobile_lite_bp)
    app.register_blueprint(v6_listcreator_bp)
    app.register_blueprint(lori_automation_bp, url_prefix='/lori_automation')
    app.register_blueprint(customer_bp)
    app.register_blueprint(order_processor_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(warehousemap_bp)
    app.register_blueprint(master_audit_bp)
    app.register_blueprint(order_lookup_bp)
    app.register_blueprint(macy_bp)
    app.register_blueprint(mysql_connector_bp)
    app.register_blueprint(image_upload_bp)
    app.register_blueprint(master_image_viewer_bp)
    app.register_blueprint(dam_sync_bp)
    app.register_blueprint(bag_style_viewer_bp)
    app.register_blueprint(location_massive_audit_bp)
    app.register_blueprint(boat_service_bp)


    app.permanent_session_lifetime = timedelta(days=30)

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def home():
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        
        current_user = session['username']
        return render_template("index.html", current_user=current_user)

    @app.route("/robots.txt")
    def robots_txt():
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        proto = request.environ.get("SERVER_PROTOCOL", "HTTP/1.1")
        log_event("INFO", f'{client_ip} "GET /robots.txt {proto}"')
        # app.static_folder points at your 'static' directory
        return send_from_directory(app.static_folder, "robots.txt")
    
    
    @app.route("/health")
    def health_check():
        """Health check endpoint for monitoring"""
        return jsonify({
            "status": "healthy",
            "timestamp": time.time(),
            "service": "FDC Flask App"
        })
    
    
    @app.route("/notify", methods=["POST"])
    @login_required
    def send_notification():
        """Send a Discord notification"""
        data = request.get_json()
        message = data.get("message", "")
        color = data.get("color", 0x00ff00)  # Default to green
        
        if not message:
            return jsonify({"error": "Message is required"}), 400
        
        try:
            send_discord_notification(message, color)
            return jsonify({"status": "Notification sent successfully"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


    @app.route('/log')
    def display():
        if 'username' not in session:
            return redirect(url_for("auth.login"))
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
            app.logger.error('Error accessing log file: %s', e)
            return jsonify(error=str(e)), 500

    @app.route('/log/search', methods=['POST'])
    def search_logs():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        
        search_term = request.form.get('search_term', '').strip()
        if not search_term:
            return redirect(url_for('display'))
        
        return redirect(url_for('display', search=search_term, page=1))

        
    @app.route('/log/stream', methods=['GET'])
    def stream_log():
       # if 'username' not in session:
       #     return redirect(url_for("auth.login"))
        def generate():
            with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                file.seek(0, 2)  # Move to the end of the file
                while True:
                    line = file.readline()
                    if not line:
                        time.sleep(1)  # Wait for new log lines
                        continue

                    # Format the new log line
                    formatted_line_tuple = format_logs([line.strip()])

                    # format_logs returns a list of tuples, take the first tuple
                    if formatted_line_tuple:
                        log_class, formatted_line = formatted_line_tuple[0]
                    else:
                        log_class, formatted_line = '', line.strip()

                    # Prepare the formatted line as a single string
                    # You can choose to send both log_class and formatted_line or just the formatted_line
                    # For simplicity, we'll send a JSON string with both
                    import json
                    data = json.dumps({
                        'log_class': log_class,
                        'formatted_line': formatted_line
                    })

                    yield f"data: {data}\n\n"  # SSE format: data:<line>
        return Response(generate(), mimetype='text/event-stream')
    
    def format_logs(lines):
        formatted = []
        for line in lines:
            # Determine log class
            if 'ERROR' in line:
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