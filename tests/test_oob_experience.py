# -*- coding: utf-8 -*-
"""新机开箱即用（OOB）系列修复的回归测试（含评审修复轮）：

1. frozen 播种：core.paths.seed_frozen_data 为唯一实现，真入口 gui_app.py 与
   launcher.py 共用；目标与 get_data_dir() 对齐（不得出现 data/data 嵌套）
2. bundle 必须带 data/config.json（open_agc.spec ← build_data/config.json）；
   源码/frozen 两种播种来源
3. start.bat 便携 Python 不走 venv、errorlevel 块内延迟扩展、端口回退字面 IP
4. 自动升级按部署形态（desktop/docker/source）分派；robocopy 重试与退出码；
   zip 平铺/嵌套布局校验
"""
import asyncio
import io
import json
import os
import re
import sys
import zipfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── 1. frozen 数据播种（真入口 gui_app / 共用 core.paths.seed_frozen_data）──


def _make_frozen_bundle(tmp_path):
    """构造与 open_agc.spec 真实产出一致的模拟 bundle：
    _MEIPASS/data/config.json（build_data 无密钥模板内容）
    + _MEIPASS/skills/（含 user_generated 子目录）。"""
    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    with open(os.path.join(PROJECT_ROOT, "build_data", "config.json"), encoding="utf-8") as f:
        (bundle / "data" / "config.json").write_text(f.read(), encoding="utf-8")
    (bundle / "skills" / "user_generated").mkdir(parents=True)
    (bundle / "skills" / "example_skill.md").write_text("# demo", encoding="utf-8")
    (bundle / "skills" / "user_generated" / "my.md").write_text("# u", encoding="utf-8")
    return bundle


def _patch_frozen_env(monkeypatch, tmp_path, bundle):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    # 注意：必须用 setitem 而非 delenv——delenv 在变量原本不存在时不登记 undo，
    # 被测代码内 os.environ[...] = ... 会泄漏到后续用例
    monkeypatch.setitem(os.environ, "OPEN_AGC_DATA_DIR", str(home / ".open-agc"))
    # 避免切换真实 cwd 影响其他用例
    monkeypatch.setattr(os, "chdir", lambda p: None)
    return home


class TestSeedFrozenData:
    """core.paths.seed_frozen_data：唯一播种实现（gui_app/launcher 共用）。"""

    def test_seeds_into_data_dir(self, tmp_path, monkeypatch):
        from core.paths import seed_frozen_data, get_data_path

        bundle = _make_frozen_bundle(tmp_path)
        home = tmp_path / "home"
        monkeypatch.setitem(os.environ, "OPEN_AGC_DATA_DIR", str(home / ".open-agc"))

        seed_frozen_data(str(bundle))

        seeded = get_data_path("config.json")
        assert seeded == str(home / ".open-agc" / "data" / "config.json")
        with open(seeded, encoding="utf-8") as f:
            seeded_cfg = json.load(f)
        with open(os.path.join(PROJECT_ROOT, "build_data", "config.json"), encoding="utf-8") as f:
            assert seeded_cfg == json.load(f)
        assert (home / ".open-agc" / "data" / "skills" / "example_skill.md").exists()
        # 子目录递归复制
        assert (home / ".open-agc" / "data" / "skills" / "user_generated" / "my.md").exists()
        # C1 回归：不得出现 data/data 嵌套错位
        assert not (home / ".open-agc" / "data" / "data").exists()

    def test_no_overwrite_existing(self, tmp_path, monkeypatch):
        from core.paths import seed_frozen_data

        bundle = _make_frozen_bundle(tmp_path)
        home = tmp_path / "home"
        monkeypatch.setitem(os.environ, "OPEN_AGC_DATA_DIR", str(home / ".open-agc"))
        user_cfg = home / ".open-agc" / "data" / "config.json"
        user_cfg.parent.mkdir(parents=True)
        user_cfg.write_text('{"default_model": "user/custom"}', encoding="utf-8")

        seed_frozen_data(str(bundle))

        assert user_cfg.read_text(encoding="utf-8") == '{"default_model": "user/custom"}'


