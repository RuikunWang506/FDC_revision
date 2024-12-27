import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for,current_app
import pyodbc
import os
from datetime import datetime
import logging


boat_service_bp = Blueprint('boat_service', __name__, template_folder='../templates')


# Database connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

conn_str = (
    f"DRIVER={{{driver}}};"
    f"SERVER={server};"
    f"DATABASE={database_fdc};"
    f"Trusted_Connection=yes;"
    f"UID={username};"
    f"PWD={password};"
)
conn = pyodbc.connect(conn_str)

@boat_service_bp.route('/boat', methods=['GET'])
def boat_service():
    if 'username' not in session:
        return redirect(url_for("login"))
    current_user = session['username']
    current_app.logger.info(f"***{current_user}***__@@@checked boat page@@@")
    file_path = 'C:/Users/stream/Dropbox/Pictures-China (1)/container catalog/Container/container catalog/container import/container import.xlsb.xlsx'
    
    # Get the last modified date of the filefrom flask import current_app
    
    try:
        modified_time = os.path.getmtime(file_path)
        last_modified_date = datetime.fromtimestamp(modified_time).strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        last_modified_date = "N/A"
        print(f"Error fetching file modification time: {e}")

    # Load data from the Excel file
    try:
        df = pd.read_excel(file_path, sheet_name='Sheet1')
        df_cleaned = df[['Unnamed: 3', 'REF#', 'CONTAINER#', 'ETA', 'RCD', 'Unnamed: 6']].copy()
        df_cleaned.columns = ['Reserved', 'container', 'link', 'ETA', 'RCD', 'MEMO']

        # Convert dates from Excel serial format
        def convert_excel_date(serial):
            if pd.notnull(serial) and isinstance(serial, (int, float)):
                return pd.to_datetime('1899-12-30') + pd.to_timedelta(serial, 'D')
            return pd.NaT

        df_cleaned['ETA'] = df['ETA'].apply(convert_excel_date).dt.strftime('%m/%d/%Y').fillna('')
        df_cleaned['RCD'] = df['RCD'].apply(convert_excel_date).dt.strftime('%m/%d/%Y').fillna('')

        # Get the last 30 rows first, then sort by 'container' in descending order
        df_cleaned = df_cleaned.tail(30).sort_values(by='container', ascending=False)

        # Store raw container values for SQL query use
        df_cleaned['container_raw'] = df_cleaned['container']

        # SQL query to check on_order_qty for each container using the raw container value
        for index, row in df_cleaned.iterrows():
            container_raw = row['container_raw']
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(on_order_qty) FROM dbo.inv_data WHERE descrip = ?", container_raw)
            result = cursor.fetchone()

            # Add the status column based on query result
            if result and result[0] is not None:
                if result[0] > 0:
                    df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
                elif result[0] == 0:
                    df_cleaned.at[index, 'Status'] = '📍landed📍'
            else:
                df_cleaned.at[index, 'Status'] = '❌no data❌'

        # Drop the 'container_raw' column before rendering
        df_cleaned.drop('container_raw', axis=1, inplace=True)

        # Add hyperlinks to container and link columns
        df_cleaned['container'] = df_cleaned['container'].apply(
            lambda x: f'<a href="/item_lookup/%25{x}%25" target="_blank">{x}</a>' if pd.notnull(x) else ''
        )
        df_cleaned['link'] = df_cleaned['link'].apply(
            lambda x: f'<a href="https://www.searates.com/container/tracking/?number={x}&sealine=AUTO" target="_blank">{x}</a>' if pd.notnull(x) else ''
        )

        # Convert DataFrame to HTML for rendering
        data_html = df_cleaned.to_html(
            index=False, 
            header=True, 
            classes="table table-bordered editable", 
            escape=False, 
            na_rep="", 
            justify="center", 
            table_id="boat_table", 
            border=0
        )
    except Exception as e:
        print(f"Error processing Excel file: {e}")
        data_html = """
        <table class="table table-bordered editable" id="boat_table">
            <thead>
                <tr>
                    <th>Reserved</th>
                    <th>container</th>
                    <th>link</th>
                    <th>ETA</th>
                    <th>RCD</th>
                    <th>MEMO</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                <tr><td colspan='7'>Error loading data</td></tr>
            </tbody>
        </table>
        """
    
    return render_template('boat.html', table_data=data_html, last_modified_date=last_modified_date)
