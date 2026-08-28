# ARM64 deb CI 自动构建设计

## 背景与问题

- 当前 `build_deb.sh` 已支持 `amd64` / `arm64`，但 **PyInstaller 不能交叉编译**。
- `.github/workflows/docker-release.yml` 的 `linux-deb` job 只在 `ubuntu-22.04`（x86_64）上运行，matrix 只有 `amd64`，因此 Release 只产出 `amd64` deb。
- UOS / 银河麒麟桌面版（基于 Debian/Ubuntu）在麒麟芯片（ARM64/aarch64）上安装 `amd64` deb 会报「软件包架构不匹配」。
- 目标：Release 流水线同时产出 `amd64` 和 `arm64` 两个 deb，无需用户自备 ARM 构建机。

## 方案选择

采用 **GitHub ARM64 runner**（方案 A）：

- `amd64` 仍跑在 `ubuntu-22.04`（保持与现有 `libwebkit2gtk-4.0-37` 依赖一致）。
- `arm64` 跑在 `ubuntu-22.04-arm`（GitHub ARM64 runner）上，PyInstaller 原生编译出 arm64 二进制。

备选方案（QEMU 模拟）暂不启用；若 ARM runner 调度失败再退到 QEMU。

## 设计变更

### 1. CI workflow 修改（`.github/workflows/docker-release.yml`）

- `linux-deb` job 的 matrix 从 `arch: [amd64]` 扩展为：
  ```yaml
  strategy:
    matrix:
      include:
        - { arch: amd64, runner: ubuntu-22.04 }
        - { arch: arm64, runner: ubuntu-22.04-arm }
  runs-on: ${{ matrix.runner }}
  ```
- 两个架构共用同一套构建步骤（前端构建、Python 依赖、PyInstaller、deb 组装、`dpkg-deb`）。
- `DEBIAN/control` 的 `Architecture:` 使用 `${{ matrix.arch }}`。
- Release 同时上传：
  - `dist/Open-AGC-<VERSION>-Linux-amd64.deb`
  - `dist/Open-AGC-<VERSION>-Linux-arm64.deb`

### 2. 本地脚本保留

- `build_deb.sh arm64` 保留，作为在 ARM64 UOS/麒麟机器上手动打包的备用路径。

### 3. 文档

- README 或打包说明中补充：UOS/银河麒麟（ARM64）请下载 `Linux-arm64.deb`；x86_64 机器请下载 `Linux-amd64.deb`。

## 依赖与约束

- arm64 deb 的 `Depends` 仍为 `libwebkit2gtk-4.0-37, libgtk-3-0`；Ubuntu 22.04 ARM 仓库提供 arm64 版本。
- 银河麒麟服务器版（RPM 系）不在本次范围内，如需支持再单独评估 `rpmbuild`。
- 不改动现有 Windows/macOS 构建 job。

## 测试与验证

- 本地无法直接验证 arm64 构建（无 ARM 机器）；通过以下方式降低风险：
  - workflow YAML 语法检查。
  - `amd64` 构建路径保持不变，确保原有 x86_64 deb 不受影响。
  - `ubuntu-22.04-arm` runner 的构建步骤与 `amd64` 完全一致，仅 runner 与 `Architecture` 字段不同。
- 推送到 `release` 分支后观察 CI 是否同时产出两个 deb，并在 UOS/麒麟 ARM64 机器上安装验证。

## 风险

- GitHub ARM64 runner 对私有仓库可能需要付费/企业版；若调度不到，job 会排队或失败。届时切换到 QEMU 方案（`docker/setup-qemu-action` + `arm64v8/ubuntu:22.04` 容器）。
- ARM64 下某些 Python 依赖（如 `cryptography`、`numpy`）需使用 manylinux aarch64 wheel；若 PyPI 无对应 wheel，构建会失败，需要额外编译依赖。
