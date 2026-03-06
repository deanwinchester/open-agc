"""
Desktop GUI wrapper for Open-AGC using pywebview.
Embeds the web interface in a native window with system tray controls.
"""
import sys
import os
import threading
import time
import signal


def start_server(port=8000):
    """Start the uvicorn server in a background thread."""
    import uvicorn
    uvicorn.run("api.server:app", host="0.0.0.0", port=port, log_level="warning")


def create_window(port=8000):
    """Create a native window with the web UI embedded."""
    try:
        import webview
    except ImportError:
        print("pywebview not installed. Install with: pip install pywebview")
        print(f"Falling back to browser mode: http://localhost:{port}")
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
        return False

    # Wait for server to be ready
    import requests
    for _ in range(30):
        try:
            resp = requests.get(f"http://localhost:{port}/static/index.html", timeout=1)
            if resp.status_code == 200:
                break
        except:
            time.sleep(0.5)

    # Create native window
    window = webview.create_window(
        title="🐼 Open-AGC Panda",
        url=f"http://localhost:{port}",
        width=1200,
        height=800,
        min_size=(800, 600),
        resizable=True,
        text_select=True,
        confirm_close=True,
    )

    # Add menu items for restart/about
    def on_closing():
        os._exit(0)

    window.events.closing += on_closing

    webview.start(
        debug=False,
        gui=None,  # Auto-detect best backend
    )
    return True


def main():
    port = int(os.environ.get("PORT", 8000))

    # Handle PyInstaller frozen mode
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
        os.chdir(base_dir)

        # Setup writable data dir
        data_dir = os.path.expanduser("~/.open-agc")
        os.makedirs(data_dir, exist_ok=True)
        os.environ["OPEN_AGC_DATA_DIR"] = data_dir

        # Copy initial data files if needed
        import shutil
        for item in ["data", "skills"]:
            src = os.path.join(base_dir, item)
            dst = os.path.join(data_dir, item)
            if os.path.exists(src):
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    if not os.path.exists(dst):
                        shutil.copy2(src, dst)

    print("=" * 40)
    print("  🐼 Open-AGC Panda is starting...")
    print(f"  http://localhost:{port}")
    print("=" * 40)

    # Start server in background thread
    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()

    # Try to create native window, fallback to browser
    if not create_window(port):
        # Keep main thread alive if using browser mode
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🐼 Shutting down...")
            sys.exit(0)


if __name__ == "__main__":
    main()
