from flask import Blueprint, render_template, request, jsonify,session,redirect,url_for,flash
import os
import json
from datetime import datetime, timedelta
import calendar
import pandas as pd


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
        return redirect(url_for("auth.login"))
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

@dailytodo.route('/online_audit', methods=['GET'])
def ecaudit():
    if 'username' not in session:
        session['next'] = request.url
        return redirect(url_for("auth.login"))
    current_user = session['username']

    if current_user not in {'yeti', 'ting', 'nicye', 'ahua', 'tmx'}:
        flash("You're not authorized to access this page.", "error")
        return redirect(url_for("home"))
    
    # Get historical data parameters
    view_type = request.args.get('view_type', 'all')
    view_year = int(request.args.get('year', datetime.now().year))
    view_month = int(request.args.get('month', datetime.now().month))
    view_week = int(request.args.get('week', datetime.now().isocalendar()[1]))
    platform_filter = request.args.get('platform', None)
    
    # Get historical data based on parameters
    if view_type == 'week':
        historical_data = get_weekly_data(view_year, view_week)
    elif view_type == 'month':
        historical_data = get_monthly_data(view_year, view_month)
    else:
        historical_data = get_all_todo_data()
    
    # Initialize default values
    historical_table = "<p>No historical data available</p>"
    platform_totals_html = "<p>No platform totals available</p>"
    
    # Get available platforms, years, months, and weeks for filters
    all_data = get_all_todo_data()
    platforms = set()
    dates = sorted(all_data.keys())
    
    for date_data in all_data.values():
        for section in date_data.values():
            platforms.update(section.keys())
    
    date_objects = [datetime.strptime(date, '%Y-%m-%d') for date in dates]
    years = sorted(set(date.year for date in date_objects))
    months = sorted(set((date.year, date.month) for date in date_objects))
    weeks = sorted(set((date.year, date.isocalendar()[1]) for date in date_objects))
    
    # Aggregate the historical data
    aggregated_data = aggregate_labels(historical_data, platform_filter)
    
    if aggregated_data:
        df_data = []
        for platform, data in aggregated_data.items():
            for date, count in data['dates'].items():
                df_data.append({
                    'Date': date,
                    'Platform': platform,
                    'Labels': count
                })
        
        df = pd.DataFrame(df_data)
        if not df.empty:
            pivot_df = df.pivot_table(
                index='Date', 
                columns='Platform', 
                values='Labels', 
                aggfunc='sum', 
                fill_value=0
            )
            
            pivot_df = pivot_df.sort_index()
            pivot_df['Total'] = pivot_df.sum(axis=1)
            platform_totals = pivot_df.sum(axis=0)
            platform_totals_df = platform_totals.to_frame(name='Total Labels')
            platform_totals_df.index.name = 'Platform'
            platform_totals_df = platform_totals_df.reset_index()
            pivot_df.columns.name = None         # Remove the extra 'Platform' label
            pivot_df.reset_index(inplace=True)   # Move 'Date' from index to a normal column
            pivot_df['Day of Week'] = pivot_df['Date'].apply(lambda x: pd.to_datetime(x).strftime('%A'))
            cols = ['Date', 'Day of Week', 'Total'] + [c for c in pivot_df.columns if c not in ('Date', 'Day of Week', 'Total')]
            pivot_df = pivot_df[cols]
            historical_table = pivot_df.to_html(classes="table table-bordered", index=False)
            platform_totals_html = platform_totals_df.to_html(
                classes="table table-bordered",
                index=False
            )
            chart_data = {
                "labels": pivot_df["Date"].tolist(),
                "datasets": [
                    {
                        "label": col,
                        "data": pivot_df[col].tolist(),
                    } for col in pivot_df.columns if col not in ['Date', 'Day of Week', 'Total']
                ]
            }

    return render_template(
        'online_audit.html',
        historical_table=historical_table,
        platform_totals=platform_totals_html,
        available_platforms=sorted(platforms),
        available_years=years,
        available_months=months,
        available_weeks=weeks,
        current_view_type=view_type,
        current_year=view_year,
        current_month=view_month,
        current_week=view_week,
        current_platform=platform_filter,
        current_user=current_user,
        chart_data=json.dumps(chart_data)
    )

@dailytodo.app_template_filter('week_to_month')
def week_to_month(week_number, year):
    # Calculate the first day of this week
    first_day = datetime.strptime(f'{year}-W{int(week_number):02d}-1', '%Y-W%W-%w')
    # Get the month name
    return first_day.strftime('%B')

def get_all_todo_data():
    """Read all JSON files in the data directory and return as a dictionary"""
    all_data = {}
    for filename in os.listdir(DATA_DIR):
        if filename.endswith('.json'):
            date = filename.split('.')[0]
            file_path = os.path.join(DATA_DIR, filename)
            with open(file_path, 'r') as f:
                all_data[date] = json.load(f)
    return all_data

def get_weekly_data(year, week):
    """Get data for a specific week"""
    first_day_of_week = datetime.strptime(f'{year}-W{week}-1', '%Y-W%W-%w')
    dates = [(first_day_of_week + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
    weekly_data = {}
    for date in dates:
        file_path = os.path.join(DATA_DIR, f"{date}.json")
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                weekly_data[date] = json.load(f)
    return weekly_data

def get_monthly_data(year, month):
    """Get data for a specific month"""
    num_days = calendar.monthrange(year, month)[1]
    dates = [f"{year}-{month:02d}-{day:02d}" for day in range(1, num_days + 1)]
    monthly_data = {}
    for date in dates:
        file_path = os.path.join(DATA_DIR, f"{date}.json")
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                monthly_data[date] = json.load(f)
    return monthly_data

def aggregate_labels(data, platform=None):
    """Aggregate label counts by platform and date"""
    result = {}
    
    for date, day_data in data.items():
        for section, platforms in day_data.items():
            for plat, values in platforms.items():
                if platform and plat != platform:
                    continue
                
                # Safely get and convert label values
                label_value = values.get('labels', '')
                label_count = int(label_value) if label_value and label_value.strip() else 0
                
                if plat not in result:
                    result[plat] = {'total': 0, 'dates': {}}
                
                result[plat]['total'] += label_count
                result[plat]['dates'][date] = label_count
    
    return result


