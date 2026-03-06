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
    """Get the skills directory."""
    dir_path = os.path.join(get_base_dir(), "skills")
    os.makedirs(dir_path, exist_ok=True)
    
    # If it's empty, try to populate it with default skills from the bundled app
    if not os.listdir(dir_path):
        if getattr(sys, 'frozen', False):
            # PyInstaller creates a temp folder and stores path in _MEIPASS
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

