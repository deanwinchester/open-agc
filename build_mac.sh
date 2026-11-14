#!/bin/bash
# ===========================================
#  Build Open-AGC.dmg for macOS
#  Supports: x86_64, arm64, or universal (both)
#
#  Usage:
#    ./build_mac.sh            # Build for current architecture
#    ./build_mac.sh universal  # Build universal binary (x86 + ARM)
#    ./build_mac.sh x86_64     # Build for Intel only
#    ./build_mac.sh arm64      # Build for Apple Silicon only
# ===========================================

set -e

APP_NAME="Open-AGC"

# Read VERSION from file
if [ -f VERSION ]; then
    VERSION=$(cat VERSION)
else
    VERSION="0.0.0"
fi
BUILD_ARCH="${1:-$(uname -m)}"

echo "============================================="
echo "  🐼 Building ${APP_NAME} v${VERSION}"
echo "  Target: ${BUILD_ARCH}"
echo "============================================="

# Navigate to project root
cd "$(dirname "$0")"
PROJECT_DIR=$(pwd)

# ---- Helper: build for a single architecture ----
build_single_arch() {
    local arch=$1
    echo ""
    echo ">>> Building for ${arch}..."
    
    export TARGET_ARCH="${arch}"
    
    pyinstaller open_agc.spec --clean --noconfirm \
        --distpath "dist/${arch}" \
        --workpath "build/${arch}"
    
    echo "  ✅ ${arch} build complete"
}

# ---- 1. Prepare build environment ----
echo ""
echo "[1/5] Preparing build environment..."

# rapidocr-onnxruntime 尚未提供 Python 3.13+ 的兼容发行版；与 CI 一致优先
# 使用 3.10，也允许 3.11/3.12。可通过 PYTHON_BIN 显式指定解释器。
if [ -z "${PYTHON_BIN:-}" ]; then
    for candidate in python3.10 python3.11 python3.12; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            PYTHON_BIN=$(command -v "${candidate}")
            break
        fi
    done
fi
if [ -z "${PYTHON_BIN:-}" ] || ! "${PYTHON_BIN}" -c \
    'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 13)))'; then
    echo "  ERROR: macOS 构建需要 Python 3.10–3.12（CI 使用 3.10）。"
    echo "  请先安装，例如 brew install python@3.10，或设置 PYTHON_BIN。"
    exit 1
fi
if [ -d "build_venv" ]; then
    if ! build_venv/bin/python -c \
        'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] < (3, 13)))'; then
        echo "  ERROR: 现有 build_venv 使用了不兼容的 Python："
        build_venv/bin/python --version
        echo "  请先移走该目录，再重新运行构建（脚本不会擅自删除旧环境）。"
        exit 1
    fi
else
    "${PYTHON_BIN}" -m venv build_venv
fi

# 构建前端（Vite 产物会被 PyInstaller 收集）
echo "  Building frontend with Vite..."
if command -v npm &> /dev/null; then
    [ ! -d "node_modules/@vitejs/plugin-vue" ] && npm install
    npm run build
else
    echo "  ERROR: npm not found — frontend build required for packaging!"
    echo "  Please install Node.js from https://nodejs.org/"
    exit 1
fi

source build_venv/bin/activate

# 不隐藏 stderr：此前 Python 3.13 依赖不兼容被 2>/dev/null 吞掉，脚本
# 只显示前端构建完成便静默退出，用户无法判断 DMG 为什么没有生成。
pip install --upgrade pip -q
pip install pyinstaller -q
pip install -r requirements.txt -q
pip install httptools websockets pywebview -q || true

# 依赖完整性的最后防线：缺失时必须在生成 DMG 前失败。此前本机 pip 在
# RapidOCR 处中断，后半段依赖未装上，仍打出了启动即崩溃的损坏包。
python - <<'PY'
import chromadb
import docx
import mcp
import openpyxl
import psutil
import pymupdf
import rapidocr_onnxruntime
print("  ✅ Runtime dependency preflight passed")
PY

# ---- 2. Build ----
echo "[2/5] Building application..."

if [ "${BUILD_ARCH}" = "universal" ]; then
    # Build for both architectures, then merge with lipo
    echo "  Building universal binary (x86_64 + arm64)..."
    
    build_single_arch "x86_64"
    build_single_arch "arm64"
    
    echo ""
    echo "  Merging into universal binary..."
    
    # Use the x86_64 build as base, then merge binaries with lipo
    rm -rf "dist/${APP_NAME}.app"
    cp -R "dist/x86_64/${APP_NAME}.app" "dist/${APP_NAME}.app"
    
    # Find all Mach-O binaries and merge them
    find "dist/x86_64/${APP_NAME}.app" -type f | while read x86_file; do
        rel_path="${x86_file#dist/x86_64/}"
        arm_file="dist/arm64/${rel_path}"
        out_file="dist/${rel_path}"
        
        if [ -f "${arm_file}" ]; then
            # Check if it's a Mach-O binary
            if file "${x86_file}" | grep -q "Mach-O"; then
                lipo -create "${x86_file}" "${arm_file}" -output "${out_file}" 2>/dev/null || \
                    cp "${x86_file}" "${out_file}"
            fi
        fi
    done
    
    echo "  ✅ Universal binary merged"
    
else
    # Single architecture build
    build_single_arch "${BUILD_ARCH}"
    
    # Move to standard dist location
    rm -rf "dist/${APP_NAME}.app"
    mv "dist/${BUILD_ARCH}/${APP_NAME}.app" "dist/${APP_NAME}.app"
fi

echo "  ✅ Build complete: dist/${APP_NAME}.app"

# ---- 3. Validate packaged imports, then code sign (ad-hoc) ----
echo "[3/5] Checking imports inside the packaged application..."

# 宿主机能 import 不代表 PyInstaller 已收集对应模块。直接用包内解释器
# 再跑一遍关键导入，任何缺失都必须在 DMG 生成前让构建失败。
# 必须在签名前运行：动态源码导入可能生成 __pycache__，签后运行会破坏签名。
"dist/${APP_NAME}.app/Contents/MacOS/${APP_NAME}" \
    --pyrun packaging/macos_import_smoke.py

echo "  Code signing..."
codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null && \
    echo "  ✅ Signed (ad-hoc)" || \
    echo "  ⚠️  Code signing skipped"

# ---- 4. Create DMG ----
echo "[4/5] Creating DMG disk image..."

if [ "${BUILD_ARCH}" = "universal" ]; then
    DMG_NAME="${APP_NAME}-${VERSION}-macOS-universal.dmg"
else
    DMG_NAME="${APP_NAME}-${VERSION}-macOS-${BUILD_ARCH}.dmg"
fi

DMG_DIR="dist/dmg_staging"
rm -rf "${DMG_DIR}"
mkdir -p "${DMG_DIR}"

cp -R "dist/${APP_NAME}.app" "${DMG_DIR}/"
ln -s /Applications "${DMG_DIR}/Applications"

rm -f "dist/${DMG_NAME}"
hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "${DMG_DIR}" \
    -ov \
    -format UDZO \
    "dist/${DMG_NAME}"

echo "  ✅ DMG created: dist/${DMG_NAME}"

# ---- 5. Clean up ----
echo "[5/5] Cleaning up..."
rm -rf "${DMG_DIR}"
rm -rf build/
rm -rf dist/x86_64 dist/arm64 dist/Open-AGC 2>/dev/null

echo ""
echo "============================================="
echo "  ✅ Build complete!"
echo "  📦 dist/${DMG_NAME}"
echo "============================================="
echo ""
echo "To install: Open the .dmg → drag Open-AGC to Applications"
