@echo off
chcp 65001 >nul
title OpsPilot 前端
cd /d "%~dp0frontend"

where pnpm >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 pnpm，请先安装：npm install -g pnpm
    pause
    exit /b 1
)

if not exist "node_modules" (
    echo 首次运行，正在安装依赖 ...
    call pnpm install
)

echo 正在启动前端开发服务器 ...
call pnpm dev
pause
