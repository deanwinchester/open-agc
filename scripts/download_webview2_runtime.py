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


def download_and_extract(out_dir: str = "build/webview2_runtime") -> str:
    """下载最新 fixed-version 运行时并解包到 out_dir。返回运行时目录路径。"""
    with urllib.request.urlopen(INDEX_URL, timeout=15) as r:
        version = json.load(r)["versions"][-1]
    url = PKG_URL.format(version=version)

    os.makedirs(out_dir, exist_ok=True)
    nupkg = os.path.join(out_dir, "_wv2rt.nupkg")
    print(f"[webview2] Downloading fixed runtime {version} ...")
    urllib.request.urlretrieve(url, nupkg)

    with zipfile.ZipFile(nupkg) as z:
        for name in z.namelist():
            if not name.startswith(EXTRACT_PREFIX):
                continue
            rel = name[len(EXTRACT_PREFIX):]
            if not rel:
                continue
            dest = os.path.join(out_dir, rel)
            if name.endswith("/"):
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
    os.remove(nupkg)

    exe = os.path.join(out_dir, "msedgewebview2.exe")
    if not os.path.isfile(exe):
        raise RuntimeError(f"WebView2 runtime extraction failed: {exe} not found")
    print(f"[webview2] Fixed runtime {version} extracted to {out_dir}")
    return out_dir


if __name__ == "__main__":
    download_and_extract(sys.argv[1] if len(sys.argv) > 1 else "build/webview2_runtime")
