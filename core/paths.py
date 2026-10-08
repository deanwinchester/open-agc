import os
import sys
import shutil

def get_base_dir() -> str:
    """Get the base directory for storing application data."""
    if os.environ.get("OPEN_AGC_DATA_DIR"):
        return os.environ.get("OPEN_AGC_DATA_DIR")
        
    if getattr(sys, 'frozen', False):
        # Running as compiled app (PyInstaller)
        if sys.platform == "darwin":
            import json
            import re
            app_name = "Open-AGC"
            try:
                with open(os.path.join(sys._MEIPASS, "data", "brand.json"), encoding="utf-8") as f:
                    candidate = json.load(f).get("app_name", "")
                if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
                    app_name = candidate
            except (OSError, ValueError, AttributeError, TypeError):
                pass
            base_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name)
        elif sys.platform == "win32":
            base_dir = os.path.join(os.getenv("APPDATA", ""), "Open-AGC")
        else:
            base_dir = os.path.join(os.path.expanduser("~"), ".open_agc")
    else:
        # Running from source
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


_BRAND_CACHE = None


def get_brand() -> dict:
    """读取品牌配置（build_data/brand.json；打包版在 _MEIPASS/data/）。

    品牌线（如 zxs）可在此放 prompt_identity / prompt_industry 等提示词
    定制字段；开源版无此文件，返回 {}。结果缓存（运行期不变）。"""
    global _BRAND_CACHE
    if _BRAND_CACHE is not None:
        return _BRAND_CACHE
    import json
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(sys._MEIPASS, "data", "brand.json"))
    candidates.append(os.path.join(get_base_dir(), "build_data", "brand.json"))
    brand = {}
    for p in candidates:
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    brand = json.load(f)
                break
        except (OSError, ValueError):
            pass
    _BRAND_CACHE = brand if isinstance(brand, dict) else {}
    return _BRAND_CACHE

def get_data_dir() -> str:
    """Get the base data directory."""
    dir_path = os.path.join(get_base_dir(), "data")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path

def get_data_path(filename: str) -> str:
    """Get the full path for a file inside the data directory."""
    return os.path.join(get_data_dir(), filename)


def _copy_missing(src_dir: str, dst_dir: str) -> None:
    """复制 src_dir 下所有条目到 dst_dir；已存在的跳过（用户文件优先）。"""
    if not os.path.isdir(src_dir):
        return
    os.makedirs(dst_dir, exist_ok=True)
    for item in os.listdir(src_dir):
        src = os.path.join(src_dir, item)
        dst = os.path.join(dst_dir, item)
        if os.path.exists(dst):
            continue
        if os.path.isfile(src):
            shutil.copy2(src, dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst)


def seed_frozen_data(bundle_dir: str) -> None:
    """frozen 首启播种：bundle 的 data/* → get_data_dir()/，skills/* → get_data_dir()/skills/。

    目标与 get_data_dir()（$OPEN_AGC_DATA_DIR/data）对齐，否则首启找不到 config.json。
    gui_app.py（打包真实入口）与 launcher.py 共用本实现，避免两份逻辑漂移。
    """
    data_dir = get_data_dir()
    _copy_missing(os.path.join(bundle_dir, "data"), data_dir)
    _copy_missing(os.path.join(bundle_dir, "skills"), os.path.join(data_dir, "skills"))

def get_skills_dir() -> str:
    """Get the skills directory (under data/ for Docker persistence)."""
    dir_path = os.path.join(get_data_dir(), "skills")
    os.makedirs(dir_path, exist_ok=True)

    # Migration: copy old skills from <base>/skills/ to <data>/skills/ if empty
    old_skills = os.path.join(get_base_dir(), "skills")
    if os.path.isdir(old_skills) and old_skills != dir_path and not os.listdir(dir_path):
        try:
            for item in os.listdir(old_skills):
                src = os.path.join(old_skills, item)
                dst = os.path.join(dir_path, item)
                if os.path.isfile(src) and item.endswith(".md"):
                    shutil.copy2(src, dst)
        except OSError:
            pass

    # If still empty, populate with default skills from the bundled app
    if not os.listdir(dir_path):
        if getattr(sys, 'frozen', False):
            bundled_skills = os.path.join(sys._MEIPASS, "skills")
        else:
            bundled_skills = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")

        if os.path.exists(bundled_skills) and bundled_skills != dir_path:
            for item in os.listdir(bundled_skills):
                src = os.path.join(bundled_skills, item)
                dst = os.path.join(dir_path, item)
                if os.path.isfile(src) and item.endswith(".md"):
                    shutil.copy2(src, dst)

    return dir_path

def get_bin_dir() -> str:
    """Get the directory for storing binary executables (under data/ for Docker persistence)."""
    dir_path = os.path.join(get_data_dir(), "bin")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path

def get_models_dir() -> str:
    """Get the directory for storing LLM models."""
    dir_path = os.path.join(get_data_dir(), "models")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def resolve_sandbox_dir(configured: str = None) -> str:
    """解析沙箱根目录（唯一口径，全项目共用）。

    - 绝对路径配置原样使用；
    - 相对路径（如默认的 "./workspace"）不再随 CWD 漂移：frozen 下解析到
      数据目录 <data>/workspace（用户可写；CWD 是 _MEIPASS，Linux 下
      /opt/open-agc/_internal 为 root 所有，写不进去——生产实证），
      源码模式解析到项目根目录。
    """
    if configured and os.path.isabs(configured):
        return os.path.abspath(configured)
    rel = (configured or "").strip() or "workspace"
    if getattr(sys, 'frozen', False):
        # 与 data/ 并列（<base>/workspace），不要塞进 data 里（用户要求）
        root = os.path.join(get_base_dir(), rel)
    else:
        root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)
    return os.path.abspath(root)


def get_user_plugins_dir() -> str:
    """Get the directory for user-installed plugins (under data/ for Docker persistence)."""
    dir_path = os.path.join(get_data_dir(), "plugins")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path
