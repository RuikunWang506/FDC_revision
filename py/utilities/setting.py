import os
from pathlib import Path
from urllib.parse import quote_plus

from py.utilities.nssm_env import load_nssm_env_file

load_nssm_env_file()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    SECRET_KEY = _env("SECRET_KEY")
    FDC_REQUIRE_SECRETS = _env("FDC_REQUIRE_SECRETS", "0").lower() in ("1", "true", "yes")

    # Database configuration for MySQL — credentials come from env (see nssm-fdcef-environment.env).
    DB_CONFIG = {
        "host": _env("MYSQL_HOST", "192.168.254.30"),
        "port": _env_int("MYSQL_PORT", 3306),
        "user": _env("MYSQL_USER", "yeti"),
        "password": _env("MYSQL_PASSWORD"),
        "database": _env("MYSQL_DATABASE", "onlinedb"),
    }

    # Cloudflare Turnstile (login CAPTCHA). Empty keys fail closed at verify time.
    TURNSTILE_SITE_KEY = _env("TURNSTILE_SITE_KEY")
    TURNSTILE_SECRET_KEY = _env("TURNSTILE_SECRET_KEY")

    SQL_SERVER_CONFIG = {
        "server": _env("SQLSERVER_HOST", "192.168.254.204"),
        "username": _env("SQLSERVER_USER"),
        "password": _env("SQLSERVER_PASSWORD"),
        "driver": _env("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server"),
        "database_fdc": _env("SQLSERVER_DATABASE_FDC", "omsdata_FDC"),
        "database_oms_ef": _env("SQLSERVER_DATABASE_OMS_EF", "oms_EF"),
    }
    
    # Legacy paths - DEPRECATED: Use DAM system instead
    LOCAL_DROPBOX_DIR = r"C:\users\stream\dropbox"  # Still used by V7listcreator
    BURLINGTON_HIGHLIGHT = r"C:\users\stream\dropbox\ruikun" 
    DROPBOX_CONTAINER_CATALOG_DIR = r"C:\users\stream\Dropbox\Pictures-China (1)\container catalog"
    FTP = r"D:\FTP"
    
    # Image DAM paths - Use these for DAM system
    IMAGE_DAM_CATALOG_DIR = r"D:\Catalog_pictures\catalog_original"
    IMAGE_DAM_BIG_PICTURE_DIR = r"D:\Individual_big_picture"
    IMAGE_DAM_DROPBOX_DIR = r"C:\Users\Stream\Dropbox"
    IMAGE_DAM_TRASH_DIR = r"D:\catalog\trash"
    SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL")
    SLACK_WEBHOOK_URL_SHIPPING_RECEIPT = _env("SLACK_WEBHOOK_URL_SHIPPING_RECEIPT")
    SLACK_TOKEN = _env("SLACK_TOKEN")
    SLACK_CHANNEL = _env("SLACK_CHANNEL")
    SLACK_WEBHOOK_URL_V7_LIST_CREATOR = _env("SLACK_WEBHOOK_URL_V7_LIST_CREATOR")
    # Optional override for webmail “message sent” Slack posts (defaults to V7 webhook in notify_mail_sent_optional).
    SLACK_WEBHOOK_URL_MAIL_SENT = os.getenv('SLACK_WEBHOOK_URL_MAIL_SENT', '').strip()
    # Private DM target (Attendance / ops alerts to Tristen only).
    SLACK_DM_USER = _env("SLACK_DM_USER", "tristen")
    SLACK_DM_USER_ID = _env("SLACK_DM_USER_ID")
    # Attendance Wi-Fi join/leave channel (e.g. #attedence private channel id C…).
    SLACK_ATTENDANCE_CHANNEL = _env("SLACK_ATTENDANCE_CHANNEL")
    SERVICE_ACCOUNT_FILE = r'C:/FDC_FLASK_APP/credentials.json'
    
    # Update these scopes to be consistent
    SCOPES = OAUTH_SCOPES = [
        'https://www.googleapis.com/auth/userinfo.profile',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/spreadsheets',
        'openid'
    ]
    
    LOG_FILE = r'C:/FDC_FLASK_APP/log.txt'  # Legacy single file (left in place; new writes go to LOG_DIR)
    LOG_DIR = os.path.join(r'C:/FDC_FLASK_APP', 'output', 'logs')
    SYSTEM_LOG_FILE = os.path.join(LOG_DIR, 'system.log')
    ERROR_LOG_FILE = os.path.join(LOG_DIR, 'error.log')
    USER_LOG_FILE = os.path.join(LOG_DIR, 'user.log')
    ONLINE_LOG_FILE = os.path.join(LOG_DIR, 'online.log')
    VISITOR_LOG_FILE = os.path.join(LOG_DIR, 'visitor.log')

    FDC_AI_PROVIDER = os.getenv('FDC_AI_PROVIDER', 'ollama').strip() or 'ollama'
    FDC_AI_BASE_URL = (os.getenv('FDC_AI_BASE_URL') or 'http://127.0.0.1:11434').rstrip('/')
    FDC_AI_FALLBACK_URL = (os.getenv('FDC_AI_FALLBACK_URL') or '').strip().rstrip('/')
    FDC_AI_MODEL = (os.getenv('FDC_AI_MODEL') or '').strip()
    FDC_AI_KEEP_ALIVE = os.getenv('FDC_AI_KEEP_ALIVE', '10m')
    FDC_AI_NUM_GPU = os.getenv('FDC_AI_NUM_GPU', '').strip()

    
    SMTP_SERVER = _env("SMTP_SERVER", "smtp.fatcow.com")
    SMTP_PORT = _env_int("SMTP_PORT", 465)
    EMAIL_ADDRESS = _env("EMAIL_ADDRESS")
    EMAIL_PASSWORD = _env("EMAIL_PASSWORD")

    GOOGLE_CREDENTIALS_FILE = r'C:/FDC_FLASK_APP/client_secret.json'

    TIKTOK_SHOP_API_KEY = _env("TIKTOK_SHOP_API_KEY")
    TIKTOK_SHOP_APP_KEY = _env("TIKTOK_SHOP_APP_KEY")
    TIKTOK_SHOP_SECRET = _env("TIKTOK_SHOP_SECRET")
    TIKTOK_SHOP_SHOP_ID = _env("TIKTOK_SHOP_ID")
    TIKTOK_SHOP_REFRESH_TOKEN = _env("TIKTOK_SHOP_REFRESH_TOKEN")
    # Note: Update TIKTOK_SHOP_API_URL based on your region
    # Regional endpoints:
    # - US: https://open-api-us.tiktokshop.com (may not resolve in all networks)
    # - EU: https://open-api-eu.tiktokshop.com
    # - SEA: https://open-api-sea.tiktokshop.com
    # - Global: https://open-api.tiktokglobalshop.com (works in most networks)
    # - China: https://open-api.tiktokshop.cn
    TIKTOK_SHOP_API_URL = os.getenv('TIKTOK_SHOP_API_URL', 'https://open-api.tiktokglobalshop.com')
    
    # ShipStation API Configuration

    DATABASE_URL = (
        f"mysql+mysqlconnector://{quote_plus(DB_CONFIG['user'])}:{quote_plus(DB_CONFIG['password'])}"
        f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    )

    # --- Public vs LAN base URLs ---
    # Password reset and other emails must always use the public HTTPS origin (never a LAN IP).
    PUBLIC_APP_ORIGIN = os.getenv('PUBLIC_APP_ORIGIN', 'https://www.fdcef.com').rstrip('/')
    # Shown in navbar when user is on LAN; override if your listen port differs from default.
    LAN_ORIGIN_HINT = os.getenv('LAN_ORIGIN_HINT', 'http://192.168.254.30:5555').rstrip('/')

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
    }

    # Create directories for legacy code
    for dir_path in OUTPUT_DIRS.values():
        dir_path.mkdir(parents=True, exist_ok=True)

    # Re-read Jinja templates from disk without restarting the process (works with debug off).
    TEMPLATES_AUTO_RELOAD = True
    # Cache CSS/JS/images in the browser so the homepage is not re-downloaded every visit.
    SEND_FILE_MAX_AGE_DEFAULT = int(os.getenv('FDC_STATIC_MAX_AGE', str(12 * 60 * 60)))


