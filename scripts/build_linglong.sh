#!/bin/bash
# ===========================================
#  Build Open-AGC Linglong (.uab) package
#  玲珑（如意玲珑）打包脚本
#
#  Usage:
#    ./scripts/build_linglong.sh           # 先构建 deb，再转玲珑包
#
#  Env overrides:
#    APP_ID    玲珑应用 id（默认 com.openagc.panda）
#    APP_NAME  玲珑应用名（默认 Open-AGC）
#    LL_BASE   玲珑基础环境（默认 org.deepin.base/23.1.0）
#
#  前置要求：
#    - 已安装 ll-builder（UOS/deepin: sudo apt install ll-builder）
#    - 本机能构建 deb（见 build_deb.sh）
#    - 目标机装有玲珑运行时（UOS 20+ 自带；麒麟需先装玲珑环境）
# ===========================================

set -e
trap 'echo "ERROR: linglong build failed at line $LINENO (exit $?)" >&2' ERR

# Navigate to project root
cd "$(dirname "$0")/.."

if [ -f VERSION ]; then
    VERSION=$(cat VERSION | tr -d '[:space:]')
else
    VERSION="0.0.0"
fi

APP_ID="${APP_ID:-com.openagc.panda}"
APP_NAME="${APP_NAME:-Open-AGC}"
LL_BASE="${LL_BASE:-org.deepin.base/23.1.0}"

# 玲珑版本要求四段式：1.0.2rc19 -> 1.0.2.19
LL_VERSION=$(echo "${VERSION}" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+)rc([0-9]+)$/\1.\2/;t;s/[^0-9.]//g')

case "$(uname -m)" in
    x86_64)  BUILD_ARCH="amd64" ;;
    aarch64|arm64) BUILD_ARCH="arm64" ;;
    *) echo "ERROR: Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

DEB_NAME="dist/Open-AGC-${VERSION}-Linux-${BUILD_ARCH}.deb"
PROJECT_DIR="dist/linglong"

echo "============================================="
echo "  🐼 Building Linglong package"
echo "  ${APP_ID} v${LL_VERSION} (${BUILD_ARCH})"
echo "  Base: ${LL_BASE}"
echo "============================================="

# ---- 1. Ensure deb exists ----
if [ ! -f "${DEB_NAME}" ]; then
    echo "[1/4] deb not found, building via build_deb.sh first..."
    ./build_deb.sh
else
    echo "[1/4] Using existing deb: ${DEB_NAME}"
fi

if ! command -v ll-builder >/dev/null 2>&1 \
    && ! { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }; then
    echo "ERROR: 需要 ll-builder 或 docker 之一。ll-builder: sudo apt install linglong-builder"
    exit 1
fi

# ---- 2. Assemble linglong project ----
echo "[2/4] Assembling linglong project at ${PROJECT_DIR}..."
rm -rf "${PROJECT_DIR}"
mkdir -p "${PROJECT_DIR}"
cp "${DEB_NAME}" "${PROJECT_DIR}/open-agc.deb"

sed -e "s|@APP_ID@|${APP_ID}|g" \
    -e "s|@APP_NAME@|${APP_NAME}|g" \
    -e "s|@LL_VERSION@|${LL_VERSION}|g" \
    -e "s|@LL_BASE@|${LL_BASE}|g" \
    packaging/linglong.yaml.template > "${PROJECT_DIR}/linglong.yaml"

# ---- 3. Build ----
echo "[3/4] ll-builder build (base ${LL_BASE} 首次会拉取基础环境，耗时较长)..."
# 优先 docker 构建：宿主 ll-box 在定制内核（如 UOS 5.10-arm64）上起不来
# 容器时（newuidmap 写 uid_map 被拒），docker 内环境干净可控。
# 注意 overlay 工作区放容器本地盘——bind mount 宿主目录时，ll-box 的
# userns 映射 uid 打不开宿主 workdir（Permission denied 生产实证）。
# 无 docker 或 docker 构建失败时回退宿主 ll-builder。
LL_CONTAINER_OK=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    LL_IMAGE="open-agc-llbuild:bookworm"
    if ! docker image inspect "${LL_IMAGE}" >/dev/null 2>&1; then
        echo "  Building llbuild image (debian12 + linglong-builder)..."
        docker build -t "${LL_IMAGE}" -f scripts/docker/Dockerfile.llbuild .
    fi
    docker run --rm --privileged \
        --add-host mirror-repo-linglong.deepin.com:42.56.65.201 \
        -v "$PWD/dist/linglong:/out" \
        "${LL_IMAGE}" bash -c '
            set -e
            rm -rf /tmp/llwork && mkdir -p /tmp/llwork
            cp /out/open-agc.deb /out/linglong.yaml /tmp/llwork/
            cd /tmp/llwork && ll-builder build && ll-builder export
            mkdir -p /out/artifacts
            cp -r linglong /out/ 2>/dev/null || true
            find . -maxdepth 2 -name "*.uab" -o -maxdepth 2 -name "*.layer" | while read f; do cp -r "$f" /out/artifacts/; done
        ' && LL_CONTAINER_OK=1
    # 容器以 root 运行，产物归 root 所有——归还所有权
    sudo chown -R "$(id -u):$(id -g)" dist/linglong 2>/dev/null || true
fi
if [ "${LL_CONTAINER_OK}" != "1" ]; then
    echo "  docker 构建不可用或失败，回退宿主 ll-builder..."
    (cd "${PROJECT_DIR}" && ll-builder build)
    echo "[4/4] Exporting .uab..."
    (cd "${PROJECT_DIR}" && ll-builder export || echo "[warn] ll-builder export 失败，可检查 ${PROJECT_DIR} 下产物")
fi

echo ""
echo "============================================="
echo "  ✅ Linglong build complete!"
echo "  📦 产物目录: ${PROJECT_DIR}"
echo "  安装: ll-cli install ./<产物>.uab"
echo "============================================="
