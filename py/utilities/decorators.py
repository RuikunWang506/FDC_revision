"""Shared Flask route decorators."""
from functools import wraps

from flask import redirect, request, session, url_for


def login_required(f):
    """Decorator to require login for routes."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            session["next"] = request.url
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated_function
