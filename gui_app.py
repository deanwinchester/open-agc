"""
Desktop GUI wrapper for Open-AGC using pywebview.
Embeds the web interface in a native window with system tray controls.
"""
import sys
import os
import threading
import time
import signal


def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def start_server(port):
    """Start the uvicorn server in a background thread."""
    import uvicorn
    import sys
    import os
    
    # Strictly prevent printing errors in noconsole mode
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        
    try:
        from api.server import app
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        with open("server_crash.log", "a") as f:
            f.write(f"Server crash: {e}\n")


def check_server_and_load(window, port):
    import requests
    import time
    for _ in range(60):
        try:
            resp = requests.get(f"http://localhost:{port}/static/index.html", timeout=1)
            if resp.status_code == 200:
                window.load_url(f"http://localhost:{port}")
                return
        except:
            time.sleep(0.5)
            
    # Timeout reached without success
    try:
        window.evaluate_js("""
            document.querySelector('.loader').style.display = 'none';
            document.querySelector('h2').innerText = '启动失败 (Startup Failed)';
            document.querySelector('h2').style.color = '#ef4444';
            document.querySelector('p').innerHTML = '后台服务未能正常启动，可能是端口被占用或内部错误。<br>请尝试查看 server_crash.log。';
        """)
    except Exception:
        pass

def create_window(port):
    """Create a native window with the web UI embedded."""
    try:
        import webview
    except ImportError:
        print("pywebview not installed. Install with: pip install pywebview")
        print(f"Falling back to browser mode: http://localhost:{port}")
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
        return False

    loading_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Loading...</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #f7f9fa; color: #333; }
            .container { text-align: center; }
            .loader { border: 4px solid #e2e8f0; border-top: 4px solid #3b82f6; border-radius: 50%; width: 48px; height: 48px; animation: spin 1s linear infinite; margin: 0 auto 20px auto; }
            @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
            h2 { margin: 0; font-weight: 500; font-size: 20px; color: #475569; }
            p { color: #94a3b8; font-size: 14px; margin-top: 8px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="loader"></div>
            <h2>Starting Open-AGC Panda...</h2>
            <p>Loading core components...</p>
        </div>
    </body>
    </html>
    """

    # Create native window
    window = webview.create_window(
        title="🐼 Open-AGC Panda",
        html=loading_html,
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

    import threading
    t = threading.Thread(target=check_server_and_load, args=(window, port), daemon=True)
    t.start()

    webview.start(
        debug=False,
        gui=None,  # Auto-detect best backend
    )
    return True


def main():
    # Attempt to find a free port instead of hardcoding 8765
    try:
        port = int(os.environ.get("PORT", find_free_port()))
    except:
        port = 8765

    # Handle PyInstaller frozen mode
    if getattr(sys, 'frozen', False):
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        else:
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except Exception:
                pass
                
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        else:
            try:
                sys.stderr.reconfigure(encoding='utf-8')
            except Exception:
                pass
            
        base_dir = sys._MEIPASS
        os.chdir(base_dir)

        # Setup writable data dir using unified paths logic
        from core.paths import get_base_dir
        data_dir = get_base_dir()
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

    def safe_print(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('ascii', 'replace').decode('ascii'))

    safe_print("=" * 40)
    safe_print("  [*] Open-AGC Panda is starting...")
    safe_print(f"  http://localhost:{port}")
    safe_print("=" * 40)

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
            safe_print("\n[*] Shutting down...")
            sys.exit(0)


if __name__ == "__main__":
    main()
