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
    python -m venv venv
)

:: Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
if exist "requirements.txt" (
    echo Checking / Installing dependencies...
    python -m pip install -r requirements.txt
)

:: Start the server
echo Starting the API server on http://localhost:8000 ...
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000

pause
