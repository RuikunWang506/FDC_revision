import pandas as pd
from flask import Blueprint, request, jsonify, render_template, current_app, Response
import pdfkit
import os

products_bp = Blueprint('products', __name__, template_folder='templates')

# Function to read the Excel file and process the data
def read_excel_data():
    file_path = 'shopify_products.xlsx'
    df = pd.read_excel(file_path)
    product_columns = df.columns[:8]
    image_columns = df.columns[8:]
    df['images'] = df[image_columns].apply(lambda row: [url for url in row if pd.notna(url)], axis=1)
    return df[product_columns.tolist() + ['images']], df  # Also return the full dataframe for updates

# Removed the delete_image route since it's no longer needed

# Renamed the route to display products
@products_bp.route('/elsa')
def display_products():
    products, _ = read_excel_data()
    product_list = products.to_dict(orient='records')
    return render_template('products.html', products=product_list)
