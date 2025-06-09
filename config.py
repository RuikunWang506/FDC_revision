import os

class Config:
    CATALOG_ORIGINAL = r"D:\Catalog_pictures\catalog_original"
    BIG_PICTURE_DIR  = r"D:\Individual_big_picture"
    LOCAL_DROPBOX_DIR = r"C:\users\stream\dropbox"
    Dimension_DIR = r"D:\Individual_big_picture\picture-temu"
    FTP = r"D:\FTP"
    SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1JvogexnVvPEPTxeuXhqt5zvYh7vMzOqhxCe6yxBWqaI/edit'
    SPREADSHEET_ID = SPREADSHEET_URL.split('/d/')[1].split('/edit')[0]
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
    TMX_sheet = 'https://docs.google.com/spreadsheets/d/1nNLDB4LruCniq6MiKXlBPeDhNu9IkoX3pZdNLUSPka8/edit?gid=2057295499#gid=2057295499'

    
    # Email configuration
    SMTP_SERVER = "smtp.fatcow.com"
    SMTP_PORT = 465
    EMAIL_ADDRESS = "Tristen@fdcus.com"
    EMAIL_PASSWORD = "Tr1st3n827"
    
    
    GOOGLE_CREDENTIALS_FILE = r'C:/FDC_FLASK_APP/client_secret.json'
    
