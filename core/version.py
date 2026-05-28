import os

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSION_FILE = os.path.join(_APP_ROOT, "VERSION")


def get_version() -> str:
    """Return the current application version.

    Fallback chain: VERSION file → APP_VERSION env var → 0.0.0
    """
    if os.path.exists(_VERSION_FILE):
        with open(_VERSION_FILE) as f:
            return f.read().strip()
    return os.environ.get("APP_VERSION", "0.0.0")


def set_version(version: str) -> None:
    """Write the VERSION file."""
    with open(_VERSION_FILE, "w") as f:
        f.write(version.strip() + "\n")