def test_gui_app_frozen_entry_seeds_data(tmp_path, monkeypatch):
    """打包真实入口 gui_app.main() 的 frozen 播种（C1：两个 spec 入口均为 gui_app.py）。"""
    import gui_app

    bundle = _make_frozen_bundle(tmp_path)
    home = _patch_frozen_env(monkeypatch, tmp_path, bundle)
    # 跳过端口探测/服务线程/窗口创建，只跑到播种逻辑
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setattr(gui_app, "start_server", lambda port: None)
    monkeypatch.setattr(gui_app, "create_window", lambda port: True)

    gui_app.main()

    assert (home / ".open-agc" / "data" / "config.json").exists()
    assert (home / ".open-agc" / "data" / "skills" / "example_skill.md").exists()
    assert not (home / ".open-agc" / "data" / "data").exists()


def test_launcher_frozen_seeds_into_data_dir(tmp_path, monkeypatch):
    """launcher 保留入口与真入口共用同一播种实现。"""
    import launcher

    bundle = _make_frozen_bundle(tmp_path)
    home = _patch_frozen_env(monkeypatch, tmp_path, bundle)

    launcher.setup_environment()

    from core.paths import get_data_path

    seeded = get_data_path("config.json")
    assert seeded == str(home / ".open-agc" / "data" / "config.json")
    assert os.path.exists(seeded)
    assert (home / ".open-agc" / "data" / "skills" / "example_skill.md").exists()
    assert not (home / ".open-agc" / "data" / "data").exists()


def test_launcher_frozen_keeps_existing_user_config(tmp_path, monkeypatch):
    """不覆盖语义：用户已有配置优先。"""
    import launcher

    bundle = _make_frozen_bundle(tmp_path)
    home = _patch_frozen_env(monkeypatch, tmp_path, bundle)
    user_cfg = home / ".open-agc" / "data" / "config.json"
    user_cfg.parent.mkdir(parents=True)
    user_cfg.write_text('{"default_model": "user/custom"}', encoding="utf-8")

    launcher.setup_environment()

    assert user_cfg.read_text(encoding="utf-8") == '{"default_model": "user/custom"}'


def test_open_agc_spec_bundles_config_json():
    """C2：CI 用 open_agc.spec 必须把无密钥默认配置打进 bundle 的 data/config.json。"""
    with open(os.path.join(PROJECT_ROOT, "open_agc.spec"), encoding="utf-8") as f:
        content = f.read()
    assert "('build_data/config.json', 'data')" in content
    # 两个 spec 的入口都是 gui_app.py —— 播种修复必须落在真入口上（C1）
    assert "['gui_app.py']" in content


# ── 2. 源码/frozen 首启播种默认配置 ──


def test_seed_default_config_from_template(tmp_path, monkeypatch):
    """CONFIG_PATH 为默认路径且文件缺失时，从模板播种并读出相同内容。"""
    import api.config as api_config

    target = tmp_path / "data" / "config.json"
    template = tmp_path / "build_data" / "config.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"default_model": "seed/model"}', encoding="utf-8")

    monkeypatch.setattr(api_config, "CONFIG_PATH", str(target))
    # get_data_path 同步指向临时目录 → 视为真实默认路径
    monkeypatch.setattr(api_config, "get_data_path", lambda name: str(target.parent / name))
    monkeypatch.setattr(api_config, "_TEMPLATE_CONFIG", str(template))

    cfg = api_config.load_config()
    assert cfg == {"default_model": "seed/model"}
    assert target.read_text(encoding="utf-8") == template.read_text(encoding="utf-8")


