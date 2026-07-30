"""
sudo password popup chain tests.

Covers:
- sudo -n failure output ("a password is required" etc.) -> ShellTool raises
  SandboxBlocked(category='sudo') so the agent's auth flow pops the password
  dialog (previously only a text hint was returned and nothing triggered the
  popup, sending the LLM into a retry loop).
- Session-level shared store (api.state._session_sudo_passwords /
  _session_permission_whitelists): readable across "instances" — fresh agent
  hydration and the shell point-of-use fallback by session_id.
- Sudo env scrub: Popen for sudo commands gets SUDO_ASKPASS=/bin/false and no
  SSH_ASKPASS (prevents invisible GUI askpass hangs on desktop Linux); non-sudo
  commands inherit the user env plus a PYTHONIOENCODING=utf-8 default
  (log-encoding fix; PYTHONUTF8 deliberately NOT injected to avoid changing
  the default encoding of bare open()), without scrubbing.

All tests stub subprocess.Popen — no real sudo, no network, no DB.
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tools.shell as shell_mod  # noqa: E402
from tools.base import SandboxBlocked  # noqa: E402
from tools.shell import ShellTool  # noqa: E402

TEST_SID = 987001


@pytest.fixture(autouse=True)
def _clean_shared_store():
    from api.state import _session_sudo_passwords, _session_permission_whitelists
    _session_sudo_passwords.clear()
    _session_permission_whitelists.clear()
    yield
    _session_sudo_passwords.clear()
    _session_permission_whitelists.clear()


class _FakeStdin:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


def _make_fake_popen(captured: dict, output: bytes = b"", returncode: int = 0):
    """Build a subprocess.Popen replacement that records its call args and
    writes `output` into the stdout file object ShellTool passes in."""
    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["command"] = cmd
            captured["popen_kwargs"] = kwargs
            self.pid = 987654
            self.returncode = returncode
            self.stdin = _FakeStdin() if kwargs.get("stdin") is not None else None
            if self.stdin is not None:
                captured["stdin"] = self.stdin
            out = kwargs.get("stdout")
            if out is not None and output:
                out.write(output)
                out.flush()

        def poll(self):
            return self.returncode

        def kill(self):
            pass

    return _FakePopen


def _stub_shell(monkeypatch, tmp_path, captured, output=b"", returncode=0):
    monkeypatch.setattr(shell_mod, "_get_shell_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(shell_mod.subprocess, "Popen",
                        _make_fake_popen(captured, output=output, returncode=returncode))


# ── sudo -n failure -> SandboxBlocked(category='sudo') ──

def test_sudo_n_failure_raises_sandbox_blocked(monkeypatch, tmp_path):
    captured = {}
    _stub_shell(monkeypatch, tmp_path, captured,
                output=b"sudo: a password is required\n", returncode=1)
    tool = ShellTool()
    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(command="sudo apt-get update", timeout=5,
                     _permission_whitelist={"sudo"}, _session_id=TEST_SID)
    sb = exc_info.value
    assert sb.category == "sudo"
    assert sb.sandbox_dir == "permission"
    assert sb.tool_name == "execute_shell"
    assert sb.description == "需要 sudo 密码"
    # The command was rewritten to non-interactive sudo before execution
    assert "sudo -n " in captured["command"]


def test_sudo_n_success_does_not_raise(monkeypatch, tmp_path):
    captured = {}
    _stub_shell(monkeypatch, tmp_path, captured, output=b"ok\n", returncode=0)
    tool = ShellTool()
    result = tool.execute(command="sudo whoami", timeout=5,
                          _permission_whitelist={"sudo"}, _session_id=TEST_SID)
    assert "Exit Code: 0" in result


# ── Session-level shared store readable across "instances" ──

def test_shared_store_read_across_instances(monkeypatch, tmp_path):
    from api.state import _session_sudo_passwords, _session_permission_whitelists
    from agent.agent import OpenAGCAgent

    _session_sudo_passwords[TEST_SID] = "s3cret-pass"
    _session_permission_whitelists.setdefault(TEST_SID, set()).add("sudo")

    # (a) Agent hydration: a fresh instance reads the shared store.
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = TEST_SID
    agent._session_permission_whitelist = set()
    agent._session_sudo_password = ""
    agent._hydrate_session_shared()
    assert agent._session_sudo_password == "s3cret-pass"
    assert "sudo" in agent._session_permission_whitelist
    # Same set object: later additions propagate to the shared store.
    assert agent._session_permission_whitelist is _session_permission_whitelists[TEST_SID]

    # (b) Shell point-of-use: no _sudo_password / _permission_whitelist kwargs,
    # yet the shared cache is picked up via _session_id (sudo -S + stdin feed).
    captured = {}
    _stub_shell(monkeypatch, tmp_path, captured, output=b"ok\n", returncode=0)
    tool = ShellTool()
    result = tool.execute(command="sudo apt-get update", timeout=5,
                          _session_id=TEST_SID)
    assert "sudo -S " in captured["command"]
    assert captured["stdin"].written == b"s3cret-pass\n"
    assert "Exit Code: 0" in result


def test_sync_permission_shared_writes_store():
    from api.state import _session_sudo_passwords, _session_permission_whitelists
    from agent.agent import OpenAGCAgent

    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = TEST_SID
    agent._sync_permission_shared("sudo", "pw-xyz")
    assert _session_sudo_passwords[TEST_SID] == "pw-xyz"
    assert "sudo" in _session_permission_whitelists[TEST_SID]

    # _get_shared_sudo_password returns the shared value and refreshes the cache
    agent._session_sudo_password = ""
    assert agent._get_shared_sudo_password() == "pw-xyz"
    assert agent._session_sudo_password == "pw-xyz"


# ── Sudo env scrub ──

def test_sudo_env_scrub(monkeypatch, tmp_path):
    from api.state import _session_sudo_passwords
    _session_sudo_passwords[TEST_SID] = "pw"
    monkeypatch.setenv("SUDO_ASKPASS", "/usr/bin/gui-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/ssh-askpass")
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    captured = {}
    _stub_shell(monkeypatch, tmp_path, captured, output=b"ok\n", returncode=0)
    tool = ShellTool()
    tool.execute(command="sudo whoami", timeout=5,
                 _permission_whitelist={"sudo"}, _session_id=TEST_SID)
    env = captured["popen_kwargs"].get("env")
    assert env is not None, "sudo command must get a scrubbed env"
    assert env.get("SUDO_ASKPASS") == "/bin/false"  # original GUI value gone
    assert "SSH_ASKPASS" not in env

    # Non-sudo commands: env is the user environment plus PYTHONIOENCODING
    # default (log-encoding fix) — no scrubbing, SUDO_ASKPASS untouched.
    # PYTHONUTF8 不注入：避免改变裸 open() 默认编码（第三方脚本读写既有
    # GBK 文件会出新乱码）。
    captured2 = {}
    _stub_shell(monkeypatch, tmp_path, captured2, output=b"ok\n", returncode=0)
    tool.execute(command="echo hello", timeout=5)
    env2 = captured2["popen_kwargs"].get("env")
    assert env2 is not None
    assert "PYTHONUTF8" not in env2
    assert env2["PYTHONIOENCODING"] == "utf-8"
    assert env2.get("SUDO_ASKPASS") == "/usr/bin/gui-askpass"  # inherited, not scrubbed
