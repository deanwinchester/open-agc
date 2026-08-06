"""
Open-AGC Launcher — Entry point for the packaged application.
Starts the FastAPI server and opens the browser automatically.
Used by PyInstaller as the single-file entry point.
"""
import os
import sys
import webbrowser
import threading
import time

# Fix encoding on Windows (Chinese GBK locale) to prevent UnicodeEncodeError
if sys.stdout and sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and sys.stderr.encoding != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def get_base_dir():
    """Get the base directory (handles both dev and packaged modes)."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller bundle
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.abspath(__file__))

def setup_environment():
    """Set up paths and environment for the packaged app."""
    base_dir = get_base_dir()
    
    # If running from a bundle, ensure data directory exists in a writable location
    if getattr(sys, 'frozen', False):
        # Use user's home directory for writable data
        app_data = os.path.join(os.path.expanduser("~"), ".open-agc")
        os.makedirs(app_data, exist_ok=True)

        # Point the app to the writable data directory
        os.environ["OPEN_AGC_DATA_DIR"] = app_data

        # 播种逻辑与打包真实入口 gui_app.py 共用 core.paths.seed_frozen_data
        # （bundle data/* → <data>/，skills/* → <data>/skills/，不覆盖已有文件）
        from core.paths import seed_frozen_data
        seed_frozen_data(base_dir)
    
    # Change to base directory so relative paths work
    os.chdir(base_dir)

def open_browser_delayed(port=8000, delay=2.0):
    """Open the browser after a short delay to let the server start."""
    def _open():
        time.sleep(delay)
        webbrowser.open(f"http://localhost:{port}")
    
    t = threading.Thread(target=_open, daemon=True)
    t.start()

def main():
    setup_environment()
    
    port = int(os.environ.get("PORT", 8000))
    # 默认仅监听回环地址；局域网访问需显式设置 OPEN_AGC_HOST=0.0.0.0
    host = os.environ.get("OPEN_AGC_HOST", "127.0.0.1")
    
    print("=" * 40)
    print("  🐼 Open-AGC Panda is starting...")
    print(f"  http://localhost:{port}")
    print("=" * 40)
    
    # Auto-open browser
    open_browser_delayed(port)
    
    # Start the server
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host=host,
        port=port,
        log_level="info",
        # 访问控制按 scope["client"] 分类，必须关掉 uvicorn 默认的
        # proxy_headers（默认信任 127.0.0.1 的 XFF 并改写 client，
        # 同机透传式反代下可伪造 127.0.0.1 免密绕过）。本应用面向直连，
        # 无受信代理场景。
        proxy_headers=False,
    )

if __name__ == "__main__":
    main()
