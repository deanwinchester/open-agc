"""pywebview 5.0+ GTK 后端对 WebKitGTK < 2.40 的兼容补丁。

pywebview 5.0 起 GTK 后端改用 WebKitGTK 2.40 才引入的 API：
- ``WebView.evaluate_javascript`` / ``evaluate_javascript_finish``
  （2.40 新增；旧版为 ``run_javascript`` / ``run_javascript_finish``），
  旧版上调用 ``evaluate_js`` 直接 AttributeError。
- ``NavigationAction.get_frame_name`` 在 UOS/deepin 的 2.38 构建中经
  GObject introspection 不可用（AttributeError）。它出现在
  ``decide-policy`` 信号回调 ``on_navigation`` 里，异常中断回调会使
  导航决策悬而未决 → 页面白屏（实测 UOS ARM64 webkit2gtk 2.38.6）。

``apply_if_needed()`` 在 WebKitGTK < 2.40 时把这两处换回旧 API；
>= 2.40 的发行版（以及非 Linux 平台）不做任何改动。
"""
import json
import logging
from threading import Semaphore

logger = logging.getLogger(__name__)


def _gtk_module_if_old():
    """需要补丁时返回 webview.platforms.gtk 模块，否则返回 None。"""
    import sys
    if not sys.platform.startswith('linux'):
        return None
    try:
        from webview.platforms import gtk as gtk_mod
    except Exception:
        # 非 GTK 环境（无 gi / Windows / macOS）无需补丁
        return None
    ver = getattr(gtk_mod, 'webkit_ver', None)  # (major, minor, micro)
    if not ver or (ver[0], ver[1]) >= (2, 40):
        return None
    return gtk_mod


def _make_on_navigation(gtk_mod):
    """与 pywebview 6.1 原版一致，但 get_frame_name 不可用时降级：
    放弃 _blank 识别，导航照常放行（不调用 decision.use()/ignore()，
    返回 None 交给 WebKit 默认策略处理）。"""
    webkit = gtk_mod.webkit
    settings = gtk_mod.settings
    webbrowser = gtk_mod.webbrowser

    def on_navigation(self, webview_obj, decision, decision_type):
        try:
            if type(decision) == webkit.NavigationPolicyDecision:
                uri = None
                frame_name = None
                try:
                    action = decision.get_navigation_action()
                except Exception:
                    action = None
                try:
                    req = action.get_request() if action is not None else decision.get_request()
                    uri = req.get_uri()
                except Exception:
                    pass
                if action is not None:
                    try:
                        frame_name = action.get_frame_name()
                    except AttributeError:
                        # WebKitGTK < 2.40：get_frame_name 挂在
                        # NavigationPolicyDecision 上（2.40 已移除该旧位置）
                        try:
                            frame_name = decision.get_frame_name()
                        except Exception:
                            frame_name = None
                    except Exception:
                        frame_name = None

                if frame_name == '_blank' and uri:
                    if settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']:
                        webbrowser.open(uri, 2, True)
                        decision.ignore()
                    else:
                        self.load_url(uri)
            elif type(decision) == webkit.ResponsePolicyDecision:
                if not decision.is_mime_type_supported():
                    self._download_filename = decision.get_response().get_suggested_filename()
                    decision.download()
                else:
                    decision.use()
        except Exception:
            # 任何意外都不能中断 decide-policy 回调，否则导航挂起白屏
            logger.exception('on_navigation compat handler failed')

    return on_navigation


def _make_evaluate_js(gtk_mod):
    """用 WebKitGTK < 2.40 的 run_javascript API 实现 evaluate_js，
    行为对齐 pywebview 6.1 原版（信号量同步 + _convert_js_value 转换）。"""
    glib = gtk_mod.glib

    def evaluate_js(self, script, parse_json=True):
        def _evaluate_js():
            try:
                self.webview.run_javascript(script, None, _callback, None)
            except Exception:
                logger.exception('Error evaluating JavaScript')
                result_semaphore.release()

        def _callback(webview_obj, task, data):
            nonlocal result
            try:
                value = webview_obj.run_javascript_finish(task)
                # run_javascript_finish 返回 WebKit2.JavascriptResult，
                # 需先取 JSCValue 再走原版转换
                js_value = value.get_js_value() if value else None
                res = self._convert_js_value(js_value)
                if parse_json and res:
                    try:
                        result = json.loads(res)
                    except Exception:
                        pass
                else:
                    result = res
            except Exception:
                logger.exception('Error evaluating JavaScript')
            result_semaphore.release()

        result_semaphore = Semaphore(0)
        result = None
        glib.idle_add(_evaluate_js)
        result_semaphore.acquire()

        return result

    return evaluate_js


def apply_if_needed():
    """WebKitGTK < 2.40 时给 pywebview GTK 后端打兼容补丁。返回是否已打。"""
    gtk_mod = _gtk_module_if_old()
    if gtk_mod is None:
        return False
    ver = gtk_mod.webkit_ver
    gtk_mod.BrowserView.on_navigation = _make_on_navigation(gtk_mod)
    gtk_mod.BrowserView.evaluate_js = _make_evaluate_js(gtk_mod)
    logger.info(
        'Applied pywebview GTK compat patches for WebKitGTK %s.%s.%s (< 2.40)',
        ver[0], ver[1], ver[2],
    )
    print(f'[webview] WebKitGTK {ver[0]}.{ver[1]} < 2.40，已应用 GTK 兼容补丁')
    return True


_context_menu_patched = False


def enable_context_menu_linux():
    """Linux(GTK)：恢复 WebView 右键菜单（输入框右键粘贴等）。

    pywebview 在非 debug 模式下 connect('context-menu', True) 抑制右键菜单
    （gtk.py BrowserView.__init__），打包版输入框无法右键粘贴。做法：包裹
    __init__，初始化期间过滤掉 'context-menu' 信号的注册——只摘掉抑制
    处理器，其余信号照常，且不动 _state['debug']（翻转 debug 会连带开启
    开发者工具，右键多出 Inspect Element 检查器，生产实证）。重复调用幂等。
    """
    global _context_menu_patched
    if _context_menu_patched:
        return False
    import sys
    if not sys.platform.startswith('linux'):
        return False
    try:
        from webview.platforms import gtk as gtk_mod
    except Exception:
        return False

    _orig_init = gtk_mod.BrowserView.__init__
    _orig_connect = gtk_mod.webkit.WebView.connect

    def _connect_filtered(self, signal, *args, **kwargs):
        if signal == 'context-menu':
            return  # 不注册右键抑制处理器，保留 WebKit 默认右键菜单
        return _orig_connect(self, signal, *args, **kwargs)

    def _patched_init(self, window):
        gtk_mod.webkit.WebView.connect = _connect_filtered
        try:
            _orig_init(self, window)
        finally:
            gtk_mod.webkit.WebView.connect = _orig_connect

    gtk_mod.BrowserView.__init__ = _patched_init
    _context_menu_patched = True
    return True
