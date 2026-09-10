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
# 失败时打出行号，避免 set -e 静默退出（生产实证：前端构建完就没下文，
# 实际是后续某步失败但无任何提示）
trap 'echo "ERROR: build failed at line $LINENO (exit $?)" >&2' ERR

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
    echo "ERROR: requested arch (${BUILD_ARCH}) differs from host ($(host_arch))."
    echo "       PyInstaller cannot cross-compile — please build on a ${BUILD_ARCH} host."
    exit 1
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
# CI 场景可在宿主预构建前端（产物与架构无关）后设 AGC_SKIP_FRONTEND=1 跳过——
# arm64 容器全程 QEMU 模拟，在里面跑 npm/vite 既慢又有兼容风险。
if [ "${AGC_SKIP_FRONTEND}" = "1" ]; then
    echo "  AGC_SKIP_FRONTEND=1，跳过前端构建（假定已在宿主完成）"
else
echo "  Building frontend with Vite..."

# Resolve Node.js：优先本地便携 .node/bin（与 start.sh 同一套），再查 PATH，
# 都没有（或版本 < 18，vite 构建需要）则下载便携 Node.js 到 .node/。
# 此前只查存在性——GitLab runner 宿主自带 node 16，vite 直接报错（生产实证）。
_node_ok() {  # $1 = node 二进制路径；可执行且主版本 >= 18
    [ -x "$1" ] || return 1
    local major
    major=$("$1" -e 'process.stdout.write(String(process.versions.node.split(".")[0]))' 2>/dev/null) || return 1
    [ "${major:-0}" -ge 18 ] 2>/dev/null
}
if [ -f ".node/bin/npm" ]; then
    if _node_ok .node/bin/node; then
        export PATH="$PWD/.node/bin:$PATH"
    else
        echo "  Existing .node/ unusable (wrong arch or node < 18), re-downloading..."
        rm -rf .node
    fi
fi
if ! command -v npm &> /dev/null || ! _node_ok "$(command -v node 2>/dev/null)"; then
    command -v npm &> /dev/null && echo "  PATH node < 18 ($(node --version))，改用便携 Node.js..."
    [ ! -d .node ] && echo "  Downloading portable Node.js to .node/..."
    mkdir -p .node
    NODE_ARCH="linux-x64"
    case "$(uname -m)" in
        aarch64) NODE_ARCH="linux-arm64" ;;
        armv7l)  NODE_ARCH="linux-armv7l" ;;
    esac
    echo "  Architecture: $(uname -m) -> ${NODE_ARCH}"
    NODE_URL="https://nodejs.org/dist/v22.14.0/node-v22.14.0-${NODE_ARCH}.tar.xz"
    if command -v curl &> /dev/null; then
        curl -fL --progress-bar "$NODE_URL" -o /tmp/node-agc.tar.xz || { echo "  ERROR: Node.js download failed"; exit 1; }
    elif command -v wget &> /dev/null; then
        wget --show-progress "$NODE_URL" -O /tmp/node-agc.tar.xz || { echo "  ERROR: Node.js download failed"; exit 1; }
    else
        echo "  ERROR: neither curl nor wget found. Install Node.js manually: https://nodejs.org/"
        exit 1
    fi
    tar -xf /tmp/node-agc.tar.xz -C .node --strip-components=1 || { echo "  ERROR: extract failed"; rm -f /tmp/node-agc.tar.xz; exit 1; }
    rm -f /tmp/node-agc.tar.xz
    if ! .node/bin/node --version &>/dev/null; then
        echo "  ERROR: downloaded Node.js not executable. Install manually: https://nodejs.org/"
        rm -rf .node
        exit 1
    fi
    export PATH="$PWD/.node/bin:$PATH"
fi

if command -v npm &> /dev/null; then
    [ ! -d "node_modules/@vitejs/plugin-vue" ] && npm install
    npm run build
