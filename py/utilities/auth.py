from flask import (
    Blueprint,
    request,
    render_template,
    jsonify,
    redirect,
    url_for,
    session,
    flash,
    current_app,
    make_response,
)
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import mysql.connector
from mysql.connector import Error
import requests
from user_agents import parse
import re
import secrets
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection
from py.utilities.logging_setup import log_event
from datetime import datetime, timedelta
from py.utilities.setting import Config
import json
from py.utilities.setting import login_required

auth_bp = Blueprint('auth', __name__, template_folder='templates')

@auth_bp.before_request
def log_request():
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    method = request.method
    path = request.path
    http_version = request.environ.get('SERVER_PROTOCOL', 'HTTP/1.1')
    
    # Store the request details for after_request logging (don't log here)
    request._logging_details = {
        'client_ip': client_ip,
        'method': method,
        'path': path,
        'http_version': http_version
    }

@auth_bp.after_request
def log_response(response):
    if hasattr(request, '_logging_details'):
        details = request._logging_details
        status_code = response.status_code
        
        # Skip logging for server IP
        if details['client_ip'] == '173.198.50.202':
            return response
        
        # Skip logging successful login attempts (only log failures)
        if details['path'] == '/login' and 200 <= status_code < 300:
            return response
        
        # Get username if available
        username = session.get('username', 'anonymous')
        
        # Format log message with color coding
        if 200 <= status_code < 300:
            # Success - green with username
            log_message = f'🟢 SUCCESS: {details["client_ip"]} "{details["method"]} {details["path"]} {details["http_version"]}" {status_code} [User: {username}]'
        elif 400 <= status_code < 500:
            # Client error - red
            log_message = f'🔴 CLIENT_ERROR: {details["client_ip"]} "{details["method"]} {details["path"]} {details["http_version"]}" {status_code}'
        elif 500 <= status_code < 600:
            # Server error - red
            log_message = f'🔴 SERVER_ERROR: {details["client_ip"]} "{details["method"]} {details["path"]} {details["http_version"]}" {status_code}'
        else:
            # Other status codes
            log_message = f'🟡 OTHER: {details["client_ip"]} "{details["method"]} {details["path"]} {details["http_version"]}" {status_code}'
        
        log_event('INFO', log_message)
    return response
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # If user is already logged in, redirect to home
    if 'username' in session:
        return redirect(url_for("home"))
    
    # Get the next page from query parameters or referrer
    next_page = request.args.get('next') or request.referrer
    
    # Store the next page in session
    if next_page and not next_page.endswith('/login'):
        session['next'] = next_page
    
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    location_data = get_location(client_ip)

    def login_page(**extra):
        ctx = {
            "client_ip": client_ip,
            "location": location_data,
            "turnstile_site_key": Config.TURNSTILE_SITE_KEY or None,
        }
        ctx.update(extra)
        return render_template("login.html", **ctx)
    
    if request.method == "POST":
        turnstile_token = request.form.get("cf-turnstile-response")
        client_ip_for_turnstile = (
            client_ip.split(",")[0].strip() if client_ip else None
        )
        if not verify_turnstile_response(turnstile_token, client_ip_for_turnstile):
            flash("Please complete the security check (CAPTCHA) and try again.")
            log_event(
                "WARNING",
                f"TURNSTILE_FAIL: IP: {client_ip}, UA: {request.headers.get('User-Agent', '')[:200]}",
            )
            return login_page()

        username = request.form.get("username")
        password = request.form.get("password")
        remember = request.form.get("remember")
        
        if check_credentials(username, password):
            update_last_login(username)
            # Check if user has an email address
            connection = get_db_connection()
            if connection:
                try:
                    cursor = connection.cursor()
                    cursor.execute("SELECT email FROM users WHERE username = %s", (username,))
                    result = cursor.fetchone()
                    
                    if not result or not result[0]:
                        # Store the next page in session before redirecting
                        session['next'] = next_page
                        session['username'] = username
                        return redirect(url_for('auth.update_email'))
                except Error as e:
                    print(f"Error checking email: {e}")
                finally:
                    if 'cursor' in locals():
                        cursor.close()
                    if connection and connection.is_connected():
                        connection.close()
            
            # Set session data after successful authentication
            session['username'] = username
            session['logged_in'] = True  # Add this for login_required decorator compatibility
            session['user_permissions'] = get_user_permissions(username)
            
            if remember:
                session.permanent = True
            else:
                session.permanent = False
            
            flash(f"Logged in as {username}")
            log_user_activity(username)
            
            # Redirect TMX users to gpginvoice page
            if username == 'tmx':
                return redirect(url_for("gpgtmx.gpginvoice"))

            from py.other.clientview import client_portal_home_url, is_client_portal_only_user

            if is_client_portal_only_user(username):
                session.pop("next", None)
                return redirect(client_portal_home_url())

            # For other users, redirect to stored next page or home
            redirect_url = session.pop('next', url_for("home"))
            return redirect(redirect_url)
        else:
            flash("Invalid username or password")
            # Log failed login attempt with detailed information
            log_event('WARNING', f'🔴 FAILED_LOGIN: Username: "{username}", Password: "[REDACTED]", IP: {client_ip}, Location: {location_data.get("city", "Unknown")}, User-Agent: {request.headers.get("User-Agent", "Unknown")}')
    
    return login_page()

