from flask import request, render_template, session, flash, redirect, url_for
from py.utilities.setting import login_required

def register_view_all_routes(app):
    """Register view all routes with the Flask app."""

    @app.route("/list_routes", methods=["GET"])
    @login_required
    def list_routes():
        current_user = session['username']

        # Only allow admin users to access this route
        if current_user not in ['yeti', 'yetimaster']:
            flash("You don't have permission to access this page.")
            return redirect(url_for("home"))

        routes_list = []
        for rule in app.url_map.iter_rules():
            # Skip static files and POST-only routes
            if 'static' not in rule.endpoint and 'GET' in rule.methods:
                route_info = {
                    'endpoint': rule.endpoint,
                    'methods': ', '.join(sorted(rule.methods)),
                    'path': str(rule),
                    'blueprint': rule.endpoint.split('.')[0] if '.' in rule.endpoint else 'main'
                }
                routes_list.append(route_info)

        # Sort routes by blueprint and then by path
        routes_list.sort(key=lambda x: (x['blueprint'], x['path']))

        return render_template(
            "routes_list.html",
            routes=routes_list,
            current_user=current_user,
            title="Application Routes"
        )