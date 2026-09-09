# -*- coding: utf-8 -*-
"""下载并解包 WebView2 Fixed Version 运行时（供 frozen 包内嵌使用）。

从 NuGet 包 WebView2.Runtime.x64 提取 contentFiles/any/any/WebView2/（完整
浏览器引擎：msedgewebview2.exe 等）到 build/webview2_runtime/。打包脚本在
PyInstaller 之前调用本脚本；open_agc.spec 会把该目录打进 frozen 包，
gui_app.py 设 WEBVIEW2_RUNTIME_PATH 指向它，使 edgechromium 零依赖目标机的
系统 WebView2 运行时。

用法：python scripts/download_webview2_runtime.py [out_dir]
"""
import json
import os
import sys
import urllib.request
import zipfile

INDEX_URL = "https://api.nuget.org/v3-flatcontainer/webview2.runtime.x64/index.json"
PKG_URL = ("https://api.nuget.org/v3-flatcontainer/webview2.runtime.x64/"
           "{version}/webview2.runtime.x64.{version}.nupkg")
EXTRACT_PREFIX = "contentFiles/any/any/WebView2/"

# NuGet 上最新版 152.0.4191.62 的包不完整——缺浏览器核心 msedge.dll
# （104MB vs 完整版 247MB），打进包会在目标机上 WebView2 初始化报
# CO_E_SERVER_EXEC_FAILURE、窗口白屏。固定用已验证完整的版本。
PINNED_VERSION = "151.0.4129.107"


def _resolve_version() -> str:
    """优先返回固定版本；被下架时回退到索引中最后一个可用版本（仍会被
    _verify_runtime 完整性校验把关，拿不到完整包就构建期报错而不是出
    一个白屏的安装包）。"""
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=15) as r:
            versions = json.load(r)["versions"]
        if PINNED_VERSION in versions:
            return PINNED_VERSION
        print(f"[webview2] pinned {PINNED_VERSION} not on NuGet, "
              f"falling back to latest {versions[-1]}")
        return versions[-1]
    except Exception as e:
        print(f"[webview2] version index unavailable ({e}), using pinned "
              f"{PINNED_VERSION}")
        return PINNED_VERSION


def download_and_extract(out_dir: str = "build/webview2_runtime") -> str:
    """下载最新 fixed-version 运行时并解包到 out_dir。返回运行时目录路径。

    先解到临时目录、校验完整性（msedge.dll 是浏览器核心，缺它运行时会在
    目标机上报 CO_E_SERVER_EXEC_FAILURE 白屏）再原子替换就位——任何中途
    失败都不会留下残缺运行时被打进包。"""
    import shutil
    import tempfile

    version = _resolve_version()
    url = PKG_URL.format(version=version)

    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="_wv2rt_", dir=parent)
    try:
        nupkg = os.path.join(tmp_dir, "_wv2rt.nupkg")
        print(f"[webview2] Downloading fixed runtime {version} ...")
        urllib.request.urlretrieve(url, nupkg)

        with zipfile.ZipFile(nupkg) as z:
            for name in z.namelist():
                if not name.startswith(EXTRACT_PREFIX):
                    continue
                rel = name[len(EXTRACT_PREFIX):]
                if not rel:
                    continue
                dest = os.path.join(tmp_dir, rel)
                if name.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with z.open(name) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
        os.remove(nupkg)

        _verify_runtime(tmp_dir)

        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp_dir, out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    print(f"[webview2] Fixed runtime {version} extracted to {out_dir}")
    return out_dir


def _verify_runtime(path: str) -> None:
    """校验解包出的运行时完整：核心文件存在且体积合理（完整运行时
    600MB+，msedge.dll 单体 200MB+；残缺解包会让打包产物在目标机白屏）。"""
    for core in ("msedgewebview2.exe", "msedge.dll"):
        p = os.path.join(path, core)
        if not os.path.isfile(p):
            raise RuntimeError(f"WebView2 runtime extraction failed: {p} not found")
    dll_size = os.path.getsize(os.path.join(path, "msedge.dll"))
    if dll_size < 100 * 1024 * 1024:
        raise RuntimeError(
            f"WebView2 runtime looks incomplete: msedge.dll only {dll_size} bytes")


if __name__ == "__main__":
    download_and_extract(sys.argv[1] if len(sys.argv) > 1 else "build/webview2_runtime")
