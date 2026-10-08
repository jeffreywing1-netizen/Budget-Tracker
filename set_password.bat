@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Please run start.bat once first so the app's Python environment gets created.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m app.set_password
echo.
pause
endlocal
