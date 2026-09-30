@echo off
chcp 65001 >nul
title OpsPilot 后端
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv，请先在 backend 目录创建并安装依赖：
    echo         python -m venv .venv
    echo         .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

echo 正在启动后端 http://127.0.0.1:8001 ...
echo [注意] --reload 仅供开发热更新；生产部署请去掉 --reload 并按需改 --host 0.0.0.0（局域网 agent 回连需要）。
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
pause
