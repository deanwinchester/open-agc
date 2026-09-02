"""core.pywebview_gtk_compat 的单元测试。

pywebview 5.0+ 的 GTK 后端用 WebKitGTK 2.40+ API，在 UOS/deepin 的
2.38 上：on_navigation 里 NavigationAction.get_frame_name 抛
AttributeError（导航挂起白屏）、evaluate_javascript 不存在。
补丁把这两处换回旧 API。测试用假 gtk 模块模拟旧 WebKitGTK 环境。
"""
import sys
import types

import pytest

import core.pywebview_gtk_compat as compat


def _make_fake_gtk(webkit_ver=(2, 38, 6)):
    """构造一个模拟 WebKitGTK < 2.40 的假 webview.platforms.gtk 模块。"""
    mod = types.ModuleType('webview.platforms.gtk')
    mod.webkit_ver = webkit_ver

    class NavigationPolicyDecision:
        pass

    class ResponsePolicyDecision:
        pass

    mod.webkit = types.SimpleNamespace(
        NavigationPolicyDecision=NavigationPolicyDecision,
        ResponsePolicyDecision=ResponsePolicyDecision,
    )
    mod.settings = {'OPEN_EXTERNAL_LINKS_IN_BROWSER': True}
    mod.webbrowser = types.SimpleNamespace(open=lambda *a: None)
    mod.glib = types.SimpleNamespace(idle_add=lambda fn: fn())

    class BrowserView:
        pass

    mod.BrowserView = BrowserView
    return mod


@pytest.fixture
def fake_gtk(monkeypatch):
    mod = _make_fake_gtk()
    monkeypatch.setitem(sys.modules, 'webview.platforms.gtk', mod)
    monkeypatch.setattr(sys, 'platform', 'linux')
    return mod


def test_noop_on_non_linux(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    assert compat.apply_if_needed() is False


def test_noop_on_new_webkit(monkeypatch):
    mod = _make_fake_gtk(webkit_ver=(2, 40, 0))
    monkeypatch.setitem(sys.modules, 'webview.platforms.gtk', mod)
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert compat.apply_if_needed() is False
    # 未打补丁：BrowserView 上没有我们注入的方法
    assert not hasattr(mod.BrowserView, 'on_navigation')


def test_patch_applied_on_old_webkit(fake_gtk):
    assert compat.apply_if_needed() is True
    assert hasattr(fake_gtk.BrowserView, 'on_navigation')
    assert hasattr(fake_gtk.BrowserView, 'evaluate_js')


def test_on_navigation_survives_missing_get_frame_name(fake_gtk):
    """UOS 实测场景：NavigationAction.get_frame_name 抛 AttributeError。
    普通导航应静默放行（不调 use/ignore，交给 WebKit 默认策略）。"""
    compat.apply_if_needed()

    calls = []

    class FakeAction:
        def get_frame_name(self):
            raise AttributeError("no attribute 'get_frame_name'")

        def get_request(self):
            return types.SimpleNamespace(get_uri=lambda: 'http://localhost:8765')

    decision = fake_gtk.webkit.NavigationPolicyDecision()
    decision.get_navigation_action = lambda: FakeAction()
    decision.get_frame_name = lambda: (_ for _ in ()).throw(AttributeError("old API also gone"))
    decision.use = lambda: calls.append('use')
    decision.ignore = lambda: calls.append('ignore')

    view = fake_gtk.BrowserView()
    # 不应抛异常；普通导航不做任何决策调用
    fake_gtk.BrowserView.on_navigation(view, None, decision, None)
    assert calls == []


def test_on_navigation_blank_uses_old_decision_api(fake_gtk):
    """_blank 链接：新位置（NavigationAction）不可用时回退到
    NavigationPolicyDecision.get_frame_name（2.40 移除前的旧位置）。"""
    compat.apply_if_needed()

    calls = []

    class FakeAction:
        def get_frame_name(self):
            raise AttributeError("gone")

        def get_request(self):
            return types.SimpleNamespace(get_uri=lambda: 'http://example.com')

    decision = fake_gtk.webkit.NavigationPolicyDecision()
    decision.get_navigation_action = lambda: FakeAction()
    decision.get_frame_name = lambda: '_blank'
    decision.use = lambda: calls.append('use')
    decision.ignore = lambda: calls.append('ignore')

    view = fake_gtk.BrowserView()
    fake_gtk.BrowserView.on_navigation(view, None, decision, None)
    assert calls == ['ignore']


def test_evaluate_js_uses_run_javascript(fake_gtk):
    """旧 WebKitGTK 上 evaluate_js 必须走 run_javascript 并解析返回值。"""
    compat.apply_if_needed()

    class FakeJsResult:
        def get_js_value(self):
            return object()  # 交给 _convert_js_value

    class FakeWebView:
        def run_javascript(self, script, cancel, callback, data):
            assert script == 'return 42;'
            callback(self, 'task', None)

        def run_javascript_finish(self, task):
            return FakeJsResult()

    view = fake_gtk.BrowserView()
    view.webview = FakeWebView()
    view._convert_js_value = lambda v: '42'

    assert fake_gtk.BrowserView.evaluate_js(view, 'return 42;') == 42
    # parse_json=False 时原样返回字符串
    assert fake_gtk.BrowserView.evaluate_js(view, 'return 42;', False) == '42'