else
    echo "  ERROR: npm not found — frontend build required for packaging!"
    echo "  Please install Node.js from https://nodejs.org/"
    exit 1
fi
fi  # AGC_SKIP_FRONTEND

# ---- 2. Build with PyInstaller ----
# 有 docker 时优先在 buster 容器（glibc 2.28 / glib 2.58）内构建二进制：
# 基线比所有目标发行版都老 → 产物在麒麟 V10(glib 2.64)/UOS(2.66)/新 Ubuntu
# 上都能跑（向前兼容）。本机直接构建时 PyGObject 按构建机的 glib 解析，
# 装到 glib 更老的机器上会 GI 解析失败 → ffi 空调用段错误（麒麟生产实证）。
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "[2/5] Building application with PyInstaller (in buster container)..."
    case "${BUILD_ARCH}" in
        amd64) PYI_IMAGE="python:3.10-buster" ;;
        arm64) PYI_IMAGE="arm64v8/python:3.10-buster" ;;
    esac
    # CI/离线环境可用 AGC_PYI_IMAGE 覆盖构建镜像（如经加速站 retag 的镜像、
    # 内置 qemu-aarch64-static 的 arm64 镜像）。默认行为不变。
    PYI_IMAGE="${AGC_PYI_IMAGE:-${PYI_IMAGE}}"
    echo "  Image: ${PYI_IMAGE}"
    # pip 缓存可挂到宿主目录（CI 场景避免每次重下 ~1-2GB 依赖）
    PIP_CACHE_ARGS=()
    if [ -n "${AGC_PIP_CACHE_DIR}" ]; then
        mkdir -p "${AGC_PIP_CACHE_DIR}"
        PIP_CACHE_ARGS=(-v "${AGC_PIP_CACHE_DIR}":/pipcache -e PIP_CACHE_DIR=/pipcache)
    fi
    docker run --rm ${AGC_DOCKER_PLATFORM:+--platform "${AGC_DOCKER_PLATFORM}"} \
        -v "$PWD":/src -w /src "${PIP_CACHE_ARGS[@]}" "${PYI_IMAGE}" bash -c "
        set -e
        echo 'deb http://archive.debian.org/debian buster main' > /etc/apt/sources.list
        echo 'deb http://archive.debian.org/debian-security buster/updates main' >> /etc/apt/sources.list
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            pkg-config libgirepository1.0-dev libcairo2-dev \
            gir1.2-gtk-3.0 gir1.2-webkit2-4.0 libwebkit2gtk-4.0-dev
        python -m venv /tmp/build_venv
        source /tmp/build_venv/bin/activate
        pip install --upgrade pip -q
        pip install pyinstaller -q
        pip install -r requirements.txt -q
        pip install pywebview -q
        pip install 'PyGObject==3.42.2'
        python -c 'import gi; gi.require_version(\"WebKit2\", \"4.0\")'
        export TARGET_ARCH=\"${PYI_ARCH}\"
        pyinstaller open_agc.spec --clean --noconfirm --distpath dist/linux --workpath build/linux
    "
    # 容器以 root 运行，产物归 root 所有——归还所有权给构建用户
    sudo chown -R "$(id -u):$(id -g)" dist build 2>/dev/null || true
    if [ ! -f "dist/linux/${APP_NAME}/${APP_NAME}" ]; then
        echo "  ERROR: 容器内 PyInstaller 构建失败 — dist/linux/${APP_NAME}/${APP_NAME} not found!"
        exit 1
    fi
    echo "  ✅ Build complete (buster container): dist/linux/${APP_NAME}/"

