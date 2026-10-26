# -*- coding: utf-8 -*-
"""发布清单与桌面升级包的 SHA-256 完整性校验回归测试。"""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from core.auto_upgrade import AutoUpgrader


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeResp:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def iter_content(self, chunk_size=1):
        yield self._payload


def _make_upgrader(monkeypatch):
    import core.auto_upgrade as auto_upgrade

    monkeypatch.setattr(auto_upgrade, "get_channel", lambda: "desktop")
    upgrader = auto_upgrade.AutoUpgrader()
    upgrader.current_version = "1.0.0"
    upgrader.latest_version = "2.0.0"
    return upgrader


def _prepare_linux_download(monkeypatch, tmp_path, payload, asset_value):
    upgrader = _make_upgrader(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    asset_name = "Open-AGC-2.0.0-Linux-amd64.deb"
    upgrader._manifest_assets = {"linux-amd64": asset_value}
    asset = {"name": asset_name, "browser_download_url": "http://example/update.deb"}
    if isinstance(asset_value, dict) and asset_value.get("sha256"):
        asset["sha256"] = asset_value["sha256"]
    upgrader.latest_assets = [asset]
    monkeypatch.setattr("core.paths.get_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        "core.auto_upgrade.requests.get", lambda *args, **kwargs: _FakeResp(payload)
    )
    return upgrader, tmp_path / "updates" / asset_name


def test_matching_sha256_allows_download(monkeypatch, tmp_path):
    payload = b"valid-deb"
    digest = hashlib.sha256(payload).hexdigest()
    asset_name = "Open-AGC-2.0.0-Linux-amd64.deb"
    upgrader, dest = _prepare_linux_download(
        monkeypatch, tmp_path, payload, {"name": asset_name, "sha256": digest}
    )

    assert upgrader._download_linux_deb() is True
    assert dest.read_bytes() == payload
    assert "清单未含哈希" not in upgrader.last_message


def test_mismatched_sha256_rejects_installer_without_bat(monkeypatch, tmp_path):
    upgrader = _make_upgrader(monkeypatch)
    monkeypatch.setattr("core.auto_upgrade.tempfile.gettempdir", lambda: str(tmp_path))
    asset_name = "Open-AGC-2.0.0-Setup.exe"
    asset = {
        "name": asset_name,
        "browser_download_url": "http://example/setup.exe",
        "sha256": hashlib.sha256(b"expected").hexdigest(),
    }
    monkeypatch.setattr(
        "core.auto_upgrade.requests.get", lambda *args, **kwargs: _FakeResp(b"tampered")
    )

    assert upgrader._prepare_windows_installer(
        asset, asset_name, str(tmp_path / "Open-AGC.exe")
    ) is False
    assert not (tmp_path / asset_name).exists()
    assert not (tmp_path / "open_agc_apply_update.bat").exists()
    assert upgrader.staged_installer is None
    assert "校验失败" in upgrader.last_message


def test_legacy_manifest_without_sha256_warns_but_allows(monkeypatch, tmp_path):
    payload = b"legacy-deb"
    asset_name = "Open-AGC-2.0.0-Linux-amd64.deb"
    upgrader, dest = _prepare_linux_download(monkeypatch, tmp_path, payload, asset_name)

    assert upgrader._download_linux_deb() is True
    assert dest.read_bytes() == payload
    assert "清单未含哈希，未做完整性校验" in upgrader.last_message


@pytest.mark.parametrize("object_form", [False, True])
def test_internal_manifest_supports_string_and_object_assets(monkeypatch, object_form):
    upgrader = _make_upgrader(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    upgrader.manifest_url = "http://example/version.json"
    asset_name = "Open-AGC-2.0.0-Setup.exe"
    digest = hashlib.sha256(b"setup").hexdigest()
    asset_value = {"name": asset_name, "sha256": digest} if object_form else asset_name

    class _ManifestResp:
        status_code = 200

        def json(self):
            return {
                "version": "2.0.0",
                "base_url": "http://example/release/v2.0.0/",
                "assets": {"windows": asset_value},
            }

    monkeypatch.setattr(
        "core.auto_upgrade.requests.get", lambda *args, **kwargs: _ManifestResp()
    )

    assert upgrader._fetch_internal_manifest() == "2.0.0"
    assert upgrader._desktop_asset_name("2.0.0") == asset_name
    assert upgrader.latest_assets[0]["name"] == asset_name
    assert upgrader.latest_assets[0]["browser_download_url"].endswith(asset_name)
    if object_form:
        assert upgrader.latest_assets[0]["sha256"] == digest


def test_release_manifest_contains_real_asset_sha256(tmp_path):
    release_dir = tmp_path / "release" / "v2.0.0"
    release_dir.mkdir(parents=True)
    payload = b"setup-binary"
    asset_name = "Open-AGC-2.0.0-Setup.exe"
    (release_dir / asset_name).write_bytes(payload)
    env = os.environ.copy()
    env["AGC_ARTIFACTS_ROOT"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "make_release_manifest.py"), "2.0.0"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "release" / "version.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "2.0.0"
    assert manifest["assets"]["windows"] == {
        "name": asset_name,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
