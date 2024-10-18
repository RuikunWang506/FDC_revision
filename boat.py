import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for
import pyodbc
import os
from datetime import datetime

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
    f"UID={username};"
    f"PWD={password};"
)
conn = pyodbc.connect(conn_str)

@boat_service_bp.route('/boat', methods=['GET'])
def boat_service():
    if 'username' not in session:
        return redirect(url_for("login"))

    file_path = 'C:/Users/stream/Dropbox/Pictures-China (1)/container catalog/Container/container catalog/container import/Copy of container import.xlsb.xlsx'
    
    # Get the last modified date of the file
    try:
        modified_time = os.path.getmtime(file_path)
        last_modified_date = datetime.fromtimestamp(modified_time).strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        last_modified_date = "N/A"
        print(f"Error fetching file modification time: {e}")

    # Load data from the Excel file
    try:
        df = pd.read_excel(file_path, sheet_name='Sheet1')
        df_cleaned = df[['Unnamed: 3','REF#', 'CONTAINER#', 'ETA', 'RCD', 'Unnamed: 6']].copy()
        df_cleaned.columns = ['Reserved', 'container', 'link', 'ETA', 'RCD', 'MEMO']

        # Correct the 'ETA' and 'RCD' columns
        df_cleaned['ETA'] = pd.to_datetime(df_cleaned['ETA'], errors='coerce').fillna('')
        df_cleaned['RCD'] = pd.to_datetime(df_cleaned['RCD'], errors='coerce').fillna('')

        # Ensure that 'NaT' or any missing values are replaced with an empty string
        df_cleaned['ETA'] = df_cleaned['ETA'].replace(['NaT', pd.NaT], '')
        df_cleaned['RCD'] = df_cleaned['RCD'].replace(['NaT', pd.NaT], '')
        df_cleaned = df_cleaned.tail(30)

        # Add hyperlinks to container and link columns, but keep raw container value for SQL query
        df_cleaned['container_raw'] = df_cleaned['container']  # Keep the raw container value
        df_cleaned['container'] = df_cleaned['container_raw'].apply(
            lambda x: f'<a href="/item_lookup/%25{x}" target="_blank">{x}</a>' if pd.notnull(x) else ''
        )
        df_cleaned['link'] = df_cleaned['link'].apply(
            lambda x: f'<a href="https://www.searates.com/container/tracking/?number={x}&sealine=AUTO" target="_blank">{x}</a>' if pd.notnull(x) else ''
        )

        # SQL query to check `on_order_qty` for each container using the raw container value
        for index, row in df_cleaned.iterrows():
            container_raw = row['container_raw']  # Use the raw container value for the SQL query

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

        # Clean and sort the dataframe
        df_cleaned.dropna(how='all', inplace=True)
        df_sorted = df_cleaned.sort_values(by='container', ascending=False)

        # Convert to HTML
        data_html = df_sorted.to_html(
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
        # Create a complete table structure with headers and an error row
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
