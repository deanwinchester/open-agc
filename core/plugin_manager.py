"""
Plugin Manager — unified plugin discovery, loading, and lifecycle management.

Plugins live in the `plugins/` directory. Each plugin is a subdirectory with:
    plugin.json   — manifest (name, version, menu, dependencies)
    __init__.py   — init_plugin(context) → PluginInstance

Usage:
    from core.plugin_manager import discover_plugins, get_plugin, list_plugins
    plugins = discover_plugins("plugins", broadcast_fn=..., server_config=...)
    for p in plugins:
        app.include_router(p.router, prefix=f"/api/plugin/{p.name}")
"""
import os
import sys
import json
import threading
import importlib
import traceback
from typing import Optional, Callable, Dict, List, Any


class PluginContext:
    """Context passed to init_plugin()."""
    def __init__(self, name: str, plugin_dir: str, db_dir: str = "",
                 static_dir: str = "", broadcast_fn: Callable = None,
                 server_config: dict = None, logger: Callable = None):
        self.name = name
        self.plugin_dir = plugin_dir
        self.db_dir = db_dir or os.path.join(plugin_dir, "data")
        self.static_dir = static_dir
        self.broadcast_fn = broadcast_fn
        self.server_config = server_config or {}
        self.logger = logger or print
        self._store = None

    # ── 标准化能力（实测插件开发暴露的缺口：每个插件都在重造轮子）──

    def llm_client(self):
        """系统默认大模型客户端（跟随「设置」页配置，禁止自行硬编码 key/模型）。"""
        from core.llm_client import LLMClient
        return LLMClient()

    def llm_text(self, messages, default: str = "") -> str:
        """最常用形态：messages → 回复文本。失败返回 default 不抛异常。"""
        try:
            resp, _model = self.llm_client().chat(messages=messages)
            choices = getattr(resp, "choices", None) or []
            if choices:
                return choices[0].message.content or default
            return default
        except Exception as e:
            self.logger(f"[Plugin:{self.name}] llm_text error: {e}")
            return default

    @property
    def store(self):
        """插件私有 JSON KV 存储（落在 db_dir/_store.json，带锁、原子写）。

        用法：context.store.get(key, default) / .set(key, value) /
        .delete(key) / .keys() / .all()。适合项目列表、设置、小数据。"""
        if self._store is None:
            os.makedirs(self.db_dir, exist_ok=True)
            self._store = _PluginStore(os.path.join(self.db_dir, "_store.json"))
        return self._store


class PluginInstance:
    """Returned by init_plugin()."""
    def __init__(self, name: str = "", router=None, static_dir: str = None,
                 on_load: Callable = None, on_unload: Callable = None,
                 state: dict = None, router_prefix: str = None):
        self.name = name
        self.router = router
        self.static_dir = static_dir
        self.on_load = on_load
        self.on_unload = on_unload
        self.state = state or {}
        self.router_prefix = router_prefix

    def __repr__(self):
        return f"PluginInstance({self.name})"


class PluginInfo:
    """Metadata about a loaded plugin."""
    def __init__(self, name: str, version: str, description: str = "",
                 manifest: dict = None, instance: PluginInstance = None,
                 plugin_dir: str = ""):
        self.name = name
        self.version = version
        self.description = description
        self.manifest = manifest or {}
        self.instance = instance
        self.plugin_dir = plugin_dir


