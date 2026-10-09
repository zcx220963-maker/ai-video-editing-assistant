# -*- coding: utf-8 -*-
"""判断模型(Jev 类"系统一"模型)客户端:守卫的第二意见层。

定位(快慢分工):主模型负责"生成",判断模型负责"裁决"。本项目四个收尾守卫
(claims_plan_card / claims_step_executed / ask_gate / 计划卡形状)目前是**词面判据**
——零成本、零延迟,但 README 自己承认"认不出刻意绕开的措辞"。本模块把判断类
任务交给 Jev 这类决策模型:输入结构化事实,输出 Noul 概率(0-1)。

通道:TypeSafe 官方端点 POST /v1/systemone(非 OpenAI 兼容的 /chat/completions)。
请求体 {model, state, questions},响应体 {model, answers, usage}。三次守卫调用都是
"是否声称 X"的 yes/no,对应 TypeSafe 的 Noul 原语——返回 noul 概率,无独立 confidence
(文档:"Choice and Score answers also carry a confidence; Noul does not")。
映射:verdict = noul > 0.5,confidence = noul(声称违规的概率本身就是置信度)。

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
import re
import urllib.error
import urllib.request

from .secrets import current_user_id
from typing import Any

logger = logging.getLogger("agent_framework")

_DEFAULT_TIMEOUT = 6.0
_MIN_CONFIDENCE = 0.80


async def _cfg_for(user_id: str) -> dict[str, str] | None:
    """按用户解析判断模型配置:① 前端「设置」三件套 ② env 回落。

    判断模型与主模型同一条优先级链——前端配置压过环境变量。
    """
    from .secrets import (JUDGE_API_KEY_NAME, JUDGE_BASE_KEY, JUDGE_MODEL_KEY,
                          bound_storage, get_user_key)
    if bound_storage() is not None and user_id:
        try:
            base = (await get_user_key(user_id, JUDGE_BASE_KEY)) or ""
            model = (await get_user_key(user_id, JUDGE_MODEL_KEY)) or ""
            key = (await get_user_key(user_id, JUDGE_API_KEY_NAME)) or ""
        except Exception:  # noqa: BLE001
            base = model = key = ""
        if base and model and key:
            return {"base": base.rstrip("/"), "model": model, "key": key}
    return _cfg()


async def test_judge(user_id: str = "") -> dict[str, Any]:
    """设置页「测试连接」用:按当前身份解析配置,发一个最小裁决。"""
    import time as _t
    uid = user_id or current_user_id()
    cfg = await _cfg_for(uid)
    via = "Jev" if cfg else "主 LLM"
    t0 = _t.monotonic()
    try:
        v = await judge("连通性自检:1 + 1 是否等于 2?", text="2",
                        facts={"ping": True}, user_id=uid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}
    return {"ok": v is not None,
            "latency_ms": int((_t.monotonic() - t0) * 1000),
            "verdict": (v or {}).get("verdict"),
            "confidence": (v or {}).get("confidence"),
            "via": via}


def _cfg() -> dict[str, str] | None:
    """env 回落层;三项没配齐 → None(功能关闭,行为与旧版一致)。"""
    base = (os.environ.get("JUDGE_BASE_URL") or "").strip().rstrip("/")
    model = (os.environ.get("JUDGE_MODEL") or "").strip()
    key = (os.environ.get("JUDGE_API_KEY") or "").strip()
    if not (base and model and key):
        return None
    return {"base": base, "model": model, "key": key}


def _post_systemone(cfg: dict[str, str], state: Any,
                    questions: dict[str, Any], timeout: float) -> float | None:
    """TypeSafe /v1/systemone 端点;返回 Noul 概率(0-1),失败 → None。

    请求体:{model, state, questions}。state 是待裁决的内容(结构化 object 或文本),
    questions 是 {id: {type:"noul", instructions, criteria}} 的映射。响应里每个 question
    返回 {type:"noul", noul: 0~1}。Noul 无独立 confidence——概率本身就是确信度。
    """
    body = json.dumps({
        "model": cfg["model"],
        "state": state,
        "questions": questions,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"] + "/systemone", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        answers = data.get("answers") or {}
        ans = answers.get("violation") or {}
        if ans.get("type") != "noul":
            return None
        return float(ans.get("noul", 0.0))
    except Exception as exc:  # noqa: BLE001 - 判断模型任何故障都不阻塞交付
        logger.warning("判断模型调用失败(退回词面判据):%s", exc)
        return None


async def _judge_with_llm(question: str, text: str, facts: dict[str, Any]) -> dict[str, Any] | None:
    """没配 Jev 时用主 LLM 做第二意见判断。LLM 比 Jev 贵但比词面判据强。"""
    try:
        from .llm_openai import get_default_llm
        llm = get_default_llm()
        prompt = (
            f"判断任务：{question}\n"
            f"待裁决的答复：{text[:4000]}\n"
            f"客观事实：{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"答复是否与事实矛盾（声称做了但事实没做）？只返回JSON："
            f'{{"verdict": true/false, "confidence": 0.0-1.0}}'
        )
        messages = [
            {"role": "system", "content": "你是判断模型。只输出JSON，不输出其他内容。"},
            {"role": "user", "content": prompt},
        ]
        resp = await llm.complete(messages)
        content = (resp.content or "").strip()
        m = re.search(r'\{[^}]+\}', content)
        if not m:
            return None
        r = json.loads(m.group())
        verdict = bool(r.get("verdict", False))
        confidence = float(r.get("confidence", 0.5))
        if confidence < float(os.environ.get("JUDGE_MIN_CONFIDENCE", _MIN_CONFIDENCE)):
            return None
        return {"verdict": verdict, "confidence": confidence, "reason": ""}
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 判断回落失败(退回词面判据):%s", exc)
        return None


async def judge(question: str, *, text: str, facts: dict[str, Any],
                timeout: float = _DEFAULT_TIMEOUT,
                user_id: str = "") -> dict[str, Any] | None:
    """问判断模型一个问题。

    question:判断任务(如"这条收尾答复是否假称已提交计划卡")——作为 Noul instructions
    text:    待裁决的模型终答——作为 state.reply
    facts:   服务端核到的事实(如 {"plan_candidates": 0, "submit_plan_called": false})
             ——作为 state.facts,与 reply 并列供 Jev 对照
    返回 {"verdict": bool, "confidence": float, "reason": str} 或 None(关闭/失败/低置信)。

    Noul 只返回 0-1 概率,无 reason;映射:verdict = noul > 0.5,
    confidence = noul(verdict=true 时)或 1-noul(verdict=false 时)。
    """
    from .secrets import current_user_id
    uid = user_id or current_user_id()
    cfg = await _cfg_for(uid)
    if cfg is None:
        return await _judge_with_llm(question, text, facts)
    state = {"reply": text[:6000], "facts": facts}
    questions = {
        "violation": {
            "type": "noul",
            "instructions": question,
            "criteria": {
                "true": "答复与事实矛盾——声称做了但事实没做,或向用户索要了界面上不存在的选项",
                "false": "答复与事实一致——没声称做了,或确实做了,或如实说明未做",
            },
        }
    }
    noul = await asyncio.to_thread(_post_systemone, cfg, state, questions, timeout)
    if noul is None:
        return None
    verdict = noul > 0.5
    confidence = noul if verdict else (1.0 - noul)
    if confidence < float(os.environ.get("JUDGE_MIN_CONFIDENCE", _MIN_CONFIDENCE)):
        return None                      # 低置信不行动:宁可漏纠不误伤
    return {"verdict": verdict, "confidence": confidence, "reason": ""}



def actionable(verdict: dict[str, Any] | None) -> bool:
    """裁决是否达到行动标准:verdict=True 且置信度 ≥ JUDGE_MIN_CONFIDENCE。"""
    if not verdict or not verdict.get("verdict"):
        return False
    try:
        return float(verdict.get("confidence", 0.0)) >= float(
            os.environ.get("JUDGE_MIN_CONFIDENCE", _MIN_CONFIDENCE))
    except (TypeError, ValueError):
        return False


async def get_judge():
    """供守卫层取用：有任一裁决通道时返回本模块（有 .judge），否则 None。

    两条通道（与 ``judge()`` 内部的优先级一致）：

    1. **Jev 通道**——env 的 ``JUDGE_*`` 三项，或当前用户在设置页配的三件套；
    2. **主 LLM 兜底通道**——没有 Jev 但有能出网的模型密钥（``judge()`` 会走
       ``_judge_with_llm``）。

    为什么这条必须 await 且按身份解析：门禁原先只看 env（``_cfg()``），于是设置页配的
    Jev 与「没配 Jev 回落主 LLM」这两条都不生效——守卫层拿到 None，整个第二意见层是死的。
    没有密钥时才返回 None：那条通道跑不通，别白调一次。
    """
    from .secrets import resolve_api_key

    uid = current_user_id()
    if await _cfg_for(uid) is not None:
        return _self
    key, _source = await resolve_api_key(uid, fallback="")
    return _self if key else None


# 模块句柄(get_judge 返回它,调用方直接 await judge_mod.judge(...))
import sys as _sys  # noqa: E402
_self = _sys.modules[__name__]
