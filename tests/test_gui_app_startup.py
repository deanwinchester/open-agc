# -*- coding: utf-8 -*-
"""gui_app 启动时序测试：浏览器回退必须等服务就绪，否则 localhost 拒绝连接。"""
import http.server
import threading
import time

import gui_app


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_wait_for_server_ready_true_when_up():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert gui_app.wait_for_server_ready(port, timeout=5) is True
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_server_ready_false_on_timeout():
    # 用一个未监听的端口（保留但快速超时）
    assert gui_app.wait_for_server_ready(59999, timeout=1) is False


def test_wait_returns_immediately_after_server_crash(monkeypatch):
    failure = threading.Event()
    failure.set()
    monkeypatch.setattr(gui_app, "_server_failure", failure)
    started = time.monotonic()
    assert gui_app.wait_for_server_ready(59999, timeout=60) is False
    # 首次调用可能在检查事件前导入 requests；冷导入也必须在 1 秒内返回。
    assert time.monotonic() - started < 1.0


def test_window_shows_crash_reason_immediately(monkeypatch):
    class _Window:
        def __init__(self):
            self.scripts = []

        def evaluate_js(self, script):
            self.scripts.append(script)

    failure = threading.Event()
    failure.set()
    monkeypatch.setattr(gui_app, "_server_failure", failure)
    monkeypatch.setattr(gui_app, "_server_failure_message", "No module named 'psutil'")
    monkeypatch.setattr(gui_app, "_crash_log_hint", lambda: "/tmp/server_crash.log")
    window = _Window()
    started = time.monotonic()
    gui_app.check_server_and_load(window, 59999)
    assert time.monotonic() - started < 1.0
    rendered = "\n".join(window.scripts)
    assert "启动失败" in rendered
    assert "No module named 'psutil'" in rendered
    assert "/tmp/server_crash.log" in rendered


def test_crash_log_path_is_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    p = gui_app._crash_log_path()
    assert p.startswith(str(tmp_path))
    # 实际可写
    with open(p, "a", encoding="utf-8") as f:
        f.write("test\n")
