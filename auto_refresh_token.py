import json
import requests
import time
import schedule

# Load credentials from TK_credential.json
with open('TK_credential.json') as f:
    credentials = json.load(f)

app_key = credentials['app_key']
app_secret = credentials['app_secret']
refresh_token = credentials['refresh_token']

def refresh_access_token():
    url = "https://auth.tiktok-shops.com/api/v2/token/refresh"
    params = {
        "app_key": app_key,
        "app_secret": app_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }

    response = requests.get(url, params=params)
    if response.status_code == 200:
        response_data = response.json()
        if response_data["code"] == 0:
            new_access_token = response_data["data"]["access_token"]
            new_refresh_token = response_data["data"]["refresh_token"]
            credentials["access_token"] = new_access_token
            credentials["refresh_token"] = new_refresh_token

            with open('TK_credential.json', 'w') as f:
                json.dump(credentials, f, indent=4)
            
            print("Access token refreshed and saved to TK_credential.json")
        else:
            print(f"Failed to refresh token: {response_data['message']}")
    else:
        print(f"Failed to refresh token: {response.status_code}")

# Schedule the job every 3 days
schedule.every(3).days.do(refresh_access_token)

while True:
    schedule.run_pending()
    time.sleep(1)
