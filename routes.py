from flask import (request, render_template, jsonify, redirect, url_for, session, send_from_directory, Response, has_request_context)
# Standard library imports
import os
import re
import time
import json
from datetime import timedelta
# Application-specific imports (core only — optional tools load via module_loader)
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import login_required
from py.utilities.auth import auth_bp
from py.utilities.logging_setup import log_event
from py.utilities.monitor import send_discord_notification
from py.utilities import lan_detection
from py.utilities.i18n import inject_i18n_context, set_locale as set_user_locale, client_i18n_bundle
from py.utilities.module_loader import (
    register_optional_modules,
    inject_module_template_helpers,
    health_modules_payload,
    tool_id_available,
    module_is_loaded,
)


def register_routes(app):

    @app.context_processor
    def inject_i18n():
        ctx = inject_i18n_context()
        ctx["client_i18n_bundle"] = client_i18n_bundle
        return ctx

    @app.route("/set_locale/<locale>", methods=["GET"])
    def set_locale(locale):
        from flask import make_response, redirect, request, url_for

        loc = set_user_locale(locale)
        next_url = (request.args.get("next") or "").strip()
        if not next_url or not next_url.startswith("/"):
            next_url = url_for("home")
        resp = make_response(redirect(next_url))
        resp.set_cookie("fdc_locale", loc, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

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
        from py.utilities.dashboard_backgrounds import (
            get_dashboard_background,
            list_dashboard_backgrounds,
        )
        from py.utilities.dashboard_permissions import (
            get_dashboard_favorites,
            navbar_items_from_favorites,
            tool_visible_for_user,
        )

        uname = session.get("username")
        perms = get_user_permissions(uname) if uname else {}

        def tool_allowed(tool_id: str) -> bool:
            if not uname:
                return False
            if not tool_id_available(app, tool_id):
                return False
            return tool_visible_for_user(perms, uname, tool_id)

        from py.utilities.log_viewer_permissions import user_can_view_logs

        def can_view_logs() -> bool:
            return user_can_view_logs(session.get("username"))

        try:
            from py.other.clientview import user_hides_staff_nav_links, product_detail_search_url
        except Exception:
            def user_hides_staff_nav_links(_username=None):
                return False

            def product_detail_search_url():
                return "/product_detail"

        def hide_staff_nav_links() -> bool:
            try:
                return user_hides_staff_nav_links(session.get("username"))
            except Exception:
                return False

        _pd_search = product_detail_search_url

        def product_detail_search_url_safe():
            try:
                return _pd_search()
            except Exception:
                return "/product_detail"

        dashboard_favorites = get_dashboard_favorites(uname) if uname else []
        navbar_favorite_items = []
        if uname and not hide_staff_nav_links():
            navbar_favorite_items = navbar_items_from_favorites(dashboard_favorites, tool_allowed)

        master_mode = False
        master_username = ""
        master_impersonating = False
        master_user_choices = []
        try:
            from py.utilities.master_mode import (
                is_impersonating,
                is_master_operator,
                list_switchable_users,
            )

            master_mode = is_master_operator()
            master_username = (session.get("master_username") or "") if master_mode else ""
            master_impersonating = is_impersonating()
            if master_mode:
                master_user_choices = list_switchable_users()
        except Exception:
            pass

        return {
            "current_user": uname,
            "tool_allowed": tool_allowed,
            "can_view_logs": can_view_logs,
            "hide_staff_nav_links": hide_staff_nav_links,
            "product_detail_search_url": product_detail_search_url_safe,
            "dashboard_favorites": dashboard_favorites,
            "navbar_favorite_items": navbar_favorite_items,
            "dashboard_bg": get_dashboard_background(uname),
            "dashboard_backgrounds": list_dashboard_backgrounds(uname),
            "master_mode": master_mode,
            "master_username": master_username,
            "master_impersonating": master_impersonating,
            "master_user_choices": master_user_choices,
        }

    @app.route('/delete_system_restarted', methods=['POST'])
    @login_required
    def delete_system_restarted():
        from py.utilities.log_viewer_permissions import user_can_view_logs

        if not user_can_view_logs(session.get("username")):
            return jsonify({"success": False, "error": "Access denied"}), 403
        log_path = app.config.get('SYSTEM_LOG_FILE') or Config.SYSTEM_LOG_FILE
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            filtered = [line for line in lines if 'system restarted' not in line.lower()]
            with open(log_path, 'w', encoding='utf-8') as f:
                f.writelines(filtered)
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})

    app.register_blueprint(auth_bp)
    try:
        from py.utilities.log_center import log_center_bp
        from py.utilities.presence import presence_bp
        from py.utilities.help_routes import help_bp

        app.register_blueprint(log_center_bp)
        app.register_blueprint(presence_bp)
        app.register_blueprint(help_bp)
    except Exception as e:
        app.logger.error('Failed to register log/help/presence blueprints: %s', e)
    inject_module_template_helpers(app)
    register_optional_modules(app)

    app.permanent_session_lifetime = timedelta(days=30)

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def home():
        current_user = session["username"]
        portal_minimal = False
        try:
            from py.other.clientview import is_client_portal_only_user

            portal_minimal = bool(is_client_portal_only_user(current_user))
        except Exception:
            portal_minimal = False
        portal_ctx: dict = {}
        if portal_minimal:
            try:
                from py.utilities.customer_purchase_access import build_portal_home_context

                portal_ctx = build_portal_home_context(current_user)
            except Exception:
                portal_ctx = {}
        dashboard_favorites: list = []
        recent_arrivals: list = []
        bulletin_preview: list = []
        if not portal_minimal:
            from py.utilities.dashboard_permissions import get_dashboard_favorites

            dashboard_favorites = get_dashboard_favorites(current_user)
            try:
                from py.Toolkits.boat import recent_arrivals_for_dashboard

                recent_arrivals = recent_arrivals_for_dashboard(limit=8)
            except Exception:
                recent_arrivals = []
            try:
                from py.utilities.dashboard_permissions import tool_visible_for_user
                from py.utilities.auth import get_user_permissions

                if tool_visible_for_user(
                    get_user_permissions(current_user), current_user, "bulletin"
                ):
                    from py.other.bulletin import preview_posts, serialize_post

                    bulletin_preview = [
                        serialize_post(post, current_user)
                        for post in preview_posts(current_user)
                    ]
            except Exception:
                bulletin_preview = []
        return render_template(
            "index.html",
            current_user=current_user,
            portal_minimal=portal_minimal,
            dashboard_favorites=dashboard_favorites,
            recent_arrivals=recent_arrivals,
            bulletin_preview=bulletin_preview,
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
        """Health check endpoint for monitoring. Core up => healthy; failed tools listed separately."""
        modules = health_modules_payload(app)
        return jsonify({
            "status": "healthy",
            "degraded": bool(modules.get("failed_count")),
            "timestamp": time.time(),
            "service": "FDC Flask App",
            "modules": modules,
        })
    
    @app.route("/tk")
    def tk_redirect():
        """Redirect /tk to /master_image_viewer"""
        if module_is_loaded(app, "master_image_viewer"):
            return redirect(url_for('master_image_viewer.master_image_viewer'))
        return redirect(url_for("home"))

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


    @app.route('/get_favorites')
    @login_required
    def get_favorites():
        from py.utilities.dashboard_permissions import get_dashboard_favorites

        return jsonify(get_dashboard_favorites(session.get('username') or ''))

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

    @app.route('/save_dashboard_background', methods=['POST'])
    @login_required
    def save_dashboard_background():
        from py.utilities.dashboard_backgrounds import save_dashboard_background as persist_bg

        data = request.get_json(silent=True) or {}
        result = persist_bg(session.get('username') or '', data.get('id') or '')
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.route('/user_dashboard_background')
    @login_required
    def user_dashboard_background():
        from flask import send_file
        from py.utilities.dashboard_backgrounds import (
            custom_background_exists,
            custom_background_path,
        )

        username = session.get('username') or ''
        if not custom_background_exists(username):
            return ('', 404)
        return send_file(
            custom_background_path(username),
            mimetype='image/jpeg',
            max_age=0,
            download_name='dashboard-background.jpg',
        )

    @app.route('/dashboard_background_from_url', methods=['POST'])
    @login_required
    def dashboard_background_from_url():
        from py.utilities.dashboard_backgrounds import fetch_public_image_jpeg, jpeg_data_url

        data = request.get_json(silent=True) or {}
        url = (data.get('url') or '').strip()
        try:
            jpeg = fetch_public_image_jpeg(url)
            return jsonify({'success': True, 'data_url': jpeg_data_url(jpeg)})
        except Exception as exc:
            return jsonify({'success': False, 'error': str(exc) or 'Could not load that URL.'}), 400

    @app.route('/upload_dashboard_background', methods=['POST'])
    @login_required
    def upload_dashboard_background():
        from py.utilities.dashboard_backgrounds import save_custom_background

        upload = request.files.get('image')
        raw = upload.read() if upload else b''
        result = save_custom_background(session.get('username') or '', raw)
        status = 200 if result.get('success') else 400
        return jsonify(result), status

    @app.before_request
    def _ensure_yetimaster_session():
        from py.utilities.master_mode import enable_master_session

        uname = (session.get('username') or '').strip().lower()
        if uname == 'yetimaster' and not session.get('master_username'):
            enable_master_session()

    @app.before_request
    def _enforce_dashboard_tool_permissions():
        from py.utilities.dashboard_permissions import enforce_dashboard_tool_access

        return enforce_dashboard_tool_access()