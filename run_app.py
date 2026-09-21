import os
import sys
import io
import webbrowser
import threading
import time
import uvicorn

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

def open_browser(port):
    time.sleep(1.5)
    print(f"Opening Mega Mall Dashboard in browser on port {port}...")
    webbrowser.open(f"http://localhost:{port}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    print("=" * 65)
    print("  MEGA MALL SYSTEM")
    print("=" * 65)
    print(f"Starting server at: http://{host}:{port}")
    print("Default Admin Account:  admin / Adm$Dfm0379")
    print("Concrete Company:       concrete / 12341234")
    print("Trust Company:          trust / Trs$jLP3211")
    print("MMD Company:            mmd / Mmd!iEg2427")
    print("TD Company:             td / Td!DOB0296")
    print("=" * 65)

    # Launch browser only in local interactive mode (not in cloud/headless environments)
    is_cloud = bool(os.environ.get("RENDER") or os.environ.get("PORT") or os.environ.get("HEADLESS"))
    if not is_cloud:
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # Start FastAPI server
    from server import app
    uvicorn.run(app, host=host, port=port, log_level="info")
