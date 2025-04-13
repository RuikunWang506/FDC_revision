    @app.route("/login", methods=["GET", "POST"])
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
                if connection:
                    try:
                        cursor = connection.cursor()
                        cursor.execute("SELECT email FROM users WHERE username = %s", (username,))
                        result = cursor.fetchone()
                        
                        if not result or not result[0]:
                            # Store the next page in session before redirecting
                            session['next'] = next_page
                            session['username'] = username
                            return redirect(url_for('update_email'))
                    except Error as e:
                        app.logger.error(f"Error checking email: {e}")
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
        
        return render_template("login.html", client_ip=client_ip, location=location_data)

    # Google OAuth2 integration
    from google_auth_oauthlib.flow import Flow
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from itsdangerous import Serializer

    def get_google_user_info():
        try:
            creds = Credentials.from_authorized_user_file(Config.GOOGLE_CREDENTIALS_FILE, Config.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        Config.GOOGLE_CREDENTIALS_FILE, Config.SCOPES)
                    creds = flow.run_local_server(port=0)
                with open(Config.GOOGLE_CREDENTIALS_FILE, 'w') as token:
                    token.write(creds.to_json())

            service = build('oauth2', 'v2', credentials=creds)
            user_info = service.userinfo().get().execute()
            return user_info
        except Exception as e:
            app.logger.error(f"Error getting Google user info: {e}")
            return None
    
    @app.route('/google-login')
    def google_login():
        flow = Flow.from_client_secrets_file(
            Config.GOOGLE_CREDENTIALS_FILE,
            scopes=Config.SCOPES,
            redirect_uri=url_for('google_callback', _external=True))
        
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true')
        
        session['state'] = state
        return redirect(authorization_url)

    @app.route('/google-callback')
    def google_callback():
        state = session['state']
        flow = Flow.from_client_secrets_file(
            Config.GOOGLE_CREDENTIALS_FILE,
            scopes=Config.SCOPES,
            state=state,
            redirect_uri=url_for('google_callback', _external=True))
        
        flow.fetch_token(authorization_response=request.url)
        
        credentials = flow.credentials
        session['google_credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        
        user_info = get_google_user_info()
        if user_info:
            connection = get_db_connection()
            if connection:
                try:
                    cursor = connection.cursor()
                    query = "SELECT username FROM users WHERE email = %s"
                    cursor.execute(query, (user_info['email'],))
                    result = cursor.fetchone()
                    
                    if result:
                        session['username'] = result[0]
                        flash(f"Logged in as {result[0]} via Google")
                        return redirect(url_for("home"))
                    else:
                        flash("No account found with this Google email")
                        return redirect(url_for("login"))
                except Error as e:
                    app.logger.error(f"Error checking Google login: {e}")
                finally:
                    if 'cursor' in locals():
                        cursor.close()
                    if connection and connection.is_connected():
                        connection.close()
        
        return redirect(url_for("login"))
    

@app.route("/logout")
    def logout():
        session.pop('username', None)
        flash("Logged out")
        return redirect(url_for("login"))


    @app.route("/", methods=["GET", "POST"])
    def home():
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))
        current_user = session['username']
        return render_template("index.html", current_user=current_user)
    
    @app.route("/update_user", methods=["POST"])
    def update_user():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        current_user = session['username']
        
        # Only allow admin users
        if current_user not in ['yeti', 'yetimaster']:
            return jsonify({"error": "Permission denied"}), 403
            
        data = request.get_json()
        user_id = data.get('userId')
        password = data.get('password')
        updated_username = data.get('username')
        
        if not user_id or not password:
            return jsonify({"error": "Missing required fields"}), 400
            
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                query = "UPDATE users SET password = %s WHERE id = %s"
                cursor.execute(query, (password, user_id))
                connection.commit()
                
                if cursor.rowcount == 0:
                    return jsonify({"error": "User not found"}), 404
                    
                app.logger.info(f"User {updated_username} updated by {current_user}")
                return jsonify({"message": "User updated successfully"})
            except Error as e:
                app.logger.error(f"Error updating user: {e}")
                return jsonify({"error": "Database error"}), 500
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return jsonify({"error": "Database connection failed"}), 500

    @app.route("/delete_user", methods=["POST"])
    def delete_user():
        if 'username' not in session:
            return jsonify({"error": "Unauthorized"}), 401
            
        current_user = session['username']
        
        # Only allow admin users
        if current_user not in ['yeti', 'yetimaster']:
            return jsonify({"error": "Permission denied"}), 403
            
        data = request.get_json()
        user_id = data.get('userId')
        
        if not user_id:
            return jsonify({"error": "Missing user ID"}), 400
            
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                query = "DELETE FROM users WHERE id = %s"
                cursor.execute(query, (user_id,))
                connection.commit()
                
                if cursor.rowcount == 0:
                    return jsonify({"error": "User not found"}), 404
                    
                app.logger.info(f"User {user_id} deleted by {current_user}")
                return jsonify({"message": "User deleted successfully"})
            except Error as e:
                app.logger.error(f"Error deleting user: {e}")
                return jsonify({"error": "Database error"}), 500
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return jsonify({"error": "Database connection failed"}), 500