@auth_bp.route("/logout")
def logout():
    session.pop('username', None)
    session.pop('logged_in', None)  # Clear logged_in flag too
    flash("Logged out")
    return redirect(url_for("auth.login"))

@auth_bp.route("/update_email", methods=["GET", "POST"])
@login_required
def update_email():
    username = session['username']
    connection = get_db_connection()
    
    if request.method == "POST":
        new_email = request.form.get("email")
        
        if not new_email or not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
            flash("Please enter a valid email address")
            return redirect(url_for("auth.update_email"))
        
        try:
            cursor = connection.cursor()
            cursor.execute("UPDATE users SET email = %s WHERE username = %s", (new_email, username))
            connection.commit()
            flash("Email updated successfully")
            current_app.logger.info(f"User {username} updated their email to {new_email}")
            # Redirect to stored next page or home
            redirect_url = session.pop('next', url_for("home"))
            return redirect(redirect_url)
        except Error as e:
            connection.rollback()
            flash(f"Error updating email: {e}")
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return render_template("update_email.html")

def _dashboard_tools_grouped(tools):
    """Group dashboard tool dicts by category; order follows first appearance in tools."""
    order = []
    by_cat = {}
    for t in tools:
        c = (t.get("category") or "").strip() or "Other"
        if c not in by_cat:
            order.append(c)
            by_cat[c] = []
        by_cat[c].append(t)
    return [(c, by_cat[c]) for c in order]


def _sidebar_users_ordered(users_list):
    """Sidebar nav: yeti first, then everyone else A→Z (case-insensitive)."""
    yeti_rows = []
    others = []
    for u in users_list:
        name = str((u or {}).get("username") or "").strip()
        if name.lower() == "yeti":
            yeti_rows.append(u)
        else:
            others.append(u)
    others.sort(key=lambda x: str((x or {}).get("username") or "").lower())
    return yeti_rows + others


def _mail_accounts_for_permissions_ui():
    """Mailboxes from MySQL for ACL checkboxes (same DB as users)."""
    conn = get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, label, email FROM mail_accounts ORDER BY label ASC, id ASC"
        )
        return list(cur.fetchall())
    except Error:
        return []
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        if conn.is_connected():
            conn.close()


def _cluster_users_by_group(users_sorted):
    blocks = []
    cur_key = None
    for u in users_sorted:
        gid = u.get("user_group_id")
        gname = u.get("group_name") or "Unassigned"
        gslug = u.get("group_slug") or ""
        key = (gid, gname, gslug)
        if key != cur_key:
            cur_key = key
            blocks.append(
                {
                    "group_id": gid,
                    "group_name": gname,
                    "group_slug": gslug,
                    "users": [],
                }
            )
        blocks[-1]["users"].append(u)
    return blocks


def _visitor_group_id(connection):
    cur = connection.cursor(dictionary=True)
    try:
        cur.execute("SELECT id FROM user_groups WHERE slug = %s LIMIT 1", ("visitor",))
        row = cur.fetchone()
        return row["id"] if row else None
    finally:
        cur.close()


