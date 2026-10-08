@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Creating Python environment - this only happens once...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo FAILED to create the Python environment. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo Checking dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo FAILED to install dependencies. Check your internet connection and try again.
    pause
    exit /b 1
)

start "" /min cmd /c "timeout /t 4 /nobreak >nul && start "" http://127.0.0.1:8420"

REM Only open the server to other devices once a password has been set (see set_password.bat).
set HOST=127.0.0.1
if exist "data\access.json" set HOST=0.0.0.0

echo.
if "%HOST%"=="0.0.0.0" (
    for /f %%i in ('python -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('10.255.255.255',1));print(s.getsockname()[0])" 2^>nul') do set LANIP=%%i
    echo Network access is ON. From another device on your home network open:
    echo     http://%COMPUTERNAME%:8420
    if defined LANIP echo     or http://%LANIP%:8420
    echo Username can be anything; the password is the one you set with set_password.bat.
) else (
    echo Network access is OFF ^(this PC only^). To let another device use the app, run
    echo set_password.bat and allow_network_access.bat, then restart this.
)
echo.
echo Starting the server... keep this window open while you use the app.
echo.
python -m uvicorn app.main:app --host %HOST% --port 8420

echo.
echo The server has stopped. If that was unexpected, check the message above.
pause

endlocal
