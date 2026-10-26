# -*- coding: utf-8 -*-
"""沙箱路径边界回归：前缀混淆、符号链接与 NTFS junction 逃逸。"""
import asyncio
import os
import subprocess

import pytest
from fastapi import HTTPException

import api.deliverables_registry as dr
import api.routes.uploads as uploads
import api.server as server
from tools.base import BaseTool, SandboxBlocked


def _resolve_under_root(root, path):
    resolve = getattr(dr, "resolve_under_root", None)
    assert callable(resolve), "缺少统一的 resolve_under_root 路径校验函数"
    return resolve(str(root), str(path))


def _make_symlink(link, target):
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"当前环境不允许创建符号链接: {exc}")


def _make_junction(link, target):
    if os.name != "nt":
        pytest.skip("NTFS junction 仅在 Windows 上测试")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"创建 NTFS junction 失败: {result.stdout}{result.stderr}")


def test_resolve_under_root_accepts_root_and_regular_file(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    regular = root / "ok.txt"
    regular.write_text("ok", encoding="utf-8")

    assert _resolve_under_root(root, root) == os.path.realpath(root)
    assert _resolve_under_root(root, regular) == os.path.realpath(regular)


def test_resolve_under_root_rejects_prefix_sibling(tmp_path):
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace_secret"
    root.mkdir()
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    assert _resolve_under_root(root, secret) is None


def test_resolve_under_root_rejects_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "escape.txt"
    _make_symlink(link, outside)

    assert _resolve_under_root(root, link) is None


def test_resolve_under_root_rejects_junction_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "escape"
    _make_junction(link, outside)

    assert _resolve_under_root(root, link / "secret.txt") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows 路径大小写语义")
def test_resolve_under_root_windows_case_insensitive(tmp_path):
    root = tmp_path / "Workspace"
    root.mkdir()
    regular = root / "ReadMe.txt"
    regular.write_text("ok", encoding="utf-8")

    assert _resolve_under_root(str(root).swapcase(), str(regular).swapcase()) is not None


def test_tool_check_sandbox_rejects_symlink_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "escape.txt"
    _make_symlink(link, outside)

    with pytest.raises(SandboxBlocked):
        BaseTool.check_sandbox(str(link), sandbox_dir=str(root), config={})


def test_tool_check_sandbox_rejects_junction_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = root / "escape"
    _make_junction(link, outside)

    with pytest.raises(SandboxBlocked):
        BaseTool.check_sandbox(str(link / "secret.txt"), sandbox_dir=str(root), config={})


def test_tool_check_sandbox_allows_regular_file(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    regular = root / "ok.txt"
    regular.write_text("ok", encoding="utf-8")

    BaseTool.check_sandbox(str(regular), sandbox_dir=str(root), config={})


def test_file_route_rejects_prefix_sibling(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace_secret"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(server, "load_config", lambda: {"sandbox_dir": str(root)})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_sandbox_file("../workspace_secret/secret.txt"))
    assert exc_info.value.status_code == 403


def test_file_route_rejects_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "escape.txt"
    _make_symlink(link, outside)
    monkeypatch.setattr(server, "load_config", lambda: {"sandbox_dir": str(root)})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_sandbox_file("escape.txt"))
    assert exc_info.value.status_code == 403


def test_file_route_rejects_junction_escape(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "escape"
    _make_junction(link, outside)
    monkeypatch.setattr(server, "load_config", lambda: {"sandbox_dir": str(root)})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_sandbox_file("escape/secret.txt"))
    assert exc_info.value.status_code == 403


def test_file_route_serves_regular_file(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    regular = root / "ok.txt"
    regular.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(server, "load_config", lambda: {"sandbox_dir": str(root)})

    response = asyncio.run(server.get_sandbox_file("ok.txt"))
    assert os.path.realpath(response.path) == os.path.realpath(regular)


def test_upload_download_rejects_symlink_escape(tmp_path, monkeypatch):
    upload_dir = tmp_path / "workspace" / "uploads"
    upload_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = upload_dir / "escape.txt"
    _make_symlink(link, outside)
    monkeypatch.setattr(uploads, "_uploads_dir", lambda: str(upload_dir))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(uploads.download_uploaded("escape.txt"))
    assert exc_info.value.status_code in (403, 404)


def test_upload_delete_rejects_symlink_escape(tmp_path, monkeypatch):
    upload_dir = tmp_path / "workspace" / "uploads"
    upload_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = upload_dir / "escape.txt"
    _make_symlink(link, outside)
    monkeypatch.setattr(uploads, "_uploads_dir", lambda: str(upload_dir))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(uploads.delete_uploaded("escape.txt"))
    assert exc_info.value.status_code in (403, 404)
    assert outside.read_text(encoding="utf-8") == "secret"


def test_upload_download_and_delete_reject_junction_escape(tmp_path, monkeypatch):
    upload_dir = tmp_path / "workspace" / "uploads"
    upload_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = upload_dir / "escape"
    _make_junction(link, outside)
    monkeypatch.setattr(uploads, "_uploads_dir", lambda: str(upload_dir))

    with pytest.raises(HTTPException) as download_exc:
        asyncio.run(uploads.download_uploaded("escape"))
    assert download_exc.value.status_code == 403
    with pytest.raises(HTTPException) as delete_exc:
        asyncio.run(uploads.delete_uploaded("escape"))
    assert delete_exc.value.status_code == 403
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_upload_download_and_delete_allow_regular_file(tmp_path, monkeypatch):
    upload_dir = tmp_path / "workspace" / "uploads"
    upload_dir.mkdir(parents=True)
    regular = upload_dir / "ok.txt"
    regular.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(uploads, "_uploads_dir", lambda: str(upload_dir))

    response = asyncio.run(uploads.download_uploaded("ok.txt"))
    assert os.path.realpath(response.path) == os.path.realpath(regular)
    result = asyncio.run(uploads.delete_uploaded("ok.txt"))
    assert result["status"] == "success"
    assert not regular.exists()