@auth_bp.route("/manage_users", methods=["GET", "POST"])
@login_required
def manage_users():
    if session["username"] != "yeti":
        flash("Access denied. Only yeti can manage users.")
        return redirect(url_for("home"))

    connection = get_db_connection()
    if not connection:
        flash("Database connection error")
        return redirect(url_for("home"))

    list_cursor = None
    try:
        _ensure_user_groups_schema_once()
        from py.utilities.user_groups import (
            PROTECTED_DELETE_SLUGS,
            backfill_null_user_groups,
            fetch_groups_list,
            group_member_counts,
            parse_permissions_ui,
            slugify,
        )

        if request.method == "POST":
            act = request.form.get("action") or ""

            if act == "save_group_permissions":
                try:
                    gid_int = int(request.form.get("group_id") or 0)
                except (TypeError, ValueError):
                    flash("Invalid group.")
                    return redirect(url_for("auth.manage_users"))
                dashboard_restrict = request.form.get("dashboard_restrict") == "1"
                mail_restrict = request.form.get("mail_restrict") == "1"
                tools_sel = request.form.getlist("tools")
                mail_sel = []
                for x in request.form.getlist("mail_account_ids"):
                    try:
                        mail_sel.append(int(x))
                    except (TypeError, ValueError):
                        continue
                client_pd_restrict = request.form.get("client_pd_restrict") == "1"
                client_pd_sections_sel = request.form.getlist(
                    "client_product_detail_sections"
                )
                log_viewer_allowed = request.form.get("log_viewer_allowed") == "1"
                cur = connection.cursor(dictionary=True)
                try:
                    cur.execute("SELECT slug FROM user_groups WHERE id = %s", (gid_int,))
                    gr = cur.fetchone()
                    if not gr:
                        flash("Group not found.")
                        return redirect(url_for("auth.manage_users"))
                    base: dict = {}
                    cur.execute(
                        "SELECT permissions FROM user_groups WHERE id = %s", (gid_int,)
                    )
                    prow = cur.fetchone()
                    rawp = prow.get("permissions") if prow else None
                    if rawp:
                        try:
                            base = json.loads(rawp) if isinstance(rawp, str) else dict(rawp or {})
                        except json.JSONDecodeError:
                            base = {}
                    if not isinstance(base, dict):
                        base = {}
                    if dashboard_restrict:
                        base["tools"] = tools_sel
                    else:
                        base.pop("tools", None)
                    if mail_restrict:
                        base["mail_account_ids"] = mail_sel
                    else:
                        base.pop("mail_account_ids", None)
                    if client_pd_restrict:
                        base["client_product_detail_restrict"] = True
                        base["client_product_detail_sections"] = client_pd_sections_sel
                    else:
                        base.pop("client_product_detail_restrict", None)
                        base.pop("client_product_detail_sections", None)
                    base.pop("master_image_viewer_customer_purchase_only", None)
                    base.pop("client_product_detail_customer_purchase_only", None)
                    base.pop("master_image_viewer_hide_path", None)
                    if log_viewer_allowed:
                        base["log_viewer_allowed"] = True
                    else:
                        base.pop("log_viewer_allowed", None)
                    cur2 = connection.cursor()
                    cur2.execute(
                        "UPDATE user_groups SET permissions = %s WHERE id = %s",
                        (json.dumps(base), gid_int),
                    )
                    connection.commit()
                    cur2.close()
                    flash("Group permissions saved. All members of this group pick up changes immediately.")
                except Error as e:
                    connection.rollback()
                    flash(f"Error saving group permissions: {e}")
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                return redirect(url_for("auth.manage_users"))

            if act == "create_group":
                name = (request.form.get("name") or "").strip()
                slug_in = (request.form.get("slug") or "").strip()
                slug = slugify(slug_in or name)
                if not name:
                    flash("Group name is required.")
                    return redirect(url_for("auth.manage_users"))
                try:
                    cur = connection.cursor(dictionary=True)
                    cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM user_groups")
                    nxt = (cur.fetchone() or {}).get("n", 0)
                    cur.close()
                    cur2 = connection.cursor()
                    cur2.execute(
                        """
                        INSERT INTO user_groups (name, slug, permissions, sort_order)
                        VALUES (%s, %s, NULL, %s)
                        """,
                        (name, slug, int(nxt)),
                    )
                    connection.commit()
                    cur2.close()
                    flash(f'Created group "{name}".')
                except Error as e:
                    connection.rollback()
                    flash(f"Could not create group (duplicate slug?): {e}")
                return redirect(url_for("auth.manage_users"))

            if act == "copy_group":
                name = (request.form.get("name") or "").strip()
                slug = slugify((request.form.get("slug") or "").strip() or name)
                try:
                    source_id = int(request.form.get("source_group_id") or 0)
                except (TypeError, ValueError):
                    flash("Invalid source group.")
                    return redirect(url_for("auth.manage_users"))
                if not name:
                    flash("New group name is required.")
                    return redirect(url_for("auth.manage_users"))
                cur = connection.cursor(dictionary=True)
                try:
                    cur.execute(
                        "SELECT name, slug, permissions FROM user_groups WHERE id = %s",
                        (source_id,),
                    )
                    src = cur.fetchone()
                    if not src:
                        flash("Source group not found.")
                        return redirect(url_for("auth.manage_users"))
                    cur.execute(
                        "SELECT id FROM user_groups WHERE slug = %s LIMIT 1", (slug,)
                    )
                    if cur.fetchone():
                        flash(f'Group slug "{slug}" already exists — choose a different name.')
                        return redirect(url_for("auth.manage_users"))
                    perms_out = None
                    raw_perms = src.get("permissions")
                    if raw_perms is not None:
                        if isinstance(raw_perms, str):
                            perms_out = raw_perms
                        else:
                            perms_out = json.dumps(raw_perms)
                    cur.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM user_groups"
                    )
                    nxt = int((cur.fetchone() or {}).get("n", 0))
                    cur2 = connection.cursor()
                    cur2.execute(
                        """
                        INSERT INTO user_groups (name, slug, permissions, sort_order)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (name, slug, perms_out, nxt),
                    )
                    connection.commit()
                    cur2.close()
                    flash(
                        f'Created group "{name}" as a copy of "{src.get("name")}".'
                    )
                except Error as e:
                    connection.rollback()
                    flash(f"Could not copy group: {e}")
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                return redirect(url_for("auth.manage_users"))

            if act == "delete_group":
                try:
                    gid_int = int(request.form.get("group_id") or 0)
                except (TypeError, ValueError):
                    flash("Invalid group.")
                    return redirect(url_for("auth.manage_users"))
                cur = connection.cursor(dictionary=True)
                try:
                    cur.execute("SELECT slug FROM user_groups WHERE id = %s", (gid_int,))
                    gr = cur.fetchone()
                    if not gr:
                        flash("Group not found.")
                        return redirect(url_for("auth.manage_users"))
                    slug = str(gr.get("slug") or "")
                    if slug in PROTECTED_DELETE_SLUGS:
                        flash("That group cannot be deleted.")
                        return redirect(url_for("auth.manage_users"))
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM users WHERE user_group_id = %s",
                        (gid_int,),
                    )
                    cnt = (cur.fetchone() or {}).get("c", 0)
                    if cnt:
                        flash("Move or reassign all users out of this group before deleting it.")
                        return redirect(url_for("auth.manage_users"))
                    cur2 = connection.cursor()
                    cur2.execute("DELETE FROM user_groups WHERE id = %s", (gid_int,))
                    connection.commit()
                    cur2.close()
                    flash("Group deleted.")
                except Error as e:
                    connection.rollback()
                    flash(f"Error deleting group: {e}")
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                return redirect(url_for("auth.manage_users"))

            if act == "assign_user_group":
                try:
                    uid_int = int(request.form.get("user_id") or 0)
                    gid_int = int(request.form.get("group_id") or 0)
                except (TypeError, ValueError):
                    flash("Invalid selection.")
                    return redirect(url_for("auth.manage_users"))
                cur = connection.cursor(dictionary=True)
                try:
                    cur.execute(
                        "SELECT username FROM users WHERE id = %s",
                        (uid_int,),
                    )
                    ur = cur.fetchone()
                    cur.execute(
                        "SELECT slug FROM user_groups WHERE id = %s",
                        (gid_int,),
                    )
                    gr = cur.fetchone()
                    if not ur or not gr:
                        flash("User or group not found.")
                        return redirect(url_for("auth.manage_users"))
                    uname_l = str(ur.get("username") or "").strip().lower()
                    gslug = str(gr.get("slug") or "").strip().lower()
                    if gslug == "yeti" and uname_l != "yeti":
                        flash('Only the "yeti" account may belong to the Yeti group.')
                        return redirect(url_for("auth.manage_users"))
                    if uname_l == "yeti" and gslug != "yeti":
                        flash('User yeti must remain in the Yeti group.')
                        return redirect(url_for("auth.manage_users"))
                    cur2 = connection.cursor()
                    cur2.execute(
                        "UPDATE users SET user_group_id = %s WHERE id = %s",
                        (gid_int, uid_int),
                    )
                    connection.commit()
                    cur2.close()
                    flash("User group updated.")
                except Error as e:
                    connection.rollback()
                    flash(f"Error updating group: {e}")
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                return redirect(url_for("auth.manage_users"))

            elif "update_email" in request.form:
                user_id = request.form.get("user_id")
                new_email = request.form.get("new_email")

                if new_email and not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
                    flash("Please enter a valid email address")
                else:
                    try:
                        cursor = connection.cursor()
                        cursor.execute("UPDATE users SET email = %s WHERE id = %s", (new_email, user_id))
                        connection.commit()
                        flash("Email updated successfully")
                    except Error as e:
                        connection.rollback()
                        flash(f"Error updating email: {e}")
                    finally:
                        if "cursor" in locals():
                            cursor.close()
            else:
                username = request.form.get("username")
                password = request.form.get("password")
                email = request.form.get("email", "")
                try:
                    new_gid = int(request.form.get("user_group_id") or 0)
                except (TypeError, ValueError):
                    new_gid = 0

                if not username or not password:
                    flash("Username and password are required")
                elif email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                    flash("Please enter a valid email address if providing one")
                else:
                    gid_insert = None
                    dc = connection.cursor(dictionary=True)
                    try:
                        if new_gid:
                            dc.execute(
                                "SELECT slug FROM user_groups WHERE id = %s",
                                (new_gid,),
                            )
                            gs = dc.fetchone()
                            if not gs:
                                flash("Invalid group.")
                            elif str(gs.get("slug") or "").lower() == "yeti":
                                flash("New users cannot be added directly to the Yeti group.")
                            else:
                                gid_insert = new_gid
                        else:
                            gid_insert = _visitor_group_id(connection)
                    finally:
                        dc.close()

                    if gid_insert is None:
                        pass
                    else:
                        try:
                            cursor = connection.cursor()
                            cursor.execute(
                                "SELECT username FROM users WHERE username = %s",
                                (username,),
                            )
                            if cursor.fetchone():
                                flash("Username already exists")
                            else:
                                email_exists = False
                                if email:
                                    cursor.execute(
                                        "SELECT email FROM users WHERE email = %s",
                                        (email,),
                                    )
                                    if cursor.fetchone():
                                        flash("Email already exists")
                                        email_exists = True

                                if not email_exists:
                                    cursor.execute(
                                        """
                                        INSERT INTO users (username, password, email, created_at, user_group_id)
                                        VALUES (%s, %s, %s, NOW(), %s)
                                        """,
                                        (username, password, email, gid_insert),
                                    )
                                    connection.commit()
                                    flash("User added successfully")
                        except Error as e:
                            connection.rollback()
                            flash(f"Error adding user: {e}")
                        finally:
                            if "cursor" in locals():
                                cursor.close()

        from py.utilities.dashboard_permissions import DASHBOARD_TOOLS
        from py.utilities.client_product_detail_permissions import (
            CLIENT_PRODUCT_DETAIL_SECTIONS,
        )

        backfill_null_user_groups(connection)

        list_cursor = connection.cursor(dictionary=True)
        list_cursor.execute(
            """
            SELECT u.id, u.username, u.email, u.password, u.created_at, u.last_login_at,
                   u.user_group_id, u.customer_purchase_fdc_ids, u.customer_purchase_efashion_ids,
                   u.customer_purchase_include_approved_styles, u.customer_purchase_manual_styles,
                   u.master_image_viewer_customer_purchase_only,
                   u.client_product_detail_customer_purchase_only,
                   u.master_image_viewer_hide_path,
                   ug.name AS group_name, ug.slug AS group_slug, ug.sort_order AS group_sort
            FROM users u
            LEFT JOIN user_groups ug ON u.user_group_id = ug.id
            ORDER BY COALESCE(ug.sort_order, 999) ASC, ug.name ASC, u.username ASC
            """
        )
        users = list_cursor.fetchall()
        for u in users:
            ts = u.get("last_login_at")
            if ts is None:
                u["last_login_display"] = None
            elif isinstance(ts, datetime):
                u["last_login_display"] = ts.strftime("%Y-%m-%d %H:%M")
            else:
                u["last_login_display"] = str(ts)

        groups_raw = fetch_groups_list(connection)
        counts = group_member_counts(connection)
        group_ui_by_id: dict = {}
        groups_ui = []
        for g in groups_raw:
            ui = parse_permissions_ui(g.get("permissions"))
            group_ui_by_id[g["id"]] = ui
            groups_ui.append(
                {
                    "id": g["id"],
                    "name": g["name"],
                    "slug": g["slug"],
                    "sort_order": g.get("sort_order", 0),
                    "dashboard_restrict": ui["dashboard_restrict"],
                    "mail_restrict": ui["mail_restrict"],
                    "tools_sel": ui["tools_sel"],
                    "mail_sel": ui["mail_sel"],
                    "client_pd_restrict": ui["client_pd_restrict"],
                    "client_pd_sections_sel": ui["client_pd_sections_sel"],
                    "log_viewer_allowed": ui["log_viewer_allowed"],
                    "member_count": counts.get(g["id"], 0),
                }
            )

        def _effective_user_flag(user_row, col_name: str, group_key: str) -> bool:
            val = user_row.get(col_name)
            if val is not None:
                return bool(val)
            gid = user_row.get("user_group_id")
            if gid and gid in group_ui_by_id:
                return bool(group_ui_by_id[gid].get(group_key))
            return False

        for u in users:
            u["eff_miv_customer_purchase_only"] = _effective_user_flag(
                u,
                "master_image_viewer_customer_purchase_only",
                "master_image_viewer_customer_purchase_only",
            )
            u["eff_cpd_customer_purchase_only"] = _effective_user_flag(
                u,
                "client_product_detail_customer_purchase_only",
                "client_product_detail_customer_purchase_only",
            )
            u["eff_miv_hide_path"] = _effective_user_flag(
                u, "master_image_viewer_hide_path", "master_image_viewer_hide_path"
            )

        users_by_group = _cluster_users_by_group(users)
        mail_accounts = _mail_accounts_for_permissions_ui()
        dashboard_tools_grouped = _dashboard_tools_grouped(DASHBOARD_TOOLS)
        sidebar_users = _sidebar_users_ordered(users)
        all_groups_select = fetch_groups_list(connection)
        default_visitor_id = _visitor_group_id(connection)

        return render_template(
            "manage_users.html",
            users=users,
            users_by_group=users_by_group,
            groups_ui=groups_ui,
            all_groups_select=all_groups_select,
            default_visitor_id=default_visitor_id,
            sidebar_users=sidebar_users,
            current_user=session["username"],
            dashboard_tools_grouped=dashboard_tools_grouped,
            mail_accounts=mail_accounts,
            client_product_detail_sections=CLIENT_PRODUCT_DETAIL_SECTIONS,
        )
    except Error as e:
        flash(f"Error fetching users: {e}")
        return redirect(url_for("home"))
    finally:
        if list_cursor is not None:
            try:
                list_cursor.close()
            except Exception:
                pass
        if connection and connection.is_connected():
            connection.close()


@auth_bp.route("/request_password_reset", methods=["POST"])
def request_password_reset():
    try:
        data = request.get_json()
        identifier = data.get('identifier')
        
        if not identifier:
            return jsonify({"error": "Please enter your username or email address"}), 400
        
        connection = get_db_connection()
        if not connection:
            return jsonify({"error": "Database connection error"}), 500
        
        try:
            cursor = connection.cursor()
            
            # Check if the identifier is an email
            if '@' in identifier:
                cursor.execute("SELECT username, email FROM users WHERE email = %s", (identifier,))
            else:
                cursor.execute("SELECT username, email FROM users WHERE username = %s", (identifier,))
            
            result = cursor.fetchone()
            
            if result:
                username = result[0]
                email = result[1]
                
                if not email:
                    return jsonify({"error": "No email address associated with this account. Please contact support."}), 400
                
                token = generate_reset_token(username)
                reset_path = url_for("auth.reset_password", token=token)
                pub = getattr(Config, "PUBLIC_APP_ORIGIN", "https://www.fdcef.com").rstrip("/")
                reset_url = pub + reset_path
                
                # Send email with reset link
                subject = "Password Reset Request"
                body = f"""
                Hello {username},
                
                You recently requested to reset your password for your FDC Toolkit account.
                
                Please click the link below to reset your password:
                {reset_url}
                
                If you did not request a password reset, please ignore this email or contact support.
                
                This link will expire in 1 hour.
                """
                
                if send_email(email, subject, body):
                    current_app.logger.info(f"Password reset email sent to {email} for user {username}")
                    return jsonify({"message": f"Password reset link sent to {email}"}), 200
                else:
                    return jsonify({"error": "Error sending reset email"}), 500
            else:
                current_app.logger.info(f"Password reset request for non-existent user: {identifier}")
                return jsonify({"error": "No account found with that username or email"}), 404
        except Error as e:
            return jsonify({"error": f"Database error: {str(e)}"}), 500
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    except Exception as e:
        # Log the exception for debugging
        print(f"Unhandled exception in request_password_reset: {str(e)}")
        return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500

@auth_bp.route("/reset_password/<token>", methods=["GET", "POST"])
def reset_password(token):
    username = verify_reset_token(token)
    if not username:
        flash("Invalid or expired token")
        return redirect(url_for("auth.login"))
    
    if request.method == "POST":
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        
        if not password or not confirm_password:
            flash("Both password fields are required")
            return redirect(url_for("auth.reset_password", token=token))
        
        if password != confirm_password:
            flash("Passwords don't match")
            return redirect(url_for("auth.reset_password", token=token))
        
        connection = get_db_connection()
        if not connection:
            flash("Database connection error")
            return redirect(url_for("auth.reset_password", token=token))
        
        try:
            cursor = connection.cursor()
            cursor.execute("UPDATE users SET password = %s WHERE username = %s", (password, username))
            connection.commit()
            
            # Delete the used token
            cursor.execute("DELETE FROM password_resets WHERE token = %s", (token,))
            connection.commit()
            
            flash("Password updated successfully. You can now log in with your new password.")
            return redirect(url_for("auth.login"))
        except Error as e:
            connection.rollback()
            flash(f"Error updating password: {e}")
            return redirect(url_for("auth.reset_password", token=token))
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    # GET request - show the reset password form
    return render_template("reset_password.html", token=token, username=username)


def generate_reset_token(username):
    """Generate a secure token for password reset"""
    token = secrets.token_urlsafe(32)
    # Store token in database with expiration time
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            expires = datetime.now() + timedelta(hours=1)
            cursor.execute(
                "INSERT INTO password_resets (username, token, expires) VALUES (%s, %s, %s)",
                (username, token, expires)
            )
            connection.commit()
            return token
        except Error as e:
            print(f"Error storing reset token: {e}")
            return None
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    return None

def verify_reset_token(token):
    """Verify a password reset token"""
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT username FROM password_resets WHERE token = %s AND expires > NOW()",
                (token,)
            )
            result = cursor.fetchone()
            if result:
                return result[0]
        except Error as e:
            print(f"Error verifying reset token: {e}")
            return None
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    return None

# Database configuration
db_config = Config.DB_CONFIG

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    return get_mysql_connection()


def verify_turnstile_response(token, remote_ip=None):
    """Validate Cloudflare Turnstile token from the login form."""
    site_key = (getattr(Config, "TURNSTILE_SITE_KEY", None) or "").strip()
    secret = (getattr(Config, "TURNSTILE_SECRET_KEY", None) or "").strip()
    if not site_key or not secret:
        return True
    if not token:
        return False
    try:
        payload = {"secret": secret, "response": token}
        if remote_ip:
            payload["remoteip"] = remote_ip
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            timeout=10,
        )
        data = resp.json()
        return bool(data.get("success"))
    except (requests.RequestException, ValueError, TypeError) as e:
        current_app.logger.warning("Turnstile siteverify failed: %s", e)
        return False


def update_last_login(username):
    """Set users.last_login_at to NOW() after a successful password login."""
    if not username:
        return
    connection = get_db_connection()
    if not connection:
        return
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE users SET last_login_at = NOW() WHERE username = %s",
            (username,),
        )
        connection.commit()
    except Error as e:
        current_app.logger.warning(
            "update_last_login failed for %s — run sql/add_users_last_login_at.sql if column missing: %s",
            username,
            e,
        )
    finally:
        if "cursor" in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def check_credentials(username, password):
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            query = "SELECT password FROM users WHERE username = %s"
            cursor.execute(query, (username,))
            result = cursor.fetchone()
            if result:
                # User exists, check password
                if result[0] == password:
                    return True
                else:
                    return False
            else:
                # User doesn't exist
                return False
        except Error as e:
            print(f"Database error during credential check: {e}")
            return False
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    return False


_USER_GROUPS_SCHEMA_INITIALIZED = False


def _ensure_user_groups_schema_once() -> None:
    """Create user_groups / user_group_id once per process; seed defaults if empty."""
    global _USER_GROUPS_SCHEMA_INITIALIZED
    if _USER_GROUPS_SCHEMA_INITIALIZED:
        return
    connection = get_db_connection()
    if not connection:
        return
    try:
        from py.utilities.user_groups import (
            ensure_user_groups_schema,
            seed_user_groups_if_empty,
            backfill_null_user_groups,
        )

        ensure_user_groups_schema(connection)
        seed_user_groups_if_empty(connection)
        backfill_null_user_groups(connection)
        from py.utilities.user_groups import seed_client_portal_group

        seed_client_portal_group(connection)
        _USER_GROUPS_SCHEMA_INITIALIZED = True
    except Exception as e:
        print(f"user_groups schema init: {e}")
    finally:
        if connection.is_connected():
            connection.close()


def get_user_permissions(username):
    """Effective ACL JSON from the user's group (not per-user column). yeti = unrestricted."""
    uname = (username or "").strip()
    if uname.lower() == "yeti":
        return {}
    _ensure_user_groups_schema_once()
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT ug.permissions AS group_permissions
                FROM users u
                LEFT JOIN user_groups ug ON u.user_group_id = ug.id
                WHERE u.username = %s
                """,
                (uname,),
            )
            user_data = cursor.fetchone()
            if not user_data:
                return {}
            permissions_json = user_data.get("group_permissions")
            if permissions_json is None:
                return {}
            try:
                parsed = (
                    json.loads(permissions_json)
                    if isinstance(permissions_json, str)
                    else dict(permissions_json or {})
                )
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                print(f"Invalid JSON for group permissions: {username}")
                return {}
        except Error as e:
            print(f"Error fetching user permissions: {e}")
            return {}
        finally:
            if "cursor" in locals():
                cursor.close()
            if connection.is_connected():
                connection.close()
    return {}


def get_user_group_slug(username) -> str:
    """Lowercase user_groups.slug for the user, or empty string."""
    uname = (username or "").strip()
    if not uname:
        return ""
    _ensure_user_groups_schema_once()
    connection = get_db_connection()
    if not connection:
        return ""
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT ug.slug AS group_slug
            FROM users u
            LEFT JOIN user_groups ug ON u.user_group_id = ug.id
            WHERE u.username = %s
            LIMIT 1
            """,
            (uname,),
        )
        row = cursor.fetchone()
        return str((row or {}).get("group_slug") or "").strip().lower()
    except Error:
        return ""
    finally:
        if "cursor" in locals():
            cursor.close()
        if connection.is_connected():
            connection.close()