def test_seed_default_config_frozen_bundle(tmp_path, monkeypatch):
    """C2：frozen 下播种来源为 bundle 内 sys._MEIPASS/data/config.json。"""
    import api.config as api_config

    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    (bundle / "data" / "config.json").write_text(
        '{"default_model": "frozen/model"}', encoding="utf-8"
    )
    target = tmp_path / "appdata" / "data" / "config.json"
    monkeypatch.setattr(api_config, "CONFIG_PATH", str(target))
    monkeypatch.setattr(api_config, "get_data_path", lambda name: str(target.parent / name))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    # _TEMPLATE_CONFIG 指向不存在的路径，证明走的是 bundle 而非仓库模板
    monkeypatch.setattr(api_config, "_TEMPLATE_CONFIG", str(tmp_path / "nonexistent.json"))

    cfg = api_config.load_config()
    assert cfg == {"default_model": "frozen/model"}


def test_no_seed_when_config_path_redirected(tmp_path, monkeypatch):
    """CONFIG_PATH 被 monkeypatch 到临时路径（非默认路径）时不播种。"""
    import api.config as api_config

    target = tmp_path / "elsewhere" / "config.json"
    template = tmp_path / "build_data" / "config.json"
    template.parent.mkdir(parents=True)
    template.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(api_config, "CONFIG_PATH", str(target))
    monkeypatch.setattr(api_config, "_TEMPLATE_CONFIG", str(template))

    assert api_config.load_config() == {}
    assert not target.exists()


# ── 3. start.bat 便携 Python 健壮性（源码断言）──


def _read_start_bat():
    with open(os.path.join(PROJECT_ROOT, "start.bat"), encoding="utf-8") as f:
        return f.read()


def test_start_bat_no_venv_from_portable_python():
    content = _read_start_bat()
    # 不得用便携解释器（%PYTHON% 可能指向 .python\python.exe）创建 venv
    assert not re.search(r"%PYTHON%\s+-m\s+venv", content)
    assert not re.search(r"\.python\\python\.exe[^\r\n]*-m\s+venv", content)


def test_start_bat_has_portable_uvicorn_branch():
    content = _read_start_bat()
    assert r".python\python.exe -m uvicorn" in content
    # 下载失败提示包含手动下载与代理指引
    assert "extract it into .python" in content
    assert "HTTPS_PROXY" in content


def test_start_bat_no_percent_errorlevel_inside_paren_blocks():
    """I1：cmd 括号块内 %errorlevel% 在解析期一次性展开（恒为进块前的值），
    块内必须改用 if errorlevel N / if not errorlevel 1 运行时语法。"""
    depth = 0
    offenders = []
    for i, line in enumerate(_read_start_bat().splitlines(), 1):
        stripped = line.strip()
        if depth > 0 and "%errorlevel%" in stripped:
            offenders.append(f"line {i}: {stripped}")
        # 关闭行（")" / ") else ("）先减；行尾 "(" 开块后加
        if stripped.startswith(")"):
            depth -= 1
        if stripped.endswith("("):
            depth += 1
    assert not offenders, "括号块内使用了 %errorlevel%: " + "; ".join(offenders)


def test_start_bat_port_fallback_binds_literal_ip():
    """Minor：chr(39)*2 是两个引号字符而非空字符串，bind 必抛 gaierror。"""
    content = _read_start_bat()
    assert "chr(39)" not in content
    assert "('127.0.0.1',0)" in content


# ── 4. 自动升级按部署形态分派 ──


class _FakeResp:
    def __init__(self, payload: bytes, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]


