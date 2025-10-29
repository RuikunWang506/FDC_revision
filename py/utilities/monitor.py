import requests
import time
import json
from datetime import datetime
import sys

# Configuration
HEALTH_URL = "http://fdcef.com/health"  # Adjust if different
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1383311381448757248/Ba2TYpvCOUh7Hx2tPkJUCaS_3e_zYhX1_Zfq2Crj7rScbCq9ldwYcfmPMCPnN8aKHZNW"  # Discord webhook
CHECK_INTERVAL = 60  # Check every 60 seconds
DOWN_THRESHOLD = 3  # Number of consecutive failures before alerting

def send_discord_notification(message, color=0xff0000):
    data = {
        "embeds": [{
            "title": "FDC Flask App Status",
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data)
        response.raise_for_status()
        print("Discord notification sent successfully!")
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Discord notification: {e}")
        if hasattr(e, 'response') and e.response:
            print(f"Response status: {e.response.status_code}")
            print(f"Response text: {e.response.text}")
    except Exception as e:
        print(f"Unexpected error sending Discord notification: {e}")

def check_health():
    try:
        response = requests.get(HEALTH_URL, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "healthy":
                return True
        return False
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Sending test Discord notification...")
        send_discord_notification("🧪 Test notification from FDC Flask App monitor", 0x00ff00)
        return
    
    consecutive_failures = 0
    service_down = False

    print("Starting FDC Flask App health monitor...")
    send_discord_notification("🟢 FDC Flask App health monitor started", 0x00ff00)

    while True:
        if check_health():
            if service_down:
                send_discord_notification("🟢 FDC Flask App is back online!", 0x00ff00)
                service_down = False
            consecutive_failures = 0
            print(f"[{datetime.now()}] Service is healthy")
        else:
            consecutive_failures += 1
            print(f"[{datetime.now()}] Service check failed ({consecutive_failures}/{DOWN_THRESHOLD})")

            if consecutive_failures >= DOWN_THRESHOLD and not service_down:
                send_discord_notification("🔴 FDC Flask App appears to be down! Please check the service.", 0xff0000)
                service_down = True

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()