def log_user_activity(username):
    client_ip   = request.headers.get('X-Forwarded-For', request.remote_addr)
    user_agent  = parse(request.headers.get('User-Agent'))
    referrer    = request.referrer

    # fetch location info
    location = get_location(client_ip) or {}
    city      = location.get('city', 'Unknown')
    region    = location.get('regionName', 'Unknown')
    country   = location.get('country', 'Unknown')
    lat       = location.get('lat', 'N/A')
    lon       = location.get('lon', 'N/A')
    timezone  = location.get('timezone', 'Unknown')
    isp       = location.get('isp', 'Unknown')
    org       = location.get('org', 'Unknown')

    current_app.logger.info(
        f"***{username}*** has logged in. "
        f"IP: ***{client_ip}***; "
        f"Location: @@@{city}, {region}, {country}@@@ "
        f"(lat: {lat}, lon: {lon}); "
        f"Timezone: @@@{timezone}@@@; ISP: {isp}; Org: {org}; "
        f"Browser: @@@{user_agent.browser.family} {user_agent.browser.version_string}@@@; "
        f"OS: {user_agent.os.family} {user_agent.os.version_string}; "
        f"Device: @@@{user_agent.device.family}@@@; "
        f"Method: {request.method}; URL: {request.url}; Referrer: {referrer}"
    )

