"""
Desktop GUI wrapper for Open-AGC using pywebview.
Embeds the web interface in a native window with system tray controls.
"""
import sys
import os
import threading
import time
import signal

# --- Tiktoken Monkeypatch for PyInstaller ---
try:
    import tiktoken
    from tiktoken.core import Encoding
    
    def get_mock_encoding(name):
        return Encoding(
            name="cl100k_base",
            pat_str=r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]|\s+(?!\S)|\s+""",
            mergeable_ranks={},
            special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258, "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260, "<|endofprompt|>": 100276}
        )

    try:
        tiktoken.get_encoding("cl100k_base")
    except Exception:
        tiktoken.get_encoding = lambda name: get_mock_encoding(name)
        tiktoken.encoding_for_model = lambda model: get_mock_encoding("cl100k_base")
except Exception:
    pass
# --------------------------------------------


def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _crash_log_path() -> str:
    """崩溃日志写入可写的数据目录（OPEN_AGC_DATA_DIR，frozen 下为
    ~/.open-agc）。deb 安装到 /opt/open-agc 是 root 所有，裸写 cwd 会
    Permission denied 并掩盖真实错误——必须先落到用户可写位置。"""
    try:
        data_dir = os.environ.get("OPEN_AGC_DATA_DIR") or os.path.join(
            os.path.expanduser("~"), ".open-agc")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "server_crash.log")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "open-agc-server_crash.log")


def _crash_log_hint() -> str:
    return _crash_log_path()


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
        # 默认仅监听回环地址；局域网访问需显式设置 OPEN_AGC_HOST=0.0.0.0
        host = os.environ.get("OPEN_AGC_HOST", "127.0.0.1")
        # proxy_headers=False：同 launcher.py——禁用 uvicorn 默认的 XFF 信任，
        # 防止伪造 X-Forwarded-For: 127.0.0.1 绕过访问控制。
        uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)
    except Exception as e:
        try:
            with open(_crash_log_path(), "a", encoding="utf-8") as f:
                f.write(f"Server crash: {e}\n")
        except Exception:
            # 连日志都写不进时打到 stderr，避免掩盖真实崩溃原因
            try:
                print(f"Server crash: {e}", file=sys.stderr)
            except Exception:
                pass


def wait_for_server_ready(port, timeout=60):
    """轮询直到 uvicorn 服务就绪或超时。返回 True 表示就绪。

    浏览器回退与原生窗口都必须等服务起来再打开页面——否则浏览器会在
    服务监听前就发起请求，显示「localhost 拒绝了我们的连接请求」。
    """
    import requests
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"http://localhost:{port}/static/icon_rounded.png", timeout=1)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _gtk_diag_dump(window, tag, delay):
    """诊断：输出 pywebview GTK 窗口内部状态（透明度/URL/DOM/localStorage）。
    仅 OPEN_AGC_GTK_DIAG=1 时启用——用于排查 UOS 上 WebView 白屏
    （pywebview 先 set_opacity(0) 再等 load_changed 恢复 1.0 的链路是否断开）。"""
    time.sleep(delay)
    try:
        bv = getattr(window, 'gui', None)
        wv = getattr(bv, 'webview', None)
        if wv is not None:
            print(f"[diag:{tag}] opacity={wv.props.opacity} uri={wv.get_uri()} visible={wv.props.visible}")
        else:
            print(f"[diag:{tag}] no gui/webview handle (gui={type(bv).__name__})")
    except Exception as e:
        print(f"[diag:{tag}] widget state error: {e}")
    try:
        r = window.evaluate_js(
            "JSON.stringify({state:document.readyState,"
            "body:document.body?document.body.innerHTML.length:-1,"
            "app:(document.getElementById('app')||{innerHTML:''}).innerHTML.length,"
            "ls:(function(){try{localStorage.setItem('__t','1');return 'ok'}catch(e){return 'ERR:'+e.message}})()})"
        )
        print(f"[diag:{tag}] js={r}")
    except Exception as e:
        print(f"[diag:{tag}] evaluate_js error: {e}")


def check_server_and_load(window, port):
    if wait_for_server_ready(port, timeout=60):
        window.load_url(f"http://localhost:{port}")
        if os.environ.get('OPEN_AGC_GTK_DIAG') == '1':
            for tag, delay in (('t+4s', 4), ('t+10s', 10)):
                threading.Thread(target=_gtk_diag_dump, args=(window, tag, delay), daemon=True).start()
        return

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

def _setup_webview2_runtime():
    """Windows 上使用包内嵌的 WebView2 fixed-version 运行时，使 pywebview 走
    edgechromium（Chromium）而非 IE(mshtml)——edgechromium 才支持文件拖放与
    Ctrl+C/V。目标机无需预装 WebView2 运行时。返回运行时路径（无内嵌则 None）。
    """
    try:
        import webview
    except Exception:
        return None
    candidates = []
    if getattr(sys, 'frozen', False):
        # frozen COLLECT：sys._MEIPASS 指向 _internal
        candidates.append(os.path.join(sys._MEIPASS, 'webview2_runtime'))
    candidates.append(os.path.abspath(os.path.join('build', 'webview2_runtime')))
    for p in candidates:
        if os.path.isfile(os.path.join(p, 'msedgewebview2.exe')):
            try:
                webview.settings['WEBVIEW2_RUNTIME_PATH'] = p
                print(f"[webview2] 使用内嵌运行时: {p}")
                return p
            except Exception as e:
                print(f"[webview2] 设置运行时路径失败: {e}")
    return None


def create_window(port):
    """Create a native window with the web UI embedded.

    原生窗口依赖系统 GUI 后端（Windows WinForms / macOS Cocoa / Linux GTK +
    WebKit2 typelib）。Linux 上若 PyGObject/WebKit2 不可用（如目标机缺
    gir1.2-webkit2-4.0 或 Python 版本不匹配），整个创建过程可能抛异常——
    回退到默认浏览器打开 Web UI，保证应用可用。
    """
    def _open_browser_when_ready():
        """等待后端服务就绪后再打开浏览器（后台线程，不阻塞 create_window）。
        服务 60s 内未就绪则提示查看 server_crash.log，不再裸开 URL。"""
        if wait_for_server_ready(port, timeout=60):
            import webbrowser
            webbrowser.open(f"http://localhost:{port}")
        else:
            try:
                print(f"后台服务未能正常启动（localhost 拒绝连接）。请查看 "
                      f"{_crash_log_hint()} 排查。")
            except Exception:
                pass

    def _browser_fallback(reason):
        try:
            print(f"Native window unavailable ({reason}); falling back to browser mode: http://localhost:{port}")
        except Exception:
            pass
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
        return False

    try:
        import webview
    except ImportError:
        print("pywebview not installed. Install with: pip install pywebview")
        print(f"Falling back to browser mode: http://localhost:{port}")
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
        return False

    # Windows：优先使用包内嵌的 WebView2 fixed-version 运行时（edgechromium，
    # 支持文件拖放与 Ctrl+C/V）；无内嵌时回退系统 WebView2/IE。
    _setup_webview2_runtime()

    # Linux：pywebview 5.0+ 的 GTK 后端依赖 WebKitGTK 2.40+ API，UOS/deepin
    # 的 2.38 上会 AttributeError 中断 decide-policy 回调导致导航挂起白屏。
    try:
        from core.pywebview_gtk_compat import apply_if_needed
        apply_if_needed()
    except Exception:
        pass

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

    # Create native window — 任一环节失败（GTK 后端缺 gi、WebKit2 typelib
    # 缺失、Python 版本不匹配等）都回退浏览器模式，保证 Web UI 可用。
    try:
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

        # OPEN_AGC_GTK_DIAG=1 时开启 debug（GTK 下启用 WebKit 开发者工具，
        # 窗口内右键 → Inspect Element 可看控制台）
        _diag_mode = os.environ.get('OPEN_AGC_GTK_DIAG') == '1'
        _start_kwargs = {'debug': _diag_mode, 'gui': None}  # gui=None：自动选后端
        if sys.platform.startswith('linux'):
            # pywebview 默认 private_mode=True，GTK 后端会据此关闭
            # enable_html5_local_storage。该设置在 WebKitGTK 2.40+ 已废弃
            # （无效），但 2.38 等旧版上真实生效——localStorage 变成 null，
            # 前端启动即 TypeError 崩溃，窗口白屏（UOS/deepin 实测）。
            # Linux 上显式关闭 private_mode 并把存储目录放到数据目录下。
            _start_kwargs['private_mode'] = False
            _storage = os.path.join(
                os.environ.get('OPEN_AGC_DATA_DIR')
                or os.path.join(os.path.expanduser('~'), '.open-agc'),
                'webview_storage')
            os.makedirs(_storage, exist_ok=True)
            _start_kwargs['storage_path'] = _storage
        webview.start(**_start_kwargs)
        return True
    except Exception as _gui_err:
        return _browser_fallback(_gui_err)


def main():
    # Attempt to find a free port instead of hardcoding 8765
    try:
        port = int(os.environ.get("PORT", find_free_port()))
    except:
        port = 8765

    # Fix encoding on Windows (Chinese GBK locale) to prevent UnicodeEncodeError
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    elif sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    elif sys.stderr.encoding != "utf-8":
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Handle PyInstaller frozen mode
    if getattr(sys, 'frozen', False):
        base_dir = sys._MEIPASS
        os.chdir(base_dir)

        # Set writable data dir BEFORE calling get_base_dir()
        app_data = os.path.join(os.path.expanduser("~"), ".open-agc")
        os.environ["OPEN_AGC_DATA_DIR"] = app_data

        # Copy initial data/skills from bundle to writable dir if not exist
        # （播种目标与 get_data_dir() 对齐：data/* → <data>/，skills/* → <data>/skills/）
        from core.paths import seed_frozen_data
        seed_frozen_data(base_dir)

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
