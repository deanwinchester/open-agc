import os

def get_data_dir() -> str:
    """Get the base data directory, either from env var or default './data'."""
    data_dir = os.environ.get("OPEN_AGC_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "data")
    return "data"

def get_data_path(filename: str) -> str:
    """Get the full path for a file inside the data directory."""
    return os.path.join(get_data_dir(), filename)

def get_skills_dir() -> str:
    """Get the skills directory, either from env var or default './skills'."""
    data_dir = os.environ.get("OPEN_AGC_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "skills")
    return "skills"
