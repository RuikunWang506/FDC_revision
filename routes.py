from flask import (request, render_template, jsonify, redirect, url_for, session, flash, send_from_directory,send_file, Response, make_response,render_template_string)
from werkzeug.utils import secure_filename
# Standard library imports
import os
import re
import time
import json
import mysql.connector
from mysql.connector import Error
# External library imports
import pandas as pd
from slack_sdk import WebClient
from user_agents import parse
from googleapiclient.discovery import build
from google.oauth2 import service_account
# Application-specific imports
from py.utilities.setting import Config
from py.utilities.database_utils import get_mysql_connection
from py.other.product_image import register_product_image_routes
from py.utilities.dam_routes import register_dam_routes
from py.Toolkits.Label_Maker import register_label_maker_routes
from py.Toolkits.view_all_routes import register_view_all_routes
from py.search.location_service import location_service_bp
from py.search.container import container_bp
from py.search.item_lookup import item_lookup_bp
from py.other.mobile_lite import mobile_lite_bp
import py.Toolkits.Label_Maker, py.order.order_search, py.utilities.SQL_connector
from py.order.macy_special import process_orders
from datetime import timedelta
from py.utilities.setting import login_required  # Import the login_required decorator
import py.utilities.models
import zipfile
from py.utilities.setting import resize_image  # Import the master resizing function
from PIL import Image
from pathlib import Path

from py.Toolkits.V6listcreator import v6_listcreator_bp
from py.loris.lori_automation import lori_automation_bp
from py.other.customer import customer_bp
from py.order.order_processor import order_processor_bp
from py.utilities.auth import auth_bp
from py.utilities.logging_setup import log_event, log_viewer_bp
from py.other.warehousemap import warehousemap_bp
from py.other.master_audit import master_audit_bp
from py.order.order_search import order_lookup_bp
from py.order.macy_special import macy_bp
from py.utilities.mysql_connector import mysql_connector_bp
from py.utilities.image_upload_routes import image_upload_bp
from py.other.master_image_viewer import master_image_viewer_bp
from py.dam.dam_sync_routes import dam_sync_bp
from py.other.bag_style_viewer import bag_style_viewer_bp



def register_routes(app):

    register_product_image_routes(app)
    register_dam_routes(app)
    register_label_maker_routes(app)
    register_view_all_routes(app)
    app.register_blueprint(location_service_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(item_lookup_bp)
    app.register_blueprint(mobile_lite_bp)
    app.register_blueprint(v6_listcreator_bp)
    app.register_blueprint(lori_automation_bp, url_prefix='/lori_automation')
    app.register_blueprint(customer_bp)
    app.register_blueprint(order_processor_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(warehousemap_bp)
    app.register_blueprint(master_audit_bp)
    app.register_blueprint(log_viewer_bp)
    app.register_blueprint(order_lookup_bp)
    app.register_blueprint(macy_bp)
    app.register_blueprint(mysql_connector_bp)
    app.register_blueprint(image_upload_bp)
    app.register_blueprint(master_image_viewer_bp)
    app.register_blueprint(dam_sync_bp)
    app.register_blueprint(bag_style_viewer_bp)


    app.permanent_session_lifetime = timedelta(days=30)

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def home():
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        log_event("INFO", f'{client_ip} "{request.method} {request.path}"')
        
        current_user = session['username']
        return render_template("index.html", current_user=current_user)

    @app.route("/robots.txt")
    def robots_txt():
        client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        proto = request.environ.get("SERVER_PROTOCOL", "HTTP/1.1")
        log_event("INFO", f'{client_ip} "GET /robots.txt {proto}"')
        # app.static_folder points at your 'static' directory
        return send_from_directory(app.static_folder, "robots.txt")
    
    
    