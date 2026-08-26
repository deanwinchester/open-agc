# Linux/UOS deb 打包与 Release CI 自动构建实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Open-AGC 补充 Linux/UOS deb 打包能力，并在推送到 release 分支时通过 GitHub Actions 自动构建。

**Architecture:** 新增 `build_deb.sh` 本地构建脚本（Linux 环境下运行），产出标准 deb 目录结构后用 `dpkg-deb` 打包；在现有 `.github/workflows/docker-release.yml` 中追加 `linux-deb` job，使用 `ubuntu-latest` runner 自动完成 PyInstaller 构建与 deb 组装，并上传到 GitHub Release。

**Tech Stack:** PyInstaller, dpkg-deb, GitHub Actions, bash

## Global Constraints

- 不修改现有 Windows/macOS 构建脚本与 CI job
- deb 包必须支持 amd64；arm64 通过 CI matrix 扩展（如runner支持）
- 包名：`open-agc`，安装路径 `/opt/open-agc/`
- 桌面快捷方式：`/usr/share/applications/open-agc.desktop`
- 图标：`/usr/share/icons/hicolor/256x256/apps/open-agc.png`
- 系统依赖：`libwebkit2gtk-4.0-37`, `libgtk-3-0`（pywebview Linux 后端）
- 版本号从 `VERSION` 文件读取

---

### Task 1: 创建 deb 打包脚本 `build_deb.sh`

**Files:**
- Create: `build_deb.sh`

**Interfaces:**
- Produces: `dist/Open-AGC-<VERSION>-Linux-<ARCH>.deb`

- [ ] **Step 1: 编写脚本**

```bash
#!/bin/bash
# ===========================================
#  Build Open-AGC deb package for Linux/UOS
#  Usage: ./build_deb.sh [amd64|arm64]
# ===========================================

set -e

APP_NAME="Open-AGC"
PKG_NAME="open-agc"

# Read VERSION from file
if [ -f VERSION ]; then
    VERSION=$(cat VERSION)
else
    VERSION="0.0.0"
fi

# Architecture: default to current
ARCH="${1:-$(uname -m)}"
case "$ARCH" in
    x86_64) DEB_ARCH="amd64" ;;
    aarch64) DEB_ARCH="arm64" ;;
    *) echo "ERROR: Unsupported architecture: $ARCH"; exit 1 ;;
esac

echo "============================================="
echo "  Building ${APP_NAME} v${VERSION} for Linux (${DEB_ARCH})"
echo "============================================="

cd "$(dirname "$0")"

# ---- 1. Build frontend ----
echo "[1/5] Building frontend..."
if command -v npm &> /dev/null; then
    [ ! -d "node_modules/@vitejs/plugin-vue" ] && npm install
    npm run build
else
    echo "ERROR: npm not found — frontend build required for packaging!"
    exit 1
fi

# ---- 2. Build with PyInstaller ----
echo "[2/5] Building with PyInstaller..."
if [ ! -d "build_venv" ]; then
    python3 -m venv build_venv
fi
source build_venv/bin/activate
pip install --upgrade pip -q
pip install pyinstaller -q
pip install -r requirements.txt -q
pip install pywebview -q

pyinstaller open_agc.spec --clean --noconfirm \
    --distpath "dist/linux" \
    --workpath "build/linux"

# ---- 3. Assemble deb directory ----
echo "[3/5] Assembling deb directory..."
DEB_DIR="dist/deb_staging/${PKG_NAME}_${VERSION}_${DEB_ARCH}"
rm -rf "${DEB_DIR}"
mkdir -p "${DEB_DIR}/DEBIAN"
mkdir -p "${DEB_DIR}/opt/${PKG_NAME}"
mkdir -p "${DEB_DIR}/usr/share/applications"
mkdir -p "${DEB_DIR}/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output
cp -R "dist/linux/${APP_NAME}/"* "${DEB_DIR}/opt/${PKG_NAME}/"

# Icon
if [ -f "static/icon_rounded.png" ]; then
    cp "static/icon_rounded.png" "${DEB_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"
elif [ -f "static/icon.png" ]; then
    cp "static/icon.png" "${DEB_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"
fi

# Desktop entry
cat > "${DEB_DIR}/usr/share/applications/${PKG_NAME}.desktop" <<EOF
[Desktop Entry]
Name=Open-AGC
Comment=Open-AGC - General AI Agent
Exec=/opt/${PKG_NAME}/${APP_NAME}
Icon=${PKG_NAME}
Type=Application
Categories=Utility;Development;
Terminal=false
StartupNotify=true
EOF

# ---- 4. DEBIAN control files ----
echo "[4/5] Creating DEBIAN control files..."

# control
cat > "${DEB_DIR}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${DEB_ARCH}
Depends: libwebkit2gtk-4.0-37, libgtk-3-0
Maintainer: Open-AGC <dev@open-agc.local>
Description: Open-AGC - General AI Agent
 Open-AGC is a general-purpose AI agent for daily office, research,
 development and LLM experimentation.
EOF

# postinst
cat > "${DEB_DIR}/DEBIAN/postinst" <<'EOF'
#!/bin/bash
set -e
chmod +x /opt/open-agc/Open-AGC 2>/dev/null || true
ln -sf /opt/open-agc/Open-AGC /usr/local/bin/open-agc 2>/dev/null || true
update-desktop-database /usr/share/applications 2>/dev/null || true
gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
exit 0
EOF
chmod 755 "${DEB_DIR}/DEBIAN/postinst"

# prerm
cat > "${DEB_DIR}/DEBIAN/prerm" <<'EOF'
#!/bin/bash
set -e
pkill -f "/opt/open-agc/Open-AGC" 2>/dev/null || true
exit 0
EOF
chmod 755 "${DEB_DIR}/DEBIAN/prerm"

# postrm
cat > "${DEB_DIR}/DEBIAN/postrm" <<'EOF'
#!/bin/bash
set -e
rm -f /usr/local/bin/open-agc
update-desktop-database /usr/share/applications 2>/dev/null || true
exit 0
EOF
chmod 755 "${DEB_DIR}/DEBIAN/postrm"

# ---- 5. Build deb ----
echo "[5/5] Building deb package..."
DEB_FILE="dist/${APP_NAME}-${VERSION}-Linux-${DEB_ARCH}.deb"
dpkg-deb --build "${DEB_DIR}" "${DEB_FILE}"

# Clean up staging
rm -rf "dist/deb_staging"
rm -rf "build/linux"

echo ""
echo "============================================="
echo "  Build complete!"
echo "  Package: ${DEB_FILE}"
echo "============================================="
echo ""
echo "To install: sudo apt install ${DEB_FILE}"
echo "To run: open-agc or find Open-AGC in applications menu"
```

