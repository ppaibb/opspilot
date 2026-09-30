@echo off
chcp 65001 >nul
title OpsPilot Full Check
cd /d "%~dp0"

rem Unified verification entry; all logic lives in scripts\run_checks.py
rem Usage (from Git Bash): cmd //c 检查-全量.bat [--secret-scan]

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Root .venv not found: %CD%\.venv\Scripts\python.exe
    exit /b 1
)
if not exist "scripts\run_checks.py" (
    echo [ERROR] scripts\run_checks.py not found.
    exit /b 1
)

".venv\Scripts\python.exe" scripts\run_checks.py %*
set "CHECK_EXIT=%ERRORLEVEL%"

if "%CHECK_EXIT%"=="0" (
    echo [OK] All checks passed.
) else (
    echo [FAILED] Checks failed, exit code %CHECK_EXIT%.
)
exit /b %CHECK_EXIT%
