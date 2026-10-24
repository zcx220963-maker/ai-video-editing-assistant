# -*- coding: utf-8 -*-
"""风格包穿过计划门：规划轮挂上卡、执行轮从卡上取回（离线，不联网）。

这一条链断在哪一环都是**静默**的：用户在入口选了「剪影年轮」，卡面上没有；
确认执行后 `plan_motion` 收到空串，于是整片按默认视觉写码——不报错、不警告，
只是成品不是用户选的那套。三段各自钉一件事：

① `SubmitPlanTool` 把当前身份里的 `style_skill` 挂到**每一个**候选上；
② 规划轮的 checkpoint scope 也记一份（老卡没有候选字段时靠它回退）；
③ `execute_plan` 从候选读回、候选缺了退回 scope；`run B` 的 scope 继续带着它，
   刷新/续跑不会丢。

运行：  python tests/test_plan_style.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import test_plan_flow as flow  # noqa: E402  夹具与真装配同源，不另造一套
from test_plan_gate import one_plan  # noqa: E402

from agent_framework.agent import _plan_style_skill  # noqa: E402
from agent_framework.hooks import _current_hook_ctx  # noqa: E402
from agent_framework.identity import use_identity  # noqa: E402
from agent_framework.plan import SubmitPlanTool  # noqa: E402
from agent_framework.run_state import RunState  # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class _Issues:
    errors: list[str] = []
    warnings: list[str] = []
    ok = True

    def groups(self):
        return []


class _Gate:
    """只做「收下并编号」的那部分校验：这一段测的是挂包，不是四重校验。"""

    async def validate(self, payload):
        plans = payload.get("plans") or []
        return [dict(p, plan_id=str(p.get("plan_id") or f"p{i + 1}"))
                for i, p in enumerate(plans)], _Issues()


def part1_tool_attaches() -> None:
    print("① SubmitPlanTool：本轮选了包 → 每个候选都带上；没选 → 一个键都不多")
    tool = SubmitPlanTool(_Gate())
    cp = SimpleNamespace(plan={})
    ctx = SimpleNamespace(state=RunState(), extras={"checkpoint": cp})
    token = _current_hook_ctx.set(ctx)
    try:
        with use_identity("u", "c", style_skill="style_demo"):
            out = asyncio.run(tool.execute([{"steps": one_plan()["steps"]},
                                            {"steps": one_plan()["steps"]}]))
        cands = cp.plan.get("candidates") or []
        check(len(cands) == 2 and all(c.get("style_skill") == "style_demo"
                                      for c in cands),
              f"两个候选都挂了包名：{[c.get('style_skill') for c in cands]}")
        check("accepted" in str(out), "工具照常回执（挂包不改变产出）")

        cp.plan.clear()
        with use_identity("u", "c"):
            asyncio.run(tool.execute([{"steps": one_plan()["steps"]}]))
        cands = cp.plan.get("candidates") or []
        check(cands and "style_skill" not in cands[0],
              "没选包 → 候选上不出现空串键（老计划卡逐字不变）")
    finally:
        _current_hook_ctx.reset(token)


def part2_readback_policy() -> None:
    print("\n② execute_plan 的取回顺序：候选优先 → 老卡退回 scope → 都没有为空")
    check(_plan_style_skill({"style_skill": "style_a"},
                            {"scope": {"style_skill": "style_b"}}) == "style_a",
          "候选上有包名 → 用它（以用户确认的那张卡为准）")
    check(_plan_style_skill({}, {"scope": {"style_skill": "style_b"}}) == "style_b",
          "老卡没有这一位 → 退回规划轮 scope（否则老卡重放静默换视觉）")
    check(_plan_style_skill({"style_skill": ""},
                            {"scope": {"style_skill": "style_b"}}) == "style_b",
          "候选上是空串也退回 scope（空串等于没选）")
    check(_plan_style_skill({}, None) == "", "两处都没有 → 空串，走缺省视觉")
    check(_plan_style_skill({}, {"scope": None}) == "", "scope 缺失不抛")
    check(_plan_style_skill({}, {"scope": "坏值"}) == "", "scope 不是 dict 也不抛")


async def part3_real_planning() -> None:
    print("\n③ 真装配：规划轮把包名落进候选与 scope，计划帧也带着它")
    storage, agent, mgr, mq, _llm = await flow._wire([
        ("tool", "submit_plan", {"plans": [one_plan()]}, "给了 1 版计划。"),
        # ---- run B ----
        ("answer", "这一轮先把计划记下了。"),
    ])
    try:
        await agent.plan(flow.USER, flow.CONV, "把采访剪成一条旅行片",
                         run_id="run-style-a", style_skill="style_demo")
        cp_a = await mgr.load("run-style-a")
        cands = (cp_a.plan or {}).get("candidates") or []
        check(bool(cands) and cands[0].get("style_skill") == "style_demo",
              f"候选上带着包名：{cands[0].get('style_skill') if cands else '无候选'}")
        check((cp_a.scope or {}).get("style_skill") == "style_demo",
              "规划轮 checkpoint scope 也记一份（老卡回退的数据源）")

        frames = [f for f in mq.frames if f.get("type") == "plan"]
        check(bool(frames) and frames[0]["plans"][0].get("style_skill") == "style_demo",
              "回投的 plan 帧带着包名（前端卡面/确认条能显示「风格 · …」）")

        await agent.execute_plan(flow.USER, flow.CONV, "run-style-a",
                                 {"selected_plan": "p1"}, run_id="run-style-b")
        cp_b = await mgr.load("run-style-b")
        check((cp_b.scope or {}).get("style_skill") == "style_demo",
              "执行轮的 scope 继续带着包名（后续 resume/approve 读得到）")
    finally:
        await storage.close()


async def part4_no_style() -> None:
    print("\n④ 没选包：整条链一个空串键都不多（老计划卡逐字不变）")
    storage, agent, mgr, mq, _llm = await flow._wire([
        ("tool", "submit_plan", {"plans": [one_plan()]}, "给了 1 版计划。"),
        ("answer", "好。"),
    ])
    try:
        await agent.plan(flow.USER, flow.CONV, "剪一条旅行片", run_id="run-plain-a")
        cp_a = await mgr.load("run-plain-a")
        cands = (cp_a.plan or {}).get("candidates") or []
        check(bool(cands) and "style_skill" not in cands[0],
              "候选上没有 style_skill 键")
        check(not (cp_a.scope or {}).get("style_skill"),
              "scope 里的包名为空（读回时等价于「没选」，不会误注入）")
        frames = [f for f in mq.frames if f.get("type") == "plan"]
        check(bool(frames) and "style_skill" not in frames[0]["plans"][0],
              "计划帧上没有这个键（前端按缺省显示「自动」）")
    finally:
        await storage.close()



def main() -> None:
    part1_tool_attaches()
    part2_readback_policy()
    asyncio.run(part3_real_planning())
    asyncio.run(part4_no_style())
    print(f"\n{'全过' if _fails == 0 else str(_fails) + ' 条红'}（{_checks} 项）")
    raise SystemExit(1 if _fails else 0)


if __name__ == "__main__":
    main()
