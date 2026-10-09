@echo off
setlocal
chcp 65001 >nul
title OpsPilot 一键部署（增量更新）
cd /d "%~dp0"

rem 用法：部署-更新.bat root@目标机IP [参数]
rem 例：  部署-更新.bat root@10.10.0.9
rem       部署-更新.bat root@10.10.0.9 -FrontendOnly
rem       部署-更新.bat root@10.10.0.9 -CheckOnly

if not "%~1"=="" goto :run

echo 用法: %~nx0 root@目标机IP [参数]
echo 参数: -BackendOnly / -FrontendOnly / -CheckOnly
echo       -BackendTag X.Y.Z -FrontendTag X.Y.Z
echo       -StageDir /opt/xxx
echo.
set /p TARGET=目标机 [root@10.10.0.9]:
if "%TARGET%"=="" set "TARGET=root@10.10.0.9"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\update\deploy_update.ps1" -Host_ %TARGET%
goto :done

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\update\deploy_update.ps1" -Host_ %*

:done
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" (
    echo [OK] 部署完成
) else (
    echo [FAILED] 退出码 %RC%
)
pause
exit /b %RC%
