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
# 联网检索的后端选择与密钥（前端「设置」→ app_secrets，与模型密钥同一套热读机制）。
# 为什么要有 provider/base_url 两个非密钥项一起存：searxng 这类自建实例**没有 key**，
# 只有地址；而 brave/tavily/serpapi 只有 key 没有地址。单独存 key 的话自建那条路走不通。
SEARCH_PROVIDER_KEY = "search_provider"   # brave|tavily|serpapi|searxng（空 = 免 key 回落链）
SEARCH_API_KEY_NAME = "search_api_key"
SEARCH_BASE_KEY = "search_base_url"       # searxng 实例地址；填了才生效
ENV_SEARCH_KEY_NAME = "SEARCH_API_KEY"
ENV_SEARCH_PROVIDER = "SEARCH_PROVIDER"
ENV_SEARCH_BASE = "SEARCH_BASE_URL"
# 图像出图后端三件套（R3 §5.3）：照 search_* 那套模式，含"自建实例有地址无 key"那一档。
# provider 为空 = 没配出图后端，整条出图路径优雅降级为素材库检索。
IMAGE_PROVIDER_KEY = "image_provider"     # siliconflow|minimax|zhipu|aliyun|volc（空 = 降级为素材库检索）
IMAGE_API_KEY_NAME = "image_api_key"
IMAGE_BASE_KEY = "image_base_url"         # 自建实例地址；填了才生效
# 出图模型名与尺寸（R6-T24）。以前这两个值是**写死在请求体里的常量**
# （`server.py` 的 `"model": "doubao-seedream-5-0-pro"` + `"size": "1024x1024"`），
# 于是「换一家后端」必须改代码，而火山那条默认值会被原样发给硅基流动——换后端的人第一
# 次调用就拿到 404/400，还得回来读源码才知道哪里写死了。改成与 provider 同一套热读后，
# 「换后端」与「换模型」是两件独立可配的事；空值 = 用该 provider 的默认模型（见
# `image_backend.PROVIDER_DEFAULT_MODELS`），不是「不传 model」。
IMAGE_MODEL_KEY = "image_model"
IMAGE_SIZE_KEY = "image_size"
ENV_IMAGE_KEY_NAME = "IMAGE_API_KEY"
ENV_IMAGE_PROVIDER = "IMAGE_PROVIDER"
ENV_IMAGE_BASE = "IMAGE_BASE_URL"
ENV_IMAGE_MODEL = "IMAGE_MODEL"
ENV_IMAGE_SIZE = "IMAGE_SIZE"
# 配额也走同一套按身份热读：以前它只有 env 一处，超限的用户只能改编排文件 + 重建容器，
# 而页面上一句「配额已用尽」连出路都没有。现在页面填的值压过 env，保存即生效。
QUOTA_LIMIT_KEY = "quota_limit"           # 窗口内 token 上限，0 或空 = 只记账不拦
QUOTA_WINDOW_KEY = "quota_window"         # 窗口秒数
ENV_QUOTA_LIMIT = "QUOTA_LIMIT"
ENV_QUOTA_WINDOW = "QUOTA_WINDOW"
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
           "resolve_api_key", "resolve_search_config", "resolve_image_config",
           "resolve_image_options",
           "resolve_quota", "parse_int", "mask",
           "SEARCH_PROVIDER_KEY", "SEARCH_API_KEY_NAME", "SEARCH_BASE_KEY",
           "ENV_SEARCH_KEY_NAME", "ENV_SEARCH_PROVIDER", "ENV_SEARCH_BASE",
           "IMAGE_PROVIDER_KEY", "IMAGE_API_KEY_NAME", "IMAGE_BASE_KEY",
           "IMAGE_MODEL_KEY", "IMAGE_SIZE_KEY", "ENV_IMAGE_MODEL", "ENV_IMAGE_SIZE",
           "ENV_IMAGE_KEY_NAME", "ENV_IMAGE_PROVIDER", "ENV_IMAGE_BASE",
           "QUOTA_LIMIT_KEY", "QUOTA_WINDOW_KEY", "ENV_QUOTA_LIMIT", "ENV_QUOTA_WINDOW"]


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


async def resolve_search_config(user_id: str = "") -> tuple[str, str, str, str]:
    """联网检索后端的按身份热读，返回 (provider, api_key, base_url, 来源名)。

    优先级与模型密钥一致：**前端「设置」压过环境变量**。provider 为空 = 没配后端，
    调用方走免 key 回落链；searxng 是自建实例，**只有 base_url 就够**（无 key）。

    来源名只写层名与 provider，绝不带出 key 值本身——展示侧任何一次泄漏都是事故。
    """
    uid = user_id or current_user_id()
    provider = key = base = ""
    if _storage is not None and uid:
        try:
            provider = ((await _storage.secrets.get(uid, SEARCH_PROVIDER_KEY)) or "").strip()
            key = ((await _storage.secrets.get(uid, SEARCH_API_KEY_NAME)) or "").strip()
            base = ((await _storage.secrets.get(uid, SEARCH_BASE_KEY)) or "").strip()
        except Exception:  # noqa: BLE001 - 存储抖动退回环境变量层，不让检索整条挂掉
            provider = key = base = ""
        if provider or key or base:
            return provider, key, base, SOURCE_PG
    env_provider = os.environ.get(ENV_SEARCH_PROVIDER, "").strip()
    env_key = os.environ.get(ENV_SEARCH_KEY_NAME, "").strip()
    env_base = os.environ.get(ENV_SEARCH_BASE, "").strip()
    if env_provider or env_key or env_base:
        return env_provider, env_key, env_base, f"环境变量 {ENV_SEARCH_PROVIDER or ENV_SEARCH_KEY_NAME}"
    return "", "", "", SOURCE_NONE


