@echo off
REM ============================================================
REM AMD Competency Selector – Windows 啟動腳本
REM 部署主機 : 10.95.37.121
REM 使用者連結: http://10.95.37.121:5000
REM ============================================================
REM 用法：
REM   開發模式  → 雙擊此檔案 或 執行 start.bat
REM   正式環境  → 執行 start.bat prod
REM ============================================================

chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM ── 從 .env 讀取 PORT（預設 5000）───────────────────────────
set "PORT=5000"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="PORT" set "PORT=%%B"
    )
)

echo.
echo  =====================================================
echo   AMD Competency Selector – Internal Deployment Tool
echo   Deploy Host : 10.95.37.121
echo   User URL    : http://10.95.37.121:%PORT%
echo  =====================================================
echo.

REM ── 檢查 Python 是否安裝 ─────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+.
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM ── 檢查 .env 是否存在 ───────────────────────────────────
if not exist ".env" (
    echo [WARN] .env not found. Copying from .env.example...
    copy ".env.example" ".env" >nul
    echo [WARN] Please open .env and fill in SUB_KEY and other values before continuing.
    notepad ".env"
    pause
    exit /b 1
)

REM ── 安裝依賴（若尚未安裝）────────────────────────────────
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing Python dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check your network or proxy settings.
        pause
        exit /b 1
    )
)

REM ── 判斷啟動模式 ─────────────────────────────────────────
if /i "%1"=="prod" (
    echo [INFO] Starting in PRODUCTION mode via waitress...
    echo [INFO] Binding : http://0.0.0.0:%PORT%
    echo [INFO] User URL: http://10.95.37.121:%PORT%
    echo.
    python -c "import waitress" >nul 2>&1
    if errorlevel 1 (
        pip install waitress
    )
    waitress-serve --port=%PORT% --host=0.0.0.0 server:app
) else (
    echo [INFO] Starting in DEVELOPMENT mode...
    echo [INFO] Local URL : http://localhost:%PORT%
    echo [INFO] Network   : http://10.95.37.121:%PORT%
    echo [INFO] Tip: run 'start.bat prod' for production mode.
    echo.
    set DEBUG=true
    python server.py
)

pause