def get_location(ip):
    # X-Forwarded-For may contain a CSV list; use the first client IP.
    if not ip:
        return {}
    ip = ip.split(',')[0].strip()

    try:
        response = requests.get(f'https://ipinfo.io/{ip}/json', timeout=2)
        if response.status_code != 200:
            # 429 is routine when the free tier rate limit is hit; avoid warning spam.
            if response.status_code == 429:
                current_app.logger.debug(
                    "Location lookup skipped for IP %s: HTTP 429 (rate limited)", ip
                )
            else:
                current_app.logger.warning(
                    "Location lookup failed for IP %s: HTTP %s", ip, response.status_code
                )
            return {}

        payload = response.json() or {}
        if payload.get('bogon'):
            current_app.logger.warning(
                f"Location lookup rejected for IP {ip}: bogon/private address"
            )
            return {}

        lat = 'N/A'
        lon = 'N/A'
        loc = payload.get('loc', '')
        if ',' in loc:
            parts = loc.split(',', 1)
            lat = parts[0].strip() or 'N/A'
            lon = parts[1].strip() or 'N/A'

        # Normalize ipinfo payload shape to match existing logging keys.
        return {
            'status': 'success',
            'city': payload.get('city', 'Unknown'),
            'regionName': payload.get('region', 'Unknown'),
            'country': payload.get('country', 'Unknown'),
            'lat': lat,
            'lon': lon,
            'timezone': payload.get('timezone', 'Unknown'),
            'isp': payload.get('org', 'Unknown'),
            'org': payload.get('org', 'Unknown')
        }
    except requests.RequestException as e:
        current_app.logger.warning(f"Location lookup timed out/failed for IP {ip}: {e}")
        return {}

