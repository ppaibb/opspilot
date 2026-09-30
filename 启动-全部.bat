@echo off
chcp 65001 >nul
title OpsPilot 启动器
cd /d "%~dp0"

echo 正在分别打开后端与前端窗口 ...
start "OpsPilot 后端" cmd /c "启动-后端.bat"
start "OpsPilot 前端" cmd /c "启动-前端.bat"

echo 已启动，可关闭本窗口。
timeout /t 3 >nul
