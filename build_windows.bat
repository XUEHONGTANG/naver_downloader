@echo off
chcp 65001 >nul
REM Run once on Windows to build dist\NaverDownloader.exe
cd /d "%~dp0"
py -m venv .venv-build || python -m venv .venv-build || (echo Python not found. Install from https://www.python.org and check "Add Python to PATH". & pause & exit /b 1)
.venv-build\Scripts\python -m pip install -q playwright pyinstaller
.venv-build\Scripts\pyinstaller --noconfirm --onefile --windowed --name "NaverDownloader" naver_downloader.py
echo.
echo Done! File: dist\NaverDownloader.exe
pause
