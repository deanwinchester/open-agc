"""Path safety helpers.

Validate user/LLM-controlled names before they are joined onto base
directories, so path traversal (``..``, absolute paths, drive letters)
cannot escape the intended directory.
"""
import os
import re

# Plain file/dir names only: letters, digits, dot, underscore, dash.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Windows drive-letter prefix like "C:" or "d:".
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def is_safe_name(name) -> bool:
    """Return True if *name* is a plain file/directory name.

    Only ``[A-Za-z0-9_.-]`` is allowed (suffixes like ``.md`` are fine);
    path separators, empty strings and ``.``/``..`` are rejected.
    """
    if not name or not isinstance(name, str):
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def resolve_under(base_dir: str, name: str) -> str:
    """Resolve user-controlled *name* strictly inside *base_dir*.

    Returns the absolute, symlink-resolved path. Raises ValueError when
    the name is empty, absolute, contains ``..``, or otherwise escapes
    *base_dir*.
    """
    if not name or not isinstance(name, str):
        raise ValueError("empty name")

    # Reject absolute paths (POSIX, Windows drive-letter and UNC/rooted).
    if os.path.isabs(name) or _DRIVE_RE.match(name) or name.startswith(("\\\\", "//")):
        raise ValueError(f"absolute path not allowed: {name!r}")

    # Reject parent traversal in any component (both separator styles).
    parts = name.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError(f"parent traversal not allowed: {name!r}")

    base = os.path.normcase(os.path.realpath(base_dir))
    candidate = os.path.normcase(os.path.realpath(os.path.join(base_dir, name)))
    try:
        common = os.path.commonpath([base, candidate])
    except ValueError:
        # Different drives / mixed absolute-relative — cannot be inside.
        raise ValueError(f"path escapes base directory: {name!r}")
    if common != base:
        raise ValueError(f"path escapes base directory: {name!r}")
    return candidate
