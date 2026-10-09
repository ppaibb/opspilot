"""轻量 ASGI 端到端测试客户端。

背景：starlette 0.36.3 的 TestClient 向 httpx.Client 传 `app=` 参数，而 httpx 0.28
已移除该参数（TypeError: unexpected keyword argument 'app'），存量套件因此全部
改为"直接调用路由函数"——代价是中间件/鉴权/校验层被绕过。这里用 httpx 自带的
ASGITransport（异步）+ asyncio.run 门面搭一个客户端，让端到端测试真正走完整
HTTP 栈，零依赖变更。升级 fastapi/starlette 后本文件可整体替换回官方 TestClient。
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx


class ASGITestClient:
    """走完整 ASGI 栈（中间件→路由→校验）的同步测试客户端。"""

    def __init__(self, app: Any, base_url: str = "http://testserver"):
        self._transport = httpx.ASGITransport(app=app)
        self._base_url = base_url

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return asyncio.run(self._request(method, url, **kwargs))

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(transport=self._transport, base_url=self._base_url) as client:
            return await client.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        pass
