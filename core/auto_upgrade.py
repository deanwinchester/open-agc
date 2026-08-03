"""
Auto-upgrade with per-deployment-channel strategy.

Checks GitHub Releases for a newer version.
- docker/source: downloads the source tarball and upgrades in-place
  (no Docker socket or image pull required).
- desktop (PyInstaller bundle): downloads the packaged release asset
  (Windows zip / macOS dmg) instead — overwriting source files would be
  a no-op since the code lives inside the binary.

The same module is used by the manual upgrade API.
"""
import os
import sys
import json
import logging
import subprocess
import tarfile
import tempfile
import threading
import zipfile
import shutil
from io import BytesIO
from typing import Optional

import requests
from packaging.version import Version

from core.version import get_version, set_version

logger = logging.getLogger(__name__)

GITHUB_REPO = "deanwinchester/open-agc"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_API_TIMEOUT = 15


def get_channel() -> str:
    """部署形态判定：desktop（打包客户端）/ docker / source。"""
    if getattr(sys, "frozen", False):
        return "desktop"
    if os.environ.get("OPEN_AGC_DOCKER") or os.path.exists("/.dockerenv"):
        return "docker"
    return "source"


def schedule_process_exit(delay: float = 2.0) -> None:
    """延迟退出主进程（守护线程），让响应先送达客户端再重启。"""
    def _exit():
        import time
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()

# Files/dirs to copy from the release tarball
UPGRADE_SOURCES = [
    "core", "tools", "agent", "api", "plugins",
    "static", "skills",
    "main.py", "launcher.py", "gui_app.py",
    "requirements.txt", "docker-entrypoint.sh", "VERSION",
    "package.json", "vue-app",
]


