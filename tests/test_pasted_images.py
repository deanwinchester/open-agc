# -*- coding: utf-8 -*-
"""粘贴图片落盘回归：聊天窗口粘贴的图片此前只以 base64 存在于消息里，
agent 在文件系统找不到位置（用户反馈）。修复：ws 收到 images 时解码
落盘 <sandbox>/uploads/，并把路径注入 query 文本。"""
import base64
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.ws as ws  # noqa: E402

_PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082")).decode()


def _data_url(ext="png"):
    return f"data:image/{ext};base64,{_PNG_1PX}"


class TestPersistPastedImages:
    def test_saves_data_url_to_uploads(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        monkeypatch.setattr("api.routes.uploads._uploads_dir",
                            lambda: str(uploads))
        saved = ws._persist_pasted_images([_data_url(), _data_url("jpeg")])
        assert len(saved) == 2
        names = sorted(os.listdir(uploads))
        assert len(names) == 2
        assert {n.rsplit(".", 1)[-1] for n in names} == {"png", "jpg"}
        for rel in saved:
            assert rel.startswith("uploads/")
            # 内容与原 base64 一致
            body = (uploads / os.path.basename(rel)).read_bytes()
            assert body == base64.b64decode(_PNG_1PX)

    def test_skips_non_data_url_and_bad_input(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        monkeypatch.setattr("api.routes.uploads._uploads_dir",
                            lambda: str(uploads))
        assert ws._persist_pasted_images(None) == []
        assert ws._persist_pasted_images([]) == []
        assert ws._persist_pasted_images(["uploads/existing.png"]) == []
        assert ws._persist_pasted_images(["data:text/plain;base64,aGk="]) == []
        assert not uploads.exists() or os.listdir(uploads) == []

    def test_uploads_dir_failure_is_silent(self, monkeypatch):
        monkeypatch.setattr("api.routes.uploads._uploads_dir",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        assert ws._persist_pasted_images([_data_url()]) == []

    def test_query_injection_format(self):
        """注入文本与 [已上传文件: ...] 同风格，agent 从文本即可寻址。"""
        paths = ["uploads/paste_20260731_a_1.png"]
        suffix = f"\n\n[用户粘贴的图片已保存: {', '.join(paths)}]"
        assert "uploads/paste_" in suffix and "已保存" in suffix
