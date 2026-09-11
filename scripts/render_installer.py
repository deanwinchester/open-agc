#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染 NSIS 安装包脚本（packaging/installer.nsi.template → dist/installer.nsi）。

品牌解析优先级：命令行参数 > build_data/brand.json > 默认值。
zxs 定制版在 build_data/brand.json 里配显示名（如「中新助手」）与安装目录名；
main/开源版没这个文件，一切照旧（Open-AGC）。

为什么不直接在 bat 里 echo 生成 nsi：cmd 按 GBK 读 bat，中文显示名会乱码；
python 写 UTF-8+BOM 的 nsi（配合模板里 Unicode true）则没有这个问题。

用法：python scripts/render_installer.py [APP_NAME] [DISPLAY_NAME] [VERSION]
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "packaging", "installer.nsi.template")
BRAND_JSON = os.path.join(ROOT, "build_data", "brand.json")
VERSION_FILE = os.path.join(ROOT, "VERSION")


def _read_version() -> str:
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def main() -> int:
    args = sys.argv[1:]
    app_name = args[0] if len(args) > 0 else None
    display_name = args[1] if len(args) > 1 else None
    version = args[2] if len(args) > 2 else None

    if os.path.exists(BRAND_JSON):
        try:
            with open(BRAND_JSON, encoding="utf-8") as f:
                brand = json.load(f)
            app_name = app_name or brand.get("app_name")
            display_name = display_name or brand.get("display_name")
        except Exception as e:
            print(f"[warn] brand.json 读取失败（{e}），用默认值", file=sys.stderr)

    app_name = app_name or "Open-AGC"
    display_name = display_name or app_name
    version = version or _read_version()

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        content = f.read()
    # File 命令用绝对路径——makensis 对脚本内相对路径的解析基准不稳定
    # （生产实证：相对路径 dist\Open-AGC 在 runner 工作目录下 "no files found"）
    payload_abs = os.path.join(os.getcwd(), "dist", "Open-AGC")
    out_abs = os.path.join(os.getcwd(), "dist")
    content = (content
               .replace("@APP_NAME@", app_name)
               .replace("@DISPLAY_NAME@", display_name)
               .replace("@VERSION@", version)
               .replace("@PAYLOAD_DIR@", payload_abs)
               .replace("@OUT_ABS@", out_abs)
               .replace("@EXE_NAME@", "Open-AGC.exe"))
    os.makedirs("dist", exist_ok=True)
    out = os.path.join("dist", "installer.nsi")
    # UTF-8 BOM：makensis 的 Unicode 模式按 BOM 识别 UTF-8
    with open(out, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(content)
    print(f"installer script rendered: {out} "
          f"(app={app_name}, display={display_name}, v={version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
