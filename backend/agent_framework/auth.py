"""身份与鉴权（spec §7）：`user_id` 一律由 token 反查，不再信客户端传值。

  POST /register → 服务端生成 user_id + 32 字节随机 token，明文只在那一次响应里出现，
  PG 的 `users.token_hash` 只存 sha256。此后：
    HTTP 带 `Authorization: Bearer <token>`；
    浏览器 WebSocket 不能带自定义头，改用 `?token=<token>` 查询参数。

  校验失败统一 401（不区分「用户不存在」与「token 错」，避免探测）；`/health` 不鉴权，
  存活探针要能被任何网关打到。跨 owner 的读取由 repository 的 owner 条件兜住
  （返回「未找到」而不是 403），本模块只负责「你是谁」。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, WebSocket

BEARER = "bearer "


def bearer_token(authorization: str | None) -> str:
    """从 Authorization 头取明文 token；不是 Bearer 方案就当没带。"""
    value = (authorization or "").strip()
    if not value.lower().startswith(BEARER):
        return ""
    return value[len(BEARER):].strip()


class Authenticator:
    def __init__(self, storage: Any) -> None:
        self._users = storage.users

    async def register(self, device_name: str = "") -> tuple[str, str]:
        """签发新身份，返回 (user_id, 明文 token)。"""
        return await self._users.register(device_name)

    async def resolve(self, plain_token: str) -> str | None:
        return await self._users.verify(plain_token)

    async def http_user_id(self, request: Request) -> str:
        """FastAPI 依赖：把请求凭证换成服务端认定的 user_id。"""
        token = bearer_token(request.headers.get("authorization"))
        uid = await self.resolve(token) if token else None
        if uid is None:
            raise HTTPException(401, "缺少或无效凭证：先 POST /register 取 token")
        return uid

    async def ws_user_id(self, websocket: WebSocket) -> str | None:
        """WebSocket 握手：token 不合法就不 accept 直接拒绝（浏览器侧表现为连接失败）。"""
        uid = await self.resolve(websocket.query_params.get("token", ""))
        if uid is None:
            await websocket.close(code=4401)
            return None
        return uid