class AutoUpgrader:
    """Check for and optionally perform source-code upgrades."""

    def __init__(self):
        self.app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.current_version: str = get_version()
        self.latest_version: Optional[str] = None
        self.channel: str = get_channel()
        # 最近一次 fetch_latest_release 拿到的 release assets（desktop 升级用）
        self.latest_assets: list = []
        # 升级结果信息，供 API 层回传给前端
        self.last_message: str = ""
        # desktop Windows：已启动 apply_update.bat，主进程即将退出
        self.restart_required: bool = False

    def fetch_latest_release(self) -> Optional[str]:
        """Query GitHub Releases API for the latest release tag."""
        try:
            resp = requests.get(
                GITHUB_API,
                timeout=GITHUB_API_TIMEOUT,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                payload = resp.json()
                tag = payload.get("tag_name", "").lstrip("v")
                if tag:
                    self.latest_version = tag
                    self.latest_assets = payload.get("assets", []) or []
                    return tag
            elif resp.status_code == 403 and "rate limit" in resp.text.lower():
                logger.warning("GitHub API rate limited -- skipping upgrade check")
            else:
                logger.warning("GitHub API returned %d", resp.status_code)
        except requests.RequestException as e:
            logger.warning("Cannot check for upgrades: %s", e)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Failed to parse GitHub release data: %s", e)
        return None

    def is_upgrade_available(self) -> bool:
        if not self.latest_version:
            return False
        try:
            return Version(self.latest_version) > Version(self.current_version)
        except Exception:
            # Fallback: numeric segment comparison for non-PEP440 versions
            def _parse(v):
                return [int(x) for x in v.split('.')]
            try:
                cur = _parse(self.current_version)
                lat = _parse(self.latest_version)
                while len(cur) < len(lat):
                    cur.append(0)
                while len(lat) < len(cur):
                    lat.append(0)
                return lat > cur
            except Exception:
                return self.latest_version != self.current_version

    def download_and_extract_tarball(self, version: str) -> Optional[str]:
        """Download the release source tarball and extract to temp dir."""
        tarball_url = (
            f"https://github.com/{GITHUB_REPO}/archive/refs/tags/v{version}.tar.gz"
        )
        logger.info("Downloading %s ...", tarball_url)
        try:
            resp = requests.get(tarball_url, timeout=120, stream=True)
            if resp.status_code != 200:
                logger.error("Failed to download tarball: HTTP %d", resp.status_code)
                return None

            tmp_dir = tempfile.mkdtemp(prefix="openagc_upgrade_")
            with tarfile.open(fileobj=BytesIO(resp.content), mode="r:gz") as tar:
                tar.extractall(path=tmp_dir)

            extracted_dirs = [
                d for d in os.listdir(tmp_dir)
                if d.startswith("open-agc") and os.path.isdir(os.path.join(tmp_dir, d))
            ]
            if not extracted_dirs:
                logger.error("Tarball did not contain expected directory")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return None

            return os.path.join(tmp_dir, extracted_dirs[0])
        except (requests.RequestException, tarfile.TarError, OSError) as e:
            logger.error("Failed to download/extract tarball: %s", e)
            return None

    def install_deps(self) -> bool:
        req_file = os.path.join(self.app_root, "requirements.txt")
        if not os.path.exists(req_file):
            return True
        logger.info("Updating Python dependencies ...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", req_file],
                capture_output=True, text=True, check=True, timeout=120,
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error("pip install failed: %s", e.stderr[-500:])
            return False
        except subprocess.TimeoutExpired:
            logger.error("pip install timed out")
            return False

    def _merge_dir(self, src: str, dst: str) -> None:
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.isdir(s):
                self._merge_dir(s, d)
            else:
                shutil.copy2(s, d)

    def copy_upgrade_files(self, src_dir: str) -> bool:
        logger.info("Installing v%s files ...", self.latest_version)
        success = True
        for name in UPGRADE_SOURCES:
            src = os.path.join(src_dir, name)
            dst = os.path.join(self.app_root, name)
            if not os.path.exists(src):
                logger.warning("Skipping %s (not in release)", name)
                continue
            try:
                if os.path.isdir(src):
                    self._merge_dir(src, dst)
                else:
                    shutil.copy2(src, dst)
            except OSError as e:
                logger.error("Failed to copy %s: %s", name, e)
                success = False
        return success

    def perform_upgrade(self) -> bool:
        """按部署形态分派升级策略。Returns True on success."""
        if self.channel == "desktop":
            return self._perform_desktop_upgrade()
        return self._perform_source_upgrade()

    # ── desktop 通道：下载打包资产，而非覆盖源码 ──

    @staticmethod
    def _desktop_asset_name(version: str) -> str:
        """CI 产出的 release 资产命名（见 .github/workflows/docker-release.yml）。"""
        if sys.platform == "darwin":
            return f"Open-AGC-{version}-macOS-arm64.dmg"
        return f"Open-AGC-{version}-Windows-x64.zip"

    def _select_asset(self, name: str) -> Optional[dict]:
        for asset in self.latest_assets:
            if asset.get("name") == name:
                return asset
        return None

    def _download_file(self, url: str, dest: str) -> bool:
        logger.info("Downloading %s ...", url)
        try:
            resp = requests.get(url, timeout=300, stream=True)
            if resp.status_code != 200:
                logger.error("Download failed: HTTP %d", resp.status_code)
                return False
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
            return True
        except (requests.RequestException, OSError) as e:
            logger.error("Download failed: %s", e)
            return False

    def _perform_desktop_upgrade(self) -> bool:
        if not self.latest_version:
            if not self.fetch_latest_release():
                self.last_message = "无法查询 GitHub 最新版本"
                logger.error("Cannot check GitHub for latest release")
                return False

        if not self.is_upgrade_available():
            self.last_message = "已是最新版本"
            return False

        if sys.platform == "darwin":
            return self._download_macos_dmg()
        if sys.platform == "win32":
            return self._stage_windows_update()

        self.last_message = f"不支持的桌面平台: {sys.platform}"
        logger.error("Unsupported desktop platform: %s", sys.platform)
        return False

    def _download_macos_dmg(self) -> bool:
        """macOS 桌面端：下载 dmg 到 ~/Downloads，指引用户手动拖装。"""
        asset_name = self._desktop_asset_name(self.latest_version)
        asset = self._select_asset(asset_name)
        if not asset:
            self.last_message = f"Release 中找不到资产 {asset_name}"
            logger.error("Asset %s not found in latest release", asset_name)
            return False
        dest = os.path.join(os.path.expanduser("~"), "Downloads", asset_name)
        if not self._download_file(asset["browser_download_url"], dest):
            self.last_message = f"下载 {asset_name} 失败"
            return False
        self.last_message = (
            f"已下载 {asset_name} 到下载目录，请退出 Open-AGC 后手动安装："
            "打开 dmg 并将新 App 拖入「应用程序」替换旧版本"
        )
        logger.info("DMG downloaded to %s", dest)
        return True

    def _stage_windows_update(self) -> bool:
        """Windows 桌面端：下载 zip 解压到 exe 同级 update_staging/，
        生成 apply_update.bat（等待主进程退出→覆盖程序文件→重启）并启动它。"""
        asset_name = self._desktop_asset_name(self.latest_version)
        asset = self._select_asset(asset_name)
        if not asset:
            self.last_message = f"Release 中找不到资产 {asset_name}"
            logger.error("Asset %s not found in latest release", asset_name)
            return False

        exe_path = os.path.abspath(sys.executable)
        exe_dir = os.path.dirname(exe_path)
        staging = os.path.join(exe_dir, "update_staging")
        zip_path = os.path.join(tempfile.gettempdir(), asset_name)
        try:
            if not self._download_file(asset["browser_download_url"], zip_path):
                self.last_message = f"下载 {asset_name} 失败"
                return False
            if os.path.exists(staging):
                shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(staging)
        except PermissionError as e:
            # 装在 C:\Program Files 等无写权限目录时走到这里
            self.last_message = (
                f"程序目录没有写入权限（{e}）。"
                "请右键「以管理员身份运行」后重试，或将 Open-AGC 安装到用户目录"
            )
            logger.error("Permission denied staging update: %s", e)
            return False
        except (zipfile.BadZipFile, OSError) as e:
            self.last_message = f"解压更新包失败: {e}"
            logger.error("Failed to extract update zip: %s", e)
            return False
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass

        # 解压后必须能定位 Open-AGC.exe（兼容平铺与嵌套一层两种 zip 布局），
        # 否则 robocopy 不会覆盖真正的程序文件，升级会静默无效
        payload = self._locate_payload(staging)
        if not payload:
            self.last_message = f"更新包 {asset_name} 内容异常：解压后找不到 Open-AGC.exe"
            logger.error("Update payload invalid: Open-AGC.exe not found under %s", staging)
            shutil.rmtree(staging, ignore_errors=True)
            return False

        bat_path = self._write_apply_update_bat(exe_dir, exe_path, staging, payload)
        if not bat_path:
            return False

        self._launch_updater(bat_path)
        self.restart_required = True
        self.last_message = "更新已就绪，程序即将自动重启"
        logger.info("Update staged in %s; apply_update.bat launched", staging)
        return True

    @staticmethod
    def _locate_payload(staging: str) -> Optional[str]:
        """在解压目录中定位含 Open-AGC.exe 的有效载荷目录。
        兼容平铺（exe 在根）与嵌套一层（单一顶层目录内有 exe）两种布局。"""
        if os.path.exists(os.path.join(staging, "Open-AGC.exe")):
            return staging
        try:
            entries = os.listdir(staging)
        except OSError:
            return None
        if len(entries) == 1:
            nested = os.path.join(staging, entries[0])
            if os.path.isdir(nested) and os.path.exists(os.path.join(nested, "Open-AGC.exe")):
                return nested
        return None

    def _write_apply_update_bat(self, exe_dir: str, exe_path: str, staging: str, payload: str) -> Optional[str]:
        bat_path = os.path.join(exe_dir, "apply_update.bat")
        log_path = os.path.join(exe_dir, "apply_update_error.log")
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f"set \"PID={os.getpid()}\"\r\n"
            f"set \"SRC={payload}\"\r\n"
            f"set \"STAGING={staging}\"\r\n"
            f"set \"DST={exe_dir}\"\r\n"
            f"set \"EXE={exe_path}\"\r\n"
            f"set \"LOG={log_path}\"\r\n"
            "rem Wait for the main process to exit, then overwrite program files\r\n"
            "timeout /t 3 /nobreak >nul\r\n"
            ":wait_loop\r\n"
            "tasklist /FI \"PID eq %PID%\" | find \"%PID%\" >nul\r\n"
            "if %errorlevel% equ 0 (\r\n"
            "    timeout /t 1 /nobreak >nul\r\n"
            "    goto wait_loop\r\n"
            ")\r\n"
            "rem /R:3 /W:2 - default /R:1000000 would hang forever on locked files\r\n"
            "robocopy \"%SRC%\" \"%DST%\" /E /IS /IT /R:3 /W:2 /NFL /NDL /NJH /NJS >nul\r\n"
            "set RC=%errorlevel%\r\n"
            "rem robocopy exit codes 0-7 are success levels; >=8 means failure\r\n"
            "if %RC% leq 7 goto :copy_ok\r\n"
            "(\r\n"
            "echo Update failed: robocopy exit code %RC%.\r\n"
            "echo Some program files may be locked by another process.\r\n"
            "echo The downloaded update is kept at: %STAGING%\r\n"
            "echo Close all Open-AGC processes, then re-run apply_update.bat manually.\r\n"
            ") > \"%LOG%\"\r\n"
            "rem On failure: keep staging and this script for manual retry; do not restart.\r\n"
            "exit /b %RC%\r\n"
            ":copy_ok\r\n"
            "rmdir /s /q \"%STAGING%\" >nul 2>&1\r\n"
            "start \"\" \"%EXE%\"\r\n"
            "del \"%~f0\" >nul 2>&1\r\n"
        )
        # cmd.exe 按系统 ANSI 代码页读取 .bat；路径含代码页无法表示的字符时
        # 静默 replace 会损坏路径 —— 改为预先 strict 校验并如实报错。
        # （mbcs 编解码器仅 Windows 存在；非 Windows 仅在测试中走到这里，用 ascii 即可）
        bat_encoding = "mbcs" if sys.platform == "win32" else "ascii"
        try:
            content.encode(bat_encoding)
        except UnicodeEncodeError:
            self.last_message = (
                "安装路径包含当前系统代码页无法表示的字符，无法生成更新脚本；"
                "请将 Open-AGC 安装到纯英文路径后重试"
            )
            logger.error("Cannot %s-encode apply_update.bat content (install path)", bat_encoding)
            return None
        try:
            with open(bat_path, "w", encoding=bat_encoding, newline="") as f:
                f.write(content)
            return bat_path
        except PermissionError as e:
            self.last_message = (
                f"程序目录没有写入权限（{e}）。"
                "请右键「以管理员身份运行」后重试，或将 Open-AGC 安装到用户目录"
            )
            logger.error("Permission denied writing %s: %s", bat_path, e)
            return None
        except OSError as e:
            self.last_message = f"无法写入 {bat_path}: {e}"
            logger.error("Failed to write apply_update.bat: %s", e)
            return None

    def _launch_updater(self, bat_path: str) -> None:
        """启动 apply_update.bat（脱离当前进程），随后主进程延迟退出。"""
        subprocess.Popen(
            ["cmd", "/c", bat_path],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        schedule_process_exit()

    # ── docker/source 通道：源码 tarball 就地升级 ──

    def _perform_source_upgrade(self) -> bool:
        """Download latest release and apply it. Returns True on success."""
        if not self.latest_version:
            if not self.fetch_latest_release():
                logger.error("Cannot check GitHub for latest release")
                return False

        if not self.is_upgrade_available():
            return False

        logger.info("Upgrading v%s -> v%s ...", self.current_version, self.latest_version)

        src_dir = self.download_and_extract_tarball(self.latest_version)
        if not src_dir:
            logger.error("Upgrade aborted: download failed")
            return False

        try:
            if not self.copy_upgrade_files(src_dir):
                logger.error("Upgrade aborted: file copy failed")
                return False

            set_version(self.latest_version)

            # Rebuild the frontend Vite bundle so updated static/ source files
            # produce a matching dist/ bundle.
            try:
                npm_dir = self.app_root
                package_json = os.path.join(npm_dir, "package.json")
                if os.path.exists(package_json):
                    if subprocess.run(["npm", "--version"], capture_output=True, timeout=10).returncode == 0:
                        logger.info("Rebuilding frontend (npm run build)...")
                        subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                                       cwd=npm_dir, capture_output=True, timeout=120)
                        build = subprocess.run(["npm", "run", "build"],
                                                cwd=npm_dir, capture_output=True, text=True, timeout=120)
                        if build.returncode == 0:
                            logger.info("Frontend rebuilt successfully")
                        else:
                            logger.warning("Frontend build failed: %s", build.stderr[-500:])
                    else:
                        logger.warning("npm not available, frontend may be stale")
            except Exception as _fe:
                logger.warning("Frontend rebuild skipped: %s", _fe)

            if not self.install_deps():
                logger.warning("Dependency update may need manual fix")
        finally:
            shutil.rmtree(os.path.dirname(src_dir), ignore_errors=True)

        logger.info("Upgrade to v%s complete", self.latest_version)
        return True


def run_auto_upgrade() -> None:
    """Entry point for auto-upgrade (used by Docker entrypoint)."""
    logging.basicConfig(
        level=logging.INFO,
        format="[AutoUpgrade] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        upgrader = AutoUpgrader()
        if not upgrader.fetch_latest_release():
            logger.info("Could not check GitHub for updates")
            return
        if not upgrader.is_upgrade_available():
            logger.info("Up to date (v%s)", upgrader.current_version)
            return
        logger.info("Upgrade available: v%s -> v%s", upgrader.current_version, upgrader.latest_version)
        if upgrader.perform_upgrade():
            logger.info("Upgrade applied — continue starting services")
    except Exception as e:
        logger.error("Unexpected error: %s", e)
