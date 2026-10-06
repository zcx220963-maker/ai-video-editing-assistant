# -*- coding: utf-8 -*-
"""判断模型(Jev 类"系统一"模型)客户端:守卫的第二意见层。

定位(快慢分工):主模型负责"生成",判断模型负责"裁决"。本项目四个收尾守卫
(claims_plan_card / claims_step_executed / ask_gate / 计划卡形状)目前是**词面判据**
——零成本、零延迟,但 README 自己承认"认不出刻意绕开的措辞"。本模块把判断类
任务交给 Jev 这类决策模型:输入结构化事实,输出 JSON 裁决 + 置信度。

成本账:每轮收尾最多 1~2 次调用,3k token ≈ $0.0001(Jev 输出免费)——
用零头的钱买词面判据补不到的拦截率。

纪律:
* **未配置 = 完全不启用**(行为与旧版逐字一致);**任何失败 = None** = 退回词面
  判据,绝不阻塞交付——判断模型是增强,不是依赖;
* 词面判据先跑(免费),词面已拦截就不再调 Jev(不重复花钱);
* 只在「准备收尾」时刻调用,有 nudge 预算上限兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("agent_framework")

_DEFAULT_TIMEOUT = 6.0
_MIN_CONFIDENCE = 0.80


def _cfg() -> dict[str, str] | None:
    """读判断模型配置;三项没配齐 → None(功能关闭,行为与旧版一致)。"""
    base = (os.environ.get("JUDGE_BASE_URL") or "").strip().rstrip("/")
    model = (os.environ.get("JUDGE_MODEL") or "").strip()
    key = (os.environ.get("JUDGE_API_KEY") or "").strip()
    if not (base and model and key):
        return None
    return {"base": base, "model": model, "key": key}


def _post_chat(cfg: dict[str, str], messages: list[dict[str, str]],
               timeout: float) -> str | None:
    """OpenAI 兼容 chat/completions(判断模型几乎都提供此形态);失败 → None。"""
    body = json.dumps({
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        return (data.get("choices") or [{}])[0].get("message", {}).get("content")
    except Exception as exc:  # noqa: BLE001 - 判断模型任何故障都不阻塞交付
        logger.warning("判断模型调用失败(退回词面判据):%s", exc)
        return None


def _parse_verdict(raw: str | None) -> dict[str, Any] | None:
    """解析裁决 JSON(容忍代码围栏);字段不齐/置信度不可信 → None。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict) or "verdict" not in data:
        return None
    try:
        data["confidence"] = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    return data


async def judge(question: str, *, text: str, facts: dict[str, Any],
                timeout: float = _DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """问判断模型一个问题。

    question:判断任务(如"这条收尾答复是否假称已提交计划卡")
    text:    待裁决的模型终答
    facts:   服务端核到的事实(如 {"plan_candidates": 0, "submit_plan_called": false})
    返回 {"verdict": bool, "confidence": float, "reason": str} 或 None(关闭/失败)。
    """
    cfg = _cfg()
    if cfg is None:
        return None
    messages = [
        {"role": "system",
         "content": ("你是 Agent 系统的质检裁决器。根据给定事实判断模型答复是否违反规则,"
                     "只输出一个 JSON 对象:"
                     '{"verdict": true/false, "confidence": 0.0~1.0, "reason": "一句话"}。'
                     "verdict=true 表示违反规则。事实与答复冲突时以事实为准。")},
        {"role": "user",
         "content": json.dumps({"question": question, "facts": facts, "reply": text[:6000]},
                               ensure_ascii=False)},
    ]
    raw = await asyncio.to_thread(_post_chat, cfg, messages, timeout)
    verdict = _parse_verdict(raw)
    if verdict is None:
        return None
    if verdict["confidence"] < float(os.environ.get("JUDGE_MIN_CONFIDENCE",
                                                    _MIN_CONFIDENCE)):
        return None                      # 低置信不行动:宁可漏纠不误伤
    return verdict



def actionable(verdict: dict[str, Any] | None) -> bool:
    """裁决是否达到行动标准:verdict=True 且置信度 ≥ JUDGE_MIN_CONFIDENCE。"""
    if not verdict or not verdict.get("verdict"):
        return False
    try:
        return float(verdict.get("confidence", 0.0)) >= float(
            os.environ.get("JUDGE_MIN_CONFIDENCE", _MIN_CONFIDENCE))
    except (TypeError, ValueError):
        return False


def get_judge():
    """供守卫层取用:已配置返回本模块(有 .judge),未配置返回 None。"""
    return _self if _cfg() else None


# 模块句柄(get_judge 返回它,调用方直接 await judge_mod.judge(...))
import sys as _sys  # noqa: E402
_self = _sys.modules[__name__]
