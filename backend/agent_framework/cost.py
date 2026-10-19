"""模型调用的花费（元）：按缓存命中 / 未命中 / 输出分开计价，并给每条 run 封顶。

为什么要有这一层（2026-10-17）：一天之内 8 条 run 全部撞到 120 轮迭代上限、零产物，
合计 1.18 亿输入 token；用户只看得到「两次就烧了 10 块钱」，而系统里没有任何一处知道
这笔钱花在哪——`token_usage` 只记 prompt / completion 两个总数，供应商返回的
缓存命中数被丢掉了，迭代上限是唯一的刹车，而它按**轮数**数、不按**钱**数。

DeepSeek 的价目（官方页，2026-10）里最要紧的是这一条：**输入命中缓存 0.02 元 / 百万，
未命中 1 元 / 百万，差 50 倍**。于是同样一轮，前缀没变就几乎不要钱，前缀被改（历史折叠
回头改写了一条旧结果、工具清单换了）就是全价。不分开记，就永远看不出钱烧在哪。

价格按「元 / 百万 token」。高峰时段（北京时间周一至周五 9:00-12:00、14:00-18:00）
是空闲时段的两倍；法定节假日也算空闲，但这里**不认节假日**——按高峰算只会多算，
预算因此偏紧一点，不会偏松。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

#: 北京时间（价目表的时段按它写）。
_BEIJING = timezone(timedelta(hours=8))

#: 空闲时段价格（元 / 百万 token）：命中、未命中、输出。高峰 ×2。
#: 键是模型名里要出现的片段，按顺序匹配，第一个命中的算数。
PRICES: tuple[tuple[str, dict[str, float]], ...] = (
    ("pro", {"hit": 0.15, "miss": 4.5, "out": 13.5}),
    ("flash", {"hit": 0.02, "miss": 1.0, "out": 4.0}),
)
#: 认不出的模型按这一档算（项目缺省就是它；别的供应商换价目时在上面加一行）。
DEFAULT_PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0}

#: 每条 run 的花费上限（元）。环境变量 ``RUN_BUDGET_YUAN`` 可改；``0`` 关掉封顶。
DEFAULT_RUN_BUDGET_YUAN = 3.0


def run_budget_yuan() -> float:
    """每条 run 的花费上限（元）。读不出就用缺省，负数当 0（= 不封顶）。"""
    raw = os.environ.get("RUN_BUDGET_YUAN", "")
    try:
        return max(0.0, float(raw)) if raw.strip() else DEFAULT_RUN_BUDGET_YUAN
    except ValueError:
        return DEFAULT_RUN_BUDGET_YUAN


def is_peak(when: datetime | None = None) -> bool:
    """这一刻是不是高峰时段（北京时间工作日 9-12、14-18）。"""
    t = (when or datetime.now(timezone.utc)).astimezone(_BEIJING)
    if t.weekday() >= 5:
        return False
    return 9 <= t.hour < 12 or 14 <= t.hour < 18


def price_of(model: str) -> dict[str, float]:
    name = (model or "").lower()
    for key, price in PRICES:
        if key in name:
            return price
    return DEFAULT_PRICE


def cost_yuan(usage: Mapping[str, Any] | None, *, model: str = "",
              when: datetime | None = None) -> float:
    """一次调用的 usage → 花了多少元。

    认得 ``cache_hit_tokens`` / ``cache_miss_tokens`` 就分开计价；供应商没回这两项时
    **整段按未命中算**——宁可把账算重，也不许把一笔看不清的钱算成几乎不要钱。
    """
    u = usage if isinstance(usage, Mapping) else {}
    prompt = int(u.get("prompt_tokens") or 0)
    out = int(u.get("completion_tokens") or 0)
    hit = u.get("cache_hit_tokens")
    if isinstance(hit, (int, float)) and hit >= 0:
        hit = min(int(hit), prompt)
        miss = u.get("cache_miss_tokens")
        miss = int(miss) if isinstance(miss, (int, float)) and miss >= 0 else prompt - hit
    else:
        hit, miss = 0, prompt
    p = price_of(model)
    k = 2.0 if is_peak(when) else 1.0
    return k * (hit * p["hit"] + miss * p["miss"] + out * p["out"]) / 1_000_000


def usage_from_raw(raw: Any) -> dict[str, int] | None:
    """SDK 的 usage 对象 → 一份平铺的 dict（含缓存命中/未命中，供应商给了才有）。

    两种供应商写法都认：DeepSeek 的 ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``，
    OpenAI 的 ``prompt_tokens_details.cached_tokens``。都没有就不写这两项（下游按未命中算）。
    """
    if raw is None:
        return None
    prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": int(getattr(raw, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(raw, "total_tokens", 0) or 0),
    }
    hit = getattr(raw, "prompt_cache_hit_tokens", None)
    miss = getattr(raw, "prompt_cache_miss_tokens", None)
    if hit is None:
        details = getattr(raw, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is not None:
            hit, miss = cached, prompt - int(cached)
    if hit is not None:
        usage["cache_hit_tokens"] = int(hit)
        usage["cache_miss_tokens"] = int(miss if miss is not None else prompt - int(hit))
    return usage


def fmt_yuan(value: float) -> str:
    return f"¥{value:.2f}"
