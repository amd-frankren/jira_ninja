@echo off
REM ============================================================
REM Competency Selector – Internal Service Runner
REM Called by Windows Task Scheduler (CompetencySelector-Server)
REM
REM AZURE_CONFIG_DIR: tells Azure CLI to use Administrator's
REM cached credentials even when running as SYSTEM account.
REM ============================================================
cd /d "%~dp0"

REM Point Azure CLI to Administrator's credential cache
set "AZURE_CONFIG_DIR=C:\Users\Administrator\.azure"

"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe" -m waitress --port=5000 --host=0.0.0.0 server:app >> "%~dp0server.log" 2>&1