class TestChannelDetection:
    def test_desktop_when_frozen(self, monkeypatch):
        import core.auto_upgrade as auto_upgrade

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert auto_upgrade.get_channel() == "desktop"

    def test_docker_via_env_var(self, monkeypatch):
        import core.auto_upgrade as auto_upgrade

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv("OPEN_AGC_DOCKER", "1")
        assert auto_upgrade.get_channel() == "docker"

    def test_docker_via_dockerenv_file(self, monkeypatch):
        import core.auto_upgrade as auto_upgrade

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delenv("OPEN_AGC_DOCKER", raising=False)
        orig_exists = os.path.exists
        monkeypatch.setattr(
            os.path, "exists",
            lambda p: True if p == "/.dockerenv" else orig_exists(p),
        )
        assert auto_upgrade.get_channel() == "docker"

    def test_source_by_default(self, monkeypatch):
        import core.auto_upgrade as auto_upgrade

        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delenv("OPEN_AGC_DOCKER", raising=False)
        # 显式 stub /.dockerenv 不存在，避免依赖宿主机环境（Linux/容器 CI）
        orig_exists = os.path.exists
        monkeypatch.setattr(
            os.path, "exists",
            lambda p: False if p == "/.dockerenv" else orig_exists(p),
        )
        assert auto_upgrade.get_channel() == "source"


