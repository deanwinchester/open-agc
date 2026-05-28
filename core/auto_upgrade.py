"""
Auto-upgrade for Docker deployments.

Checks GitHub Releases for a newer version on container start.
If AUTO_UPGRADE=true and a newer version exists, pulls the new image
and recreates the container via Docker socket.
"""
import os
import sys
import json
import logging
import subprocess
from typing import Optional

import requests
from packaging.version import Version

from core.version import get_version, set_version

logger = logging.getLogger(__name__)

GITHUB_REPO = "deanwinchester/open-agc"
GHCR_NAMESPACE = "ghcr.io/deanwinchester/open-agc"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_API_TIMEOUT = 15


class AutoUpgrader:
    """Check for and optionally perform Docker image upgrades."""

    def __init__(self):
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
        """Compare current and latest versions."""
        if not self.latest_version:
            return False
        try:
            return Version(self.latest_version) > Version(self.current_version)
        except Exception:
            return self.latest_version > self.current_version

    @staticmethod
    def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run a Docker CLI command."""
        return subprocess.run(
            ["docker"] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

    def pull_image(self, tag: str) -> bool:
        """Pull the specified image tag from GHCR."""
        image = f"{GHCR_NAMESPACE}:{tag}"
        logger.info("Pulling %s ...", image)
        try:
            self._docker("pull", image, timeout=300)
            logger.info("Successfully pulled %s", image)
            return True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            logger.error("Failed to pull image: %s", e)
            return False

    def recreate_container(self) -> bool:
        """Recreate the container via docker compose."""
        compose_file = os.environ.get("COMPOSE_FILE", "/app/docker-compose.yml")

        if os.path.exists(compose_file):
            try:
                logger.info("Recreating container via docker compose ...")
                self._docker(
                    "compose", "-f", compose_file,
                    "up", "-d", "--force-recreate", "--pull", "always",
                    timeout=120,
                )
                return True
            except subprocess.CalledProcessError as e:
                logger.error("Compose up failed: %s", e.stderr)
                return False

        logger.info("No compose file found -- stopping container for restart")
        container = os.environ.get("CONTAINER_NAME", "open-agc")
        try:
            self._docker("stop", container, timeout=30)
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to stop container: %s", e.stderr)
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

        logger.info("Auto-upgrade enabled -- starting upgrade ...")

        if not self.pull_image(self.latest_version):
            logger.error("Upgrade aborted: image pull failed")
            return False

        set_version(self.latest_version)

        data_dir = os.environ.get("OPEN_AGC_DATA_DIR", "/app/data")
        data_version = os.path.join(data_dir, "VERSION")
        try:
            os.makedirs(data_dir, exist_ok=True)
            with open(data_version, "w") as f:
                f.write(self.latest_version + "\n")
        except OSError as e:
            logger.warning("Could not write data VERSION file: %s", e)

        return self.recreate_container()


def run_auto_upgrade() -> None:
    """Entry point called from docker-entrypoint.sh."""
    logging.basicConfig(
        level=logging.INFO,
        format="[AutoUpgrade] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    upgrader = AutoUpgrader()
    upgraded = upgrader.check_and_upgrade()
    if upgraded:
        logger.info("Upgrade initiated -- exiting for restart")
        sys.exit(0)


if __name__ == "__main__":
    run_auto_upgrade()