- [ ] **Step 2: 本地语法检查**

Run: `bash -n build_deb.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add build_deb.sh
git commit -m "feat: 新增 Linux/UOS deb 打包脚本 build_deb.sh"
```

---

### Task 2: 在 CI 中添加 linux-deb 自动构建

**Files:**
- Modify: `.github/workflows/docker-release.yml`

**Interfaces:**
- Consumes: `build_deb.sh`（Task 1）
- Produces: CI 自动上传 `Open-AGC-*-Linux-*.deb` 到 GitHub Release

- [ ] **Step 1: 在 macos job 后追加 linux-deb job**

在 `.github/workflows/docker-release.yml` 的 `macos:` job 结束后（`Upload to Release` step 之后），追加以下内容：

```yaml
  # ── Linux deb ──
  linux-deb:
    needs: create-release
    runs-on: ubuntu-latest
    permissions:
      contents: write
    strategy:
      matrix:
        arch: [amd64]
        # 如需 arm64 且 runner 支持，可扩展为 [amd64, arm64]
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true

      - name: Read VERSION
        id: version
        run: echo "version=$(cat VERSION)" >> $GITHUB_OUTPUT

      - name: Setup Python 3.10
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Setup Node.js
        uses: actions/setup-node@v4
        with:
          node-version: "20"

      - name: Install system dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y libwebkit2gtk-4.0-37 libgtk-3-0 dpkg-dev

      - name: Build frontend
        run: |
          npm install
          npm run build

      - name: Install Python dependencies
        run: |
          python -m venv build_venv
          source build_venv/bin/activate
          pip install --upgrade pip
          pip install pyinstaller
          pip install -r requirements.txt
          pip install pywebview

      - name: Build with PyInstaller
        run: |
          pyinstaller open_agc.spec --clean --noconfirm --distpath dist/linux --workpath build/linux

      - name: Assemble and build deb
        run: |
          ./build_deb.sh ${{ matrix.arch }}

      - name: Upload to Release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: v${{ steps.version.outputs.version }}
          files: dist/Open-AGC-*-Linux-${{ matrix.arch }}.deb
```

注意：`build_deb.sh` 内部会再次构建前端和安装依赖，为保持 CI 步骤清晰，也可在 CI 中直接内联 deb 组装逻辑（不调用 `build_deb.sh`）。推荐**直接内联**，避免双重构建。

**修正方案（推荐）**：不调用 `build_deb.sh`，而是把 deb 组装逻辑直接写在 CI 步骤中：

