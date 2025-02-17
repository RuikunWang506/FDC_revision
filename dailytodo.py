from flask import Blueprint, render_template, request, jsonify,session,redirect,url_for,flash
import os
import json
from datetime import datetime

dailytodo = Blueprint('dailytodo', __name__, template_folder='templates')

DATA_DIR = "output/dailytodo"
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

    if current_user not in {'yeti', 'ting','nicye','ahua','tmx'}:
        flash("You're not authorized to access this page.", "error")
        return redirect(url_for("home"))
    selected_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    file_path = os.path.join(DATA_DIR, f"{selected_date}.json")

    if request.method == 'POST':
        data = request.json
        with open(file_path, 'w') as f:
            json.dump(data, f)
        return jsonify({"status": "success"})

    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = {
            "FDC": {
                "FDC_FBM": {"labels": "", "print": False, "finished": False, "memo": ""},
                "EF_FBM": {"labels": "", "print": False, "finished": False, "memo": ""},
                "TEMU_USA": {"labels": "", "print": False, "finished": False, "memo": ""},
                "TEMU_HK": {"labels": "", "print": False, "finished": False, "memo": ""},
                "TIKTOK": {"labels": "", "print": False, "finished": False, "memo": ""},
                "SHEIN": {"labels": "", "print": False, "finished": False, "memo": ""}
            },
            "ERWAN": {
                "ERWAN_DROPSHIP": {"labels": "", "print": False, "finished": False, "memo": ""}
            },
            "TMX": {
                "TMX_GPG": {"labels": "", "print": False, "finished": False, "memo": ""}
            },
            "MMY": {
                "Wayfair": {"labels": "", "print": False, "finished": False, "memo": ""},
                "Walmart": {"labels": "", "print": False, "finished": False, "memo": ""},
                "MMY_FBM": {"labels": "", "print": False, "finished": False, "memo": ""},
                "TEMU_USA": {"labels": "", "print": False, "finished": False, "memo": ""},
                "TEMU_China": {"labels": "", "print": False, "finished": False, "memo": ""},
                "Ebay": {"labels": "", "print": False, "finished": False, "memo": ""},
                "Etsy": {"labels": "", "print": False, "finished": False, "memo": ""}
            }
        }
        with open(file_path, 'w') as f:
            json.dump(data, f)
    
    return render_template('dailytodo.html', data=data, selected_date=selected_date, current_user=current_user)
