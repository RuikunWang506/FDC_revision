import os

class Config:
    CATALOG_RESIZED = r"D:\Catalog_pictures\catalog_resized"
    CATALOG_ORIGINAL = r"D:\Catalog_pictures\catalog_original"
    LOG_FILE = "Log.txt"
    SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1JvogexnVvPEPTxeuXhqt5zvYh7vMzOqhxCe6yxBWqaI/edit'
    SPREADSHEET_ID = SPREADSHEET_URL.split('/d/')[1].split('/edit')[0]
    SLACK_WEBHOOK_URL = 'https://hooks.slack.com/services/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'
    SLACK_WEBHOOK_URL_SHIPPING_RECEIPT = 'https://hooks.slack.com/services/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'
    SLACK_TOKEN = 'xoxb-4986748541874-7439245251395-YMVIoCeqbhWRp4rAFx66JGMK'
    SLACK_CHANNEL = 'C07CGNKSPF1'
    SERVICE_ACCOUNT_FILE = 'credentials.json'
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    USERS = {
        'fdc': '827',
        'yetimaster':'whitebox',
        'yeti': '5788',
        'ken': '2843',
        'ahua':'5788',
        'derrick': '0914',
        'nicye':'dick',
        'ting':'827',
        'helen':'827',
        'kx':'200',
        'erin':'7555',
        'dennis':'dennis',
        'baiqiang':'1234'
    }
