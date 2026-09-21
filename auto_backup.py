import os
import shutil
import sqlite3
import time
import threading
import json
import urllib.request
import urllib.parse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "mega_mall.db"))
BACKUP_DIR = os.environ.get("BACKUP_DIR", os.path.join(BASE_DIR, "backups"))
TELEGRAM_CONFIG_PATH = os.environ.get("TELEGRAM_CONFIG_PATH", os.path.join(BASE_DIR, "telegram_config.json"))

def send_backup_to_telegram(backup_path):
    if not os.path.exists(TELEGRAM_CONFIG_PATH):
        return False
    try:
        with open(TELEGRAM_CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f"Error reading telegram config: {e}")
        return False

    if not config.get('enabled') or not config.get('bot_token') or not config.get('chat_id'):
        return False

    bot_token = config['bot_token']
    chat_id = config['chat_id']
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    try:
        file_size_mb = os.path.getsize(backup_path) / (1024 * 1024)
        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        caption = f"نسخة احتياطية جديدة\nالتاريخ: {timestamp_str}\nالحجم: {file_size_mb:.2f} MB"
        
        # Build multipart form data
        boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
        
        headers = {
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }
        
        body = bytearray()
        
        # Add chat_id
        body.extend(f'--{boundary}\r\n'.encode('utf-8'))
        body.extend(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
        body.extend(f'{chat_id}\r\n'.encode('utf-8'))
        
        # Add caption
        body.extend(f'--{boundary}\r\n'.encode('utf-8'))
        body.extend(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
        body.extend(f'{caption}\r\n'.encode('utf-8'))
        
        # Add document
        filename = os.path.basename(backup_path)
        body.extend(f'--{boundary}\r\n'.encode('utf-8'))
        body.extend(f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode('utf-8'))
        body.extend(b'Content-Type: application/octet-stream\r\n\r\n')
        
        with open(backup_path, 'rb') as f:
            body.extend(f.read())
            
        body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode('utf-8'))
        
        req = urllib.request.Request(url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.getcode() == 200:
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Backup sent to Telegram successfully.")
                return True
            else:
                print(f"Failed to send backup. Status: {response.getcode()}")
                return False
                
    except Exception as e:
        print(f"Error sending backup to Telegram: {e}")
        return False

def test_telegram_connection(bot_token, chat_id):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    message = "✅ تم توصيل بوت النسخ الاحتياطي بنجاح! سيتم إرسال نسخة احتياطية تلقائياً."
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': message}).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.getcode() == 200:
                return True
            return False
    except Exception as e:
        print(f"Error testing Telegram connection: {e}")
        return False

def create_backup():
    """Create a safe snapshot of mega_mall.db using SQLite's online backup API."""
    if not os.path.exists(DB_PATH):
        return None
        
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"mega_mall_backup_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)

    try:
        source = sqlite3.connect(DB_PATH)
        dest = sqlite3.connect(backup_path)
        with dest:
            source.backup(dest)
        dest.close()
        source.close()
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Backup successfully created: {backup_filename}")
    except Exception as e:
        shutil.copy2(DB_PATH, backup_path)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fallback file copy created: {backup_filename}")

    # Retain the last 30 backups to save disk space
    try:
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith("mega_mall_backup_") and f.endswith(".db")])
        if len(backups) > 30:
            for old_file in backups[:-30]:
                os.remove(os.path.join(BACKUP_DIR, old_file))
    except Exception:
        pass

    send_backup_to_telegram(backup_path)

    return backup_path

def start_background_backup_scheduler(interval_hours: int = 12):
    """Start a lightweight daemon thread that triggers a backup every `interval_hours`."""
    def _loop():
        # Wait 30 seconds after server startup before running the first automated backup
        time.sleep(30)
        while True:
            try:
                create_backup()
            except Exception as ex:
                print(f"[Backup Scheduler Error]: {ex}")
            time.sleep(interval_hours * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="AutoBackupThread")
    t.start()

if __name__ == "__main__":
    create_backup()
