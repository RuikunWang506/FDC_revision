# custom_query.py
import os
import pyodbc
import pandas as pd
import logging

# SQL Server connection details
server = "192.168.254.204"
username = "tristen"
password = 'Qwer1234##'
driver = 'ODBC Driver 17 for SQL Server'

# Use the custom logger for SQL queries
query_logger = logging.getLogger('query_logger')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'xlsx'}

def execute_custom_query(sql_query, database_name):
    try:
        conn = pyodbc.connect(
            f'DRIVER={driver};SERVER={server};DATABASE={database_name};UID={username};PWD={password}'
        )

        cursor = conn.cursor()
        cursor.execute(sql_query)
        columns = [column[0] for column in cursor.description]
        data = cursor.fetchall()

        # Ensure the number of columns in the data matches the columns list
        if data and len(data[0]) != len(columns):
            raise ValueError(f"Number of columns in data ({len(data[0])}) does not match the number of columns ({len(columns)})")

        # Convert the data to a list of dictionaries
        data_dicts = [dict(zip(columns, row)) for row in data]
        df = pd.DataFrame(data_dicts)

        # Sanitize and encode the data to handle special characters
        df = df.applymap(lambda x: x if pd.isnull(x) else str(x).replace('?', '').encode('ascii', 'ignore').decode())
        df = df.dropna(axis=1, how='all')
        cursor.close()
        conn.close()

        output_directory = "output/custom_query_output"
        if not os.path.exists(output_directory):
            os.makedirs(output_directory)
        file_path = os.path.join(output_directory, "query_result.xlsx")
        df.to_excel(file_path, index=False)
        excel_html = df.to_html(classes='table table-bordered table-striped', escape=False)

        # Log the successful query
        query_logger.info(sql_query)

        return excel_html, None

    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        # Log the query and the error message together with a separator
        query_logger.error(f"{sql_query} | ERROR: {error_message}")
        return None, error_message