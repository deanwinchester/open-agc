@echo off
REM ===========================================
REM  Build Open-AGC.exe for Windows
REM  Usage: build_windows.bat
REM ===========================================

setlocal enabledelayedexpansion

set APP_NAME=Open-AGC
set VERSION=1.0.0

echo =============================================
echo   Build %APP_NAME% v%VERSION% for Windows
echo =============================================

REM Navigate to project root
cd /d "%~dp0"

REM ---- 1. Prepare build environment ----
echo.
echo [1/4] Preparing build environment...

if not exist "build_venv" (
    python -m venv build_venv
)
call build_venv\Scripts\activate.bat

pip install --upgrade pip -q
pip install pyinstaller -q
pip install -r requirements.txt -q

REM ---- 2. Build with PyInstaller ----
echo [2/4] Building with PyInstaller...

REM Create a Windows-specific spec on the fly (no BUNDLE for macOS)
pyinstaller ^
    --name "%APP_NAME%" ^
    --noconsole ^
    --noconfirm ^
    --clean ^
    --add-data "static;static" ^
    --add-data "data;data" ^
    --add-data "skills;skills" ^
    --add-data "agent;agent" ^
    --add-data "core;core" ^
    --add-data "tools;tools" ^
    --add-data "api;api" ^
    --hidden-import uvicorn ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import fastapi ^
    --hidden-import starlette ^
    --hidden-import starlette.routing ^
    --hidden-import starlette.middleware ^
    --hidden-import starlette.responses ^
    --hidden-import starlette.staticfiles ^
    --hidden-import starlette.websockets ^
    --hidden-import litellm ^
    --hidden-import pydantic ^
    --hidden-import dotenv ^
    --hidden-import rich ^
    --hidden-import duckduckgo_search ^
    --hidden-import requests ^
    --hidden-import bs4 ^
    --hidden-import httptools ^
    --hidden-import websockets ^
    --hidden-import api.server ^
    --hidden-import agent.agent ^
    --hidden-import core.llm_client ^
    --hidden-import tools.shell ^
    --hidden-import tools.filesystem ^
    --hidden-import tools.python_repl ^
    --hidden-import tools.computer ^
    --hidden-import tools.memory ^
    --hidden-import tools.web_search ^
    --hidden-import tools.system_mac ^
    launcher.py

if errorlevel 1 (
    echo ERROR: PyInstaller build failed!
    exit /b 1
)

echo   Build complete: dist\%APP_NAME%\

REM ---- 3. Create installer with NSIS (if available) ----
echo [3/4] Creating installer...

where makensis >nul 2>&1
if %errorlevel% equ 0 (
    echo   NSIS found — building installer...
    
    REM Generate NSIS script
    (
        echo !include "MUI2.nsh"
        echo.
        echo Name "%APP_NAME%"
        echo OutFile "dist\%APP_NAME%-%VERSION%-Setup.exe"
        echo InstallDir "$PROGRAMFILES\%APP_NAME%"
        echo RequestExecutionLevel admin
        echo.
        echo !insertmacro MUI_PAGE_DIRECTORY
        echo !insertmacro MUI_PAGE_INSTFILES
        echo !insertmacro MUI_LANGUAGE "SimpChinese"
        echo.
        echo Section "Install"
        echo   SetOutPath "$INSTDIR"
        echo   File /r "dist\%APP_NAME%\*.*"
        echo   CreateShortCut "$DESKTOP\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
        echo   CreateDirectory "$SMPROGRAMS\%APP_NAME%"
        echo   CreateShortCut "$SMPROGRAMS\%APP_NAME%\%APP_NAME%.lnk" "$INSTDIR\%APP_NAME%.exe"
        echo   CreateShortCut "$SMPROGRAMS\%APP_NAME%\Uninstall.lnk" "$INSTDIR\uninstall.exe"
        echo   WriteUninstaller "$INSTDIR\uninstall.exe"
        echo SectionEnd
        echo.
        echo Section "Uninstall"
        echo   RMDir /r "$INSTDIR"
        echo   Delete "$DESKTOP\%APP_NAME%.lnk"
        echo   RMDir /r "$SMPROGRAMS\%APP_NAME%"
        echo SectionEnd
    ) > "dist\installer.nsi"
    
    makensis "dist\installer.nsi"
    del "dist\installer.nsi"
    
    echo   Installer created: dist\%APP_NAME%-%VERSION%-Setup.exe
) else (
    echo   NSIS not found — creating simple ZIP instead...
    
    REM Use PowerShell to create a ZIP
    powershell -Command "Compress-Archive -Path 'dist\%APP_NAME%\*' -DestinationPath 'dist\%APP_NAME%-%VERSION%-Windows.zip' -Force"
    
    echo   ZIP created: dist\%APP_NAME%-%VERSION%-Windows.zip
)

REM ---- 4. Clean up ----
echo [4/4] Cleaning up...
rd /s /q build 2>nul

echo.
echo =============================================
echo   Build complete!
echo   App: dist\%APP_NAME%\%APP_NAME%.exe
if exist "dist\%APP_NAME%-%VERSION%-Setup.exe" (
    echo   Installer: dist\%APP_NAME%-%VERSION%-Setup.exe
) else (
    echo   ZIP: dist\%APP_NAME%-%VERSION%-Windows.zip
)
echo =============================================
echo.
echo To install: Run the Setup.exe or extract the ZIP.
echo To run: Double-click %APP_NAME%.exe

pause
