# -*- coding: utf-8 -*-
"""日志编码修复测试（中文 Windows cp936/GBK locale）：

- _decode_mixed：纯 UTF-8、纯 GBK、同文件逐行混合、非法字节保底
- _LineBuffer：轮询增量读取跨轮切半的多字节字符不乱码；\r 进度逐轮产出；
  收尾完整行不丢；无 \n/\r 巨量单行超限按现状发出
- get_task_logs：GBK / UTF-8 / 混合日志文件均返回正确文本（函数级直调）
- execute_shell env 注入：popen_kwargs env 含 PYTHONIOENCODING=utf-8 且
  setdefault 不覆盖用户显式值；不注入 PYTHONUTF8（避免改变裸 open()
  默认编码导致第三方脚本读写既有 GBK 文件出新乱码）
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tools.shell as sh
from tools.shell import _LineBuffer, _decode_mixed, _python_utf8_env


@pytest.fixture()
def cp936_locale(monkeypatch):
    """固定区域回退编码为 cp936，使 GBK 用例在任何 locale 下确定性。"""
    monkeypatch.setattr("locale.getpreferredencoding", lambda: "cp936")


# ── _decode_mixed ─────────────────────────────────────────────

class TestDecodeMixed:
    def test_pure_utf8(self):
        raw = "你好，世界\nhello ascii\n第二行 ✓\n".encode("utf-8")
        assert _decode_mixed(raw) == "你好，世界\nhello ascii\n第二行 ✓\n"

    def test_pure_gbk(self, cp936_locale):
        text = "你好，世界\n第二行 GBK\n"
        raw = text.encode("gbk")
        assert _decode_mixed(raw) == text

    def test_mixed_per_line(self, cp936_locale):
        """同一文件逐行混杂 UTF-8/GBK：两行都必须正确，整块二选一必乱一半。"""
        utf8_line = "python 输出的 UTF-8 中文行\n"
        gbk_line = "cmd 内建的 GBK 中文行\n"
        raw = utf8_line.encode("utf-8") + gbk_line.encode("gbk")
        out = _decode_mixed(raw)
        assert out == utf8_line + gbk_line
        assert "\ufffd" not in out

    def test_gbk_then_utf8_order(self, cp936_locale):
        raw = ("GBK 在前\n".encode("gbk")
               + "UTF-8 在后\n".encode("utf-8")
               + "ascii line\n".encode("ascii"))
        out = _decode_mixed(raw)
        assert out == "GBK 在前\nUTF-8 在后\nascii line\n"

    def test_invalid_bytes_fallback_no_raise(self, cp936_locale):
        """二进制垃圾行：两种编码都失败率高时保底 replace，不抛异常。"""
        raw = b"ok line\n\xff\xfe\x01\x02 binary \x80\x81\n"
        out = _decode_mixed(raw)
        assert isinstance(out, str)
        assert "ok line\n" in out

    def test_empty(self):
        assert _decode_mixed(b"") == ""

    def test_crlf_preserved(self):
        raw = "行一\r\n行二\r\n".encode("utf-8")
        assert _decode_mixed(raw) == "行一\r\n行二\r\n"


# ── _LineBuffer（轮询行缓冲） ──────────────────────────────────

class TestLineBuffer:
    def test_utf8_char_split_across_feeds(self):
        """模拟两轮喂字节：UTF-8 多字节字符跨轮切半，不得乱码。"""
        buf = _LineBuffer()
        data = "中文输出 ok\n".encode("utf-8")
        # 「中」是 3 字节，切在第 2 字节后
        assert buf.feed(data[:2]) == ""
        assert buf.feed(data[2:]) == "中文输出 ok\n"
        assert buf.flush() == ""

    def test_complete_line_emitted_partial_held(self):
        """最后一个 \n 之前的完整行立即输出，其后的半行字节留到下一轮。"""
        buf = _LineBuffer()
        head = "第一行\n".encode("utf-8")          # 10 bytes
        tail_partial = "第二行\n".encode("utf-8")[:4]  # 「第」3字节 + 「二」第1字节
        assert buf.feed(head + tail_partial) == "第一行\n"
        rest = "第二行\n".encode("utf-8")[4:]
        assert buf.feed(rest) == "第二行\n"

    def test_gbk_char_split_across_feeds(self, cp936_locale):
        buf = _LineBuffer()
        data = "gbk行\n".encode("gbk")  # b'gbk\xd0\xd0\n'，「行」2 字节被切开
        assert buf.feed(data[:4]) == ""   # b'gbk\xd0'，无 \n 且无完整行
        assert buf.feed(data[4:]) == "gbk行\n"

    def test_mixed_encoding_across_feeds(self, cp936_locale):
        """UTF-8 行 + GBK 行跨轮混合：按行各自正确解码。"""
        buf = _LineBuffer()
        gbk_line = "gbk 中文行\n".encode("gbk")
        part1 = "utf8 中文行\n".encode("utf-8") + gbk_line[:5]
        assert buf.feed(part1) == "utf8 中文行\n"
        assert buf.feed(gbk_line[5:]) == "gbk 中文行\n"

    def test_flush_remainder_without_newline(self):
        """进程结束时冲刷无 \n 结尾的残余半行。"""
        buf = _LineBuffer()
        assert buf.feed("有换行\n".encode("utf-8")) == "有换行\n"
        assert buf.feed("尾部半行".encode("utf-8")) == ""
        assert buf.flush() == "尾部半行"
        assert buf.flush() == ""

    def test_cr_progress_emitted_per_feed(self):
        """tqdm 类纯 \r 刷新：每轮 feed 都产出文本，\r 原样保留给前端模拟进度。"""
        buf = _LineBuffer()
        assert buf.feed(b"10%") == ""            # 无 \n/\r，留存
        assert buf.feed(b"\r20%") == "10%\r"
        assert buf.feed(b"\r30%") == "20%\r"
        assert buf.feed(b"\r100%\n") == "30%\r100%\n"
        assert buf.flush() == ""

    def test_cr_cut_never_splits_multibyte(self):
        """\r 切割点不会切断 UTF-8 多字节字符（跨轮喂字节）。"""
        buf = _LineBuffer()
        data = "进度 50%\r完成\n".encode("utf-8")
        # 切在「度」（UTF-8 3 字节）中间：先喂「进」+「度」的前 2 字节
        assert buf.feed(data[:5]) == ""
        assert buf.feed(data[5:]) == "进度 50%\r完成\n"

    def test_cr_and_nl_cut_at_later_position(self):
        """同缓冲内 \r 与 \n 并存时切到两者中靠后的位置。"""
        buf = _LineBuffer()
        assert buf.feed("a\rb\nc\r".encode("utf-8")) == "a\rb\nc\r"
        assert buf.flush() == ""

    def test_final_flush_keeps_complete_lines(self):
        """收尾路径（feed 返回值 + flush）：完整行与残余半行都不可丢。"""
        buf = _LineBuffer()
        final = "完整行一\n完整行二\n残余半行".encode("utf-8")
        text = buf.feed(final) + buf.flush()
        assert "完整行一\n" in text
        assert "完整行二\n" in text
        assert text.endswith("残余半行")

    def test_pending_cap_flushes_oversize(self):
        """无 \n/\r 巨量单行：超过 _MAX_PENDING 按现状发出，缓冲清空有界。"""
        buf = _LineBuffer()
        big = b"x" * (buf._MAX_PENDING + 100)
        out = buf.feed(big)
        assert len(out) == len(big)
        assert buf.flush() == ""


# ── get_task_logs 端点（函数级直调） ───────────────────────────

def _make_tasks_db(tmp_path, task_id, output_files):
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, output_files TEXT)")
    conn.execute("INSERT INTO tasks (id, output_files) VALUES (?, ?)",
                 (task_id, json.dumps(output_files)))
    conn.commit()
    conn.close()
    return str(db_path)


def _fetch_logs(task_id=1, lines=50):
    from api.routes.routes_tasks import get_task_logs
    return asyncio.run(get_task_logs(task_id, lines))


class TestGetTaskLogs:
    def _setup(self, tmp_path, monkeypatch, raw: bytes, task_id=1):
        from api.routes import routes_tasks as rt
        log_file = tmp_path / "proc.log"
        log_file.write_bytes(raw)
        monkeypatch.setattr(rt, "DB_PATH",
                            _make_tasks_db(tmp_path, task_id, [str(log_file)]))
        return str(log_file)

    def test_utf8_file(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch,
                    "UTF-8 中文行\nplain ascii\n".encode("utf-8"))
        result = _fetch_logs()
        assert "UTF-8 中文行" in result["logs"]
        assert "\ufffd" not in result["logs"]
        assert len(result["lines"]) == 2

    def test_gbk_file(self, tmp_path, monkeypatch, cp936_locale):
        """纯 GBK 日志：旧实现 utf-8+replace 会全变 �。"""
        self._setup(tmp_path, monkeypatch,
                    "GBK 中文行\n第二行\n".encode("gbk"))
        result = _fetch_logs()
        assert "GBK 中文行" in result["logs"]
        assert "第二行" in result["logs"]
        assert "\ufffd" not in result["logs"]

    def test_mixed_file(self, tmp_path, monkeypatch, cp936_locale):
        raw = ("python UTF-8 中文行\n".encode("utf-8")
               + "cmd GBK 中文行\n".encode("gbk"))
        self._setup(tmp_path, monkeypatch, raw)
        result = _fetch_logs()
        assert "python UTF-8 中文行" in result["logs"]
        assert "cmd GBK 中文行" in result["logs"]
        assert "\ufffd" not in result["logs"]

    def test_line_count_cut_after_decode(self, tmp_path, monkeypatch, cp936_locale):
        raw = b"".join("第%d行 GBK\n".encode("gbk") % i for i in range(5))
        self._setup(tmp_path, monkeypatch, raw)
        result = _fetch_logs(lines=2)
        assert len(result["lines"]) == 2
        assert "第3行" in result["logs"] and "第4行" in result["logs"]
        assert "第0行" not in result["logs"]

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        from api.routes import routes_tasks as rt
        monkeypatch.setattr(rt, "DB_PATH",
                            _make_tasks_db(tmp_path, 1, [str(tmp_path / "nope.log")]))
        assert _fetch_logs() == {"logs": "", "lines": []}


# ── execute_shell env 注入 ─────────────────────────────────────

class _FakeProc:
    pid = 43210
    returncode = 0
    stdin = None

    def poll(self):
        return 0  # 进程已结束，主循环直接跳过


def _run_execute_with_captured_popen(monkeypatch, tmp_path):
    """打桩 Popen 执行一条 echo，返回 Popen 实际收到的 kwargs。"""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    out_dir = tmp_path / "shell_output"
    out_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(sh, "SHELL_OUTPUT_DIR", str(out_dir))
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sh.subprocess, "Popen", fake_popen)
    import tools.permissions as perms
    monkeypatch.setattr(perms, "check_command_permission",
                        lambda *a, **k: (True, "", "", ""))
    monkeypatch.setattr(perms, "extract_urls_from_command", lambda cmd: [])

    result = sh.ShellTool().execute(command="echo hello", timeout=5)
    assert "Exit Code: 0" in result
    return captured


class TestPythonUtf8Env:
    def test_helper_sets_defaults(self):
        env = {}
        _python_utf8_env(env)
        assert "PYTHONUTF8" not in env  # 不注入：避免改变裸 open() 默认编码
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_helper_keeps_explicit_values(self):
        env = {"PYTHONIOENCODING": "gbk", "OTHER": "x"}
        _python_utf8_env(env)
        assert env["PYTHONIOENCODING"] == "gbk"
        assert env["OTHER"] == "x"

    def test_execute_shell_popen_env_has_utf8(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        captured = _run_execute_with_captured_popen(monkeypatch, tmp_path)
        env = captured.get("env")
        assert env is not None, "popen_kwargs 必须显式携带 env"
        assert "PYTHONUTF8" not in env  # 只规范 stdio，不碰 open() 默认编码
        assert env["PYTHONIOENCODING"] == "utf-8"

    def test_execute_shell_respects_user_explicit_env(self, monkeypatch, tmp_path):
        """用户环境已显式设置时不覆盖。"""
        monkeypatch.setenv("PYTHONUTF8", "0")
        monkeypatch.setenv("PYTHONIOENCODING", "gbk")
        captured = _run_execute_with_captured_popen(monkeypatch, tmp_path)
        env = captured["env"]
        assert env["PYTHONUTF8"] == "0"  # 用户值经 os.environ 继承，不被改动
        assert env["PYTHONIOENCODING"] == "gbk"


def test_execute_shell_final_flush_keeps_all_output(monkeypatch, tmp_path):
    """进程结束时最后一轮输出（完整行 + 无 \n 残余）全部经 progress 事件发出。"""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    out_dir = tmp_path / "shell_output"
    out_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(sh, "SHELL_OUTPUT_DIR", str(out_dir))
    events = []

    class _WriterProc:
        pid = 43211
        returncode = 0
        stdin = None

        def __init__(self, out_path):
            self._out_path = out_path
            self._calls = 0

        def poll(self):
            self._calls += 1
            if self._calls == 1:
                # 运行期间无输出；结束前一次性写入（完整行 + 无 \n 残余）
                with open(self._out_path, "ab") as f:
                    f.write("收尾完整行\n收尾残余".encode("utf-8"))
                return None
            return 0

    def fake_popen(cmd, **kwargs):
        return _WriterProc(kwargs["stdout"].name)

    monkeypatch.setattr(sh.subprocess, "Popen", fake_popen)
    import tools.permissions as perms
    monkeypatch.setattr(perms, "check_command_permission",
                        lambda *a, **k: (True, "", "", ""))
    monkeypatch.setattr(perms, "extract_urls_from_command", lambda cmd: [])

    result = sh.ShellTool().execute(
        command="echo hello", timeout=5,
        _progress_cb=lambda ev: events.append(ev))
    assert "Exit Code: 0" in result
    all_progress = "".join(ev["text"] for ev in events)
    assert "收尾完整行" in all_progress
    assert "收尾残余" in all_progress


# ── _read_tail / _read_masked_output_tail 混合解码 ─────────────

class TestTailReaders:
    def test_read_tail_mixed(self, tmp_path, cp936_locale):
        f = tmp_path / "tail.log"
        f.write_bytes("UTF-8 行\n".encode("utf-8") + "GBK 行\n".encode("gbk"))
        out = sh._read_tail(str(f), 3000)
        assert "UTF-8 行" in out and "GBK 行" in out
        assert "\ufffd" not in out

    def test_read_masked_output_tail_gbk(self, tmp_path, monkeypatch, cp936_locale):
        monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
        from api.background import _read_masked_output_tail
        f = tmp_path / "bg.log"
        f.write_bytes("GBK 后台输出\n".encode("gbk"))
        out = _read_masked_output_tail(str(f), 5000)
        assert "GBK 后台输出" in out
        assert "\ufffd" not in out
