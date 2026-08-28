# -*- coding: utf-8 -*-
"""Linux 打包适配测试：pywebview GTK 后端、GIR/WebKit typelib、deb Depends、浏览器回退。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(ROOT, path), "r", encoding="utf-8") as f:
        return f.read()


class TestSpecLinuxGtk:
    def test_spec_includes_gtk_backend_hiddenimport(self):
        spec = _read("open_agc.spec")
        assert "webview.platforms.gtk" in spec

    def test_spec_collects_webkit2_javascriptcore_typelibs(self):
        spec = _read("open_agc.spec")
        assert "WebKit2" in spec
        assert "JavaScriptCore" in spec
        assert "GiModuleInfo" in spec

    def test_spec_gtk_collection_is_guarded(self):
        """GiModuleInfo 收集必须在 try/except 中，未装 GIR 时安全跳过。"""
        spec = _read("open_agc.spec")
        assert "if _info.available" in spec


class TestGuiAppBrowserFallback:
    def test_create_window_has_backend_fallback(self):
        src = _read("gui_app.py")
        assert "_browser_fallback" in src
        assert "except Exception as _gui_err" in src


class TestWorkflowGirPackages:
    def _workflow(self):
        return _read(os.path.join(".github", "workflows", "docker-release.yml"))

    def test_container_installs_gir_webkit_dev_packages(self):
        wf = self._workflow()
        assert "gir1.2-webkit2-4.0" in wf
        assert "gir1.2-gtk-3.0" in wf
        assert "libgirepository1.0-dev" in wf

    def test_container_installs_pygobject(self):
        wf = self._workflow()
        assert "PyGObject" in wf

    def test_buster_apt_uses_archive(self):
        wf = self._workflow()
        assert "archive.debian.org" in wf

    def test_deb_depends_includes_gir_webkit(self):
        wf = self._workflow()
        assert "gir1.2-webkit2-4.0" in wf.split("Depends:")[1].split("\n")[0]
        assert "gir1.2-gtk-3.0" in wf.split("Depends:")[1].split("\n")[0]


class TestBuildDebScript:
    def test_build_deb_depends_includes_gir_webkit(self):
        sh = _read("build_deb.sh")
        depends_line = next(l for l in sh.splitlines() if l.strip().startswith("Depends:"))
        assert "gir1.2-webkit2-4.0" in depends_line
        assert "gir1.2-gtk-3.0" in depends_line
