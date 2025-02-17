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
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    LOG_FILE = r'C:/FDC_FLASK_APP/log.txt'  # Log file path for better accessibility

    
    # Users dictionary
    USERS = {
        'fdc': '827',
        'yetimaster':'whitebox',
        'yeti': '5788',
        'ken': '2843',
        'ahua':'5788',
        'jennifer': '123456',
        'nicye':'dick',
        'ting':'827',
        'helen':'827',
        'kx':'200',
        'dennis':'dennis',
        'jiayi':'1234',
        'xj':'88',
        'wh':'1234',
        'tmx':'qwe123'

    }
    
