@echo off
setlocal

:: Navigate to script directory
cd /d "%~dp0"

set APP_NAME=Open-AGC
set VERSION=1.0.0

echo =============================================
echo   🐼 Building %APP_NAME% v%VERSION% (Windows)
echo =============================================

echo.
echo [1/4] Preparing build environment...

if not exist build_venv (
    python.exe -m venv build_venv
)

call build_venv\Scripts\activate.bat

echo Upgrading pip and installing tools...
python.exe -m pip install --upgrade pip -q
python.exe -m pip install pyinstaller -q
python.exe -m pip install -r requirements.txt -q
python.exe -m pip install httptools websockets pywebview pyautogui Pillow opencv-python pywin32 -q

echo.
echo [2/4] Building application...

:: Ensure fresh build
if exist "dist\%APP_NAME%" rmdir /s /q "dist\%APP_NAME%"
if exist "build\win" rmdir /s /q "build\win"

pyinstaller open_agc.spec --clean --noconfirm ^
    --distpath "dist" ^
    --workpath "build\win" 

echo   ✅ Build complete: dist\%APP_NAME%

echo.
echo [3/4] Creating ZIP package...
:: We use PowerShell to create a zip file
set ZIP_NAME=%APP_NAME%-%VERSION%-Windows-x64.zip
if exist "dist\%ZIP_NAME%" del "dist\%ZIP_NAME%"

powershell -Command "Compress-Archive -Path 'dist\%APP_NAME%' -DestinationPath 'dist\%ZIP_NAME%'"

echo   ✅ ZIP created: dist\%ZIP_NAME%

echo.
echo [4/4] Cleaning up...
rmdir /s /q build\win

echo.
echo =============================================
echo   ✅ Windows Build complete!
echo   📦 dist\%ZIP_NAME%
echo =============================================
pause
