import os
import tempfile
from pathlib import Path

class Config:
    # Database configuration for MySQL
    DB_CONFIG = {
        'host': '192.168.254.30',
        'port': 3306,
        'user': 'yeti',
        'password': 'qwe123',
        'database': 'onlinedb'
    }
    
    # SQL Server configuration
    SQL_SERVER_CONFIG = {
        'server': '192.168.254.204',
        'username': 'tristen',
        'password': 'Fdc2022!!',
        'driver': 'ODBC Driver 17 for SQL Server',
        'database_fdc': 'omsdata_FDC',
        'database_oms_ef': 'oms_EF'
    }
    
    # Legacy paths - DEPRECATED: Use DAM system instead
    LOCAL_DROPBOX_DIR = r"C:\users\stream\dropbox"  # Still used by V6listcreator
    DROPBOX_CONTAINER_CATALOG_DIR = r"C:\users\stream\Dropbox\Pictures-China (1)\container catalog"
    FTP = r"D:\FTP"
    
    # Image DAM paths - Use these for DAM system
    IMAGE_DAM_CATALOG_DIR = r"D:\Catalog_pictures\catalog_original"
    IMAGE_DAM_BIG_PICTURE_DIR = r"D:\Individual_big_picture"
    IMAGE_DAM_DROPBOX_DIR = r"C:\Users\Stream\Dropbox"
    IMAGE_DAM_TRASH_DIR = r"D:\catalog\trash"
    SLACK_WEBHOOK_URL = 'https://hooks.slack.com/services/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'
    SLACK_WEBHOOK_URL_SHIPPING_RECEIPT = 'https://hooks.slack.com/services/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'
    SLACK_TOKEN = 'xoxb-4986748541874-7439245251395-YMVIoCeqbhWRp4rAFx66JGMK'
    SLACK_CHANNEL = 'C07CGNKSPF1'
    SERVICE_ACCOUNT_FILE = r'C:/FDC_FLASK_APP/credentials.json'
    
    # Update these scopes to be consistent
    SCOPES = OAUTH_SCOPES = [
        'https://www.googleapis.com/auth/userinfo.profile',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/spreadsheets',
        'openid'
    ]
    
    LOG_FILE = r'C:/FDC_FLASK_APP/log.txt'  # Log file path for better accessibility

    
    # Email configuration
    SMTP_SERVER = "smtp.fatcow.com"
    SMTP_PORT = 465
    EMAIL_ADDRESS = "Tristen@fdcus.com"
    EMAIL_PASSWORD = "Tr1st3n827"
    
    
    GOOGLE_CREDENTIALS_FILE = r'C:/FDC_FLASK_APP/client_secret.json'
    
    # TikTok Shop API Configuration (V2 - 202309)
    TIKTOK_SHOP_API_KEY = os.getenv('TIKTOK_SHOP_API_KEY', 'TTP_k3VkrQAAAAAVoppuAGERJltMY_5k5EnTvLzFWow_TwZ_dYFG8G4ATZr2IvzJs4SmHJPG2POy2t-k7bP1LdcaKONRCZZOBwUK-qGXDA4N5GhKnpEh7IQpUz3FV0qSqmZbincx7wgRo41AczARWR71EuREIfKny4iU9IrdvJnvdQLYWcf_YyduhA')  # Access token
    TIKTOK_SHOP_APP_KEY = os.getenv('TIKTOK_SHOP_APP_KEY', '6af41c3jd9d4j')  # App key (for signature)
    TIKTOK_SHOP_SECRET = os.getenv('TIKTOK_SHOP_SECRET', '90ad10f5c10c75926b4974266c2c50ce3dc37867')
    TIKTOK_SHOP_SHOP_ID = os.getenv('TIKTOK_SHOP_ID', 'TTP_X7_XBQAAAAAgezTKnUsJ_dqag5diV7dB')  # Shop cipher (shop identifier)
    TIKTOK_SHOP_REFRESH_TOKEN = os.getenv('TIKTOK_SHOP_REFRESH_TOKEN', 'TTP_gnXpKAAAAAAt2jGFQFY3gQ9Rqa4UIGdzoCp--26F50FgtbPucWSNRy915euwFJqjIkk1STXEN48')
    # Note: Update TIKTOK_SHOP_API_URL based on your region
    # Regional endpoints:
    # - US: https://open-api-us.tiktokshop.com (may not resolve in all networks)
    # - EU: https://open-api-eu.tiktokshop.com
    # - SEA: https://open-api-sea.tiktokshop.com
    # - Global: https://open-api.tiktokglobalshop.com (works in most networks)
    # - China: https://open-api.tiktokshop.cn
    TIKTOK_SHOP_API_URL = os.getenv('TIKTOK_SHOP_API_URL', 'https://open-api.tiktokglobalshop.com')
    
    # ShipStation API Configuration

    # Database URL for SQLAlchemy
    DATABASE_URL = f"mysql+mysqlconnector://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"

    # Folder paths for image ingestion - use DAM system paths
    CATALOG_ORIGINAL_DIR = Path(IMAGE_DAM_CATALOG_DIR)
    INDIVIDUAL_BIG_PICTURE_DIR = Path(IMAGE_DAM_BIG_PICTURE_DIR)
    DROPBOX_DIR = Path(IMAGE_DAM_DROPBOX_DIR)

    # Trash directory for duplicates
    TRASH_DIR = Path(r"D:\catalog\trash")
    
    # Source identifiers
    SOURCES = {
        'catalog': 'catalog_original',
        'big_picture': 'individual_big_picture',
        'dropbox': 'dropbox'
    }

    # Image extensions to process
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}

    # Default image width for canonical images
    DEFAULT_CANONICAL_WIDTH = 800

    # Logging
    LOG_LEVEL = 'INFO'
    IMAGE_DAM_LOG_FILE = Path(r'C:/FDC_FLASK_APP/image_dam.log')

    # Minimal output directories for legacy code (to be removed)
    OUTPUT_DIRS = {
        'fedex_shipments': Path(r'C:/FDC_FLASK_APP/output/fedex_shipments'),
        'label_output': Path(r'C:/FDC_FLASK_APP/output/label_output'),
    }

    # Create directories for legacy code
    for dir_path in OUTPUT_DIRS.values():
        dir_path.mkdir(parents=True, exist_ok=True)


# Utility functions
from functools import wraps
from flask import session, request, redirect, url_for

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            # Store the current URL to redirect back after login
            session['next'] = request.url
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def resize_image(image_file, new_width):

    from PIL import Image
    import io

    with Image.open(image_file) as img:
        orig_width, orig_height = img.size
        new_height = int((new_width / orig_width) * orig_height)  # Calculate new height to maintain aspect ratio
        resized_img = img.resize((new_width, new_height), Image.LANCZOS)  # Use LANCZOS for high-quality resizing
        img_byte_arr = io.BytesIO()
        resized_img.save(img_byte_arr, format='JPEG')
        img_byte_arr.seek(0)
        return img_byte_arr
