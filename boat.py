import pandas as pd
from flask import Blueprint, render_template, session, redirect, url_for,current_app
import pyodbc
import os
from datetime import datetime
import logging
from auth import login_required


boat_service_bp = Blueprint('boat_service', __name__, template_folder='../templates')


# Database connection details
server = "192.168.254.204"
database_fdc = "omsdata_FDC"
username = "tristen"
password = 'Fdc2022!!'
driver = 'ODBC Driver 17 for SQL Server'

conn_str = (
    f"DRIVER={{{driver}}};"
    f"SERVER={server};"
    f"DATABASE={database_fdc};"
    f"Trusted_Connection=yes;"
    f"UID={username};"
    f"PWD={password};"
)
# Temporarily commented out to fix startup issue
# conn = pyodbc.connect(conn_str)

def get_boat_connection():
    """Get database connection for boat service"""
    return pyodbc.connect(conn_str)

@boat_service_bp.route('/boat', methods=['GET'])
@login_required
def boat_service():
    current_user = session['username']
    current_app.logger.info(f"***{current_user}***__@@@checked boat page@@@")
    
    # Find the latest modified file in the Dropbox folder
    folder_path = 'C:/Users/stream/Dropbox/Pictures-China (1)/container catalog/Container/container catalog/container import/'
    file_pattern = 'container import'  # Base filename pattern
    
    try:
        # Get all files that match the pattern
        matching_files = []
        for filename in os.listdir(folder_path):
            # Skip temporary Excel files (start with ~$) and hidden files
            if (file_pattern in filename and 
                filename.endswith(('.xlsx', '.xlsb')) and 
                not filename.startswith('~$') and 
                not filename.startswith('.')):
                full_path = os.path.join(folder_path, filename)
                modified_time = os.path.getmtime(full_path)
                matching_files.append((full_path, modified_time, filename))
        
        if not matching_files:
            raise FileNotFoundError("No matching container import files found")
        
        # Sort by modification time (newest first) and get the latest file
        matching_files.sort(key=lambda x: x[1], reverse=True)
        file_path = matching_files[0][0]
        latest_filename = matching_files[0][2]
        
        current_app.logger.info(f"Using latest file: {latest_filename}")
        
    except Exception as e:
        current_app.logger.error(f"Error finding latest file: {e}")
        # Fallback to original path if there's an error
        file_path = 'C:/Users/stream/Dropbox/Pictures-China (1)/container catalog/Container/container catalog/container import/container import.xlsb.xlsx'
    
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
            conn = get_boat_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT on_order_qty FROM dbo.inv_data WHERE descrip = ?", container_raw)
            result = cursor.fetchone()
            conn.close()  # Close connection after use

            # Add the status column based on query result
            if result and result[0] is not None:
                if result[0] > 0:
                    df_cleaned.at[index, 'Status'] = '🚢ocean🚢'
                elif result[0] == 0:
                    df_cleaned.at[index, 'Status'] = '📍landed📍'
            else:
                df_cleaned.at[index, 'Status'] = '❌no data❌'

            # Check if container exists in OMS system (import status)
            container_found = False
            
            try:
                # First try inv_data table with LIKE pattern in descrip field
                cursor.execute("SELECT COUNT(*) FROM dbo.inv_data WHERE descrip LIKE ?", f'%{container_raw}%')
                result1 = cursor.fetchone()
                if result1 and result1[0] > 0:
                    container_found = True
                
                # If not found, try prod_cd field in inv_data
                if not container_found:
                    cursor.execute("SELECT COUNT(*) FROM dbo.inv_data WHERE prod_cd LIKE ?", f'%{container_raw}%')
                    result2 = cursor.fetchone()
                    if result2 and result2[0] > 0:
                        container_found = True
                        
            except Exception as e:
                print(f"Error checking container {container_raw}: {e}")
                container_found = False
            
            if container_found:
                df_cleaned.at[index, 'Import Status'] = '✅imported'
            else:
                df_cleaned.at[index, 'Import Status'] = '❌not imported to oms'

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
                    <th>Import Status</th>
                </tr>
            </thead>
            <tbody>
                <tr><td colspan='8'>Error loading data</td></tr>
            </tbody>
        </table>
        """
    
    return render_template('boat.html', table_data=data_html, last_modified_date=last_modified_date,current_user=current_user)
