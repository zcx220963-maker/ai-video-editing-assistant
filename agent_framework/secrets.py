"""运行期模型密钥：前端「设置」写进 PG，两个进程按当前身份热读。

为什么不「启动时读一次塞进 client」：:8000 主服务与 :8001 Storyline 是两个进程，用户
又随时可能在页面上改 key —— 只有每次请求按当前身份回源（带秒级缓存）才能做到
「保存即生效、不重启任何服务」。
明文只出现在 resolve_api_key 的返回值里，去处只有两个：出网请求的 Authorization 头、
以及掩码前的等值比较。任何展示/日志/报错一律先过 mask()。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .identity import current_identity
from .storage.repositories import mask

API_KEY_NAME = "model_api_key"          # app_secrets 里唯一的键名（一把 key 用到底）
ENV_KEY_NAME = "OPENAI_API_KEY"         # 前端没配时的回落环境变量
CACHE_TTL_SEC = 5.0                     # 另一个进程的生效延迟上限

SOURCE_PG = "前端配置"
SOURCE_ENV = "环境变量 " + ENV_KEY_NAME
SOURCE_FALLBACK = "进程回落位"
SOURCE_NONE = "未配置"

_storage: Any | None = None
_cache: dict[str, tuple[float, str]] = {}      # user_id -> (过期时刻, 库里的值)


def bind_storage(storage: Any) -> None:
    """Storage.start() 里调用：两个服务共用同一份启动路径，所以一处绑定两边都热读。"""
    global _storage
    _storage = storage
    _cache.clear()


def unbind_storage() -> None:
    """解绑并清空缓存（测试隔离；生产里进程活着就一直绑着）。"""
    global _storage
    _storage = None
    _cache.clear()


def bound_storage() -> Any | None:
    return _storage


def current_user_id() -> str:
    """当前执行身份所属用户；不在任何 run 之内时为空串（于是跳过 PG 层）。"""
    ident = current_identity()
    return ident.user_id if ident else ""


def invalidate(user_id: str = "") -> None:
    """作废缓存：保存/删除密钥后本进程立即生效，不传 user_id 就全清。"""
    if user_id:
        _cache.pop(user_id, None)
    else:
        _cache.clear()


async def _from_storage(user_id: str) -> str:
    if _storage is None or not user_id:
        return ""
    now = time.monotonic()
    hit = _cache.get(user_id)
    if hit and hit[0] > now:
        return hit[1]
    try:
        value = await _storage.secrets.get(user_id, API_KEY_NAME)
    except Exception:  # noqa: BLE001 - 存储抖动不该让模型调用直接失败，退回后面的回落层
        value = ""
    _cache[user_id] = (now + CACHE_TTL_SEC, value or "")
    return value or ""


async def resolve_api_key(user_id: str = "", *, fallback: str = "") -> tuple[str, str]:
    """取本次调用真正该用的 key，返回 (值, 来源名)。

    优先级：前端配置（app_secrets，按当前身份）→ 环境变量 → 调用方给的回落位。
    来源名只用于诊断文案，任何情况下都不带出值本身。
    """
    uid = user_id or current_user_id()
    value = (await _from_storage(uid)).strip()
    if value:
        return value, SOURCE_PG
    env = os.environ.get(ENV_KEY_NAME, "").strip()
    if env:
        return env, SOURCE_ENV
    v = (fallback or "").strip()
    if v:
        return v, SOURCE_FALLBACK
    return "", SOURCE_NONE


__all__ = ["API_KEY_NAME", "CACHE_TTL_SEC", "ENV_KEY_NAME", "SOURCE_PG", "SOURCE_ENV",
           "SOURCE_FALLBACK", "SOURCE_NONE", "bind_storage", "unbind_storage",
           "bound_storage", "current_user_id", "invalidate", "resolve_api_key", "mask"]