def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = Config.EMAIL_ADDRESS
        msg['To'] = to_email
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP_SSL(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.login(Config.EMAIL_ADDRESS, Config.EMAIL_PASSWORD)
            server.sendmail(Config.EMAIL_ADDRESS, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False


@auth_bp.route("/edit_user", methods=["POST"])
@login_required
def edit_user():
    if session.get("username") != "yeti":
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json() or {}
    user_id = data.get("id")
    password = data.get("password")
    email = data.get("email", "")
    customer_purchase_fdc_ids = (data.get("customer_purchase_fdc_ids") or "").strip()
    customer_purchase_efashion_ids = (
        data.get("customer_purchase_efashion_ids") or ""
    ).strip()
    customer_purchase_include_approved_styles = bool(
        data.get("customer_purchase_include_approved_styles")
    )
    customer_purchase_manual_styles = (
        data.get("customer_purchase_manual_styles") or ""
    ).strip()
    master_image_viewer_customer_purchase_only = (
        1 if data.get("master_image_viewer_customer_purchase_only") else 0
    )
    client_product_detail_customer_purchase_only = (
        1 if data.get("client_product_detail_customer_purchase_only") else 0
    )
    master_image_viewer_hide_path = 1 if data.get("master_image_viewer_hide_path") else 0

    if not user_id or not password:
        return jsonify({"error": "Missing required fields"}), 400

    if email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email format"}), 400

    connection = get_db_connection()
    if not connection:
        return jsonify({"error": "Database connection error"}), 500

    try:
        _ensure_user_groups_schema_once()
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE users
            SET password = %s,
                email = %s,
                customer_purchase_fdc_ids = %s,
                customer_purchase_efashion_ids = %s,
                customer_purchase_include_approved_styles = %s,
                customer_purchase_manual_styles = %s,
                master_image_viewer_customer_purchase_only = %s,
                client_product_detail_customer_purchase_only = %s,
                master_image_viewer_hide_path = %s
            WHERE id = %s
            """,
            (
                password,
                email,
                customer_purchase_fdc_ids or None,
                customer_purchase_efashion_ids or None,
                1 if customer_purchase_include_approved_styles else 0,
                customer_purchase_manual_styles or None,
                master_image_viewer_customer_purchase_only,
                client_product_detail_customer_purchase_only,
                master_image_viewer_hide_path,
                user_id,
            ),
        )
        connection.commit()
        return jsonify({"success": True, "message": "User updated successfully"})
    except Error as e:
        connection.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        if "cursor" in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


@auth_bp.route("/download_customer_approved_styles", methods=["GET"])
@login_required
def download_customer_approved_styles():
    """CSV of cus_invt approved styles for a user's FDC / e-fashion customer IDs."""
    if session.get("username") != "yeti":
        return jsonify({"error": "Access denied"}), 403

    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    connection = get_db_connection()
    if not connection:
        return jsonify({"error": "Database connection error"}), 500

    try:
        cur = connection.cursor(dictionary=True)
        cur.execute(
            """
            SELECT username,
                   customer_purchase_fdc_ids,
                   customer_purchase_efashion_ids
            FROM users WHERE id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "User not found"}), 404

        from py.utilities.customer_purchase_access import (
            fetch_approved_styles_rows,
            get_user_customer_purchase_ids,
        )

        fdc_ids, ef_ids = get_user_customer_purchase_ids(row.get("username"))
        if not fdc_ids and not ef_ids:
            return jsonify({"error": "No FDC or e-fashion customer IDs configured"}), 400

        approved_rows = fetch_approved_styles_rows(fdc_ids, ef_ids)
        uname = str(row.get("username") or "user").strip()
        lines = ["database,customer_id,prod_cd"]
        for r in approved_rows:
            prod = (r.get("prod_cd") or "").replace('"', '""')
            cid = (r.get("customer_id") or "").replace('"', '""')
            db = (r.get("database") or "").replace('"', '""')
            lines.append(f'"{db}","{cid}","{prod}"')

        body = "\n".join(lines) + "\n"
        resp = make_response(body)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="{uname}_approved_styles.csv"'
        )
        return resp
    except Error as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if connection and connection.is_connected():
            connection.close()


@auth_bp.route("/delete_user", methods=["POST"])
@login_required
def delete_user():
    # Parse the JSON data from the request
    data = request.get_json()
    user_id = data.get('userId')
    
    if not user_id:
        return jsonify({"error": "Missing user ID"}), 400
    
    connection = get_db_connection()
    if not connection:
        return jsonify({"error": "Database connection error"}), 500
    
    try:
        cursor = connection.cursor()
        
        # First check if this is the last admin user to prevent deleting all admins
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        
        if total_users <= 1:
            return jsonify({"error": "Cannot delete the last user"}), 400
        
        # Delete the user
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        connection.commit()
        
        return jsonify({"success": True, "message": "User deleted successfully"})
    except Error as e:
        connection.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

@auth_bp.route("/user_settings", methods=["GET", "POST"])
@login_required
def user_settings():
    username = session['username']
    success_message = None
    error_message = None
    
    connection = get_db_connection()
    if not connection:
        flash("Database connection error")
        return redirect(url_for("home"))
    
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT email FROM users WHERE username = %s", (username,))
        user_data = cursor.fetchone()
        
        if not user_data:
            flash("User not found")
            return redirect(url_for("home"))
        
        if request.method == "POST":
            action = request.form.get("action")
            
            if action == "update_email":
                new_email = request.form.get("email")
                
                if not new_email or not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
                    error_message = "Please enter a valid email address"
                else:
                    try:
                        cursor.execute("UPDATE users SET email = %s WHERE username = %s", (new_email, username))
                        connection.commit()
                        success_message = "Email updated successfully"
                        user_data['email'] = new_email
                    except Error as e:
                        connection.rollback()
                        error_message = f"Error updating email: {e}"
            
            elif action == "update_password":
                current_password = request.form.get("current_password")
                new_password = request.form.get("new_password")
                confirm_password = request.form.get("confirm_password")
                
                if not current_password or not new_password or not confirm_password:
                    error_message = "All password fields are required"
                elif new_password != confirm_password:
                    error_message = "New passwords don't match"
                else:
                    # Verify current password
                    cursor.execute("SELECT password FROM users WHERE username = %s", (username,))
                    stored_password = cursor.fetchone()['password']
                    
                    if stored_password != current_password:
                        error_message = "Current password is incorrect"
                    else:
                        try:
                            cursor.execute("UPDATE users SET password = %s WHERE username = %s", (new_password, username))
                            connection.commit()
                            success_message = "Password updated successfully"
                        except Error as e:
                            connection.rollback()
                            error_message = f"Error updating password: {e}"
        
        return render_template("user_settings.html", 
                              username=username, 
                              email=user_data['email'],
                              success_message=success_message,
                              error_message=error_message)
    
    except Error as e:
        flash(f"Database error: {e}")
        return redirect(url_for("home"))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
