@echo off
setlocal
cd /d "%~dp0"
title SongSplitter

rem  .venv-gpu = PyTorch for AMD ROCm (RX 7800 XT), .venv = CPU-only fallback.
rem  Run "run.bat cpu" to force the CPU version.
set "PY=.venv-gpu\Scripts\python.exe"
if /i "%~1"=="cpu" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [SongSplitter] First run: creating virtual environment...
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    echo [SongSplitter] Installing PyTorch ^(CPU^) - this can take a few minutes...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu || goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
    echo [SongSplitter] See README.md to enable GPU acceleration.
)

rem  Keep yt-dlp current: sites like YouTube change often and old versions stop working.
echo [SongSplitter] Checking for downloader updates...
"%PY%" -m pip install -U "yt-dlp[default]" -q --disable-pip-version-check >nul 2>&1

"%PY%" app.py
pause
exit /b 0

:fail
echo.
echo [SongSplitter] Installation failed. Delete the .venv folder and try again.
pause
exit /b 1
