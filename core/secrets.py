"""Secrets vault: local credential storage with reference substitution and masking.

Credentials never leave this machine: LLM context, frontend echoes and logs only
ever see `{{secret:name.field}}` placeholders and `***`. Plaintext is used only
at the moment of local execution (shell command / python code handed to the
child process).

Storage: data/secrets.json (atomically written, module-level RLock).
"""
import json
import os
import re
import threading
from datetime import datetime
from urllib.parse import quote

from core.paths import get_data_path

# Protects read-modify-write cycles on secrets.json across threads
_lock = threading.RLock()

MASK = "***"

# {{secret:name.field}} — name is the vault key, field one of VALID_FIELDS
_REF_RE = re.compile(r"\{\{\s*secret:([A-Za-z0-9_-]+)\.([A-Za-z]+)\s*\}\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

VALID_FIELDS = ("username", "password", "host", "uri", "note", "database")

# Connection-string scheme per secret type (unknown types use the type itself)
_SCHEME_MAP = {
    "mongodb": "mongodb",
    "mongodb+srv": "mongodb+srv",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "redis": "redis",
    "http": "http",
    "https": "https",
}


def _path() -> str:
    """Resolve secrets.json lazily so OPEN_AGC_DATA_DIR overrides (tests) work."""
    return get_data_path("secrets.json")


def _load() -> dict:
    """Load the vault. Missing or corrupt file yields an empty vault."""
    path = _path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[Secrets] WARNING: failed to load secrets.json ({e}); treating as empty")
        return {}


def _save(data: dict) -> None:
    """Persist the vault atomically (tmp file + os.replace), UTF-8."""
    path = _path()
    tmp_path = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _mask_username(username: str) -> str:
    if not username:
        return ""
    if len(username) == 1:
        return "*"
    return username[0] + "***"


def _masked_entry(name: str, entry: dict) -> dict:
    """Public view of a secret — never includes the password field or its value."""
    return {
        "name": name,
        "type": entry.get("type", "generic"),
        "host": entry.get("host", ""),
        "database": entry.get("database", ""),
        "username_masked": _mask_username(entry.get("username", "") or ""),
        "note": entry.get("note", ""),
        "created_at": entry.get("created_at", ""),
    }


def list_secrets() -> list:
    """Masked view of all secrets (no password field anywhere)."""
    with _lock:
        data = _load()
        return [_masked_entry(name, entry) for name, entry in sorted(data.items())]


def get_secret(name: str) -> dict:
    """Full secret INCLUDING the plaintext password.

    Server-internal use only (execution substitution, URI building). Never
    return this from an API endpoint or send it to the LLM/frontend/logs.
    """
    with _lock:
        entry = _load().get(name)
        return dict(entry) if isinstance(entry, dict) else None


def upsert_secret(name: str, type: str = None, host: str = None, port: str = None,
                  username: str = None, password: str = None, note: str = None,
                  database: str = None) -> dict:
    """Create or update a secret. Returns the masked entry (no password).

    Every field except name is optional: None (not provided) preserves the
    existing value on update; only an explicit value (including "") overwrites.
    password=None in particular must never silently wipe the credential.
    """
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"Invalid secret name: {name!r} (allowed: ^[A-Za-z0-9_-]+$)")
    with _lock:
        data = _load()
        existing = data.get(name, {})
        created_at = existing.get("created_at") or datetime.now().isoformat(timespec="seconds")
        entry = {
            "type": ((type or "generic").strip() or "generic") if type is not None
                    else (existing.get("type") or "generic"),
            "host": (host or "") if host is not None else (existing.get("host") or ""),
            "port": (str(port) if port not in (None, "") else "")
                    if port is not None else (existing.get("port") or ""),
            "database": (database or "") if database is not None else (existing.get("database") or ""),
            "username": (username or "") if username is not None else (existing.get("username") or ""),
            "password": (existing.get("password") or "") if password is None else password,
            "note": (note or "") if note is not None else (existing.get("note") or ""),
            "created_at": created_at,
        }
        data[name] = entry
        _save(data)
        return _masked_entry(name, entry)


def delete_secret(name: str) -> bool:
    """Delete a secret. Returns True if it existed."""
    with _lock:
        data = _load()
        if name not in data:
            return False
        del data[name]
        _save(data)
        return True


def build_uri(name: str) -> str:
    """Build a connection URI for the secret, e.g. mongodb://user:pass@host:port/.

    Username/password are percent-encoded so passwords containing @ : / # etc.
    still produce a valid URI. Returns "" if the secret does not exist.
    """
    entry = get_secret(name)
    if not entry:
        return ""
    stype = (entry.get("type") or "generic").strip().lower()
    scheme = _SCHEME_MAP.get(stype, stype)
    username = entry.get("username") or ""
    password = entry.get("password") or ""
    host = entry.get("host") or ""
    port = str(entry.get("port") or "")

    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    elif password:
        # redis-style: scheme://:password@host
        auth = ":" + quote(password, safe="") + "@"

    hostport = host
    if port and ":" not in host:
        hostport = f"{host}:{port}"
    uri = f"{scheme}://{auth}{hostport}/"
    database = entry.get("database") or ""
    if database:
        uri += quote(database, safe="")
    return uri


def has_secret_ref(text: str) -> bool:
    """True if text contains a {{secret:name.field}} reference."""
    return bool(text) and bool(_REF_RE.search(text))


def substitute_refs(text: str) -> str:
    """Replace {{secret:name.field}} references with real values.

    field ∈ username/password/host/uri/note. Unknown names or fields are left
    unchanged. Never raises — on any error the original text is returned.
    """
    if not text or not has_secret_ref(text):
        return text

    def _replace(m: "re.Match") -> str:
        name, field = m.group(1), m.group(2).lower()
        entry = get_secret(name)
        if entry is None:
            return m.group(0)
        if field == "uri":
            return build_uri(name) or m.group(0)
        if field in ("username", "password", "host", "note", "database"):
            return str(entry.get(field) or "")
        return m.group(0)

    try:
        return _REF_RE.sub(_replace, text)
    except Exception:
        return text


def mask_secrets(text: str) -> str:
    """Replace known password values and credential-bearing URIs with ***.

    Passwords shorter than 4 chars are skipped as bare values (masking "1"
    would drown the output in ***), though a credential-bearing URI containing
    one is still masked as a whole. The percent-encoded form of each password
    is masked too, so URI-encoded fragments can't escape. Empty values are
    skipped; URIs without username/password are skipped (the host alone is
    already visible in the masked list view). Never raises — on any error the
    original text is returned unchanged.
    """
    if not text:
        return text
    try:
        with _lock:
            data = _load()
        values = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            password = entry.get("password") or ""
            if len(password) >= 4:
                values.append(password)
                quoted = quote(password, safe="")
                if quoted != password:
                    values.append(quoted)
            # Only mask URIs that embed credentials — a credential-less URI
            # (host only) is already visible in the masked list view.
            if password or (entry.get("username") or ""):
                uri = build_uri(name)
                if uri:
                    values.append(uri)
        # Longest first so a URI containing the password is masked as a whole
        for value in sorted(set(values), key=len, reverse=True):
            text = text.replace(value, MASK)
        return text
    except Exception:
        return text
