"""
Auto-upgrade for Docker deployments.

Checks GitHub Releases for a newer version on container start.
If AUTO_UPGRADE=true and a newer version exists, downloads the source code
and upgrades in-place — no Docker socket or image pull required.
The container restarts automatically via Docker's restart policy.
"""
import os
import sys
import json
import logging
import subprocess
import tarfile
import tempfile
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

# Files/dirs to copy from the release tarball to /app
UPGRADE_SOURCES = [
    "core", "tools", "agent", "api", "skills", "plugins", "static",
    "main.py", "launcher.py", "gui_app.py",
    "requirements.txt", "docker-entrypoint.sh", "VERSION",
]

class AutoUpgrader:
    """Check for and optionally perform source-code upgrades."""

    def __init__(self):
        self.app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.current_version: str = get_version()
        self.latest_version: Optional[str] = None

    def fetch_latest_release(self) -> Optional[str]:
        """Query GitHub Releases API for the latest release tag.

        Returns version string (without leading 'v'), or None on failure.
        """
        try:
            resp = requests.get(
                GITHUB_API,
                timeout=GITHUB_API_TIMEOUT,
                headers={"Accept": "application/vnd.github.v3+json"},
            )
            if resp.status_code == 200:
                tag = resp.json().get("tag_name", "").lstrip("v")
                if tag:
                    self.latest_version = tag
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
            return self.latest_version != self.current_version

    def download_and_extract_tarball(self, version: str) -> Optional[str]:
        """Download the release source tarball and extract to temp dir.

        Returns path to the extracted tarball root, or None on failure.
        """
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

            # The tarball extracts to a directory named like "open-agc-{sha}"
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
        """Run pip install to update dependencies."""
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
        """Copy src files into dst, preserving existing files not in src."""
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.isdir(s):
                self._merge_dir(s, d)
            else:
                shutil.copy2(s, d)

    def copy_upgrade_files(self, src_dir: str) -> bool:
        """Copy upgrade files from the extracted tarball to /app.

        Uses merge strategy so Docker build artifacts (static/dist/)
        survive the upgrade — they don't exist in the source tarball.
        """
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

    def persist_upgrade(self, src_dir: str) -> bool:
        """Copy upgraded files to data/upgrade/ for Docker persistence across restarts."""
        try:
            from core.paths import get_data_dir
            upgrade_dir = os.path.join(get_data_dir(), "upgrade")
            shutil.rmtree(upgrade_dir, ignore_errors=True)
            os.makedirs(upgrade_dir, exist_ok=True)
            for name in UPGRADE_SOURCES:
                src = os.path.join(src_dir, name)
                dst = os.path.join(upgrade_dir, name)
                if not os.path.exists(src):
                    continue
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            with open(os.path.join(upgrade_dir, "VERSION"), "w") as f:
                f.write(self.latest_version + "\n")
            logger.info("Upgrade persisted to %s", upgrade_dir)
            return True
        except Exception as e:
            logger.warning("Failed to persist upgrade: %s", e)
            return False

    def check_and_upgrade(self) -> bool:
        """Check for upgrade and perform it if AUTO_UPGRADE is enabled.

        Returns True if upgrade was performed (process exits).
        Returns False if no upgrade was needed or possible.
        """
        if not self.fetch_latest_release():
            logger.info("Open-AGC v%s -- could not check GitHub for updates", self.current_version)
            return False

        if not self.is_upgrade_available():
            logger.info("Open-AGC v%s -- up to date (latest: v%s)", self.current_version, self.latest_version)
            return False

        logger.warning("Upgrade available: v%s -> v%s", self.current_version, self.latest_version)

        auto = os.environ.get("AUTO_UPGRADE", "").lower() in ("1", "true", "yes")
        if not auto:
            logger.info("Set AUTO_UPGRADE=true to enable automatic upgrades")
            return False

        logger.info("Auto-upgrade enabled -- downloading v%s ...", self.latest_version)

        src_dir = self.download_and_extract_tarball(self.latest_version)
        if not src_dir:
            logger.error("Upgrade aborted: could not download release")
            return False

        try:
            if not self.copy_upgrade_files(src_dir):
                logger.error("Upgrade aborted: file copy failed")
                return False

            set_version(self.latest_version)

            # Persist to data/upgrade/ for Docker container restart survival
            self.persist_upgrade(src_dir)

            if not self.install_deps():
                logger.warning("Dependency update had errors -- may need manual fix")
        finally:
            shutil.rmtree(os.path.dirname(src_dir), ignore_errors=True)

        logger.info("Upgrade to v%s complete -- restarting container", self.latest_version)
        return True


def run_auto_upgrade() -> None:
    """Entry point called from docker-entrypoint.sh."""
    logging.basicConfig(
        level=logging.INFO,
        format="[AutoUpgrade] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        upgrader = AutoUpgrader()
        upgraded = upgrader.check_and_upgrade()
        if upgraded:
            logger.info("Upgrade applied — exiting for restart (code 42)")
            sys.exit(42)  # 42 = upgrade applied, entrypoint detects this
    except Exception as e:
        logger.error("Unexpected error during upgrade check: %s", e)
