# -*- coding: utf-8 -*-
"""gui_app 启动时序测试：浏览器回退必须等服务就绪，否则 localhost 拒绝连接。"""
import http.server
import threading

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


def test_crash_log_path_is_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    p = gui_app._crash_log_path()
    assert p.startswith(str(tmp_path))
    # 实际可写
    with open(p, "a", encoding="utf-8") as f:
        f.write("test\n")
