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

API_KEY_NAME = "model_api_key"          # 主模型密钥（一把 key 用到底）
MAIN_MODEL_KEY = "model_name"           # 主模型名（按用户覆盖;空 = 默认 DeepSeek）
MAIN_BASE_KEY = "base_url"              # 主模型地址（按用户覆盖;空 = 默认）
JUDGE_BASE_KEY = "judge_base_url"       # 判断模型三件套(base_url/model/key)
JUDGE_MODEL_KEY = "judge_model"
JUDGE_API_KEY_NAME = "judge_api_key"
ENV_KEY_NAME = "OPENAI_API_KEY"         # 前端没配时的回落环境变量（首选）
# .env 里还留着 DEEPSEEK_API_KEY / SILICONFLOW_API_KEY 这两把历史名字。原先没有任何代码
# 读它们——用户填了、静默无效，还以为「填了就生效」。现在把它们接成同一层的回落位，
# 顺序即优先级（OPENAI_API_KEY 仍是首选，页面配置永远压过环境变量）。
ENV_KEY_NAMES = (ENV_KEY_NAME, "DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY")
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


def env_api_key() -> tuple[str, str]:
    """环境变量层的回落：按 ``ENV_KEY_NAMES`` 的顺序取第一把非空的，返回 (值, 来源名)。

    来源名写的是**变量名本身**而不是笼统的「环境变量」——用户填了三处之一，报错时得看得
    出程序实际读到了哪一把。值本身只在这个函数与调用方的返回值里出现，展示一律先过掩码。
    """
    for name in ENV_KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"环境变量 {name}"
    return "", ""


async def resolve_api_key(user_id: str = "", *, fallback: str = "") -> tuple[str, str]:
    """取本次调用真正该用的 key，返回 (值, 来源名)。

    优先级：前端配置（app_secrets，按当前身份）→ 环境变量（按 ``ENV_KEY_NAMES`` 的顺序）
    → 调用方给的回落位。来源名只用于诊断文案，任何情况下都不带出值本身。
    """
    uid = user_id or current_user_id()
    value = (await _from_storage(uid)).strip()
    if value:
        return value, SOURCE_PG
    env, source = env_api_key()
    if env:
        return env, source
    v = (fallback or "").strip()
    if v:
        return v, SOURCE_FALLBACK
    return "", SOURCE_NONE


__all__ = ["API_KEY_NAME", "CACHE_TTL_SEC", "ENV_KEY_NAME", "ENV_KEY_NAMES", "SOURCE_PG",
           "SOURCE_ENV", "SOURCE_FALLBACK", "SOURCE_NONE", "bind_storage", "unbind_storage",
           "bound_storage", "current_user_id", "env_api_key", "invalidate",
           "resolve_api_key", "mask"]


# ---- 主模型 model/base_url 的按用户覆盖（前端「设置」→ app_secrets）----


async def resolve_model_base(*, fallback_model: str,
                             fallback_base: str) -> tuple[str, str]:
    """按当前身份解析主模型名与地址:前端存了就用,否则回落构造默认。

    与 ``resolve_api_key`` 同一优先级:前端配置压过环境变量与构造默认。
    """
    user = current_user_id()
    if _storage is None or not user:
        return fallback_model, fallback_base
    try:
        model = (await _storage.secrets.get(user, MAIN_MODEL_KEY)) or ""
        base = (await _storage.secrets.get(user, MAIN_BASE_KEY)) or ""
    except Exception:  # noqa: BLE001 - 存储抖动退回默认,不阻塞模型调用
        return fallback_model, fallback_base
    return (model or fallback_model), (base or fallback_base)


async def put_user_key(user_id: str, key_name: str, value: str) -> None:
    if not value:
        return
    await _storage.secrets.put(user_id, key_name, value)


async def get_user_key(user_id: str, key_name: str) -> str:
    if _storage is None or not user_id:
        return ""
    try:
        return (await _storage.secrets.get(user_id, key_name)) or ""
    except Exception:  # noqa: BLE001
        return ""


async def drop_user_key(user_id: str, key_name: str) -> None:
    if _storage is None or not user_id:
        return
    try:
        await _storage.secrets.drop(user_id, key_name)
    except Exception:  # noqa: BLE001
        pass
