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
set USE_PORTABLE=

if exist "%LOCAL_PYTHON%" (
    set PYTHON=%LOCAL_PYTHON%
    set USE_PORTABLE=1
    goto :python_ok
)

:: Check system Python version (>= 3.9)
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set PYTHON=python
        goto :python_ok
    )
)

:: Detect system architecture
if "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    set "PY_ARCH=arm64"
    set "NODE_ARCH=win-arm64"
) else (
    set "PY_ARCH=amd64"
    set "NODE_ARCH=win-x64"
)
echo Detected architecture: %PROCESSOR_ARCHITECTURE% -^> Python: %PY_ARCH%, Node.js: %NODE_ARCH%

:: No suitable Python found - download portable to .python\
echo Python 3.9+ not found. Downloading portable Python to .python\...
echo   (about 30 MB, may take a moment)
if not exist ".python\" mkdir ".python\"

:: Download Python embeddable package with progress
powershell -Command "try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.5/python-3.12.5-embed-%PY_ARCH%.zip' -OutFile '%TEMP%\python-embed.zip' -UseBasicParsing } catch { exit 1 }"
if %errorlevel% neq 0 (
    echo ERROR: Python download failed.
    echo You can manually download the embeddable Python zip and extract it into .python\ then re-run this script:
    echo   https://www.python.org/downloads/
    echo If you are on a corporate network or behind a proxy, configure the proxy first ^(set HTTPS_PROXY=...^) and retry.
    pause
    exit /b 1
)

echo Extracting...
powershell -Command "Expand-Archive -Path '%TEMP%\python-embed.zip' -DestinationPath '.python\' -Force" >nul 2>&1

:: Verify extracted Python works
if not exist ".python\python.exe" (
    echo ERROR: Python.exe not found after extraction.
    pause
    exit /b 1
)

:: Enable pip (remove the ._pth file that disables site-packages)
for %%f in (.python\python3*._pth) do (
    if exist "%%f" move "%%f" "%%f.bak" >nul
)

:: Bootstrap pip
set PYTHON=%~dp0.python\python.exe
set USE_PORTABLE=1

echo Bootstrapping pip...
%PYTHON% -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', r'%TEMP%\get-pip.py')"
if %errorlevel% equ 0 (
    %PYTHON% "%TEMP%\get-pip.py"
)

:python_ok
echo Using: %PYTHON%
%PYTHON% --version

:: ── 2. Python environment & dependencies ───────────────────
:: 便携 Python（.python\，embeddable 包）创建的 venv 不可靠（常缺 pip），
:: 因此直接把依赖装进 .python\ 并用它启动；只有系统 Python 才走 venv。
if defined USE_PORTABLE goto :portable_python

if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
)

echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Upgrade pip inside venv
python -m pip install --upgrade pip --quiet 2>nul

if exist "requirements.txt" (
    echo Installing Python dependencies...
    call python -m pip install -r requirements.txt
)
goto :deps_done

:portable_python
echo Using portable Python directly, installing dependencies into .python\ ...
:: pip 引导失败过的 .python\ 重跑时重试引导
:: 注意：括号块内 %errorlevel% 在解析期一次性展开（恒为进块前的值），
:: 必须用 if errorlevel N / if not errorlevel 1 运行时语法
%PYTHON% -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Bootstrapping pip...
    %PYTHON% -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', r'%TEMP%\get-pip.py')"
    if not errorlevel 1 (
        %PYTHON% "%TEMP%\get-pip.py"
    )
)
if exist "requirements.txt" (
    echo Installing Python dependencies...
    %PYTHON% -m pip install -r requirements.txt
)

:deps_done

:: ── 3. Node.js / frontend build ────────────────────────────
:: Always rebuild if the built SPA is missing (build is fast)
if exist "static\vue\index.html" goto :start_server

:: Prefer local Node.js first, then check system
set "LOCAL_NODE=%~dp0.node\node.exe"

if exist "%LOCAL_NODE%" (
    :: Verify the existing node binary works
    "%LOCAL_NODE%" --version >nul 2>&1
    if errorlevel 1 (
        echo Existing .node\ binary not working (wrong architecture?), re-downloading...
        rmdir /s /q ".node" >nul 2>&1
    ) else (
        set "PATH=%~dp0.node;%PATH%"
        goto :npm_ok
    )
)

where node >nul 2>&1
if %errorlevel% equ 0 (
    set NODE=node
    set NPM=npm
    goto :npm_ok
)

:: No Node.js found - download portable to .node\
echo Node.js not found. Downloading portable Node.js to .node\...
echo   (about 45 MB, may take a moment)
if not exist ".node\" mkdir ".node\"

powershell -Command "try { Invoke-WebRequest -Uri 'https://nodejs.org/dist/v22.14.0/node-v22.14.0-%NODE_ARCH%.zip' -OutFile '%TEMP%\node.zip' -UseBasicParsing } catch { exit 1 }"
if %errorlevel% neq 0 (
    echo ERROR: Node.js download failed.
    echo Try downloading manually from: https://nodejs.org/
    pause
    exit /b 1
)

echo Extracting...
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
    :: node_modules 可能来自旧版本（缺新依赖）——除目录缺失外，关键依赖缺失也要重装
    if not exist "node_modules\@vitejs\plugin-vue" (
        echo Installing Node.js dependencies...
        call npm install
    )
    call npm run build
    echo Frontend build complete.
)

:: ── 4. Start server ────────────────────────────────────────
:start_server
if "%PORT%"=="" (
    %PYTHON% -c "import socket; s=socket.socket(); s.bind(('', 8000)); s.close()" >nul 2>&1
    if errorlevel 1 (
        echo Port 8000 is occupied, finding a free port...
        for /f %%i in ('%PYTHON% -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"') do set PORT=%%i
    ) else (
        set PORT=8000
    )
)

echo ===================================
echo Open-AGC is running at:
echo http://localhost:%PORT%
echo ===================================

if defined USE_PORTABLE (
    .python\python.exe -m uvicorn api.server:app --host 0.0.0.0 --port %PORT%
) else (
    python -m uvicorn api.server:app --host 0.0.0.0 --port %PORT%
)

pause
