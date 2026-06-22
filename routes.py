from flask import (request, render_template, jsonify, redirect, url_for, session, flash, send_from_directory,send_file, Response, make_response,render_template_string, has_request_context)
from werkzeug.utils import secure_filename
# Standard library imports
import os
import re
import time
import json
import importlib
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
from py.utilities.dam_routes import register_dam_routes
from py.Toolkits.Label_Maker import register_label_maker_routes
from py.Toolkits.view_all_routes import register_view_all_routes
from py.search.Location_massive_audit import location_massive_audit_bp
from py.search.container import container_bp
from py.search.item_lookup import item_lookup_bp
from product_detail_module import product_detail_bp
from py.search.outofstock_check import outofstock_check_bp
from py.other.mobile_lite import mobile_lite_bp
from py.utilities.pallet_detail import pallet_detail_bp
import py.Toolkits.Label_Maker, py.order.order_search, py.utilities.SQL_connector
from py.order.macy_special import process_orders
from datetime import timedelta
from py.utilities.setting import login_required  # Import the login_required decorator
import py.utilities.models
import zipfile
from py.utilities.setting import resize_image  # Import the master resizing function
from PIL import Image
from pathlib import Path

from py.Toolkits.V7listcreator import v7_listcreator_bp
from py.loris.lori_automation import lori_automation_bp
from py.other.customer import customer_bp
from py.order.order_processor import order_processor_bp
from py.utilities.auth import auth_bp
from py.utilities.logging_setup import log_event
from py.other.warehousemap import warehousemap_bp
from py.other.master_audit import master_audit_bp
from py.order.order_search import order_lookup_bp
from py.order.invoice_lookup import invoice_lookup_bp
from py.order.macy_special import macy_bp
from py.utilities.mysql_connector import mysql_connector_bp
from py.utilities.image_upload_routes import image_upload_bp
from py.other.master_image_viewer import master_image_viewer_bp
from py.other.clientview import clientview_bp
from py.dam.dam_sync_routes import dam_sync_bp
from py.other.bag_style_viewer import bag_style_viewer_bp
from py.Toolkits.boat import boat_service_bp
from py.search.location_service import location_service_bp
from py.other.external_stock_check import external_stock_check_bp
from py.other.internal_stock_check import internal_stock_check_bp
from py.other.inventory_manual_adjuster import inventory_manual_adjuster_bp
from py.other.burlington_highlight import burlington_highlight_bp
from py.other.mobile_scanner import mobile_scanner_bp
from py.other.burlington_label_processor import burlington_label_processor_bp
from py.other.vendor_app import vendor_app_bp
from py.other.vendor_tiktok_processor import vendor_tiktok_processor_bp
from py.other.email import email_bp
from py.utilities.monitor import send_discord_notification
from py.utilities import lan_detection

four_seller_bp = importlib.import_module('py.other.4_seller').four_seller_bp


