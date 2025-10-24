#!/usr/bin/env python3
"""
FDC Image Automation System Status Dashboard
Shows the current status of all automation components
"""

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from py.utilities.database_utils import get_mysql_connection
from py.utilities.setting import Config

def check_scheduled_task():
    """Check if the Windows scheduled task exists"""
    try:
        result = subprocess.run([
            'schtasks', '/query', '/tn', 'FDC Smart Auto-Filter'
        ], capture_output=True, text=True)
        
        if result.returncode == 0:
            return "✅ Scheduled task exists and is active"
        else:
            return "❌ Scheduled task not found - needs setup"
    except Exception as e:
        return f"⚠️  Could not check scheduled task: {e}"

def check_processed_files_log():
    """Check the processed files log status"""
    log_file = Path("processed_files.log")
    if log_file.exists():
        stat = log_file.stat()
        mod_time = datetime.fromtimestamp(stat.st_mtime)
        file_size = stat.st_size
        
        # Count lines in the file
        with open(log_file, 'r', encoding='utf-8') as f:
            line_count = sum(1 for _ in f)
            
        return f"✅ Log file exists - {line_count} processed files, last updated: {mod_time.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        return "⚠️  No processed files log found"

def check_database_images():
    """Check recent images in database"""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        
        # Check total catalog images
        cursor.execute("SELECT COUNT(*) FROM image_assets WHERE source = 'catalog' AND is_active = 1")
        total_catalog = cursor.fetchone()[0]
        
        # Check recent images (last 7 days)
        cursor.execute("""
            SELECT COUNT(*) FROM image_assets 
            WHERE source = 'catalog' AND is_active = 1 
            AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        """)
        recent_catalog = cursor.fetchone()[0]
        
        # Check our test images
        test_skus = ['T3665', 'T3666', 'T3667', 'T3668']
        placeholders = ', '.join(['%s'] * len(test_skus))
        cursor.execute(f"SELECT sku, filename FROM image_assets WHERE sku IN ({placeholders}) AND source = 'catalog'", test_skus)
        test_results = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        status = f"✅ Database: {total_catalog} total catalog images, {recent_catalog} added in last 7 days\n"
        status += f"   Test images found: {len(test_results)}/4 (T3665-T3668)"
        
        if test_results:
            status += "\n   " + ", ".join([f"{sku}" for sku, filename in test_results])
        
        return status
        
    except Exception as e:
        return f"❌ Database check failed: {e}"

def check_directories():
    """Check key directories"""
    directories = {
        "Dropbox Source": Config.DROPBOX_CONTAINER_CATALOG_DIR,
        "Catalog Destination": Config.IMAGE_DAM_CATALOG_DIR,
        "Big Picture Destination": Config.IMAGE_DAM_BIG_PICTURE_DIR
    }
    
    status = []
    for name, path in directories.items():
        if os.path.exists(path):
            file_count = len([f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
            status.append(f"✅ {name}: {file_count} images")
        else:
            status.append(f"❌ {name}: Directory not found")
    
    return "\n".join(status)

def check_flask_routes():
    """Check if Flask routes are accessible"""
    try:
        from app import app
        routes = []
        for rule in app.url_map.iter_rules():
            if 'image' in str(rule):
                routes.append(str(rule))
        
        return f"✅ Flask routes available: {len(routes)} image-related routes\n   Key routes: /image_upload, /image/<id>/<width>"
    except Exception as e:
        return f"❌ Flask route check failed: {e}"

def main():
    """Run the complete status check"""
    print("🎯 FDC Image Automation System Status Dashboard")
    print("=" * 60)
    print(f"📅 Status checked at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    print("🤖 AUTOMATION COMPONENTS:")
    print("-" * 30)
    print("1. Scheduled Task:", check_scheduled_task())
    print("2. Processed Files Log:", check_processed_files_log())
    print()
    
    print("💾 DATABASE & STORAGE:")
    print("-" * 30)
    print(check_database_images())
    print()
    print(check_directories())
    print()
    
    print("🌐 WEB INTERFACE:")
    print("-" * 30)
    print(check_flask_routes())
    print()
    
    print("📋 NEXT ACTIONS NEEDED:")
    print("-" * 30)
    
    # Check what still needs to be done
    actions_needed = []

    if "❌" in check_scheduled_task():
        actions_needed.append("1. Run 1.bat as Administrator once to create the Smart Auto-Filter scheduled task")
    
    if not os.path.exists("processed_files.log"):
        actions_needed.append(
            "2. Initialize Smart Auto-Filter log: either run 1.bat once as Administrator (creates the scheduled task and first run), or run py/standalone/smart_auto_filter.py manually one time"
        )
    
    # Check if Flask app is running
    actions_needed.append("3. Start Flask app with: python app.py")
    actions_needed.append("4. Access web interface at: http://localhost:8888/image_upload")
    
    if actions_needed:
        for action in actions_needed:
            print(f"   {action}")
    else:
        print("   🎉 All components are set up and working!")
    
    print()
    print("🔄 AUTOMATION FLOW:")
    print("-" * 30)
    print("   📂 Dropbox → 🤖 Smart Filter → 📁 Organized Folders → 💾 Database → 🌐 Web Interface")
    print()
    print("⏰ SCHEDULING:")
    print("-" * 30)
    print("   • Smart auto-filter runs every 3 days at 2:00 AM")
    print("   • Manual uploads available via web interface")
    print("   • Database sync happens automatically after organization")

if __name__ == "__main__":
    main()