else
    echo "[2/5] Building application with PyInstaller (host fallback)..."

    # Python 解析与 start.sh 同一套思路：优先项目本地 .python/（便携 Python，
    # start.sh 在缺 Python 的机器上会自动下载到这里），再按版本找系统
    # python3.13→3.10（UOS 默认 python3 是 3.7，requirements 全部解析失败——
    # 生产实证 zxs 机器 pip 报 Requires-Python >=3.8 整屏跳过）。
    PYTHON_BIN=""
    for cmd in .python/bin/python3 .python/bin/python \
               python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" >/dev/null 2>&1 || [ -x "$cmd" ]; then
            if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
                PYTHON_BIN="$cmd"
                break
            fi
        fi
    done
    if [ -z "$PYTHON_BIN" ]; then
        echo "ERROR: 未找到 Python >= 3.10。"
        echo "       可先运行一次 ./start.sh（会自动下载便携 Python 到 .python/），"
        echo "       或手动安装：sudo apt install python3.10 python3.10-venv"
        exit 1
    fi
    echo "  Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

    # build_venv 可能是其他平台残留（Windows 的 Scripts/ 布局没有 bin/activate）
    # 或旧 Python / 其他架构建的——这些情况都重建，否则 source/pip 在 set -e 下
    # 静默失败。架构对比必须有：QEMU 环境（binfmt F 标志）下异架构 python 也能
    # 执行，仅靠「能否运行」判断会漏判（GitLab CI amd64/arm64 共用工作区实证）。
    if [ ! -f "build_venv/bin/activate" ]; then
        [ -d "build_venv" ] && echo "  build_venv 非本机平台布局，重建..."
        rm -rf build_venv
        "$PYTHON_BIN" -m venv build_venv
    else
        VENV_INFO=$(build_venv/bin/python -c 'import sys, platform; print(sys.version_info >= (3, 10), platform.machine())' 2>/dev/null)
        HOST_MACHINE=$("$PYTHON_BIN" -c 'import platform; print(platform.machine())' 2>/dev/null)
        if [ "${VENV_INFO}" != "(True, ${HOST_MACHINE})" ]; then
            echo "  build_venv 版本/架构不符（${VENV_INFO:-不可执行} vs ${HOST_MACHINE}），用 $PYTHON_BIN 重建..."
            rm -rf build_venv
            "$PYTHON_BIN" -m venv build_venv
        fi
    fi
    source build_venv/bin/activate

    # pip 失败原因不能吞（2>/dev/null 会把网络/依赖错误全藏掉，只剩一行号）
    pip install --upgrade pip -q
    pip install pyinstaller -q
    pip install -r requirements.txt -q
    pip install pywebview -q || true

    # ---- GTK 原生窗口依赖（与 CI 的 linux-deb job 对齐）----
    # pywebview 的 GTK 后端需要 PyGObject（gi）+ 系统 GIR/WebKit 开发包，
    # 否则 PyInstaller 收集不到 gi，冻结应用 import webview 失败 → 静默回退
    # 浏览器模式（生产实证：zxs 机器全新 venv 打出的包默认进浏览器）。
    # PyGObject 无 wheel 只能源码编译：需要 gcc/make（build-essential）、
    # pkg-config、libgirepository/libffi/libcairo 开发头文件。
    if ! python -c "import gi" 2>/dev/null; then
        echo "  PyGObject 不可用，准备 GTK 后端依赖..."
        if ! pkg-config --exists girepository-1.0 2>/dev/null; then
            echo "  安装系统依赖（需要 sudo）：build-essential libgirepository1.0-dev 等"
            sudo apt-get install -y --no-install-recommends \
                build-essential pkg-config libgirepository1.0-dev \
                libffi-dev libcairo2-dev \
                gir1.2-gtk-3.0 gir1.2-webkit2-4.0 || \
                echo "  [warn] apt 安装失败"
        fi
        # 不带 -q：源码编译失败的报错必须可见。
        # 版本钉 3.42.2：3.46+ 要求 gobject-introspection>=1.64，而 UOS/buster
        # 只有 1.58；3.40–3.44.x 要求 >=1.56。其中 3.42.2 在 UOS ARM64 +
        # Python 3.12 上生产实证可用（3.44.1 虽能编译但打出的包白屏）
        pip install 'PyGObject==3.42.2'
    fi
    # 硬性卡口：gi 不可用的包等于没有原生窗口，直接判构建失败
    if ! python -c "import gi" 2>/dev/null; then
        echo "ERROR: PyGObject (gi) 仍不可用——打出的包将无法使用原生窗口。真实报错："
        python -c "import gi" || true
        echo "       请把上方报错发出来排查。"
        exit 1
    fi

    export TARGET_ARCH="${PYI_ARCH}"

    pyinstaller open_agc.spec --clean --noconfirm \
        --distpath "dist/linux" \
        --workpath "build/linux"

    if [ ! -f "dist/linux/${APP_NAME}/${APP_NAME}" ]; then
        echo "  ERROR: PyInstaller build failed — dist/linux/${APP_NAME}/${APP_NAME} not found!"
        exit 1
    fi

    echo "  ✅ Build complete (host): dist/linux/${APP_NAME}/"
