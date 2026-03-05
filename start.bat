@echo off
setlocal

:: Navigate to the script's directory
cd /d "%~dp0"

echo ===================================
echo      Starting Open-AGC (Panda)     
echo ===================================

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in your PATH.
    pause
    exit /b 1
)

:: Check for virtual environment
if not exist "venv\" (
    echo Virtual environment not found. Creating one...
    python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies if requirements.txt exists
if exist "requirements.txt" (
    echo Checking / Installing dependencies...
    pip install -r requirements.txt
)

:: Start the server
echo Starting the API server on http://localhost:8000 ...
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000

pause
