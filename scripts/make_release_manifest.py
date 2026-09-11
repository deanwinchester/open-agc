#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成内网 release 通道的版本清单 version.json。

CI 的 release:promote job 在 81（制品服务器）上执行：
    python3 scripts/make_release_manifest.py <version>
扫描 /opt/agc-artifacts/release/v<version>/ 下的产物，输出清单到
/opt/agc-artifacts/release/version.json（应用升级检测只读这个文件）。

环境变量：
    AGC_ARTIFACTS_ROOT  制品根目录（默认 /opt/agc-artifacts）
    AGC_RELEASE_BASE_URL 清单里资产 URL 的基址
        （默认 http://172.16.100.81:8080/release）
"""
import json
import os
import sys
import time


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: make_release_manifest.py <version>", file=sys.stderr)
        return 2
    version = sys.argv[1].strip().lstrip("v")
    root = os.environ.get("AGC_ARTIFACTS_ROOT", "/opt/agc-artifacts")
    base_url = os.environ.get(
        "AGC_RELEASE_BASE_URL", "http://172.16.100.81:8080/release").rstrip("/")

    release_dir = os.path.join(root, "release", f"v{version}")
    if not os.path.isdir(release_dir):
        print(f"ERROR: {release_dir} 不存在", file=sys.stderr)
        return 1

    assets = {}
    for fname in os.listdir(release_dir):
        lower = fname.lower()
        if lower.endswith("-setup.exe"):
            assets["windows"] = fname
        elif lower.endswith("-windows.zip") or lower.endswith("-windows-x64.zip"):
            assets.setdefault("windows_zip", fname)
        elif lower.endswith("-linux-amd64.deb"):
            assets["linux-amd64"] = fname
        elif lower.endswith("-linux-arm64.deb"):
            assets["linux-arm64"] = fname
        elif lower.endswith(".dmg"):
            assets["macos"] = fname
        elif lower.endswith(".uab"):
            assets["linglong-arm64" if "arm64" in lower else "linglong"] = fname

    if not assets:
        print(f"ERROR: {release_dir} 下没有可识别的产物文件", file=sys.stderr)
        return 1

    manifest = {
        "version": version,
        "released_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": f"{base_url}/v{version}/",
        "assets": assets,
    }
    out_path = os.path.join(root, "release", "version.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"manifest written: {out_path}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