from py.utilities.decorators import login_required  # re-export for existing imports


def validate_required_secrets(*, require: bool | None = None) -> None:
    """Raise if production/service is missing credentials that used to live in source."""
    if require is None:
        require = Config.FDC_REQUIRE_SECRETS or (
            str(os.environ.get("NSSM_SERVICE_NAME") or "").strip() != ""
        )
    if not require:
        return
    missing = []
    if not Config.SECRET_KEY:
        missing.append("SECRET_KEY")
    if not Config.DB_CONFIG.get("password"):
        missing.append("MYSQL_PASSWORD")
    if not Config.SQL_SERVER_CONFIG.get("password"):
        missing.append("SQLSERVER_PASSWORD")
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

def resize_image(image_file, new_width):

    from PIL import Image
    import io

    with Image.open(image_file) as img:
        orig_width, orig_height = img.size
        new_height = int((new_width / orig_width) * orig_height)  # Calculate new height to maintain aspect ratio
        resized_img = img.resize((new_width, new_height), Image.LANCZOS)  # Use LANCZOS for high-quality resizing
        if resized_img.mode in ('RGBA', 'LA', 'P'):
            resized_img = resized_img.convert('RGB')
        img_byte_arr = io.BytesIO()
        resized_img.save(img_byte_arr, format='JPEG')
        img_byte_arr.seek(0)
        return img_byte_arr