@app.route("/update_email", methods=["GET", "POST"])
    def update_email():
        if 'username' not in session:
            return redirect(url_for("login"))
            
        if request.method == "GET":
            return render_template("update_email.html")
            
        email = request.form.get("email")
        if not email:
            return jsonify({"error": "Email is required"}), 400
            
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor()
                query = "UPDATE users SET email = %s WHERE username = %s"
                cursor.execute(query, (email, session['username']))
                connection.commit()
                app.logger.info(f"Updated email for user {session['username']}")
                return jsonify({"message": "Email updated successfully"})
            except Error as e:
                app.logger.error(f"Error updating email: {e}")
                return jsonify({"error": "Database error"}), 500
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return jsonify({"error": "Database connection failed"}), 500

    @app.route("/request_password_reset", methods=["POST"])
    def request_password_reset():
        username = request.form.get("username")
        if not username:
            return jsonify({"error": "Username is required"}), 400
            
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                # Get user details including email
                query = "SELECT username, email FROM users WHERE username = %s"
                cursor.execute(query, (username,))
                result = cursor.fetchone()
                
                if not result:
                    return jsonify({"error": "Username not found"}), 404
                    
                if not result['email']:
                    return jsonify({"error": "No email registered for this user"}), 400
                    
                # Generate a temporary password
                import secrets
                import string
                temp_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12))
                
                # Update password in database
                update_query = "UPDATE users SET password = %s WHERE username = %s"
                cursor.execute(update_query, (temp_password, username))
                connection.commit()
                
                # Send email with temporary password
                email_body = f"Username: {username}\nYour temporary password is: {temp_password}\n\nPlease change it after logging in."
                if send_email(result['email'], "FDC Toolkit Password Reset", email_body):
                    return jsonify({"message": f"Password reset email sent to {result['email']}"})
                else:
                    return jsonify({"error": "Failed to send email"}), 500
                    
            except Error as e:
                app.logger.error(f"Error processing password reset: {e}")
                return jsonify({"error": "Database error"}), 500
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return jsonify({"error": "Database connection failed"}), 500

    @app.route("/manage_users", methods=["GET"])
    def manage_users():
        if 'username' not in session:
            return redirect(url_for("login"))
            
        current_user = session['username']
        
        # Only allow admin users
        if current_user not in ['yetimaster']:
            flash("You don't have permission to access this page")
            return redirect(url_for("home"))
            
        connection = get_db_connection()
        if connection:
            try:
                cursor = connection.cursor(dictionary=True)
                cursor.execute("SELECT created_at,id,username,email,password FROM users ORDER BY created_at DESC")
                users = cursor.fetchall()
                return render_template("manage_users.html", users=users, current_user=current_user)
            except Error as e:
                app.logger.error(f"Error fetching users: {e}")
                flash("Error loading user data")
                return redirect(url_for("home"))
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return redirect(url_for("home"))

    @app.route("/bookmarks", methods=["GET", "POST"])
    def bookmarks_page():  # Renamed function
        if 'username' not in session:
            session['next'] = request.url
            return redirect(url_for("login"))

        current_user = session['username']
        
        if current_user != 'yeti':
            return render_template("index.html", current_user=current_user)

        bookmarks_list = [
            {"title": "Passwords & Accounts", "year":"8888", "category": "Passwords", "url": "https://docs.google.com/spreadsheets/d/1nkle6flS263CJ65b0G8YQf46a3aTUC8vP2r3exfdFms/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TEMU-USA", "year":"2024", "category": "Accounting",  "url": "https://docs.google.com/spreadsheets/d/1-PzqDYddrHTzBZ-f0IdzZFgmQaBK_Hx_Y_IYgHCIGWw/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TEMU-HK", "year":"2024", "category": "Accounting",  "url": "https://docs.google.com/spreadsheets/d/1n_ewjw68TLFIDouP11z-AJlI1PilObQfP3lan7DepUg/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "HR_Ashley Sign-in Sheet", "year":"2024", "category": "Payroll", "url": "https://docs.google.com/spreadsheets/d/1ABTgQ4KmVkBX0KRFV9XuIH94irc3jtBlUtjjpB1HRgE/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_TIKTOK_AMBRA",  "year":"2024","category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1gwJVGsJVCpYY-LtimYCwj5_hCUc8xs1zoqzhAqO9XQ8/edit?gid=697444757#gid=697444757","linktype":"Google Sheet"},
            {"title": "Channel_Old Dec-Mar TIKTOK", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/13xcqIjFSOrDBh7xHZFsr_P4LBCKt2uGsrohIMXfeUxg/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_Tiktok Accounting 2024", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1aW4wmxQHtSXZPjgui0PhaKGszkttTKay6JVlNJflJ_M/edit?gid=462431388#gid=462431388","linktype":"Google Sheet"},
            {"title": "HR_1099 Sheet 2024", "year":"2024", "category": "Payroll", "url": "https://docs.google.com/spreadsheets/d/1bgCEWmdWmFTyAkpbOQkxDvEgBFUhPNLVJLRIOsgxWd0/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_ERWAN 2024", "year":"2024", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1vIG8VhrZTJmuuEjXFP7ZmdnsyhAbXMWnasIOH_v76Ok/edit?gid=572048210#gid=572048210","linktype":"Google Sheet"},
            {"title": "Tiktok_2025", "year":"2025", "category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1v9nzwoMk3p9LAcztZOW4et2v51Wfx21VeTlJbF-X64M/edit?gid=0#gid=0","linktype":"Google Sheet"},
            {"title": "Channel_ERWAN 2025","year": "2025","category": "Accounting", "url": "https://docs.google.com/spreadsheets/d/1nU0CIyAtQYGK9AKNBhc-l_Mr-qRaoWLen4g4oZsdaBw/edit?gid=0#gid=0","linktype":"Google Sheet"}
        ]

        return render_template("bookmark.html", current_user=current_user, bookmarks_list=bookmarks_list)
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
                return False
            finally:
                if 'cursor' in locals():
                    cursor.close()
                if connection and connection.is_connected():
                    connection.close()
        return False

    def log_user_activity(username):
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = parse(request.headers.get('User-Agent'))
        referrer = request.referrer
        query_params = request.args

        app.logger.info(
            "***%s*** has logged in. IP: ***%s***, Browser: %s %s, OS: %s %s, Device: ***%s***, "
            "Method: %s, URL: %s, Referrer: %s",
            username, client_ip, user_agent.browser.family, user_agent.browser.version_string,
            user_agent.os.family, user_agent.os.version_string, user_agent.device.family,
            request.method, request.url, referrer
        )

    def get_location(ip):
        try:
            response = requests.get(f'http://ip-api.com/json/{ip}')
            if response.status_code == 200:
                return response.json()
            else:
                app.logger.warning(f"Failed to get location for IP: {ip}")
                return {}
        except Exception as e:
            app.logger.error(f"Error fetching location data: {e}")
            return {}

            
    def load_credentials():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        credentials_path = os.path.join(base_dir, 'credentials.json')
        with open(credentials_path) as file:
            credentials = json.load(file)
        return credentials

        # Password management routes
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
            app.logger.error(f"Error sending email: {e}")
            return False

    