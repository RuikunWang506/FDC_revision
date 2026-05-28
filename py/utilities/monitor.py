import os
import requests
import time
import json
from datetime import datetime, timezone
import sys

# Configuration
HEALTH_URL = "http://fdcef.com/health"  # Adjust if different
DEFAULT_DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1383311381448757248/Ba2TYpvCOUh7Hx2tPkJUCaS_3e_zYhX1_Zfq2Crj7rScbCq9ldwYcfmPMCPnN8aKHZNW"
CHECK_INTERVAL = 60  # Check every 60 seconds
DOWN_THRESHOLD = 3  # Number of consecutive failures before alerting


def discord_webhook_url():
    """Same webhook as fdcef-status health monitor; override via DISCORD_WEBHOOK_URL env."""
    return (os.environ.get('DISCORD_WEBHOOK_URL') or DEFAULT_DISCORD_WEBHOOK_URL).strip()


def send_discord_notification(message, color=0xff0000, title="FDC Flask App Status"):
    """Post to the fdcef-status Discord webhook. Returns True on success."""
    data = {
        "username": "fdcef-status",
        "embeds": [{
            "title": title,
            "description": message,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    url = discord_webhook_url()
    try:
        response = requests.post(url, json=data, timeout=15)
        response.raise_for_status()
        try:
            from py.utilities.logging_setup import log_event
            log_event('info', f'Discord notification sent: {title}')
        except Exception:
            pass
        print("Discord notification sent successfully!")
        return True
    except requests.exceptions.RequestException as e:
        detail = str(e)
        if getattr(e, 'response', None) is not None:
            detail = f"{detail} status={e.response.status_code} body={(e.response.text or '')[:500]}"
        try:
            from py.utilities.logging_setup import log_event
            log_event('error', f'Discord notification failed: {detail}')
        except Exception:
            pass
        print(f"Failed to send Discord notification: {detail}")
        return False
    except Exception as e:
        try:
            from py.utilities.logging_setup import log_event
            log_event('error', f'Discord notification unexpected error: {e}')
        except Exception:
            pass
        print(f"Unexpected error sending Discord notification: {e}")
        return False

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