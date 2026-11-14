# -*- coding: utf-8 -*-
"""PyInstaller macOS 启动钩子：只设置包内可定位的运行时资源。"""
import os
import sys

if getattr(sys, "frozen", False):
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        # open_agc.spec 将预热的编码缓存收集到该目录，避免首次启动联网生成。
        os.environ.setdefault(
            "TIKTOKEN_CACHE_DIR", os.path.join(bundle_dir, "tiktoken_cache")
        )