def _make_zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestDesktopAssetSelection:
    def test_asset_name_windows(self, monkeypatch):
        from core.auto_upgrade import AutoUpgrader

        monkeypatch.setattr(sys, "platform", "win32")
        assert AutoUpgrader._desktop_asset_name("1.2.3") == "Open-AGC-1.2.3-Windows-x64.zip"

    def test_asset_name_macos(self, monkeypatch):
        from core.auto_upgrade import AutoUpgrader

        monkeypatch.setattr(sys, "platform", "darwin")
        assert AutoUpgrader._desktop_asset_name("1.2.3") == "Open-AGC-1.2.3-macOS-arm64.dmg"

    def _make_upgrader(self, monkeypatch, channel="desktop"):
        import core.auto_upgrade as auto_upgrade

        monkeypatch.setattr(auto_upgrade, "get_channel", lambda: channel)
        upgrader = auto_upgrade.AutoUpgrader()
        upgrader.current_version = "0.0.1"
        upgrader.latest_version = "9.9.9"
        return upgrader

    def _prepare_staging_env(self, monkeypatch, tmp_path, upgrader, zip_bytes):
        asset_name = "Open-AGC-9.9.9-Windows-x64.zip"
        upgrader.latest_assets = [
            {"name": asset_name, "browser_download_url": "http://example/x.zip"}
        ]
        monkeypatch.setattr(
            "core.auto_upgrade.requests.get",
            lambda *a, **kw: _FakeResp(zip_bytes),
        )
        exe_dir = tmp_path / "Open-AGC"
        exe_dir.mkdir()
        monkeypatch.setattr(sys, "executable", str(exe_dir / "Open-AGC.exe"))
        launched = []
        monkeypatch.setattr(
            type(upgrader), "_launch_updater",
            lambda self, bat: launched.append(bat),
        )
        return exe_dir, launched

    def test_missing_asset_returns_false_with_message(self, monkeypatch):
        upgrader = self._make_upgrader(monkeypatch)
        upgrader.latest_assets = [{"name": "unrelated.txt", "browser_download_url": "http://x"}]
        assert upgrader._stage_windows_update() is False
        assert "Open-AGC-9.9.9-Windows-x64.zip" in upgrader.last_message

    def test_windows_staging_and_bat(self, monkeypatch, tmp_path):
        """平铺布局 zip：exe 在根。bat 含重试上限/退出码检查/失败日志。"""
        upgrader = self._make_upgrader(monkeypatch)
        zip_bytes = _make_zip_bytes({
            "Open-AGC.exe": b"MZfake",
            "VERSION": b"9.9.9\n",
        })
        exe_dir, launched = self._prepare_staging_env(
            monkeypatch, tmp_path, upgrader, zip_bytes
        )

        assert upgrader._stage_windows_update() is True

        # staging 解压到 exe 同级 update_staging/
        staging = exe_dir / "update_staging"
        assert (staging / "Open-AGC.exe").read_bytes() == b"MZfake"
        assert (staging / "VERSION").read_text().strip() == "9.9.9"

        # apply_update.bat 已生成并启动，内容包含等待退出→覆盖→重启
        assert launched and launched[0].endswith("apply_update.bat")
        bat_content = (exe_dir / "apply_update.bat").read_text(encoding="ascii")
        assert "tasklist" in bat_content
        # I2：robocopy 限制重试（默认 /R:1000000 会永久挂起）且检查退出码
        assert "/R:3 /W:2" in bat_content
        assert "set RC=%errorlevel%" in bat_content
        assert "if %RC% leq 7 goto :copy_ok" in bat_content
        # I2：失败保留 staging、写错误日志、不重启
        assert "apply_update_error.log" in bat_content
        assert "exit /b %RC%" in bat_content
        assert f'set "SRC={staging}"' in bat_content
        assert f'set "STAGING={staging}"' in bat_content
        assert str(exe_dir / "Open-AGC.exe") in bat_content
        assert 'start ""' in bat_content

        assert upgrader.restart_required is True
        assert upgrader.last_message

    def test_windows_staging_nested_layout(self, monkeypatch, tmp_path):
        """I3：嵌套布局 zip（单一顶层目录 Open-AGC/，与仓库实物一致）自动下探一层。"""
        upgrader = self._make_upgrader(monkeypatch)
        zip_bytes = _make_zip_bytes({
            "Open-AGC/Open-AGC.exe": b"MZfake",
            "Open-AGC/_internal/VERSION": b"9.9.9\n",
        })
        exe_dir, launched = self._prepare_staging_env(
            monkeypatch, tmp_path, upgrader, zip_bytes
        )

        assert upgrader._stage_windows_update() is True

        staging = exe_dir / "update_staging"
        bat_content = (exe_dir / "apply_update.bat").read_text(encoding="ascii")
        # robocopy 源必须下探到嵌套的 Open-AGC\ 目录，否则只会在 exe_dir 新建子目录
        assert f'set "SRC={staging / "Open-AGC"}"' in bat_content
        assert f'set "STAGING={staging}"' in bat_content
        assert upgrader.restart_required is True

    def test_windows_staging_missing_exe_fails(self, monkeypatch, tmp_path):
        """I3：解压后找不到 Open-AGC.exe → 报失败，不生成 bat，清理 staging。"""
        upgrader = self._make_upgrader(monkeypatch)
        zip_bytes = _make_zip_bytes({"README.txt": b"no exe here"})
        exe_dir, launched = self._prepare_staging_env(
            monkeypatch, tmp_path, upgrader, zip_bytes
        )

        assert upgrader._stage_windows_update() is False
        assert "Open-AGC.exe" in upgrader.last_message
        assert not (exe_dir / "update_staging").exists()
        assert not launched
        assert upgrader.restart_required is False

    def test_windows_staging_permission_denied_message(self, monkeypatch, tmp_path):
        """Minor：Program Files 等无写权限目录 → 如实提示管理员/换安装目录。"""
        upgrader = self._make_upgrader(monkeypatch)
        zip_bytes = _make_zip_bytes({"Open-AGC.exe": b"MZfake"})
        exe_dir, _ = self._prepare_staging_env(monkeypatch, tmp_path, upgrader, zip_bytes)

        orig_makedirs = os.makedirs

        def _deny_staging(path, *a, **kw):
            if "update_staging" in str(path):
                raise PermissionError("Access is denied")
            return orig_makedirs(path, *a, **kw)

        monkeypatch.setattr(os, "makedirs", _deny_staging)

        assert upgrader._stage_windows_update() is False
        assert "管理员" in upgrader.last_message
        assert upgrader.restart_required is False

    def test_locate_payload_layouts(self, tmp_path):
        """_locate_payload：平铺/嵌套一层支持，多顶层目录或无 exe 拒绝。"""
        from core.auto_upgrade import AutoUpgrader

        flat = tmp_path / "flat"
        flat.mkdir()
        (flat / "Open-AGC.exe").write_bytes(b"MZ")
        assert AutoUpgrader._locate_payload(str(flat)) == str(flat)

        nested = tmp_path / "nested"
        (nested / "Open-AGC").mkdir(parents=True)
        (nested / "Open-AGC" / "Open-AGC.exe").write_bytes(b"MZ")
        assert AutoUpgrader._locate_payload(str(nested)) == str(nested / "Open-AGC")

        multi = tmp_path / "multi"
        (multi / "a").mkdir(parents=True)
        (multi / "b").mkdir()
        (multi / "a" / "Open-AGC.exe").write_bytes(b"MZ")
        assert AutoUpgrader._locate_payload(str(multi)) is None

        empty = tmp_path / "empty"
        empty.mkdir()
        assert AutoUpgrader._locate_payload(str(empty)) is None

    def test_real_release_zip_layout_supported(self):
        """以仓库实物 zip 校验 _locate_payload 的布局契约（平铺或嵌套一层）。"""
        zip_path = os.path.join(PROJECT_ROOT, "dist", "Open-AGC-1.0.0-Windows-x64.zip")
        if not os.path.exists(zip_path):
            pytest.skip("dist zip 不在工作区")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        tops = {n.split("/")[0] for n in names if n}
        flat = "Open-AGC.exe" in tops
        nested = False
        if len(tops) == 1:
            top = next(iter(tops))
            nested = f"{top}/Open-AGC.exe" in names
        assert flat or nested, "实物 zip 布局超出 _locate_payload 支持范围"

    def test_macos_dmg_download_only(self, monkeypatch, tmp_path):
        upgrader = self._make_upgrader(monkeypatch)
        monkeypatch.setattr(sys, "platform", "darwin")
        asset_name = "Open-AGC-9.9.9-macOS-arm64.dmg"
        upgrader.latest_assets = [
            {"name": asset_name, "browser_download_url": "http://example/x.dmg"}
        ]
        monkeypatch.setattr(
            "core.auto_upgrade.requests.get",
            lambda *a, **kw: _FakeResp(b"dmg-bytes"),
        )
        home = tmp_path / "home"
        monkeypatch.setattr(
            os.path, "expanduser", lambda p: str(home) if p == "~" else p
        )

        assert upgrader._perform_desktop_upgrade() is True
        assert (home / "Downloads" / asset_name).read_bytes() == b"dmg-bytes"
        # macOS 不自动替换 .app，指引手动安装，不触发自动重启
        assert "手动" in upgrader.last_message
        assert upgrader.restart_required is False

    def test_perform_upgrade_dispatches_by_channel(self, monkeypatch):
        import core.auto_upgrade as auto_upgrade

        for channel, meth in (("desktop", "_perform_desktop_upgrade"),
                              ("docker", "_perform_source_upgrade"),
                              ("source", "_perform_source_upgrade")):
            monkeypatch.setattr(auto_upgrade, "get_channel", lambda: channel)
            upgrader = auto_upgrade.AutoUpgrader()
            called = []
            monkeypatch.setattr(
                type(upgrader), meth, lambda self: called.append(meth) or True
            )
            assert upgrader.perform_upgrade() is True
            assert called == [meth]


def test_version_endpoint_includes_channel(monkeypatch):
    """GET /api/version 响应带 channel 字段。"""
    import core.auto_upgrade as auto_upgrade
    from api.routes import routes_system

    monkeypatch.setattr(routes_system, "get_version", lambda: "1.0.0")
    monkeypatch.setattr(auto_upgrade, "get_version", lambda: "1.0.0")
    monkeypatch.setattr(auto_upgrade, "get_channel", lambda: "source")
    monkeypatch.setattr(
        auto_upgrade.AutoUpgrader, "fetch_latest_release", lambda self: None
    )

    result = _run(routes_system.get_api_version())
    assert result["channel"] == "source"
    assert "platform" in result
