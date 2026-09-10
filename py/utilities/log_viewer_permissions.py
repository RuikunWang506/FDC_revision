"""Who may open /log (log viewer). Controlled per group in manage_users."""

from __future__ import annotations

from typing import Optional

# Staff groups that may view logs when group permissions are unrestricted (NULL).
LOG_VIEWER_ALLOWED_GROUP_SLUGS = frozenset(
    {"yeti", "superadmin", "manager", "warehouse", "visitor"}
)


def get_user_group_slug(username: Optional[str]) -> Optional[str]:
    uname = (username or "").strip()
    if not uname:
        return None
    from py.utilities.database_utils import get_db_connection
    from mysql.connector import Error

    connection = get_db_connection()
    if not connection:
        return None
    try:
        cur = connection.cursor(dictionary=True)
        cur.execute(
            """
            SELECT ug.slug AS slug
            FROM users u
            LEFT JOIN user_groups ug ON u.user_group_id = ug.id
            WHERE u.username = %s
            LIMIT 1
            """,
            (uname,),
        )
        row = cur.fetchone()
        cur.close()
        if row and row.get("slug"):
            return str(row["slug"]).strip().lower()
    except Error:
        pass
    finally:
        try:
            if connection.is_connected():
                connection.close()
        except Exception:
            pass
    return None


def user_can_view_logs(username: Optional[str]) -> bool:
    uname = (username or "").strip()
    if not uname:
        return False
    if uname.lower() == "yeti":
        return True

    from py.utilities.auth import get_user_permissions

    perms = get_user_permissions(uname)
    if perms.get("log_viewer_allowed"):
        return True

    # Unrestricted group (NULL permissions JSON): allow only staff slugs above.
    if not perms:
        slug = get_user_group_slug(uname)
        return bool(slug and slug in LOG_VIEWER_ALLOWED_GROUP_SLUGS)

    return False


def enforce_log_viewer_access(username: Optional[str]):
    """Flask response tuple (template, code) or None if allowed."""
    from flask import flash, redirect, url_for

    if user_can_view_logs(username):
        return None
    flash("You do not have permission to view the application log.", "warning")
    return redirect(url_for("home"))
