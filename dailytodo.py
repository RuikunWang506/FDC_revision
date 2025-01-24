from flask import Blueprint, render_template, request, jsonify,session,redirect,url_for,flash
import os
import json
from datetime import datetime

dailytodo = Blueprint('dailytodo', __name__, template_folder='templates')

DATA_DIR = "output/dailytodo"

# Ensure output directory exists
os.makedirs(DATA_DIR, exist_ok=True)

def get_daily_file():
    today_date = datetime.now().strftime("%Y-%m-%d")
    file_path = os.path.join(DATA_DIR, f"{today_date}.json")
    return file_path

@dailytodo.route('/dailytodo', methods=['GET', 'POST'])
def daily_todo():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("login"))
    current_user = session['username']

    if current_user not in {'yeti', 'ting','nicye','ahua'}:
        flash("You're not authorized to access this page.", "error")
        return redirect(url_for("home"))
    selected_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    file_path = os.path.join(DATA_DIR, f"{selected_date}.json")


    # Handle saving data
    if request.method == 'POST':
        data = request.json  # Capture incoming JSON
        # Save incoming data to JSON file
        with open(file_path, 'w') as f:
            json.dump(data, f)
        return jsonify({"status": "success"})

    # Handle retrieving data on page load
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)  # Load existing data
    else:
        # Default structure if the file doesn't exist yet
        data = {
            "FDC": {
                "FDC_FBM": {"labels": "", "print": False},
                "EF_FBM": {"labels": "", "print": False},
                "TEMU_USA": {"labels": "", "print": False},
                "TEMU_HK": {"labels": "", "print": False},
                "TIKTOK": {"labels": "", "print": False},
                "SHEIN": {"labels": "", "print": False}
            },
            "ERWAN": {
                "ERWAN_DROPSHIP": {"labels": "", "print": False}
            },
            "TMX": {
                "TMX_GPG": {"labels": "", "print": False}
            },
            "MMY": {
                "Wayfair": {"labels": "", "print": False},
                "Walmart": {"labels": "", "print": False},
                "MMY_FBM": {"labels": "", "print": False},
                "TEMU_USA": {"labels": "", "print": False},
                "TEMU_China": {"labels": "", "print": False},
                "Ebay": {"labels": "", "print": False},
                "Etsy": {"labels": "", "print": False}
            }
        }
        with open(file_path, 'w') as f:
            json.dump(data, f)  # Create the file with default data
    
    # Pass data to the template for rendering
    return render_template('dailytodo.html', data=data, selected_date=selected_date, current_user=current_user)
