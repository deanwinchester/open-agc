# -*- coding: utf-8 -*-
"""Linux 打包适配测试：PyQt6 QWebEngineView 原生窗口、Qt 依赖收集、deb Depends、浏览器回退。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as f:
        return f.read()


class TestSpecQtWebEngine:
    def test_spec_includes_qt_webengine_hiddenimports(self):
        spec = _read("open_agc.spec")
        assert "PyQt6.QtWebEngineWidgets" in spec
        assert "PyQt6.QtWebEngineCore" in spec
        assert "PyQt6.QtWidgets" in spec

    def test_spec_does_not_exclude_pyqt6(self):
        """PyQt6 用上了就不能再出现在 excludes（此前为不打包 Qt 而排除，
        会导致 PyQt6 被剔除出 bundle）。"""
        spec = _read("open_agc.spec")
        excludes = spec.split("excludes=[", 1)[1].split("]", 1)[0]
        assert "'PyQt6'" not in excludes

    def test_spec_no_pywebview_gtk_backend(self):
        """pywebview 已替换为 Qt，spec 不应再含 pywebview GTK 后端/GIR 收集。"""
        spec = _read("open_agc.spec")
        assert "webview.platforms.gtk" not in spec
        assert "GiModuleInfo" not in spec


class TestGuiAppBrowserFallback:
    def test_create_window_has_backend_fallback(self):
        src = _read("gui_app.py")
        assert "_browser_fallback" in src
        assert "except Exception as _gui_err" in src

    def test_create_window_uses_qt_webengine(self):
        src = _read("gui_app.py")
        assert "QWebEngineView" in src
        assert "_create_qt_window" in src


class TestWorkflowQtPackages:
    def _workflow(self):
        return _read(os.path.join(".github", "workflows", "docker-release.yml"))

    def test_deb_depends_includes_qt_webengine_libs(self):
        wf = self._workflow()
        depends_line = wf.split("Depends:")[1].split("\n")[0]
        # Qt WebEngine(Chromium) 运行时系统库
        for lib in ("libnss3", "libgbm1", "libxkbcommon0", "libasound2"):
            assert lib in depends_line, f"missing {lib} in deb Depends"
        # 不再依赖 WebKit2/GIR
        assert "gir1.2-webkit2-4.0" not in depends_line


class TestBuildDebScript:
    def test_build_deb_depends_includes_qt_webengine_libs(self):
        sh = _read("build_deb.sh")
        depends_line = next(l for l in sh.splitlines() if l.strip().startswith("Depends:"))
        for lib in ("libnss3", "libgbm1", "libxkbcommon0", "libasound2"):
            assert lib in depends_line, f"missing {lib} in build_deb.sh Depends"
        assert "gir1.2-webkit2-4.0" not in depends_line

    def test_build_deb_uses_local_node_folder(self):
        """build_deb.sh 必须优先用本地 .node/bin（与 start.sh 同一套），
        否则本地装了便携 Node 的机器上会报「npm not found」。"""
        sh = _read("build_deb.sh")
        assert ".node/bin/npm" in sh
        assert '.node/bin:$PATH' in sh or '".node/bin' in sh


class TestRequirements:
    def test_pyqt6_pinned_uos_compatible_versions(self):
        req = _read("requirements.txt")
        assert "PyQt6==6.7.1" in req
        assert "PyQt6-WebEngine==6.7.0" in req
        assert "PyQt6-Qt6==6.7.3" in req
        # pywebview 已被 Qt 替代（不作为实际依赖行存在；注释提及不算）
        dep_lines = [l.strip() for l in req.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
        assert not any(l.lower().startswith("pywebview") for l in dep_lines)
