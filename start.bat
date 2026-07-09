@echo off
setlocal

:: Navigate to the script's directory
cd /d "%~dp0"

echo ===================================
echo      Starting Open-AGC (Panda)
echo ===================================

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in your PATH.
    echo Please install Python 3 from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Ensure virtual environment exists
if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    call python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Upgrade pip inside venv
python -m pip install --upgrade pip --quiet 2>nul

:: Install dependencies
if exist "requirements.txt" (
    echo Checking / Installing dependencies...
    call python -m pip install -r requirements.txt
)

:: Build frontend assets with Vite (Node.js required)
if exist "static\dist\open-agc.css" if exist "static\dist\open-agc.min.js" (
    echo Frontend assets found (static\dist\), skipping build.
) else (
    where npm >nul 2>&1
    if not errorlevel 1 (
        if exist "package.json" (
            echo Building frontend assets with Vite...
            if exist "node_modules" (
                echo node_modules found, skipping install.
            ) else (
                echo Installing Node.js dependencies...
                call npm install
            )
            call npm run build
            echo Frontend build complete.
        )
    ) else (
        echo ===============================================
        echo  WARNING: npm not found.
        echo  Frontend assets will not be built.
        echo  The page may load without CSS/styles.
        echo  To fix: install Node.js and run:
        echo    npm install ^&^& npm run build
        echo ===============================================
    )
)

:: Start the server
if "%PORT%"=="" (
    :: Default to 8000, if occupied, find a free one
    python -c "import socket; s=socket.socket(); s.bind(('', 8000)); s.close()" >nul 2>&1
    if errorlevel 1 (
        echo Port 8000 is occupied, finding a free port...
        :: Use temporary Python script to avoid batch for/f parsing issues
        python -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()" > "%TEMP%\openagc_port.txt"
        set /p PORT=<"%TEMP%\openagc_port.txt"
        del "%TEMP%\openagc_port.txt" 2>nul
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
