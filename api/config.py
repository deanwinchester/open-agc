"""Configuration loading/saving for the Open-AGC API server."""
import json
import os
import shutil
import sys
import threading
from datetime import datetime
from core.paths import get_data_path

CONFIG_PATH = get_data_path("config.json")

# 仓库内置的无密钥默认配置模板（源码首启时播种）
_TEMPLATE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "build_data", "config.json",
)


def _find_template_config():
    """定位播种模板：frozen 读 bundle 内 data/config.json（open_agc.spec 打入）；
    源码读 <repo>/build_data/config.json。找不到返回 None。"""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "data", "config.json")
        if os.path.exists(bundled):
            return bundled
    if os.path.exists(_TEMPLATE_CONFIG):
        return _TEMPLATE_CONFIG
    return None

# Protects read-modify-write cycles on config.json across threads
_config_lock = threading.Lock()


def _is_default_config_path() -> bool:
    """仅当 CONFIG_PATH 是真实默认路径（未被测试 monkeypatch 到临时目录）时才播种。"""
    try:
        return os.path.abspath(CONFIG_PATH) == os.path.abspath(get_data_path("config.json"))
    except Exception:
        return False


def _seed_default_config() -> None:
    """首次启动且 config.json 缺失时，从无密钥默认配置模板播种。
    播种失败静默忽略，不阻断启动。"""
    if not _is_default_config_path():
        return
    template = _find_template_config()
    if not template:
        return
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(template, "r", encoding="utf-8") as f:
            content = f.read()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[Config] Seeded default config from {template}")
    except Exception:
        pass


def _merge_build_defaults(cfg: dict) -> bool:
    """把打包模板里的「部署属性」新键合并进已有配置（升级旧包时，
    播种逻辑只补缺文件不更新已有 config.json，新键永远不会进来——
    生产实证：zxs 品牌的 app_name / update_manifest_url 不生效）。

    规则：
    - update_manifest_url：升级通道是构建属性而非用户偏好——模板有就跟随
    - ui_theme.app_name：仅在用户未设置（空/缺）时填充，尊重用户自定义

    返回是否有变更（有则调用方落盘）。
    """
    if not _is_default_config_path():
        return False
    template = _find_template_config()
    if not template:
        return False
    try:
        with open(template, "r", encoding="utf-8") as f:
            defaults = json.load(f)
    except Exception:
        return False

    changed = False
    tmpl_url = (defaults.get("update_manifest_url") or "").strip()
    if tmpl_url and cfg.get("update_manifest_url") != tmpl_url:
        cfg["update_manifest_url"] = tmpl_url
        changed = True

    tmpl_name = ((defaults.get("ui_theme") or {}).get("app_name") or "").strip()
    if tmpl_name:
        ui = cfg.setdefault("ui_theme", {})
        if isinstance(ui, dict) and not (ui.get("app_name") or "").strip():
            ui["app_name"] = tmpl_name
            changed = True
    return changed


def load_config() -> dict:
    """Load configuration from config.json."""
    with _config_lock:
        if not os.path.exists(CONFIG_PATH):
            _seed_default_config()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:
                # Don't silently discard: back up the corrupt file for inspection
                backup = f"{CONFIG_PATH}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                try:
                    shutil.copy2(CONFIG_PATH, backup)
                    print(f"[Config] WARNING: config.json corrupt ({e}); backed up to {backup}")
                except Exception as be:
                    print(f"[Config] WARNING: config.json corrupt ({e}); backup failed: {be}")
                return {}
            # 合并打包模板的新部署键（品牌名、升级清单地址等），有变更才落盘
            try:
                if _merge_build_defaults(cfg):
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    print("[Config] merged build defaults (update_manifest_url/ui_theme.app_name)")
            except Exception as e:
                print(f"[Config] build defaults merge failed: {e}")
            return cfg
        return {}


def save_config(config: dict) -> None:
    """Save configuration to config.json atomically (tmp file + os.replace)."""
    with _config_lock:
        tmp_path = CONFIG_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except Exception as e:
            print(f"[Config] Save error: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


_agent_log_file = None


def log_agent_error(error_text: str):
    """Log an agent error to the agent error log file."""
    global _agent_log_file
    if _agent_log_file is None:
        log_dir = get_data_path("logs")
        os.makedirs(log_dir, exist_ok=True)
        from datetime import datetime
        _agent_log_file = os.path.join(
            log_dir, f"agent_errors_{datetime.now().strftime('%Y%m%d')}.log"
        )
    try:
        from datetime import datetime
        with open(_agent_log_file, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {error_text}\n")
    except Exception:
        pass
