#!/bin/bash
# ===========================================
#  Build Open-AGC .deb for Linux (Ubuntu/UOS)
#  Supports: amd64, arm64
#
#  Usage:
#    ./build_deb.sh         # Build for current architecture
#    ./build_deb.sh amd64   # Build for x86_64 (Intel/AMD)
#    ./build_deb.sh arm64   # Build for ARM64
# ===========================================

set -e

APP_NAME="Open-AGC"
PKG_NAME="open-agc"

# Navigate to project root
cd "$(dirname "$0")"

# Read VERSION from file
if [ -f VERSION ]; then
    VERSION=$(cat VERSION | tr -d '[:space:]')
else
    VERSION="0.0.0"
fi

# ---- Resolve target architecture ----
# Map uname -m output to Debian arch names
host_arch() {
    case "$(uname -m)" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        arm64)   echo "arm64" ;;
        *)       echo "$(uname -m)" ;;
    esac
}

BUILD_ARCH="${1:-$(host_arch)}"
case "${BUILD_ARCH}" in
    amd64|arm64) ;;
    *)
        echo "ERROR: Unsupported architecture '${BUILD_ARCH}' (expected amd64 or arm64)"
        exit 1
        ;;
esac

# Map Debian arch back to PyInstaller TARGET_ARCH
case "${BUILD_ARCH}" in
    amd64) PYI_ARCH="x86_64" ;;
    arm64) PYI_ARCH="aarch64" ;;
esac

if [ "${BUILD_ARCH}" != "$(host_arch)" ]; then
    echo "  ⚠️  Warning: requested arch (${BUILD_ARCH}) differs from host ($(host_arch))."
    echo "     PyInstaller cannot cross-compile — the binary will be for the host arch."
fi

DEB_NAME="${APP_NAME}-${VERSION}-Linux-${BUILD_ARCH}.deb"
STAGING_ROOT="dist/deb_staging"
STAGING_DIR="${STAGING_ROOT}/${PKG_NAME}_${VERSION}_${BUILD_ARCH}"

echo "============================================="
echo "  🐼 Building ${APP_NAME} v${VERSION}"
echo "  Target: Linux ${BUILD_ARCH}"
echo "============================================="

# Check required tools
if ! command -v dpkg-deb &> /dev/null; then
    echo "ERROR: dpkg-deb not found — this script must run on a Debian-based system (Ubuntu/UOS/Debian)."
    exit 1
fi

# ---- 1. Prepare build environment ----
echo ""
echo "[1/5] Preparing build environment..."

# Build frontend with Vite (required for packaging)
echo "  Building frontend with Vite..."
if command -v npm &> /dev/null; then
    [ ! -d "node_modules/@vitejs/plugin-vue" ] && npm install
    npm run build
else
    echo "  ERROR: npm not found — frontend build required for packaging!"
    echo "  Please install Node.js from https://nodejs.org/"
    exit 1
fi

if [ ! -d "build_venv" ]; then
    python3 -m venv build_venv
fi
source build_venv/bin/activate

pip install --upgrade pip -q 2>/dev/null
pip install pyinstaller -q 2>/dev/null
pip install -r requirements.txt -q 2>/dev/null
pip install pywebview -q 2>/dev/null || true

# ---- 2. Build with PyInstaller ----
echo "[2/5] Building application with PyInstaller..."

export TARGET_ARCH="${PYI_ARCH}"

pyinstaller open_agc.spec --clean --noconfirm \
    --distpath "dist/linux" \
    --workpath "build/linux"

if [ ! -f "dist/linux/${APP_NAME}/${APP_NAME}" ]; then
    echo "  ERROR: PyInstaller build failed — dist/linux/${APP_NAME}/${APP_NAME} not found!"
    exit 1
fi

echo "  ✅ Build complete: dist/linux/${APP_NAME}/"

# ---- 3. Assemble deb staging directory ----
echo "[3/5] Assembling deb directory structure..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/DEBIAN"
mkdir -p "${STAGING_DIR}/opt/${PKG_NAME}"
mkdir -p "${STAGING_DIR}/usr/share/applications"
mkdir -p "${STAGING_DIR}/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output
cp -R "dist/linux/${APP_NAME}/." "${STAGING_DIR}/opt/${PKG_NAME}/"
chmod +x "${STAGING_DIR}/opt/${PKG_NAME}/${APP_NAME}"

# DEBIAN/control
cat > "${STAGING_DIR}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${BUILD_ARCH}
Depends: libwebkit2gtk-4.0-37, libgtk-3-0
Maintainer: Open-AGC Team <noreply@open-agc.local>
Description: Open-AGC — AI agent desktop application
 Open-AGC is a local AI agent desktop app with a web-based UI,
 bundled Python backend and LLM integration.
EOF

# DEBIAN/postinst
cat > "${STAGING_DIR}/DEBIAN/postinst" <<'EOF'
#!/bin/bash
set -e

chmod +x /opt/open-agc/Open-AGC
ln -sf /opt/open-agc/Open-AGC /usr/local/bin/open-agc

if command -v update-desktop-database > /dev/null 2>&1; then
    update-desktop-database /usr/share/applications > /dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache > /dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor > /dev/null 2>&1 || true
fi

exit 0
EOF
chmod 755 "${STAGING_DIR}/DEBIAN/postinst"

# DEBIAN/prerm
cat > "${STAGING_DIR}/DEBIAN/prerm" <<'EOF'
#!/bin/bash
set -e

pkill -f "/opt/open-agc/Open-AGC" > /dev/null 2>&1 || true

exit 0
EOF
chmod 755 "${STAGING_DIR}/DEBIAN/prerm"

# DEBIAN/postrm
cat > "${STAGING_DIR}/DEBIAN/postrm" <<'EOF'
#!/bin/bash
set -e

rm -f /usr/local/bin/open-agc

exit 0
EOF
chmod 755 "${STAGING_DIR}/DEBIAN/postrm"

# Desktop entry
cat > "${STAGING_DIR}/usr/share/applications/${PKG_NAME}.desktop" <<EOF
[Desktop Entry]
Name=Open-AGC
Comment=Open-AGC — AI agent desktop application
Exec=/opt/${PKG_NAME}/${APP_NAME}
Icon=${PKG_NAME}
Terminal=false
Type=Application
Categories=Utility;Development;
StartupNotify=true
EOF

# Icon
cp "static/icon_rounded.png" "${STAGING_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"

echo "  ✅ Staged at ${STAGING_DIR}"

# ---- 4. Build the .deb package ----
echo "[4/5] Building .deb package..."

rm -f "dist/${DEB_NAME}"
dpkg-deb --build --root-owner-group "${STAGING_DIR}" "dist/${DEB_NAME}"

echo "  ✅ Package created: dist/${DEB_NAME}"

# ---- 5. Clean up ----
echo "[5/5] Cleaning up..."
rm -rf "${STAGING_ROOT}"
rm -rf "build/linux"

echo ""
echo "============================================="
echo "  ✅ Build complete!"
echo "  📦 dist/${DEB_NAME}"
echo "============================================="
echo ""
echo "To install:  sudo dpkg -i dist/${DEB_NAME}"
echo "             (or: sudo apt install ./dist/${DEB_NAME})"
echo "To run:      open-agc  (or find Open-AGC in the app menu)"