```yaml
      - name: Assemble deb directory
        run: |
          APP_NAME="Open-AGC"
          PKG_NAME="open-agc"
          VERSION="${{ steps.version.outputs.version }}"
          DEB_ARCH="${{ matrix.arch }}"
          DEB_DIR="dist/deb_staging/${PKG_NAME}_${VERSION}_${DEB_ARCH}"
          rm -rf "${DEB_DIR}"
          mkdir -p "${DEB_DIR}/DEBIAN" "${DEB_DIR}/opt/${PKG_NAME}" \
            "${DEB_DIR}/usr/share/applications" \
            "${DEB_DIR}/usr/share/icons/hicolor/256x256/apps"
          cp -R "dist/linux/${APP_NAME}/"* "${DEB_DIR}/opt/${PKG_NAME}/"
          cp "static/icon_rounded.png" "${DEB_DIR}/usr/share/icons/hicolor/256x256/apps/${PKG_NAME}.png"
          cat > "${DEB_DIR}/usr/share/applications/${PKG_NAME}.desktop" <<EOF
          [Desktop Entry]
          Name=Open-AGC
          Comment=Open-AGC - General AI Agent
          Exec=/opt/${PKG_NAME}/${APP_NAME}
          Icon=${PKG_NAME}
          Type=Application
          Categories=Utility;Development;
          Terminal=false
          StartupNotify=true
          EOF

      - name: Create DEBIAN control files
        run: |
          PKG_NAME="open-agc"
          VERSION="${{ steps.version.outputs.version }}"
          DEB_ARCH="${{ matrix.arch }}"
          DEB_DIR="dist/deb_staging/${PKG_NAME}_${VERSION}_${DEB_ARCH}"
          cat > "${DEB_DIR}/DEBIAN/control" <<EOF
          Package: ${PKG_NAME}
          Version: ${VERSION}
          Section: utils
          Priority: optional
          Architecture: ${DEB_ARCH}
          Depends: libwebkit2gtk-4.0-37, libgtk-3-0
          Maintainer: Open-AGC <dev@open-agc.local>
          Description: Open-AGC - General AI Agent
           Open-AGC is a general-purpose AI agent for daily office, research,
           development and LLM experimentation.
          EOF
          cat > "${DEB_DIR}/DEBIAN/postinst" <<'EOF'
          #!/bin/bash
          set -e
          chmod +x /opt/open-agc/Open-AGC 2>/dev/null || true
          ln -sf /opt/open-agc/Open-AGC /usr/local/bin/open-agc 2>/dev/null || true
          update-desktop-database /usr/share/applications 2>/dev/null || true
          gtk-update-icon-cache /usr/share/icons/hicolor 2>/dev/null || true
          exit 0
          EOF
          chmod 755 "${DEB_DIR}/DEBIAN/postinst"
          cat > "${DEB_DIR}/DEBIAN/prerm" <<'EOF'
          #!/bin/bash
          set -e
          pkill -f "/opt/open-agc/Open-AGC" 2>/dev/null || true
          exit 0
          EOF
          chmod 755 "${DEB_DIR}/DEBIAN/prerm"
          cat > "${DEB_DIR}/DEBIAN/postrm" <<'EOF'
          #!/bin/bash
          set -e
          rm -f /usr/local/bin/open-agc
          update-desktop-database /usr/share/applications 2>/dev/null || true
          exit 0
          EOF
          chmod 755 "${DEB_DIR}/DEBIAN/postrm"

      - name: Build deb package
        run: |
          VERSION="${{ steps.version.outputs.version }}"
          DEB_ARCH="${{ matrix.arch }}"
          DEB_DIR="dist/deb_staging/open-agc_${VERSION}_${DEB_ARCH}"
          DEB_FILE="dist/Open-AGC-${VERSION}-Linux-${DEB_ARCH}.deb"
          dpkg-deb --build "${DEB_DIR}" "${DEB_FILE}"

      - name: Upload to Release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: v${{ steps.version.outputs.version }}
          files: dist/Open-AGC-*-Linux-*.deb
```

- [ ] **Step 2: 验证 YAML 语法**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/docker-release.yml'))"`
Expected: 无异常

- [ ] **Step 3: 提交**

```bash
git add .github/workflows/docker-release.yml
git commit -m "ci: release 分支新增 Linux deb 自动构建与上传"
```

---

## Self-Review

1. **Spec coverage**：
   - ✅ `build_deb.sh` 本地构建脚本（Task 1）
   - ✅ release CI 自动构建（Task 2）
   - ✅ 不破坏现有 Windows/macOS CI
   - ✅ deb 结构符合 Debian/UOS 标准

2. **Placeholder scan**：无 TBD/TODO，所有代码完整。

3. **Type consistency**：
   - `APP_NAME="Open-AGC"`、`PKG_NAME="open-agc"` 在 Task 1 与 Task 2 中一致。
   - `VERSION` 从文件读取，与现有 CI 一致。
   - `DEB_ARCH` 映射 `x86_64→amd64`、`aarch64→arm64`。

4. **修正说明**：Task 2 原始步骤中调用 `build_deb.sh` 会导致前端二次构建，已修正为 CI 内联 deb 组装逻辑。`build_deb.sh` 仍保留给开发者在本地 Linux 环境手动构建。
