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
VERSION="1.0.0"
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
        --workpath "build/${arch}" 2>&1 | tail -5
    
    echo "  ✅ ${arch} build complete"
}

# ---- 1. Prepare build environment ----
echo ""
echo "[1/5] Preparing build environment..."

if [ ! -d "build_venv" ]; then
    python3 -m venv build_venv
fi
source build_venv/bin/activate

pip install --upgrade pip -q 2>/dev/null
pip install pyinstaller -q 2>/dev/null
pip install -r requirements.txt -q 2>/dev/null
pip install httptools websockets -q 2>/dev/null || true

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

# ---- 3. Code sign (ad-hoc) ----
echo "[3/5] Code signing..."
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
