@echo off
setlocal

:: Open-AGC startup script - auto-installs dependencies on Windows
:: All runtimes are kept local (.python\, .node\) — no system-wide installs.

cd /d "%~dp0"

echo ===================================
echo      Starting Open-AGC (Panda)
echo ===================================

:: ── 1. Python ──────────────────────────────────────────────
:: Prefer local Python first, then check system
set "LOCAL_PYTHON=%~dp0.python\python.exe"
set PYTHON=

if exist "%LOCAL_PYTHON%" (
    set PYTHON=%LOCAL_PYTHON%
    goto :python_ok
)

:: Check system Python version (>= 3.9)
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
    if %errorlevel% equ 0 (
        set PYTHON=python
        goto :python_ok
    )
)

:: No suitable Python found — download portable to .python\
echo Python 3.9+ not found. Downloading portable Python to .python\...
if not exist ".python\" mkdir ".python\"

:: Download Python embeddable package
powershell -Command "& { param($url, $out) try { $wc = New-Object System.Net.WebClient; $wc.DownloadFile($url, $out) } catch { exit 1 } }" ^
  -url "https://www.python.org/ftp/python/3.12.5/python-3.12.5-embed-amd64.zip" ^
  -out "%TEMP%\python-embed.zip" 2>nul

if %errorlevel% neq 0 (
    echo Download failed. Please install Python 3.9+ manually:
    echo   https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Extract
powershell -Command "Expand-Archive -Path '%TEMP%\python-embed.zip' -DestinationPath '.python\' -Force" >nul 2>&1

:: Enable pip (remove the ._pth file that disables site-packages)
if exist ".python\python312._pth" (
    move ".python\python312._pth" ".python\python312._pth.bak" >nul
)

:: Bootstrap pip
set PYTHON=%~dp0.python\python.exe

echo Bootstrapping pip...
%PYTHON% -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '%TEMP%\get-pip.py')" >nul 2>&1
if %errorlevel% equ 0 (
    %PYTHON% "%TEMP%\get-pip.py" --quiet >nul 2>&1
)

:python_ok
echo Using: %PYTHON%
%PYTHON% --version

:: ── 2. Virtual environment ─────────────────────────────────
if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    %PYTHON% -m venv venv
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

:: Prefer local Node.js first, then check system
set "LOCAL_NODE=%~dp0.node\node.exe"
set NODE=
set NPM=

if exist "%LOCAL_NODE%" (
    set "PATH=%~dp0.node;%PATH%"
    set NODE=node
    set NPM=npm
    goto :npm_ok
)

where node >nul 2>&1
if %errorlevel% equ 0 (
    set NODE=node
    set NPM=npm
    goto :npm_ok
)

:: No Node.js found — download portable to .node\
echo Node.js not found. Downloading portable Node.js to .node\...
if not exist ".node\" mkdir ".node\"

powershell -Command "& { param($url, $out) try { $wc = New-Object System.Net.WebClient; $wc.DownloadFile($url, $out) } catch { exit 1 } }" ^
  -url "https://nodejs.org/dist/v22.14.0/node-v22.14.0-win-x64.zip" ^
  -out "%TEMP%\node.zip" 2>nul

if %errorlevel% neq 0 (
    echo ===============================================
    echo  Download failed. Please install Node.js manually:
    echo    https://nodejs.org/
    echo  Then re-run this script.
    echo ===============================================
    pause
    exit /b 1
)

:: Extract
powershell -Command "Expand-Archive -Path '%TEMP%\node.zip' -DestinationPath '.node\' -Force" >nul 2>&1

:: Move files from the versioned subdir up (e.g. .node\node-v22.14.0-win-x64\* -> .node\)
for /d %%d in (.node\node-v*) do (
    if exist "%%d\npm.cmd" (
        xcopy "%%d\*" ".node\" /e /y /q >nul
        rmdir "%%d" /s /q >nul
    )
)

set "PATH=%~dp0.node;%PATH%"
:npm_ok

where npm >nul 2>&1
if %errorlevel% neq 0 (
    echo Node.js was downloaded but npm not found. Please install Node.js manually.
    pause
    exit /b 1
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
