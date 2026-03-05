"""
Open-AGC Launcher — Entry point for the packaged application.
Starts the FastAPI server and opens the browser automatically.
Used by PyInstaller as the single-file entry point.
"""
import os
import sys
import webbrowser
import threading
import time

def get_base_dir():
    """Get the base directory (handles both dev and packaged modes)."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller bundle
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.abspath(__file__))

def setup_environment():
    """Set up paths and environment for the packaged app."""
    base_dir = get_base_dir()
    
    # If running from a bundle, ensure data directory exists in a writable location
    if getattr(sys, 'frozen', False):
        # Use user's home directory for writable data
        app_data = os.path.join(os.path.expanduser("~"), ".open-agc")
        os.makedirs(app_data, exist_ok=True)
        
        # Copy default data files if not present
        bundled_data = os.path.join(base_dir, "data")
        if os.path.exists(bundled_data):
            import shutil
            for item in os.listdir(bundled_data):
                src = os.path.join(bundled_data, item)
                dst = os.path.join(app_data, item)
                if not os.path.exists(dst):
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                    elif os.path.isdir(src):
                        shutil.copytree(src, dst)
        
        # Point the app to the writable data directory
        os.environ["OPEN_AGC_DATA_DIR"] = app_data
        
        # Also ensure skills directory
        skills_dir = os.path.join(app_data, "skills")
        os.makedirs(skills_dir, exist_ok=True)
        bundled_skills = os.path.join(base_dir, "skills")
        if os.path.exists(bundled_skills):
            import shutil
            for item in os.listdir(bundled_skills):
                src = os.path.join(bundled_skills, item)
                dst = os.path.join(skills_dir, item)
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
    
    # Change to base directory so relative paths work
    os.chdir(base_dir)

def open_browser_delayed(port=8000, delay=2.0):
    """Open the browser after a short delay to let the server start."""
    def _open():
        time.sleep(delay)
        webbrowser.open(f"http://localhost:{port}")
    
    t = threading.Thread(target=_open, daemon=True)
    t.start()

def main():
    setup_environment()
    
    port = int(os.environ.get("PORT", 8000))
    
    print("=" * 40)
    print("  🐼 Open-AGC Panda is starting...")
    print(f"  http://localhost:{port}")
    print("=" * 40)
    
    # Auto-open browser
    open_browser_delayed(port)
    
    # Start the server
    import uvicorn
    uvicorn.run(
        "api.server:app",
        host="0.0.0.0",
        port=port,
        log_level="info"
    )

if __name__ == "__main__":
    main()