def register_routes(app):

    @app.context_processor
    def inject_lan_context():
        if not has_request_context():
            return {}
        on_lan_host = lan_detection.host_is_private_lan(request.host)
        on_lan_ip = lan_detection.client_ip_is_private_lan(request)
        use_lan = on_lan_host or on_lan_ip
        origin = ''
        try:
            origin = request.url_root.rstrip('/') if use_lan else ''
        except RuntimeError:
            pass
        hint = getattr(Config, 'LAN_ORIGIN_HINT', '') or ''
        return {
            'fdc_is_lan_session': use_lan,
            'fdc_lan_current_origin': origin,
            'fdc_lan_origin_hint': hint,
        }

    @app.context_processor
    def inject_dashboard_acl():
        """Jinja: {% if tool_allowed('item_lookup') %} for home dashboard cards."""
        from flask import session
        from py.utilities.auth import get_user_permissions
        from py.utilities.dashboard_permissions import tool_visible_for_user

        def tool_allowed(tool_id: str) -> bool:
            uname = session.get("username")
            if not uname:
                return False
            perms = get_user_permissions(uname)
            return tool_visible_for_user(perms, uname, tool_id)

        from py.utilities.log_viewer_permissions import user_can_view_logs

        def can_view_logs() -> bool:
            return user_can_view_logs(session.get("username"))

        from py.other.clientview import user_hides_staff_nav_links

        def hide_staff_nav_links() -> bool:
            return user_hides_staff_nav_links(session.get("username"))

        return {
            "current_user": session.get("username"),
            "tool_allowed": tool_allowed,
            "can_view_logs": can_view_logs,
            "hide_staff_nav_links": hide_staff_nav_links,
        }

    @app.route('/delete_system_restarted', methods=['POST'])
    @login_required
    def delete_system_restarted():
        log_path = os.path.join(app.root_path, 'log.txt') if not os.path.isabs(Config.LOG_FILE) else Config.LOG_FILE
        try:
            # Read all lines except those containing 'system restarted'
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            filtered = [line for line in lines if 'system restarted' not in line.lower()]
            # Write back filtered lines
            with open(log_path, 'w', encoding='utf-8') as f:
                f.writelines(filtered)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    register_dam_routes(app)
    register_label_maker_routes(app)
    register_view_all_routes(app)
    app.register_blueprint(container_bp)
    app.register_blueprint(item_lookup_bp)
    app.register_blueprint(product_detail_bp)
    app.register_blueprint(outofstock_check_bp)
    app.register_blueprint(mobile_lite_bp)
    app.register_blueprint(v7_listcreator_bp)
    app.register_blueprint(lori_automation_bp, url_prefix='/lori_automation')
    app.register_blueprint(customer_bp)
    app.register_blueprint(order_processor_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(warehousemap_bp)
    app.register_blueprint(master_audit_bp)
    app.register_blueprint(order_lookup_bp)
    app.register_blueprint(invoice_lookup_bp)
    app.register_blueprint(macy_bp)
    app.register_blueprint(mysql_connector_bp)
    app.register_blueprint(external_stock_check_bp)
    app.register_blueprint(internal_stock_check_bp)
    app.register_blueprint(inventory_manual_adjuster_bp)
    app.register_blueprint(image_upload_bp)
    app.register_blueprint(master_image_viewer_bp)
    app.register_blueprint(clientview_bp)
    app.register_blueprint(dam_sync_bp)
    app.register_blueprint(bag_style_viewer_bp)
    app.register_blueprint(location_massive_audit_bp)
    app.register_blueprint(boat_service_bp)
    app.register_blueprint(location_service_bp)
    app.register_blueprint(pallet_detail_bp)
    app.register_blueprint(burlington_highlight_bp)
    app.register_blueprint(mobile_scanner_bp)
    app.register_blueprint(burlington_label_processor_bp)
    app.register_blueprint(vendor_app_bp)
    app.register_blueprint(vendor_tiktok_processor_bp)
    app.register_blueprint(four_seller_bp)
    app.register_blueprint(email_bp)

    app.permanent_session_lifetime = timedelta(days=30)

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def home():
        from py.other.clientview import is_client_portal_only_user

        current_user = session["username"]
        portal_minimal = is_client_portal_only_user(current_user)
        portal_ctx: dict = {}
        if portal_minimal:
            from py.utilities.customer_purchase_access import build_portal_home_context

            portal_ctx = build_portal_home_context(current_user)
        return render_template(
            "index.html",
            current_user=current_user,
            portal_minimal=portal_minimal,
            **portal_ctx,
        )

    @app.route("/robots.txt")
    def robots_txt():
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        proto = request.environ.get("SERVER_PROTOCOL", "HTTP/1.1")
        # app.static_folder points at your 'static' directory
        return send_from_directory(app.static_folder, "robots.txt")

    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            app.static_folder,
            "icons/favicon-fdc.png",
            mimetype="image/png",
        )

    @app.route("/health")
    def health_check():
        """Health check endpoint for monitoring"""
        return jsonify({
            "status": "healthy",
            "timestamp": time.time(),
            "service": "FDC Flask App"
        })
    
    @app.route("/tk")
    def tk_redirect():
        """Redirect /tk to /master_image_viewer"""
        return redirect(url_for('master_image_viewer.master_image_viewer'))

    @app.route("/tiktok", defaults={"subpath": ""})
    @app.route("/tiktok/", defaults={"subpath": ""})
    @app.route("/tiktok/<path:subpath>")
    def legacy_tiktok_redirect(subpath=""):
        """Old /tiktok URLs → home or client product detail."""
        target = "/"
        if subpath:
            if subpath == "tk_product_detail":
                target = "/clientview/product_detail"
            elif subpath.startswith("tk_product_detail/"):
                target = "/clientview/product_detail/" + subpath[len("tk_product_detail/") :]
            elif subpath not in ("", "/"):
                target = f"/clientview/{subpath}"
        qs = request.query_string.decode()
        if qs:
            target = f"{target}?{qs}"
        return redirect(target, code=308)
    
    
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
    @login_required
    def display():
        from py.utilities.log_viewer_permissions import enforce_log_viewer_access

        current_user = session['username']
        denied = enforce_log_viewer_access(current_user)
        if denied:
            return denied
        search_term = request.args.get('search', '').strip()

        try:
            with open(Config.LOG_FILE, 'r', encoding='utf-8', errors='replace') as file:
                lines = file.readlines()

            if search_term:
                raw_lines = [ln for ln in lines if search_term.lower() in ln.lower()]
            else:
                raw_lines = lines

            formatted_lines = format_logs(raw_lines)
            reversed_lines = list(reversed(formatted_lines))

            per_page = 100
            n = len(reversed_lines)
            total_pages = max(1, (n + per_page - 1) // per_page)
            page = request.args.get('page', 1, type=int) or 1
            page = max(1, min(page, total_pages))
            start = (page - 1) * per_page
            paginated_log_lines = reversed_lines[start : start + per_page]

            return render_template(
                'log_viewer.html',
                log_lines=paginated_log_lines,
                page=page,
                total_pages=total_pages,
                current_user=current_user,
                search_term=search_term,
            ), 200

        except Exception as e:
            app.logger.error('Error accessing log file: %s', e)
            return jsonify(error=str(e)), 500

    @app.route('/log/search', methods=['POST'])
    @login_required
    def search_logs():
        from py.utilities.log_viewer_permissions import enforce_log_viewer_access

        denied = enforce_log_viewer_access(session.get("username"))
        if denied:
            return denied
        search_term = request.form.get('search_term', '').strip()
        if not search_term:
            return redirect(url_for('display'))
        return redirect(url_for('display', search=search_term, page=1))

        
    @app.route('/log/stream', methods=['GET'])
    @login_required
    def stream_log():
        """Server-Sent Events: tail LOG_FILE for live /log (anti-buffer headers + keepalive)."""
        from py.utilities.log_viewer_permissions import user_can_view_logs
        from flask import jsonify

        if not user_can_view_logs(session.get("username")):
            return jsonify({"error": "Forbidden"}), 403
        log_path = Config.LOG_FILE
        idle_sleep_s = 0.35
        keepalive_every_s = 25.0

        def generate():
            last_keepalive = time.monotonic()
            with open(log_path, 'r', encoding='utf-8', errors='replace') as file:
                file.seek(0, 2)
                while True:
                    line = file.readline()
                    if line:
                        last_keepalive = time.monotonic()
                        formatted_line_tuple = format_logs([line.strip()])
                        if formatted_line_tuple:
                            log_cls, formatted_line = formatted_line_tuple[0]
                        else:
                            log_cls, formatted_line = '', line.strip()
                        chunk = json.dumps(
                            {'log_class': log_cls, 'formatted_line': formatted_line},
                            ensure_ascii=False,
                        )
                        yield f'data: {chunk}\n\n'
                        continue
                    now = time.monotonic()
                    if now - last_keepalive >= keepalive_every_s:
                        yield ':\n\n'
                        last_keepalive = now
                    time.sleep(idle_sleep_s)

        resp = Response(generate(), mimetype='text/event-stream')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['X-Accel-Buffering'] = 'no'
        resp.headers['Connection'] = 'keep-alive'
        return resp
    
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

    @app.route('/get_favorites')
    @login_required
    def get_favorites():
        user = session['username']
        try:
            conn = get_mysql_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT favorites FROM users WHERE username = %s", (user,))
            row = cursor.fetchone()
            favorites = json.loads(row[0]) if row and row[0] else []
            return jsonify(favorites)
        except Exception as e:
            return jsonify([])
        finally:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()

    @app.route('/save_favorites', methods=['POST'])
    @login_required
    def save_favorites():
        user = session['username']
        data = request.get_json()
        favorites = data.get('favorites', [])
        try:
            conn = get_mysql_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET favorites = %s WHERE username = %s", (json.dumps(favorites), user))
            conn.commit()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False})
        finally:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()

    @app.before_request
    def _enforce_dashboard_tool_permissions():
        from py.utilities.dashboard_permissions import enforce_dashboard_tool_access

        return enforce_dashboard_tool_access()