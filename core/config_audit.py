"""
Configuration audit utilities — detects security concerns at startup.

Checks for:
- Plaintext API keys in config.json
- Suspicious domain whitelist entries
"""
import os
import json
import re

from core.paths import get_data_path


# Known suspicious or piracy-related domains (informational warnings)
_SUSPICIOUS_DOMAIN_PATTERNS = [
    r"dytt\d*\.com",       # Chinese movie piracy
    r"dy-\w+\.com\.cn",    # Chinese movie piracy
    r"baofeng\d*\.com",    # Chinese movie piracy
    r"porn",               # Adult content
    r"xxx",                # Adult content
]

# Known legitimate/common domains to suppress false warnings
_SAFE_DOMAIN_SUFFIXES = [
    ".github.com", ".hf.co", "huggingface.co", "modelscope.cn",
    "pypi.org", "python.org", "baidu.com", "google.com",
    "cloudflare.com", "docker.com", "npmjs.org",
]


def check_plaintext_api_keys(config: dict) -> list:
    """Check config for plaintext API keys and return warnings."""
    warnings = []
    api_keys = config.get("api_keys", {})
    for provider, key in api_keys.items():
        if not isinstance(key, str) or not key:
            continue
        # Skip non-key config values (URLs, paths, etc.)
        if key.startswith("http://") or key.startswith("https://"):
            continue
        # Flag obvious key patterns
        if re.match(r'^sk-', key) or re.match(r'^hf_', key) or re.match(r'^AIza', key):
            warnings.append(
                f"[SECURITY] config.json contains plaintext {provider} API key "
                f"({key[:12]}...). Consider using environment variables instead."
            )
    return warnings


def check_email_credentials(config: dict) -> list:
    """Check for plaintext email credentials."""
    warnings = []
    email = config.get("email_account", "")
    password = config.get("email_password", "")
    if email and password:
        warnings.append(
            f"[SECURITY] config.json contains plaintext email credentials "
            f"({email}). Consider using environment variables instead."
        )
    return warnings


def audit_network_whitelist(config: dict) -> list:
    """Check network permission whitelist for suspicious domains."""
    warnings = []
    perms = config.get("tool_permissions", {})
    network = perms.get("network", {})
    for domain, action in network.items():
        if action != "allow":
            continue
        # Check if domain matches suspicious patterns
        is_suspicious = False
        for pattern in _SUSPICIOUS_DOMAIN_PATTERNS:
            if re.search(pattern, domain.lower()):
                is_suspicious = True
                break
        # Check if it's a known safe domain
        is_safe = any(domain.endswith(suffix) for suffix in _SAFE_DOMAIN_SUFFIXES)
        if is_suspicious and not is_safe:
            warnings.append(
                f"[SECURITY] Network whitelist allows potentially suspicious domain: "
                f"{domain} (action: {action}). Review in config.json tool_permissions."
            )
    return warnings


def audit_all() -> list:
    """Run all audit checks and return warnings."""
    warnings = []
    config_path = get_data_path("config.json")
    if not os.path.exists(config_path):
        return warnings

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        return [f"[SECURITY] Could not read config.json: {e}"]

    warnings.extend(check_plaintext_api_keys(config))
    warnings.extend(check_email_credentials(config))
    warnings.extend(audit_network_whitelist(config))

    return warnings
