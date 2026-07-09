@echo off
setlocal

:: Open-AGC startup script — auto-installs dependencies on Windows

cd /d "%~dp0"

echo ===================================
echo      Starting Open-AGC (Panda)
echo ===================================

:: ── 1. Python ──────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Attempting to install via winget...
    winget install "Python 3.12" --silent --accept-package-agreements >nul 2>&1
    if %errorlevel% neq 0 (
        echo ===============================================
        echo  Cannot auto-install Python.
        echo  Please install Python 3 manually from:
        echo  https://www.python.org/downloads/
        echo  Then re-run this script.
        echo ===============================================
        pause
        exit /b 1
    )
    echo Python installed. Checking common paths...
    set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
    set "PATH=%PATH%;%ProgramFiles%\Python312;%ProgramFiles%\Python312\Scripts"
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo Python was installed but not found in PATH. Please restart your terminal and re-run.
        pause
        exit /b 1
    )
)

:: ── 2. Virtual environment ─────────────────────────────────
if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    call python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Upgrade pip inside venv
python -m pip install --upgrade pip --quiet 2>nul

:: ── 3. Python dependencies ─────────────────────────────────
if exist "requirements.txt" (
    echo Installing Python dependencies...
    call python -m pip install -r requirements.txt
)

:: ── 4. Node.js / frontend build ────────────────────────────
if exist "static\dist\open-agc.css" if exist "static\dist\open-agc.min.js" (
    echo Frontend assets found in static\dist, skipping build.
    goto :start_server
)

where npm >nul 2>&1
if %errorlevel% neq 0 (
    echo npm not found. Attempting to install Node.js via winget...
    winget install OpenJS.NodeJS --silent --accept-package-agreements >nul 2>&1
    if %errorlevel% neq 0 (
        echo ===============================================
        echo  Cannot auto-install Node.js via winget.
        echo  Please install Node.js manually from:
        echo  https://nodejs.org/
        echo  Then re-run this script.
        echo ===============================================
        pause
        exit /b 1
    )
    echo Node.js installed. Checking common paths...
    set "PATH=%PATH%;%ProgramFiles%\Nodejs;%ProgramFiles(x86)%\Nodejs;%LOCALAPPDATA%\Programs\nodejs"
    where npm >nul 2>&1
    if %errorlevel% neq 0 (
        echo Node.js was installed but npm not found in PATH.
        echo Please restart your terminal and re-run, or add npm to PATH manually.
        pause
        exit /b 1
    )
)

if exist "package.json" (
    echo Building frontend assets with Vite...
    if not exist "node_modules" (
        echo Installing Node.js dependencies...
        call npm install
    )
    call npm run build
    echo Frontend build complete.
)

:: ── 5. Start server ────────────────────────────────────────
:start_server
if "%PORT%"=="" (
    python -c "import socket; s=socket.socket(); s.bind(('', 8000)); s.close()" >nul 2>&1
    if errorlevel 1 (
        echo Port 8000 is occupied, finding a free port...
        for /f %%i in ('python -c "import socket; s=socket.socket(); s.bind((chr(39)*2,0)); print(s.getsockname()[1]); s.close()"') do set PORT=%%i
    ) else (
        set PORT=8000
    )
)

echo ===================================
echo Open-AGC is running at:
echo http://localhost:%PORT%
echo ===================================

python -m uvicorn api.server:app --host 0.0.0.0 --port %PORT%

pause
