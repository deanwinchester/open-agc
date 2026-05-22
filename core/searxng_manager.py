"""SearXNG manager: Docker lifecycle for self-hosted search aggregator."""
import os
import subprocess
import secrets
import time
from typing import Optional
from core.paths import get_data_dir

SEARXNG_COMPOSE_DIR = os.path.join(get_data_dir(), "searxng")
SEARXNG_COMPOSE_FILE = os.path.join(SEARXNG_COMPOSE_DIR, "docker-compose.yml")
SEARXNG_SETTINGS_FILE = os.path.join(SEARXNG_COMPOSE_DIR, "settings.yml")
SEARXNG_LIMITER_FILE = os.path.join(SEARXNG_COMPOSE_DIR, "limiter.toml")
SEARXNG_IMAGE = "searxng/searxng:latest"


class SearXNGManager:
    """Manages a SearXNG instance via Docker."""

    def __init__(self, port: int = 8888):
        self.port = port
        self.external_url = ""

    # ── Docker detection ──

    @staticmethod
    def is_docker_available() -> bool:
        """Check if Docker and docker-compose are available."""
        try:
            subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
            return True
        except Exception:
            return False

    @staticmethod
    def is_compose_available() -> bool:
        """Check if docker compose (plugin) or docker-compose is available."""
        for cmd in [["docker", "compose"], ["docker-compose"]]:
            try:
                subprocess.run(cmd + ["--version"], capture_output=True, timeout=5, check=True)
                return True
            except Exception:
                continue
        return False

    def get_url(self) -> str:
        if self.external_url:
            return self.external_url
        return f"http://localhost:{self.port}"

    # ── Health check ──

    def is_running(self) -> bool:
        """Check if SearXNG instance is responding."""
        url = self.get_url()
        try:
            import requests
            resp = requests.get(f"{url}/search?format=json&q=test", timeout=3)
            return resp.status_code < 500
        except Exception:
            return False

    def _compose_cmd(self, *args) -> list:
        """Return the appropriate docker compose command list."""
        if self.is_compose_available():
            # Prefer docker compose plugin
            for cmd in [["docker", "compose"], ["docker-compose"]]:
                try:
                    subprocess.run(cmd + ["--version"], capture_output=True, timeout=5, check=True)
                    return cmd + ["-f", SEARXNG_COMPOSE_FILE] + list(args)
                except Exception:
                    continue
        return []

    # ── Install ──

    def install(self) -> bool:
        """Generate config files and start SearXNG container."""
        if not self.is_docker_available():
            return False

        os.makedirs(SEARXNG_COMPOSE_DIR, exist_ok=True)
        self._write_compose_file()
        self._write_settings_file()
        self._write_limiter_file()

        # Pull image first
        try:
            subprocess.run(["docker", "pull", SEARXNG_IMAGE],
                           capture_output=True, timeout=120)
        except Exception:
            pass  # Non-fatal if pull fails; up will pull if needed

        return self.start()

    def start(self) -> bool:
        """Start SearXNG container."""
        cmd = self._compose_cmd("up", "-d")
        if not cmd:
            return False
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            # Wait for it to become healthy
            for _ in range(30):
                if self.is_running():
                    return True
                time.sleep(1)
            return self.is_running()
        except Exception:
            return False

    def stop(self) -> bool:
        """Stop SearXNG container."""
        cmd = self._compose_cmd("down")
        if not cmd:
            return False
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            return True
        except Exception:
            return False

    def get_status(self) -> dict:
        """Return combined status dict."""
        docker_ok = self.is_docker_available()
        compose_ok = self.is_compose_available() if docker_ok else False
        return {
            "docker_available": docker_ok,
            "compose_available": compose_ok,
            "compose_dir_exists": os.path.isdir(SEARXNG_COMPOSE_DIR),
            "running": self.is_running(),
            "port": self.port,
            "url": self.get_url(),
        }

    # ── Config file templates ──

    def _write_compose_file(self):
        secret_key = secrets.token_hex(32)
        content = f"""version: '3.8'

services:
  searxng:
    image: {SEARXNG_IMAGE}
    container_name: open-agc-searxng
    ports:
      - "{self.port}:8080"
    volumes:
      - ./settings.yml:/etc/searxng/settings.yml:ro
      - ./limiter.toml:/etc/searxng/limiter.toml:ro
      - searxng-data:/etc/searxng
    environment:
      - SEARXNG_BASE_URL=http://localhost:{self.port}/
      - SEARXNG_SECRET_KEY={secret_key}
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETGID
      - SETUID
    restart: unless-stopped

volumes:
  searxng-data:
"""
        with open(SEARXNG_COMPOSE_FILE, "w", encoding="utf-8") as f:
            f.write(content)

    def _write_settings_file(self):
        content = """# SearXNG settings (auto-generated)
use_default_settings: true

general:
  instance_name: "Open-AGC SearXNG"
  debug: false
  privacypolicy_url: false
  contact_url: false
  enable_metrics: false

search:
  formats:
    - html
    - json
  safe_search: 0
  autocomplete: ""
  lang: "all"

server:
  secret_key: "__auto__"
  bind_address: "0.0.0.0"
  port: 8080
  limiter: false
  image_proxy: false
  method: "GET"

ui:
  static_use_hash: true
  default_theme: simple
  default_locale: ""
  results_on_new_tab: false

enabled_plugins:
  - "Hash plugin"
  - "Self Information"
  - "Tracker URL remover"
  - "Ahmia blacklist"

outgoing:
  request_timeout: 10.0
  max_request_timeout: 30.0
  useragent_suffix: ""
"""
        with open(SEARXNG_SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write(content)

    def _write_limiter_file(self):
        content = """# SearXNG rate limiter (disabled for local use)
[botdetection.ip_limit]
link_token = false
"""
        with open(SEARXNG_LIMITER_FILE, "w", encoding="utf-8") as f:
            f.write(content)


# Singleton instance
_searxng_manager: Optional[SearXNGManager] = None


def get_searxng_manager() -> SearXNGManager:
    global _searxng_manager
    if _searxng_manager is None:
        _searxng_manager = SearXNGManager()
    return _searxng_manager
