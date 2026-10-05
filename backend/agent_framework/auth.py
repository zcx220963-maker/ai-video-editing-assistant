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

import base64
import hashlib
import hmac
import json
import os
import secrets as _pysecrets
import time
from typing import Any

from fastapi import HTTPException, Request, WebSocket

BEARER = "bearer "

# ---- 密码哈希（PBKDF2-HMAC-SHA256，标准库零依赖）----
_PBKDF2_ITER = 300_000


def hash_password(password: str) -> str:
    salt = _pysecrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt.encode("ascii"), _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt, expect = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("ascii"), int(iters))
        return hmac.compare_digest(dk.hex(), expect)
    except Exception:
        return False


# ---- JWT（HS256，手写实现，避免为这一个用途引 pyjwt）----
_JWT_TTL_SEC = 7 * 24 * 3600


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwt_issue(user_id: str, secret: bytes, *, ttl_sec: int = _JWT_TTL_SEC) -> str:
    header = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"},
                               separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64u(json.dumps(
        {"sub": user_id, "iat": now, "exp": now + ttl_sec,
         "jti": _pysecrets.token_hex(8)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}"
    sig = _b64u(hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest())
    return f"{signing_input}.{sig}"


def jwt_verify(token: str, secret: bytes) -> str | None:
    """验签 + 验过期，返回 sub（user_id）；任何一步不对都返回 None。"""
    try:
        header, payload, sig = token.split(".")
        expect = _b64u(hmac.new(secret, f"{header}.{payload}".encode("ascii"),
                                hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        pad = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + pad))
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        return str(claims.get("sub")) or None
    except Exception:
        return None


def bearer_token(authorization: str | None) -> str:
    """从 Authorization 头取明文 token；不是 Bearer 方案就当没带。"""
    value = (authorization or "").strip()
    if not value.lower().startswith(BEARER):
        return ""
    return value[len(BEARER):].strip()


class Authenticator:
    def __init__(self, storage: Any) -> None:
        self._storage = storage
        self._users = storage.users
        self._secret: bytes | None = None

    async def register(self, device_name: str = "") -> tuple[str, str]:
        """签发新身份（匿名/CLI 通道），返回 (user_id, 明文 token)。"""
        return await self._users.register(device_name)

    async def resolve(self, plain_token: str) -> str | None:
        return await self._users.verify(plain_token)

    # ---- 账号密码 + JWT ----

    async def _jwt_secret(self) -> bytes:
        """JWT 签名密钥：JWT_SECRET 环境变量优先；否则取 app_secrets 里
        内部身份 default 名下的 jwt_secret（首次自动生成并落库——重启不失效）；
        存储不可用时退回进程内临时密钥（重启后需重新登录，只降级不报错）。"""
        if self._secret:
            return self._secret
        env = os.environ.get("JWT_SECRET")
        if env:
            self._secret = env.encode("utf-8")
            return self._secret
        try:
            row = await self._storage.secrets.row("default", "jwt_secret")
            value = (row or {}).get("value") or ""
            if not value:
                value = _pysecrets.token_urlsafe(48)
                await self._storage.secrets.put("default", "jwt_secret", value)
            self._secret = value.encode("utf-8")
        except Exception:
            self._secret = _pysecrets.token_urlsafe(48).encode("utf-8")
        return self._secret

    async def register_user(self, username: str, password: str) -> str:
        uid = await self._users.register_user(username, hash_password(password))
        return uid

    async def issue_jwt(self, user_id: str) -> str:
        return jwt_issue(user_id, await self._jwt_secret())

    async def login(self, username: str, password: str) -> str | None:
        """账号密码 → JWT；用户不存在或密码不对一律 None（不区分，防探测）。"""
        row = await self._users.find_by_username(username)
        if row is None or not row.get("password_hash"):
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return jwt_issue(row["id"], await self._jwt_secret())

    async def resolve_credential(self, credential: str) -> str | None:
        """双凭证解析：JWT（两处点）或旧随机 token（sha256 反查）。"""
        cred = (credential or "").strip()
        if not cred:
            return None
        if cred.count(".") == 2:
            sub = jwt_verify(cred, await self._jwt_secret())
            if sub is None:
                return None
            row = await self._users.get(sub)
            return row["id"] if row else None
        return await self.resolve(cred)

    async def http_user_id(self, request: Request) -> str:
        """FastAPI 依赖：把请求凭证换成服务端认定的 user_id。"""
        token = bearer_token(request.headers.get("authorization"))
        uid = await self.resolve_credential(token) if token else None
        if uid is None:
            raise HTTPException(401, "缺少或无效凭证：请先登录")
        return uid

    async def ws_user_id(self, websocket: WebSocket) -> str | None:
        """WebSocket 握手：凭证不合法就不 accept 直接拒绝（浏览器侧表现为连接失败）。"""
        uid = await self.resolve_credential(websocket.query_params.get("token", ""))
        if uid is None:
            await websocket.close(code=4401)
            return None
        return uid