fi

# ---- 3. Assemble deb staging directory ----
echo "[3/5] Assembling deb directory structure..."

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}/DEBIAN"
mkdir -p "${STAGING_DIR}/opt/${PKG_NAME}"
mkdir -p "${STAGING_DIR}/usr/bin"
mkdir -p "${STAGING_DIR}/usr/share/applications"
mkdir -p "${STAGING_DIR}/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output
cp -R "dist/linux/${APP_NAME}/." "${STAGING_DIR}/opt/${PKG_NAME}/"
chmod +x "${STAGING_DIR}/opt/${PKG_NAME}/${APP_NAME}"

# Relative symlink so `open-agc` is on PATH (Debian policy: no /usr/local writes from maintainer scripts)
ln -s "../../opt/${PKG_NAME}/${APP_NAME}" "${STAGING_DIR}/usr/bin/${PKG_NAME}"

# DEBIAN/control
cat > "${STAGING_DIR}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${BUILD_ARCH}
Depends: libwebkit2gtk-4.0-37, libgtk-3-0, libgirepository-1.0-1, gir1.2-webkit2-4.0, gir1.2-gtk-3.0
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

# /usr/bin/open-agc 是包文件，由 dpkg 在 remove/purge 时自动删除；
# 不要在这里 rm —— 升级顺序（旧 postrm upgrade 在新文件解包之后）
# 会误删新包装好的符号链接。

if command -v update-desktop-database > /dev/null 2>&1; then
    update-desktop-database /usr/share/applications > /dev/null 2>&1 || true
fi

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

# Icon (resize to 256x256 so gtk-update-icon-cache picks it up from the hicolor/256x256 dir)
# 需要 PIL：容器构建模式下宿主 python 可能是系统老版本（无 Pillow），
# 此时复用构建镜像在容器里跑；本机构建模式下用已装依赖的 venv python。
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker run --rm -v "$PWD":/src -w /src "${PYI_IMAGE:-python:3.10-buster}" bash -c "
        pip install pillow -q &&
        python -c \"
from PIL import Image
img = Image.open('static/icon_rounded.png').convert('RGBA')
img = img.resize((256, 256), Image.LANCZOS)
img.save('${STAGING_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png')
\""
    sudo chown -R "$(id -u):$(id -g)" "${STAGING_DIR}" 2>/dev/null || true
else
    python -c "
from PIL import Image
img = Image.open('static/icon_rounded.png').convert('RGBA')
img = img.resize((256, 256), Image.LANCZOS)
img.save('${STAGING_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png')
"
fi

echo "  ✅ Staged at ${STAGING_DIR}"

# ---- 4. Build the .deb package ----
echo "[4/5] Building .deb package..."

rm -f "dist/${DEB_NAME}"
# 用 xz 压缩：新版 dpkg-deb 默认 zstd，UOS/旧版 dpkg 不支持会报
# 「对成员 control.tar.zst 使用了未知的压缩」。xz 在 Debian/UOS 上普遍可用。
dpkg-deb --build -Zxz --root-owner-group "${STAGING_DIR}" "dist/${DEB_NAME}"

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
