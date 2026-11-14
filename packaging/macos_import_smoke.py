"""在 PyInstaller 包内验证宿主预检无法覆盖的动态导入完整性。"""
import importlib
import traceback

failed = []
for name in (
    "psutil",
    "docx",
    "mcp",
    "openpyxl",
    "pymupdf",
    "rapidocr_onnxruntime",
    "chromadb",
):
    try:
        importlib.import_module(name)
        print(f"  ✅ Packaged import: {name}")
    except BaseException:
        failed.append(name)
        print(f"  ❌ Packaged import failed: {name}")
        traceback.print_exc()

if failed:
    raise SystemExit("Packaged dependency check failed: " + ", ".join(failed))
print("  ✅ Packaged dependency preflight passed")
