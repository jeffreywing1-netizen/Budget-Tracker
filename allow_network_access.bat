@echo off
REM One-time setup: lets other devices on your HOME network reach the app on port 8420.
REM Needs administrator rights, so it asks Windows for them. Applies to "Private" networks only
REM (your home Wi-Fi), and only to devices on the same local subnet -- not the internet.
setlocal

net session >nul 2>&1
if errorlevel 1 (
    echo Windows will ask for permission to add a firewall rule...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

netsh advfirewall firewall delete rule name="Budget Tracker (home network)" >nul 2>&1
netsh advfirewall firewall add rule name="Budget Tracker (home network)" dir=in action=allow protocol=TCP localport=8420 profile=private remoteip=localsubnet
if errorlevel 1 (
    echo.
    echo FAILED to add the firewall rule.
) else (
    echo.
    echo Done. Devices on your home network can now reach the app while it's running.
    echo NOTE: this only works if this PC's Wi-Fi/network is set to "Private" in Windows
    echo       (Settings ^> Network ^& internet ^> Wi-Fi ^> your network ^> Network profile type).
)
echo.
pause
endlocal
