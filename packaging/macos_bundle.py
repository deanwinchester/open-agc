# -*- coding: utf-8 -*-
"""macOS .app 元数据：供 open_agc.spec 通过 runpy 读取。"""
import json
import re
from pathlib import Path


def bundle_metadata(root, target_arch):
    if target_arch not in ("arm64", "x86_64"):
        raise ValueError(f"Unsupported macOS target architecture: {target_arch}")

    root = Path(root)
    brand = {}
    brand_path = root / "build_data" / "brand.json"
    if brand_path.is_file():
        with brand_path.open("r", encoding="utf-8") as f:
            brand = json.load(f)

    app_name = brand.get("app_name") or "Open-AGC"
    display_name = brand.get("display_name") or app_name
    version_path = root / "VERSION"
    full_version = (
        version_path.read_text(encoding="utf-8").strip()
        if version_path.is_file() else "0.0.0"
    )
    # Apple 的对外版本号使用正式版本；完整 rc/dev 版本另存 OpenAGCVersion，
    # 便于更新清单与支持诊断仍能看到实际构建号。
    release_version = re.split(r"(?i)(?:rc|a|b|dev)", full_version, maxsplit=1)[0].rstrip(".-_")
    release_version = release_version or full_version
    bundle_id = brand.get("bundle_id") or "com.openagc.app"

    return {
        "name": app_name,
        "bundle_id": bundle_id,
        "plist": {
            "CFBundleName": app_name,
            "CFBundleDisplayName": display_name,
            "CFBundleExecutable": app_name,
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": release_version,
            "CFBundleVersion": release_version,
            "OpenAGCVersion": full_version,
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSMicrophoneUsageDescription": "语音输入需要使用麦克风。",
            "NSCameraUsageDescription": "视觉功能需要使用摄像头。",
            "NSAppleEventsUsageDescription": "系统自动化功能需要控制其他应用。",
        },
    }
