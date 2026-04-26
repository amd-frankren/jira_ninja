@echo off
REM ============================================================
REM Competency Selector – Windows Service Uninstaller
REM Removes both scheduled tasks created by install-service.bat
REM ============================================================

chcp 65001 >nul

echo.
echo  ====================================================
echo   Competency Selector -- Service Uninstaller
echo  ====================================================
echo.
echo  This will REMOVE the following Scheduled Tasks:
echo    * CompetencySelector-Server
echo    * CompetencySelector-TokenRefresh
echo.
echo  The server WILL NOT stop automatically if currently running.
echo  To stop it: open Task Manager and end "waitress-serve.exe"
echo.
echo  Press any key to uninstall, or Ctrl+C to cancel...
pause >nul

echo.

schtasks /delete /tn "CompetencySelector-Server" /f >nul 2>&1
if errorlevel 1 (
    echo [SKIP]  CompetencySelector-Server was not found.
) else (
    echo [OK]   CompetencySelector-Server removed.
)

schtasks /delete /tn "CompetencySelector-TokenRefresh" /f >nul 2>&1
if errorlevel 1 (
    echo [SKIP]  CompetencySelector-TokenRefresh was not found.
) else (
    echo [OK]   CompetencySelector-TokenRefresh removed.
)

echo.
echo  ====================================================
echo   Uninstall Complete.
echo  ====================================================
echo.
echo   The server will no longer start automatically at boot.
echo   You can still run it manually with: start.bat prod
echo.
pause
