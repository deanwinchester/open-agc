@echo off
setlocal

:: Navigate to the script's directory
cd /d "%~dp0"

echo ===================================
echo      Starting Open-AGC (Panda)     
echo ===================================

:: Ensure virtual environment exists
if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    call python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
if exist "requirements.txt" (
    echo Checking / Installing dependencies...
    call python -m pip install -r requirements.txt
)

:: Start the server
if "%PORT%"=="" (
    :: Default to 8000, if occupied (approximated check via python), find a free one
    call python -c "import socket; s=socket.socket(); s.bind(('', 8000)); s.close()" >nul 2>&1
    if errorlevel 1 (
        echo Port 8000 is occupied, finding a free port...
        for /f %%i in ('python -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()"') do set PORT=%%i
    ) else (
        set PORT=8000
    )
)

echo ===================================
echo Open-AGC is running at:
echo http://localhost:%PORT%
echo ===================================

call python -m uvicorn api.server:app --host 0.0.0.0 --port %PORT%

pause