class _PluginStore:
    """插件私有 JSON KV 存储（PluginContext.store 的实现）：带锁、原子写。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict):
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def get(self, key, default=None):
        with self._lock:
            return self._read().get(key, default)

    def set(self, key, value):
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def delete(self, key):
        with self._lock:
            data = self._read()
            if key in data:
                del data[key]
                self._write(data)

    def keys(self):
        with self._lock:
            return list(self._read().keys())

    def all(self) -> dict:
        with self._lock:
            return self._read()


_loaded_plugins: Dict[str, PluginInfo] = {}
# 目录内容签名（max mtime）：扫描时只重载代码变过的插件——此前每次 scan 都
# 全量卸载重导（含 open-agc-train 的重依赖导入），事件循环阻塞数秒导致 WS
# 断连、进度停更、页面重载时插件菜单拉取失败变空（生产实证）。
_plugin_dir_signatures: Dict[str, float] = {}

_SIGNATURE_EXTS = {".py", ".js", ".json", ".vue", ".ts", ".css", ".html"}


def dir_signature(plugin_dir: str) -> float:
    """插件目录内容签名：目录内代码/清单文件的最大 mtime（无文件为 0）。
    os.walk 小目录成本可忽略；签名变化即视为代码变更需要重载。"""
    latest = 0.0
    try:
        for dirpath, dirnames, filenames in os.walk(plugin_dir):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "node_modules", ".git")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in _SIGNATURE_EXTS:
                    continue
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(dirpath, fn)))
                except OSError:
                    pass
    except OSError:
        pass
    return latest


def get_loaded_signature(name: str):
    """已加载插件载入时的目录签名；未记录返回 None。"""
    return _plugin_dir_signatures.get(name)


def discover_plugins(plugins_dir: str = "plugins",
                     broadcast_fn: Callable = None,
                     server_config: dict = None,
                     logger: Callable = None) -> List[PluginInfo]:
    """Scan plugins_dir for valid plugins and load them.

    Args:
        plugins_dir: Path to the plugins directory
        broadcast_fn: WebSocket broadcast function for progress updates
        server_config: Main project config dict
        logger: Log function (defaults to print)

    Returns:
        List of loaded PluginInfo objects
    """
    global _loaded_plugins
    logger = logger or print

    if not os.path.isdir(plugins_dir):
        logger(f"[PluginManager] Plugins directory not found: {plugins_dir}")
        return []

    # Ensure plugins_dir is on sys.path for imports
    parent = os.path.dirname(os.path.abspath(plugins_dir))
    if parent not in sys.path:
        sys.path.insert(0, parent)

    loaded = []
    for entry in sorted(os.listdir(plugins_dir)):
        # Skip private/housekeeping dirs (e.g. the _trash recycle bin)
        if entry.startswith("_") or entry.startswith("."):
            continue
        plugin_dir = os.path.join(plugins_dir, entry)
        if not os.path.isdir(plugin_dir):
            continue
        manifest_path = os.path.join(plugin_dir, "plugin.json")
        if not os.path.exists(manifest_path):
            continue
        # Skip disabled plugins
        state = _get_plugin_state(entry, plugins_dir)
        if not state.get("enabled", True):
            continue
        if entry in _loaded_plugins:
            loaded.append(_loaded_plugins[entry])
            continue

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            logger(f"[PluginManager] Failed to read {manifest_path}: {e}")
            continue

        name = manifest.get("name", entry)
        version = manifest.get("version", "0.0.0")

        try:
            info = load_plugin(entry, plugins_dir, broadcast_fn, server_config, logger)
            if info:
                loaded.append(info)
                logger(f"[PluginManager] Loaded: {name} v{version}")
        except Exception as e:
            logger(f"[PluginManager] Failed to load {name}: {e}")
            traceback.print_exc()

    return loaded


def _install_plugin_deps(manifest: dict, plugin_dir: str, logger) -> None:
    """Install Python dependencies declared in plugin.json's pip_dependencies field.

    Runs pip install for any dependency that isn't already importable.
    """
    deps = manifest.get("pip_dependencies", [])
    if not deps:
        return
    import importlib.util as _ilu
    import subprocess as _sp

    missing = []
    for dep in deps:
        # dep can be "package" or "package>=1.0" — extract the import name
        import_name = dep.split(">=")[0].split("==")[0].split("<")[0].strip()
        import_name = import_name.replace("-", "_")
        if _ilu.find_spec(import_name) is None:
            missing.append(dep)

    if not missing:
        return

    logger(f"[PluginManager] Installing plugin dependencies: {missing}")
    try:
        _sp.run(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir"] + missing,
            capture_output=True, text=True, timeout=180,
        )
        logger(f"[PluginManager] Dependencies installed: {missing}")
    except Exception as e:
        logger(f"[PluginManager] Dependency install failed: {e}")


def load_plugin(name: str, plugins_dir: str = "plugins",
                broadcast_fn: Callable = None,
                server_config: dict = None,
                logger: Callable = None) -> Optional[PluginInfo]:
    """Load a single plugin by directory name."""
    global _loaded_plugins
    logger = logger or print

    plugin_dir = os.path.join(plugins_dir, name)
    manifest_path = os.path.join(plugin_dir, "plugin.json")

    if not os.path.exists(manifest_path):
        return None

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    module_name = manifest.get("python_module", name.replace("-", "_"))
    init_path = os.path.join(plugin_dir, "__init__.py")

    if not os.path.exists(init_path):
        logger(f"[PluginManager] No __init__.py in {plugin_dir}")
        return None

    # Install any declared Python dependencies before importing the plugin
    _install_plugin_deps(manifest, plugin_dir, logger)

    # Ensure plugins_dir parent is importable
    plugins_parent = os.path.dirname(os.path.abspath(plugins_dir))
    if plugins_parent not in sys.path:
        sys.path.insert(0, plugins_parent)

    # Determine the import path
    # plugins_dir might be "plugins" (relative) or "/abs/path/plugins"
    # We need the correct Python module prefix
    plugins_basename = os.path.basename(os.path.abspath(plugins_dir))

    # Handle __init__.py in plugins_dir itself
    plugins_init = os.path.join(plugins_dir, "__init__.py")
    if not os.path.exists(plugins_init):
        # Create a minimal __init__.py
        with open(plugins_init, "w") as f:
            f.write("# Auto-generated by PluginManager\n")

    try:
        mod = importlib.import_module(f"{plugins_basename}.{name}")
    except ImportError:
        # Fallback: add plugin_dir directly to sys.path
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
        try:
            mod = importlib.import_module(name)
        except ImportError:
            # Last resort: load from file path
            spec = importlib.util.spec_from_file_location(name, init_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

    if not hasattr(mod, "init_plugin"):
        logger(f"[PluginManager] {name}: no init_plugin() function")
        return None

    # Build context — use core.paths for the canonical data dir
    from core.paths import get_data_dir
    data_dir = get_data_dir()
    db_dir = os.path.join(data_dir, "plugins", name)
    os.makedirs(db_dir, exist_ok=True)

    static_dir = manifest.get("static_dir", os.path.join(plugin_dir, "static"))

    context = PluginContext(
        name=name,
        plugin_dir=plugin_dir,
        db_dir=db_dir,
        static_dir=static_dir,
        broadcast_fn=broadcast_fn,
        server_config=server_config,
        logger=logger,
    )

    instance = mod.init_plugin(context)

    if instance is None:
        logger(f"[PluginManager] {name}: init_plugin() returned None")
        return None

    instance.name = name

    info = PluginInfo(
        name=name,
        version=manifest.get("version", "0.0.0"),
        description=manifest.get("description", ""),
        manifest=manifest,
        instance=instance,
        plugin_dir=plugin_dir,
    )

    _loaded_plugins[name] = info
    _plugin_dir_signatures[name] = dir_signature(plugin_dir)

    if instance.on_load:
        try:
            instance.on_load()
        except Exception as e:
            logger(f"[PluginManager] {name}: on_load failed: {e}")

    return info


def _purge_plugin_modules(info: PluginInfo) -> None:
    """Remove the plugin's modules from sys.modules.

    Without this, a subsequent load_plugin() would get the cached (stale)
    module objects and code changes would only take effect after a server
    restart. Covers every import path load_plugin() can produce:
    ``<plugins_basename>.<name>``, bare ``<name>`` (sys.path fallback),
    plus the manifest's python_module — each with their submodules.
    """
    candidates = {info.name}
    python_module = (info.manifest or {}).get("python_module", "")
    if python_module:
        candidates.add(python_module)
    if info.plugin_dir:
        plugins_basename = os.path.basename(os.path.dirname(os.path.abspath(info.plugin_dir)))
        if plugins_basename:
            candidates.update(f"{plugins_basename}.{c}" for c in list(candidates))
    doomed = [
        m for m in sys.modules
        if m in candidates or any(m.startswith(c + ".") for c in candidates)
    ]
    for m in doomed:
        sys.modules.pop(m, None)
    importlib.invalidate_caches()


def unload_plugin(name: str) -> bool:
    """Unload a plugin (and purge its modules from sys.modules for hot reload)."""
    global _loaded_plugins
    if name not in _loaded_plugins:
        return False
    info = _loaded_plugins[name]
    if info.instance and info.instance.on_unload:
        try:
            info.instance.on_unload()
        except Exception:
            pass
    del _loaded_plugins[name]
    _plugin_dir_signatures.pop(name, None)
    _purge_plugin_modules(info)
    return True


def unload_all_plugins(except_names=None) -> int:
    """Unload every loaded plugin (except *except_names*). Returns the number unloaded.

    Used by the scan endpoint so re-discovery picks up code changes
    without a server restart. Plugins in *except_names* stay loaded — e.g.
    open-agc-train with an active training job, whose engine is a module-level
    singleton owned by a background thread (see routes_plugins.scan_plugins).
    """
    skip = set(except_names or ())
    names = [n for n in list(_loaded_plugins.keys()) if n not in skip]
    for n in names:
        unload_plugin(n)
    return len(names)


def get_plugin(name: str) -> Optional[PluginInfo]:
    """Get info for a loaded plugin."""
    return _loaded_plugins.get(name)


def list_plugins() -> List[dict]:
    """Return manifest summaries of all loaded plugins."""
    result = []
    for p in _loaded_plugins.values():
        state = _get_plugin_state(p.name)
        result.append({
            "name": p.name,
            "version": p.version,
            "description": p.description,
            "menu": p.manifest.get("menu", {}),
            "vue_entry": p.manifest.get("vue_entry", ""),
            "enabled": state.get("enabled", True),
            "homepage": p.manifest.get("homepage", ""),
            "author": p.manifest.get("author", ""),
        })
    # Also include discovered-but-not-loaded plugins
    return result


def list_all_plugins(plugins_dir: str = "plugins") -> List[dict]:
    """List all plugins (loaded + on-disk), with state info."""
    result = []
    seen = set()
    for p in _loaded_plugins.values():
        state = _get_plugin_state(p.name, plugins_dir)
        result.append({
            "name": p.name, "version": p.version,
            "description": p.description,
            "menu": p.manifest.get("menu", {}),
            "vue_entry": p.manifest.get("vue_entry", ""),
            "enabled": state.get("enabled", True),
            "loaded": True, "homepage": p.manifest.get("homepage", ""),
            "author": p.manifest.get("author", ""),
        })
        seen.add(p.name)
    # Scan disk for unloaded plugins (create dir if missing)
    if not os.path.isdir(plugins_dir):
        os.makedirs(plugins_dir, exist_ok=True)
    if os.path.isdir(plugins_dir):
        for entry in os.listdir(plugins_dir):
            # Skip private/housekeeping dirs (e.g. the _trash recycle bin)
            if entry.startswith("_") or entry.startswith("."):
                continue
            d = os.path.join(plugins_dir, entry)
            if not os.path.isdir(d) or entry in seen:
                continue
            mf = os.path.join(d, "plugin.json")
            if not os.path.exists(mf):
                continue
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                continue
            state = _get_plugin_state(entry, plugins_dir)
            result.append({
                "name": manifest.get("name", entry),
                "version": manifest.get("version", "0.0.0"),
                "description": manifest.get("description", ""),
                "menu": manifest.get("menu", {}),
                "vue_entry": manifest.get("vue_entry", ""),
                "enabled": state.get("enabled", True),
                "loaded": False, "homepage": manifest.get("homepage", ""),
                "author": manifest.get("author", ""),
            })
    return result


def _state_file(plugins_dir: str, name: str) -> str:
    return os.path.join(plugins_dir, name, ".plugin_state")


def _get_plugin_state(name: str, plugins_dir: str = "plugins") -> dict:
    sf = _state_file(plugins_dir, name)
    if os.path.exists(sf):
        try:
            with open(sf, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"enabled": True}


def _set_plugin_state(name: str, state: dict, plugins_dir: str = "plugins"):
    sf = _state_file(plugins_dir, name)
    os.makedirs(os.path.dirname(sf), exist_ok=True)
    with open(sf, "w") as f:
        json.dump(state, f)


def toggle_plugin(name: str, plugins_dir: str = "plugins") -> dict:
    """Enable or disable a plugin. Returns new state."""
    current = _get_plugin_state(name, plugins_dir)
    current["enabled"] = not current.get("enabled", True)
    _set_plugin_state(name, current, plugins_dir)
    return current


def check_compatibility(manifest: dict, app_version: str = "1.0.0") -> tuple:
    """Check if a plugin is compatible with the given app version.
    Returns (ok: bool, message: str).
    """
    requires = manifest.get("requires", {})
    min_ver = requires.get("open-agc", "")
    if not min_ver:
        return True, ""
    # Simple semver check: ">=X.Y.Z"
    try:
        op = min_ver[:2] if min_ver[:2] in (">=", "<=", "==", "!=", "~=") else ">="
        req_ver = min_ver.lstrip(">= <= == != ~= ")
        req_parts = [int(x) for x in req_ver.split(".")]
        app_parts = [int(x) for x in app_version.split(".")]
        if op == ">=":
            ok = app_parts >= req_parts
        elif op == "==":
            ok = app_parts[:len(req_parts)] == req_parts
        else:
            ok = True
        if not ok:
            return False, f"需要 Open-AGC {min_ver}, 当前 {app_version}"
        return True, ""
    except Exception:
        return True, ""  # pass on parse errors


def install_from_git(name: str, repo_url: str, plugins_dir: str = "plugins",
                     logger: Callable = None) -> bool:
    """Clone a plugin from a Git repository into plugins/."""
    import subprocess
    from core.security import resolve_under
    logger = logger or print
    try:
        target = resolve_under(plugins_dir, name)
    except ValueError:
        logger(f"[PluginManager] Invalid plugin name: {name!r}")
        return False
    if os.path.exists(target):
        logger(f"[PluginManager] {name}: directory already exists")
        return False
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--", repo_url, target],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            logger(f"[PluginManager] git clone failed: {result.stderr}")
            return False
        logger(f"[PluginManager] Installed {name} from {repo_url}")
        return True
    except Exception as e:
        logger(f"[PluginManager] git clone error: {e}")
        return False


def fetch_marketplace(url: str = "", logger: Callable = None) -> dict:
    """Fetch the remote marketplace index. Returns the JSON data or empty dict."""
    logger = logger or print
    if not url:
        url = "https://raw.githubusercontent.com/deanwinchester/open-agc-plugins/master/marketplace.json"
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger(f"[PluginManager] Marketplace fetch failed: {e}")
        return {}
