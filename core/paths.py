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
            base_dir = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Open-AGC")
        elif sys.platform == "win32":
            base_dir = os.path.join(os.getenv("APPDATA", ""), "Open-AGC")
        else:
            base_dir = os.path.join(os.path.expanduser("~"), ".open_agc")
    else:
        # Running from source
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
    os.makedirs(base_dir, exist_ok=True)
    return base_dir

def get_data_dir() -> str:
    """Get the base data directory."""
    dir_path = os.path.join(get_base_dir(), "data")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path

def get_data_path(filename: str) -> str:
    """Get the full path for a file inside the data directory."""
    return os.path.join(get_data_dir(), filename)

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


def get_user_plugins_dir() -> str:
    """Get the directory for user-installed plugins (under data/ for Docker persistence)."""
    dir_path = os.path.join(get_data_dir(), "plugins")
    os.makedirs(dir_path, exist_ok=True)
    return dir_path
