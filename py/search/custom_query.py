import os
import pyodbc
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from py.utilities.setting import Config

# Database configurations from settings
sql_server_config = Config.SQL_SERVER_CONFIG

# Use the custom logger for SQL queries
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'xlsx'}

def convert_julian_to_date(julian_date):
    # Convert julian_date to string to handle slicing
    julian_date = str(julian_date).strip()

    # Ensure the julian_date is in the correct format
    if len(julian_date) != 5 or not julian_date.isdigit():
        return julian_date  # Return the original value if it's not a valid Julian date

    # Base date is 2023-01-01
    base_date = datetime(2023, 1, 1)

    # Define the reference Julian date (e.g., 81088) corresponding to 2023-01-01
    reference_julian_date = 81088

    # Calculate the day offset from the reference date
    day_offset = int(julian_date) - reference_julian_date

    # Calculate the final date by adding the day offset to the base date
    final_date = base_date + timedelta(days=day_offset)

    return final_date.strftime('%m/%d/%Y')

def add_sum_row_if_numeric(df):
    # Initialize a dictionary to store sums
    sum_dict = {}

    # Iterate over each column
    for col in df.columns:
        # Check if all values in the column are numeric
        if pd.to_numeric(df[col], errors='coerce').notnull().all():
            # Calculate the sum of the column
            sum_dict[col] = df[col].astype(float).sum()
        else:
            # If the column is not numeric, set the sum as NaN
            sum_dict[col] = np.nan

    # Create a DataFrame for the sum row with the same columns
    sum_df = pd.DataFrame(sum_dict, index=[0])

    # Concatenate the sum_df as the first row after the header
    df_with_sum = pd.concat([sum_df, df], ignore_index=True)

    # Return the modified DataFrame
    return df_with_sum

def format_numbers(df):
    # Format numbers to two decimals if they have more than two decimals, otherwise keep as is
    def format_value(value):
        if pd.isnull(value):
            return value
        try:
            # Convert to float to handle numeric formatting
            float_value = float(value)
            # Check if it's effectively an integer
            if float_value.is_integer():
                return int(float_value)  # Return as integer
            else:
                formatted_value = round(float_value, 2)  # Round to two decimals
                if formatted_value == int(formatted_value):
                    return int(formatted_value)  # If the result is effectively an integer, return as int
                return formatted_value
        except ValueError:
            return value

    return df.applymap(format_value)

def clean_and_format_dataframe(df):
    # Drop empty columns
    df = df.dropna(axis=1, how='all')

    # Add sum row if columns are numeric
    df_with_sum = add_sum_row_if_numeric(df)

    # Format numbers
    df_with_sum = format_numbers(df_with_sum)

    return df_with_sum

def execute_custom_query(sql_query, database_name):
    try:
        conn = pyodbc.connect(
            f'DRIVER={sql_server_config["driver"]};SERVER={sql_server_config["server"]};DATABASE={database_name};UID={sql_server_config["username"]};PWD={sql_server_config["password"]}'
        )

        cursor = conn.cursor()
        cursor.execute(sql_query)
        columns = [column[0] for column in cursor.description]
        data = cursor.fetchall()

        # Ensure the number of columns in the data matches the columns list
        if data and len(data[0]) != len(columns):
            raise ValueError(f"Number of columns in data ({len(data[0])}) does not match the number of columns ({len(columns)})")

        # Convert the data to a list of dictionaries
        data_dicts = []
        for row in data:
            row_dict = dict(zip(columns, row))
            for col in columns:
                if col.endswith('_DT'):
                    row_dict[col] = convert_julian_to_date(row_dict[col])
            data_dicts.append(row_dict)

        df = pd.DataFrame(data_dicts)

        # Sanitize and encode the data to handle special characters
        df = df.applymap(lambda x: x if pd.isnull(x) else str(x).replace('?', '').encode('ascii', 'ignore').decode())

        # Clean, format, and add the sum row
        df_cleaned = clean_and_format_dataframe(df)

        cursor.close()
        conn.close()

        # Generate Excel in memory instead of saving to disk
        from io import BytesIO
        bio = BytesIO()
        df_cleaned.to_excel(bio, index=False)
        bio.seek(0)
        excel_data = bio.getvalue()
        
        excel_html = df_cleaned.to_html(classes='table table-bordered table-striped', escape=False)

        return excel_html, None, excel_data

    except Exception as e:
        error_message = f"An error occurred: {str(e)}"
        # Log the query and the error message together with a separator
        return None, error_message
