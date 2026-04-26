@echo off
REM ============================================================
REM Competency Selector – Windows Service Installer
REM
REM Creates two Windows Scheduled Tasks:
REM   1. CompetencySelector-Server        (auto-start at boot)
REM   2. CompetencySelector-TokenRefresh  (daily token refresh)
REM
REM REQUIREMENT: Run as Administrator (Right-click → Run as administrator)
REM ============================================================

chcp 65001 >nul
setlocal EnableDelayedExpansion

set "DEPLOY_DIR=%~dp0"
REM Remove trailing backslash
if "%DEPLOY_DIR:~-1%"=="\" set "DEPLOY_DIR=%DEPLOY_DIR:~0,-1%"

REM Read PORT from .env (default 5000)
set "PORT=5000"
if exist "%DEPLOY_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%DEPLOY_DIR%\.env") do (
        if /i "%%A"=="PORT" set "PORT=%%B"
    )
)

echo.
echo  ====================================================
echo   Competency Selector -- Service Installer
echo   Deploy folder : %DEPLOY_DIR%
echo   Port          : %PORT%
echo  ====================================================
echo.
echo  This will register TWO Windows Scheduled Tasks:
echo.
echo  [1] CompetencySelector-Server
echo      Starts the web server automatically at system BOOT
echo      (30-second delay to allow network to initialise)
echo.
echo  [2] CompetencySelector-TokenRefresh
echo      Runs az account get-access-token every day at 06:00 AM
echo      to keep the Azure CLI refresh token alive.
echo.
echo  NOTES:
echo    * Administrator rights are required for Task 1.
echo    * Task 2 runs as the current user (%USERNAME%).
echo    * Server log  : %DEPLOY_DIR%\server.log
echo    * Token log   : %DEPLOY_DIR%\token_refresh.log
echo.
echo  Press any key to install, or Ctrl+C to cancel...
pause >nul

REM ── TASK 1: Server at system boot (SYSTEM account, 30s delay) ────────────
echo.
echo [1/2] Registering server startup task (CompetencySelector-Server)...

schtasks /create ^
    /tn "CompetencySelector-Server" ^
    /tr "\"%DEPLOY_DIR%\_service_runner.bat\"" ^
    /sc onstart ^
    /delay 0000:30 ^
    /ru SYSTEM ^
    /rl HIGHEST ^
    /f >nul 2>&1

if errorlevel 1 (
    echo [WARN] SYSTEM account task failed. Falling back to current user (runs at login).
    schtasks /create ^
        /tn "CompetencySelector-Server" ^
        /tr "\"%DEPLOY_DIR%\_service_runner.bat\"" ^
        /sc onlogon ^
        /ru "%USERNAME%" ^
        /rl HIGHEST ^
        /f >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Could not create server task. Please run this script as Administrator.
        pause
        exit /b 1
    )
    echo [OK]   Task created - starts when user "%USERNAME%" logs in.
    echo        (To start at boot without login: re-run as Administrator)
) else (
    echo [OK]   Task created - starts at system boot (30s delay).
)

REM ── TASK 2: Daily token refresh (current user, 06:00 AM) ─────────────────
echo.
echo [2/2] Registering daily token refresh task (CompetencySelector-TokenRefresh)...

schtasks /create ^
    /tn "CompetencySelector-TokenRefresh" ^
    /tr "cmd /c az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv >> \"%DEPLOY_DIR%\token_refresh.log\" 2>&1" ^
    /sc daily ^
    /st 06:00 ^
    /ru "%USERNAME%" ^
    /rl HIGHEST ^
    /f >nul 2>&1

if errorlevel 1 (
    echo [WARN]  Token refresh task could not be created automatically.
    echo         To create manually: open Task Scheduler and add a daily task that runs:
    echo         az account get-access-token --resource https://graph.microsoft.com
) else (
    echo [OK]   Task created - runs daily at 06:00 AM as "%USERNAME%".
)

REM ── Summary ───────────────────────────────────────────────────────────────
echo.
echo  ====================================================
echo   Installation Complete!
echo  ====================================================
echo.
echo   The server will start automatically on next reboot.
echo.
echo   To start immediately (without rebooting):
echo     start.bat prod
echo.
echo   To check task status:
echo     schtasks /query /tn "CompetencySelector-Server"
echo     schtasks /query /tn "CompetencySelector-TokenRefresh"
echo.
echo   To remove these tasks, run: uninstall-service.bat
echo.
pause
