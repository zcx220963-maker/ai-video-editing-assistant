# -*- coding: utf-8 -*-
"""判断模型(Jev 类"系统一")第二意见层的离线验证。

运行：  python tests/test_judge_gate.py   # 内存替身 + 假判断模型,不联网

钉住七件事：
① 未配置 JUDGE_* → 行为与旧版逐字一致(词面没拦就放行,不调任何东西);
② 词面判据先跑:词面已拦截的文本不再调判断模型(不重复花钱);
③ 判断模型判违规 → 与词面拦截同一处理(吃同一份预算、追加同款打回文案);
④ 判"合规"或低置信 → 放行;
⑤ 判断模型故障/返回坏 JSON → None,退回词面结果,不阻塞交付;
⑥ 预算用尽 → 不再调判断模型;
⑦ 通道门禁按当前身份解析(env Jev / 页面 Jev / 主模型密钥三条通道都算),且**桩的形状
   与线上一致**——get_judge 返回 judge 模块、调用点写 judge_mod.judge(...)。
   旧版桩把 fake 当 get_judge 的返回值,于是「把模块当函数调」的 TypeError 只在线上发生、
   桩里全绿:这一版就是为了不再出现那种假绿。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import agent as agent_mod
from agent_framework.agent import Agent, AgentOnceRun
from agent_framework.llm import ScriptedLLM
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


ESCAPE_TEXT = "三个方案都已经为你准备好了,挑选一版确认就可以开始渲染了。"
PLAIN_TEXT = "本轮先到这里,素材我还需要再核对一下。"


class FakeJudge:
    """假判断模型:record 调用次数,verdict 可设定。"""

    def __init__(self, verdict=True, confidence=0.95, broken=False):
        self.calls = 0
        self.verdict = verdict
        self.confidence = confidence
        self.broken = broken

    async def __call__(self, question, *, text, facts, timeout=None):  # noqa: ANN001
        self.calls += 1
        if self.broken:
            raise RuntimeError("判断模型不可达")
        return {"verdict": self.verdict, "confidence": self.confidence,
                "reason": "测试裁决"}


def make_round(planning=False, card_nudges=1, step_nudges=1, popup_nudges=1,
               plan_candidates=None, approved_plan=None, asked=False):
    from agent_framework.agent import RunState
    state = RunState()
    state.plan_candidates = plan_candidates or []
    state.approved_plan = approved_plan
    state.calls_attempted = []
    return SimpleNamespace(
        planning=planning, state=state, reg=ToolRegistry(), messages=[],
        iteration=0, cp=None, run_id=None, interactive=True,
        card_nudges=card_nudges, step_nudges=step_nudges,
        popup_nudges=popup_nudges, plan_nudges=1, asked_this_run=asked,
        final=None, reached_limit=False, approval_decision=None,
    )


def make_agent_once(storage):
    from agent_framework.context import ContextBuilder
    from agent_framework.llm import ScriptedLLM as _S
    return AgentOnceRun(
        _S(steps=[("answer", "任意")]), ToolRegistry(),
        context_builder=ContextBuilder("测试系统提示"),
        storage=storage,
    )


def install_judge(fake):
    """打桩到 judge 模块：``judge()`` 换成 fake，``get_judge()`` 换成返回**模块本身**的协程。

    桩的形状必须与线上一致：线上 ``get_judge()`` 返回的是 judge 模块句柄，调用点写的是
    ``judge_mod.judge(...)``。上一版桩把 fake 直接当 ``get_judge()`` 的返回值，于是桩里
    ``await judge(...)`` 恰好能用、线上却是死的（把模块当函数调 → TypeError 被 except 吞）。
    ``fake=None`` 表示「没有任何裁决通道」。
    """
    import agent_framework.judge as judge_module
    saved = (judge_module.get_judge, judge_module.judge)

    async def _gate() -> object | None:
        return None if fake is None else judge_module

    judge_module.get_judge = _gate
    judge_module.judge = fake if fake is not None else (lambda *a, **k: None)
    return saved


def restore(saved) -> None:
    import agent_framework.judge as judge_module
    judge_module.get_judge, judge_module.judge = saved


async def part1_disabled() -> None:
    print("\n[1] 未配置判断模型:行为与旧版一致,词面没拦就放行")
    st = build_storage("memory")
    once = make_agent_once(st)
    r = make_round(planning=True)
    saved = install_judge(None)
    try:
        nudged = await once._nudge_back(r, SimpleNamespace(content=ESCAPE_TEXT))
        check(nudged is False, "绕开措辞 + 无判断模型 → 放行(现状语义)")
        check(r.card_nudges == 1, "预算未消耗")
    finally:
        restore(saved)


async def part2_judge_catches() -> None:
    print("\n[2] 判断模型补漏:词面没拦的绕开措辞被打回,吃同一份预算")
    st = build_storage("memory")
    once = make_agent_once(st)
    fake = FakeJudge(verdict=True, confidence=0.95)
    saved = install_judge(fake)
    try:
        r = make_round(planning=True)
        nudged = await once._nudge_back(r, SimpleNamespace(content=ESCAPE_TEXT))
        check(nudged is True, "高置信判违规 → 打回")
        check(r.card_nudges == 0, "消耗 card 预算")
        check(any(NO_CARD_TEXT_MARK in str(m) for m in r.messages),
              "追加同款打回文案 + 判断依据注")
        check(fake.calls == 1, "判断模型恰好调一次")
    finally:
        restore(saved)


async def part3_judge_pass() -> None:
    print("\n[3] 判合规/低置信/故障 → 放行不阻塞")
    st = build_storage("memory")
    for label, fake in [
        ("判合规", FakeJudge(verdict=False, confidence=0.95)),
        ("低置信", FakeJudge(verdict=True, confidence=0.3)),
        ("故障", FakeJudge(broken=True)),
    ]:
        once = make_agent_once(build_storage("memory"))
        saved = install_judge(fake)
        try:
            r = make_round(planning=True)
            nudged = await once._nudge_back(r, SimpleNamespace(content=ESCAPE_TEXT))
            check(nudged is False and r.card_nudges == 1,
                  f"{label} → 放行、预算不动(fake.calls={fake.calls})")
        finally:
            restore(saved)


async def part4_lexical_first() -> None:
    print("\n[4] 词面优先:词面已拦截的不再调判断模型")
    st = build_storage("memory")
    once = make_agent_once(st)
    fake = FakeJudge()
    saved = install_judge(fake)
    try:
        r = make_round(planning=True)
        # 直接假称已出卡(词面判据 claims_plan_card 的正中靶心文本)
        nudged = await once._nudge_back(r, SimpleNamespace(
            content="计划卡已提交,请确认。"))
        check(nudged is True and fake.calls == 0,
              f"词面拦截生效且未调判断模型(calls={fake.calls})")
    finally:
        restore(saved)


async def part5_budget() -> None:
    print("\n[5] 预算用尽:不再调判断模型")
    st = build_storage("memory")
    once = make_agent_once(st)
    fake = FakeJudge()
    saved = install_judge(fake)
    try:
        r = make_round(planning=True, card_nudges=0, popup_nudges=0)
        nudged = await once._nudge_back(r, SimpleNamespace(content=ESCAPE_TEXT))
        check(nudged is False and fake.calls == 0,
              "预算 0 → 不调判断模型直接放行")
    finally:
        restore(saved)


async def part6_popup_and_step() -> None:
    print("\n[6] 弹窗与步骤门同样接入判断模型")
    st = build_storage("memory")
    once = make_agent_once(st)
    fake = FakeJudge(verdict=True)
    saved = install_judge(fake)
    try:
        # 弹窗门:正文在索要决策却没调 ask_user
        r = make_round(planning=True, card_nudges=0, popup_nudges=1)
        nudged = await once._nudge_back(r, SimpleNamespace(
            content="请告诉我你想要哪种风格,我再开始。"))
        check(nudged is True and r.popup_nudges == 0, "索要决策 → 打回并耗弹窗预算")

        # 步骤门:声称步骤已跑,实际零调用
        r2 = make_round(planning=False, card_nudges=0, step_nudges=1,
                        approved_plan={"steps": [{"node": "load_media"}]})
        nudged2 = await once._nudge_back(r2, SimpleNamespace(
            content="第一步已经执行完成,成片马上就好。"))
        check(nudged2 is True and r2.step_nudges == 0, "假称步骤完成 → 打回")
    finally:
        restore(saved)


async def part7_channel_gate() -> None:
    print("\n[7] 通道门禁:按当前身份解析,页面配的 Jev 与主 LLM 兜底都算通道")
    import agent_framework.judge as jm
    from agent_framework import secrets as sec

    saved_get, saved_for, saved_resolve = jm.get_judge, jm._cfg_for, sec.resolve_api_key

    async def _none_cfg(uid):        # 没有 Jev
        return None

    async def _jev(uid):             # 设置页配了 Jev 三件套
        return {"base": "https://x.test/v1", "model": "jev-1", "key": "k"}

    async def _key(v):               # 有能出网的主模型密钥 → 兜底通道可用
        return v, "环境变量 OPENAI_API_KEY"

    try:
        jm._cfg_for = _none_cfg
        sec.resolve_api_key = lambda uid="", **kw: _key("")
        check(await jm.get_judge() is None, "既无 Jev 又无模型密钥 → None（不白调一次）")

        sec.resolve_api_key = lambda uid="", **kw: _key("sk-fake-not-printed")
        got = await jm.get_judge()
        check(got is jm, "只有主模型密钥 → 兜底通道开（返回 judge 模块本身）")

        jm._cfg_for = _jev
        check(await jm.get_judge() is jm, "页面配的 Jev 三件套也算通道（旧实现只认 env）")
    finally:
        jm.get_judge, jm._cfg_for, sec.resolve_api_key = saved_get, saved_for, saved_resolve

    # 线上形状：get_judge 是协程，返回的模块上真有一个可调用的 judge
    check(asyncio.iscoroutinefunction(jm.get_judge), "get_judge 必须是 async（要按身份回源）")
    check(callable(getattr(jm, "judge", None)), "门禁返回的模块上 judge 可调用")

    # judge() 自己的两条通道：没 Jev → 主 LLM 兜底；有 Jev → /systemone
    saved_llm, saved_post = jm._judge_with_llm, jm._post_systemone
    hits = {"llm": 0, "systemone": 0}

    async def _fake_llm(question, text, facts):
        hits["llm"] += 1
        return {"verdict": True, "confidence": 0.9, "reason": "主 LLM 兜底"}

    def _fake_post(cfg, state, questions, timeout):
        hits["systemone"] += 1
        return 0.93

    jm._judge_with_llm = _fake_llm
    jm._post_systemone = _fake_post
    try:
        jm._cfg_for = _none_cfg
        v1 = await jm.judge("是否假称已出卡?", text="都准备好了", facts={"plan_candidates": 0})
        check(v1 is not None and hits["llm"] == 1 and hits["systemone"] == 0,
              "没配 Jev → judge() 走主 LLM 兜底（不再直接返回 None）")

        jm._cfg_for = _jev
        v2 = await jm.judge("是否假称已出卡?", text="都准备好了", facts={"plan_candidates": 0})
        check(v2 is not None and v2["verdict"] is True
              and hits["systemone"] == 1 and hits["llm"] == 1,
              "配了 Jev → 走 /systemone 的 Noul 概率，不碰主 LLM")
    finally:
        jm._judge_with_llm, jm._post_systemone = saved_llm, saved_post
        jm._cfg_for = saved_for


# NO_CARD 打回文案的标记词(来自 prompts/no_card_nudge.md 的内联默认)
from agent_framework.agent import NO_CARD_NUDGE_TEXT  # noqa: E402
NO_CARD_TEXT_MARK = "计划卡" if "计划卡" in NO_CARD_NUDGE_TEXT else "方案"


from types import SimpleNamespace  # noqa: E402


async def main() -> None:
    await part1_disabled()
    await part2_judge_catches()
    await part3_judge_pass()
    await part4_lexical_first()
    await part5_budget()
    await part6_popup_and_step()
    await part7_channel_gate()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
