@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Windows Python launcher 'py' was not found.
        echo Install Python for Windows first, then rerun this script.
        exit /b 1
    )
    set "PYTHON=py -3"
)

echo [0/4] Upgrading pip tooling...
%PYTHON% -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1

echo [1/4] Installing runtime dependencies...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

echo [2/4] Installing build dependencies...
%PYTHON% -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1

echo [3/4] Building gpu_tray.exe ...
%PYTHON% -m PyInstaller --noconfirm --clean gpu_tray.spec
if errorlevel 1 exit /b 1

echo [4/4] Copying config template to dist folder...
if not exist "dist\config.json" copy /Y "config.example.json" "dist\config.json" >nul

echo.
echo Build complete:
echo   %cd%\dist\gpu_tray.exe
echo.
echo Edit dist\config.json before running the executable.
exit /b 0