async def resolve_image_config(user_id: str = "", *,
                               storage: Any = None) -> tuple[str, str, str, str]:
    """图像出图后端的按身份热读，返回 (provider, api_key, base_url, 来源名)。

    照 ``resolve_search_config`` 同一套模式：前端「设置」压过环境变量。
    provider 为空 = 没配出图后端，调用方走**素材库检索降级**（§5.3）。
    自建实例只有 base_url 就够（无 key）。

    ``storage`` 显式给定时压过进程绑定：出图节点长在 **Storyline 那个进程**里，而
    ``bind_storage`` 只保证「绑过的那个进程」能读——两个进程各绑各的，不能指望对方的
    全局变量。给一个显式入参比再复制一份「页面压过 env」的判据强：判定只有一处，
    两边的取值口径就不可能漂。
    """
    st = storage if storage is not None else _storage
    uid = user_id or current_user_id()
    provider = key = base = ""
    if st is not None and uid:
        try:
            provider = ((await st.secrets.get(uid, IMAGE_PROVIDER_KEY)) or "").strip()
            key = ((await st.secrets.get(uid, IMAGE_API_KEY_NAME)) or "").strip()
            base = ((await st.secrets.get(uid, IMAGE_BASE_KEY)) or "").strip()
        except Exception:  # noqa: BLE001
            provider = key = base = ""
        if provider or key or base:
            return provider, key, base, SOURCE_PG
    env_provider = os.environ.get(ENV_IMAGE_PROVIDER, "").strip()
    env_key = os.environ.get(ENV_IMAGE_KEY_NAME, "").strip()
    env_base = os.environ.get(ENV_IMAGE_BASE, "").strip()
    if env_provider or env_key or env_base:
        return env_provider, env_key, env_base, f"环境变量 {ENV_IMAGE_PROVIDER or ENV_IMAGE_KEY_NAME}"
    return "", "", "", SOURCE_NONE


async def resolve_image_options(user_id: str = "", *,
                                storage: Any = None) -> tuple[str, str, str]:
    """出图模型名与尺寸的按身份热读，返回 (model, size, 来源名)。

    与 :func:`resolve_image_config` **分开**读：provider 与 model 是两件事，合成一个
    四元组会让「换了后端但没换模型」这件事在返回值里看不出来。空 model / 空 size 不是
    错误：调用方按 provider 的默认值补（``image_backend.default_model``），
    「不填」与「填了个空的」在这里是同一件事。
    """
    st = storage if storage is not None else _storage
    uid = user_id or current_user_id()
    model = size = ""
    if st is not None and uid:
        try:
            model = ((await st.secrets.get(uid, IMAGE_MODEL_KEY)) or "").strip()
            size = ((await st.secrets.get(uid, IMAGE_SIZE_KEY)) or "").strip()
        except Exception:  # noqa: BLE001
            model = size = ""
        if model or size:
            return model, size, SOURCE_PG
    env_model = os.environ.get(ENV_IMAGE_MODEL, "").strip()
    env_size = os.environ.get(ENV_IMAGE_SIZE, "").strip()
    if env_model or env_size:
        return env_model, env_size, f"环境变量 {ENV_IMAGE_MODEL or ENV_IMAGE_SIZE}"
    return "", "", SOURCE_NONE


def parse_int(raw: str) -> int | None:
    """页面传来的可能是 "8,000,000" / "8000000 " / 空串；解析不出整数就当作没填。

    返回 None 表示「这一层没值」，与 0（明确填零 = 不拦截）是两回事——
    把「填了 0」当成「没填」的话，环境变量层的正数会盖掉用户在页面上的关闭操作。
    """
    s = (raw or "").strip().replace(",", "").replace("_", "")
    if not s:
        return None
    try:
        value = int(float(s))
    except ValueError:
        return None
    return value if value >= 0 else None


async def resolve_quota(user_id: str = "", *, default_limit: int = 0,
                        default_window: int = 86400) -> tuple[int, int, str]:
    """配额的按身份热读，返回 (上限 token, 窗口秒, 来源名)。

    三层各表各的：**页面「设置」→ 环境变量 → 调用方给的启动默认**，谁填了谁说话，
    没填（None）才轮到下一层。这里的关键是「填了 0」与「没填」必须区分开：
    页面填 0 是用户明确要关掉拦截，环境变量里的正数不许把它盖回去；
    反过来，env 没设时也不能被误当成 0 而让启动参数的正数失效。

    上限 <=0 = 不拦截，但 UsageHook 照样落账——额度观测与成本核算不该因为不拦了就断数据源。
    """
    uid = user_id or current_user_id()
    pg_limit = pg_window = None
    if _storage is not None and uid:
        try:
            pg_limit = parse_int(await _storage.secrets.get(uid, QUOTA_LIMIT_KEY) or "")
            pg_window = parse_int(await _storage.secrets.get(uid, QUOTA_WINDOW_KEY) or "")
        except Exception:  # noqa: BLE001 - 存储抖动退回下一层，不该让模型调用整条挂掉
            pg_limit = pg_window = None
    env_limit = parse_int(os.environ.get(ENV_QUOTA_LIMIT, ""))
    env_window = parse_int(os.environ.get(ENV_QUOTA_WINDOW, ""))
    limit = (pg_limit if pg_limit is not None
             else env_limit if env_limit is not None else default_limit)
    window = (pg_window if pg_window is not None
              else env_window if env_window is not None else default_window)
    if pg_limit is not None or pg_window is not None:
        source = SOURCE_PG
    elif env_limit is not None or env_window is not None:
        source = f"环境变量 {ENV_QUOTA_LIMIT}"
    else:
        source = "启动参数默认"
    return limit, window, source


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
