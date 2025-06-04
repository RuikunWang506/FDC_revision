from flask import Blueprint, request, render_template, jsonify, redirect, url_for, session, flash, current_app
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import mysql.connector
from mysql.connector import Error
import requests
from user_agents import parse
import re
import secrets
from datetime import datetime, timedelta
from config import Config
import json

# Google OAuth imports
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
import os

auth_bp = Blueprint('auth', __name__, template_folder='templates')

def log_event(level, message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    with open('alllog.txt', 'a') as log:
        log.write(f'{timestamp} {level.upper()} {message}\n')

@auth_bp.before_request
def log_request():
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    method = request.method
    path = request.path
    http_version = request.environ.get('SERVER_PROTOCOL', 'HTTP/1.1')
    
    # Log the request
    log_event('INFO', f'{client_ip} "{method} {path} {http_version}"')
    
    # Store the request details for after_request logging
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
        log_event('INFO', f'{details["client_ip"]} "{details["method"]} {details["path"]} {details["http_version"]}" {status_code}')
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
    
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        remember = request.form.get("remember")
        
        if check_credentials(username, password):
            # Check if user has an email address
            connection = get_db_connection()
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
            
            session['username'] = username
            if remember:
                session.permanent = True
            else:
                session.permanent = False
            
            flash(f"Logged in as {username}")
            log_user_activity(username)
            
            # Redirect TMX users to gpginvoice page
            if username == 'tmx':
                return redirect(url_for("gpgtmx.gpginvoice"))
                
            # For other users, redirect to stored next page or home
            redirect_url = session.pop('next', url_for("home"))
            return redirect(redirect_url)
        else:
            flash("Invalid credentials")
            log_event('WARNING', f'Failed login attempt - Username: {username}, IP: {client_ip}')
    
    return render_template("login.html", client_ip=client_ip, location=location_data)

@auth_bp.route("/logout")
def logout():
    session.pop('username', None)
    flash("Logged out")
    return redirect(url_for("auth.login"))

@auth_bp.route("/update_email", methods=["GET", "POST"])
def update_email():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
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

@auth_bp.route("/manage_users", methods=["GET", "POST"])
def manage_users():
    # Check if user is logged in
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
    # Block access if user is not yetimaster
    if session['username'] != 'yetimaster':
        flash("Access denied. Only yetimaster can manage users.")
        return redirect(url_for("home"))
    
    connection = get_db_connection()
    if not connection:
        flash("Database connection error")
        return redirect(url_for("home"))
    
    if request.method == "POST":
        # Handle adding a new user or updating email
        if 'update_email' in request.form:
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
                    if 'cursor' in locals():
                        cursor.close()
        else:
            # Handle adding a new user
            username = request.form.get("username")
            password = request.form.get("password")
            email = request.form.get("email", "")  # Make email optional with default empty string
            
            # Validate input - only username and password are required
            if not username or not password:
                flash("Username and password are required")
            elif email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
                flash("Please enter a valid email address if providing one")
            else:
                try:
                    cursor = connection.cursor()
                    
                    # Check if username already exists
                    cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
                    if cursor.fetchone():
                        flash("Username already exists")
                    else:
                        # Check if email already exists (only if email is provided)
                        email_exists = False
                        if email:
                            cursor.execute("SELECT email FROM users WHERE email = %s", (email,))
                            if cursor.fetchone():
                                flash("Email already exists")
                                email_exists = True
                        
                        # Only proceed if email doesn't exist or is empty
                        if not email_exists:
                            # Add new user
                            cursor.execute(
                                "INSERT INTO users (username, password, email, created_at) VALUES (%s, %s, %s, NOW())",
                                (username, password, email)
                            )
                            connection.commit()
                            flash("User added successfully")
                except Error as e:
                    connection.rollback()
                    flash(f"Error adding user: {e}")
                finally:
                    if 'cursor' in locals():
                        cursor.close()
    
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT id, username, email, password, created_at FROM users ORDER BY created_at DESC")
        users = cursor.fetchall()
        return render_template("manage_users.html", users=users, current_user=session['username'])
    except Error as e:
        flash(f"Error fetching users: {e}")
        return redirect(url_for("home"))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        
        # Validate input
        if not username or not email or not password or not confirm_password:
            flash("All fields are required")
            return redirect(url_for("auth.register"))
        
        if password != confirm_password:
            flash("Passwords don't match")
            return redirect(url_for("auth.register"))
        
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            flash("Please enter a valid email address")
            return redirect(url_for("auth.register"))
        
        connection = get_db_connection()
        if not connection:
            flash("Database connection error")
            return redirect(url_for("auth.register"))
        
        try:
            cursor = connection.cursor()
            
            # Check if username already exists
            cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
            if cursor.fetchone():
                flash("Username already exists")
                return redirect(url_for("auth.register"))
            
            # Check if email already exists
            cursor.execute("SELECT email FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("Email already exists")
                return redirect(url_for("auth.register"))
            
            # Add new user
            cursor.execute(
                "INSERT INTO users (username, password, email, created_at) VALUES (%s, %s, %s, NOW())",
                (username, password, email)
            )
            connection.commit()
            
            flash("Registration successful. Please login.")
            return redirect(url_for("auth.login"))
        except Error as e:
            connection.rollback()
            flash(f"Error registering user: {e}")
            return redirect(url_for("auth.register"))
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    
    return render_template("register.html")

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
                reset_url = url_for("auth.reset_password", token=token, _external=True)
                
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
db_config = {
    'host': '192.168.254.30',
    'port': 3306,
    'user': 'yeti',
    'password': 'qwe123',
    'database': 'onlinedb'
}

def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        connection = mysql.connector.connect(**db_config)
        if connection.is_connected():
            return connection
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
    return None

def check_credentials(username, password):
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor()
            query = "SELECT password FROM users WHERE username = %s"
            cursor.execute(query, (username,))
            result = cursor.fetchone()
            if result:
                return result[0] == password
            
            session['username'] = username.upper()
            session['user_permissions'] = get_user_permissions(username.upper()) # <--- This line fetches and stores permissions
            return True
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    return False
def get_user_permissions(username):
    connection = get_db_connection()
    if connection:
        try:
            cursor = connection.cursor(dictionary=True)  # Use dictionary cursor
            cursor.execute(
                "SELECT permissions FROM users WHERE username = %s", (username,)
            )
            user_data = cursor.fetchone()
            if user_data:
                permissions_json = user_data.get('permissions')
                if permissions_json:
                    try:
                        return json.loads(permissions_json)
                    except json.JSONDecodeError:
                        print(f"Invalid JSON for user permissions: {username}")
                        return {}
                else:  # Handle NULL permissions
                    return {}
            else: # User not found
                return {}
        except Error as e:
            print(f"Error fetching user permissions: {e}")
            return {}
        finally:
            if 'cursor' in locals():
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
    return {} # Return empty dict if any issues

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
    try:
        response = requests.get(f'http://ip-api.com/json/{ip}')
        if response.status_code == 200:
            return response.json()
        else:
            print(f"Failed to get location for IP: {ip}")
            return {}
    except Exception as e:
        print(f"Error fetching location data: {e}")
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

# Google OAuth functions
def get_google_user_info():
    try:
        # Define a separate file for storing tokens
        token_file = os.path.join(os.path.dirname(Config.GOOGLE_CREDENTIALS_FILE), 'token.json')
        
        # Try to load existing credentials
        creds = None
        if os.path.exists(token_file):
            try:
                creds = Credentials.from_authorized_user_file(token_file, Config.SCOPES)
            except Exception:
                # If the token file is corrupted, ignore it
                pass
                
        # If credentials don't exist or are invalid, get new ones
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    Config.GOOGLE_CREDENTIALS_FILE, Config.SCOPES)
                creds = flow.run_local_server(port=0)
            
            # Save the credentials for the next run
            with open(token_file, 'w') as token:
                token.write(creds.to_json())

        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        return user_info
    except Exception as e:
        print(f"Error getting Google user info: {e}")
        return None

@auth_bp.route('/google-login')
def google_login():
    flow = Flow.from_client_secrets_file(
        Config.GOOGLE_CREDENTIALS_FILE,
        scopes=Config.OAUTH_SCOPES,
        redirect_uri=url_for('auth.google_callback', _external=True))
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent')  # Force consent screen to ensure refresh token
    
    session['state'] = state
    return redirect(authorization_url)

@auth_bp.route('/google-callback')
def google_callback():
    state = session['state']
    flow = Flow.from_client_secrets_file(
        Config.GOOGLE_CREDENTIALS_FILE,
        scopes=Config.OAUTH_SCOPES,
        state=state,
        redirect_uri=url_for('auth.google_callback', _external=True))
    
    try:
        flow.fetch_token(authorization_response=request.url)
        
        credentials = flow.credentials
        
        # Store complete credentials in session
        session['google_credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        
        # Create a new credentials object with all required fields
        creds = Credentials(
            token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_uri=credentials.token_uri,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            scopes=credentials.scopes
        )
        
        # Use the properly constructed credentials object
        service = build('oauth2', 'v2', credentials=creds)
        user_info = service.userinfo().get().execute()
        
        if user_info:
            # Store the Google email in session for potential account creation
            session['google_email'] = user_info.get('email')
            
            connection = get_db_connection()
            if connection:
                try:
                    cursor = connection.cursor()
                    query = "SELECT username FROM users WHERE email = %s"
                    cursor.execute(query, (user_info['email'],))
                    
                    # Make sure to fetch all results before closing the cursor
                    result = cursor.fetchone()  # This consumes the result
                    
                    if result:
                        session['username'] = result[0]
                        flash(f"Logged in as {result[0]} via Google")
                        return redirect(url_for("home"))
                    else:
                        # User not found - redirect to the no account page
                        return redirect(url_for("auth.google_no_account"))
                except Error as e:
                    current_app.logger.error(f"Error checking Google login: {e}")
                finally:
                    if 'cursor' in locals():
                        # Consume any remaining results before closing
                        cursor.fetchall()  # Consume any remaining results
                        cursor.close()
                    if connection and connection.is_connected():
                        connection.close()
    
    except Exception as e:
        current_app.logger.error(f"Error in Google callback: {str(e)}")
        flash("An error occurred during Google login. Please try again or use username/password.")
        return redirect(url_for("auth.login"))
    
    return redirect(url_for("auth.login"))

@auth_bp.route('/google-no-account')
def google_no_account():
    """
    Route for when a user tries to login with Google but doesn't have an account.
    """
    # Get the Google email from session if available
    google_email = session.get('google_email', 'your Google account')
    current_app.logger.info(f"User attempted to login with Google account: {google_email}")
    return render_template('google_no_account.html', email=google_email)



@auth_bp.route("/edit_user", methods=["POST"])
def edit_user():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
    # Parse the JSON data from the request
    data = request.get_json()
    user_id = data.get('id')
    password = data.get('password')
    email = data.get('email', '')  # Default to empty string if not provided
    
    if not user_id or not password:
        return jsonify({"error": "Missing required fields"}), 400
    
    # Validate email format only if it's not empty
    if email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return jsonify({"error": "Invalid email format"}), 400
    
    connection = get_db_connection()
    if not connection:
        return jsonify({"error": "Database connection error"}), 500
    
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE users SET password = %s, email = %s WHERE id = %s",
            (password, email, user_id)
        )
        connection.commit()
        return jsonify({"success": True, "message": "User updated successfully"})
    except Error as e:
        connection.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


@auth_bp.route("/delete_user", methods=["POST"])
def delete_user():
    if 'username' not in session:
        return jsonify({"error": "Not logged in"}), 401
    
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
def user_settings():
    if 'username' not in session:
        return redirect(url_for("auth.login"))
    
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
