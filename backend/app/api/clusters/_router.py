"""Shared FastAPI routers for cluster REST and WebSocket endpoints."""
from fastapi import APIRouter

router = APIRouter()
# Pod 日志/Exec WebSocket 只能使用一次性 session token 校验。
# 不能挂在带 HTTP Bearer 依赖的主 router 下，否则 FastAPI 会在 WebSocket
# 握手阶段尝试解析 OAuth2PasswordBearer，浏览器 WS 无法提供 Request 对象。
ws_router = APIRouter()
