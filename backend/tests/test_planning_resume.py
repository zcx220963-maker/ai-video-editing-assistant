# -*- coding: utf-8 -*-
"""**轮次身份**要活着穿过挂起与续跑（不联网、不起服务）。

真机事故（用户截图里那张「当前这轮没有可用的剪辑执行工具，你希望怎么处理?」）：
规划轮里模型主动提问（问 group_clips 按什么口径分组）→ 用户答一句 → 从断点续跑。
轮次身份原先只活在 ``run(planning=…)`` 这个**形参**里，没人落盘，续跑时它就丢了：
``Runner.approve`` 拿不到 plan_gate，回退到全量注册表——``submit_plan`` 消失、
剪辑节点回来，而 checkpoint 里的 system 段还写着「本轮是规划轮」。模型两头都不认，
只能把锅原样抛回给用户，一张已经确认过的计划就此续不上。

修法是把身份落进 ``Checkpoint.scope["planning"]``，续跑时由 ``Agent._resume_registry``
按快照重建**同一张**注册表。这份用例钉的就是这条链子：

① 身份真的落盘了：规划轮的 run 是 True，执行轮不是；
② 规划轮里提问 → 用户作答（``Agent.approve``，真机那条岔路的入口）→ 续跑接回
   规划轮的注册表：``submit_plan`` 还在、剪辑节点一个都没回来、system 段仍是
   ``<planning_round>``，而且那张卡真的出得来；
③ 反向不误伤：执行轮的快照续跑仍用全量注册表（剪辑节点必须可用），
   没有可规划节点的进程也不硬塞一张空表。

运行：  PYTHONPATH=. python tests/test_planning_resume.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import test_plan_flow as pf                      # noqa: E402  夹具同源（_wire / SpyLLM）
import test_plan_gate as pg                      # noqa: E402
from test_plan_gate import one_plan              # noqa: E402

check = pg.check

from agent_framework.agent import _APPROVAL_PAUSE_ANSWER, _planning_run  # noqa: E402
from agent_framework.ask_user import AskUserTool  # noqa: E402
from agent_framework.checkpoint import Checkpoint  # noqa: E402

USER, CONV = pf.USER, pf.CONV
RUN_A, RUN_B = "run-plan-ask", "run-exec"


def _ask_step() -> tuple:
    """规划轮里那一问：真机上问的是 group_clips 的分组口径。"""
    return ("tool", "ask_user", {
        "title": "视频里两段口播，按什么口径分组？",
        "options": [{"key": "topic", "label": "按发言主题分组", "recommended": True},
                    {"key": "scene", "label": "按场景分组"}],
    }, "")


async def case_identity_persisted() -> None:
    print("\n=== ① 轮次身份跟着快照落盘（原先只活在形参里） ===")
    storage, agent, mgr, mq, llm = await pf._wire([
        ("tool", "submit_plan", {"plans": [one_plan()]}, "先给一版思路"),
        ("answer", "给了 1 版计划，等你确认后再开工。"),
        ("tool", "load_media", {"material_ids": ["m1"]}, "按承诺先入库"),
        ("answer", "入库这步做了。"),
    ])
    try:
        await agent.plan(USER, CONV, "把采访和空镜剪成一条旅行精华", run_id=RUN_A)
        cp_a = await mgr.load(RUN_A)
        check(cp_a is not None and (cp_a.scope or {}).get("planning") is True,
              f"规划轮的 run 把身份落进 scope：{(cp_a.scope or {}) if cp_a else None}")
        check(cp_a is not None and _planning_run(cp_a) is True,
              "_planning_run 从快照里读得出「这是规划轮」")

        await agent.execute_plan(USER, CONV, RUN_A, {"selected_plan": "p1"},
                                 run_id=RUN_B, message="就按这版跑")
        cp_b = await mgr.load(RUN_B)
        check(cp_b is not None and not _planning_run(cp_b),
              f"执行轮不是规划轮，身份如实为假：{(cp_b.scope or {}) if cp_b else None}")
    finally:
        await storage.close()


async def case_resume_keeps_planning_registry() -> None:
    print("\n=== ② 规划轮里提问 → 用户作答续跑：接回同一张表与硬保证 ===")
    storage, agent, mgr, mq, llm = await pf._wire([
        _ask_step(),
        # —— 以下是续跑那一半的脚本：模型拿到用户的选择，接着把卡交出来 ——
        ("tool", "submit_plan", {"plans": [one_plan()]}, "按用户选的口径出张卡"),
        ("answer", "续跑卡给你了，确认后接着跑那两步。"),
    ])
    # 主动提问要能真弹窗：真机装配里 ask_user 本来就在主注册表里。
    agent.runner.registry.register(AskUserTool())
    try:
        out = await agent.plan(USER, CONV, "把采访剪成一条旅行精华", run_id=RUN_A)
        check(out == _APPROVAL_PAUSE_ANSWER, f"规划轮挂在弹窗上等用户作答：{out[:40]}")
        cp = await mgr.pending_approval(RUN_A)
        check(cp is not None and (cp.scope or {}).get("planning") is True,
              "挂起那条快照上，规划轮的身份还在")

        before = len(llm.calls)
        await agent.approve(RUN_A, USER, CONV, decision="approve",
                            note="按发言主题分组", answers=[
                                {"page": 1, "title": "按什么口径分组？",
                                 "answer": "按发言主题分组", "key": "topic"}])
        names = set(llm.tool_sets[before])
        check("submit_plan" in names,
              "续跑接回规划轮：submit_plan 还在（丢了它就等于出口被抽走）")
        check(not (names & pf.EDITING_NODES),
              f"续跑没把剪辑节点放回来：{sorted(names & pf.EDITING_NODES)}")
        sys_after = pf._system_of(llm.calls[before])
        check("<planning_round>" in sys_after,
              "续跑那一轮的 system 段仍是 <planning_round>（提示与工具集不再自相矛盾）")

        frames = [f for f in mq.frames if f.get("type") == "plan"]
        check(bool(frames) and frames[-1]["run_id"] == RUN_A,
              "续跑里 submit_plan 真的出了卡（而不是又一轮「我没有执行入口」）")
        cp_after = await mgr.load(RUN_A)
        cands = (cp_after.plan or {}).get("candidates") or []
        check([c["plan_id"] for c in cands] == ["p1"],
              "那张卡落在同一条 run 的指针行上（确认接口取的就是它）")
    finally:
        await storage.close()


async def case_execution_resume_untouched() -> None:
    print("\n=== ③ 反向不误伤：执行轮/普通轮续跑仍用全量注册表 ===")
    storage, agent, mgr, mq, llm = await pf._wire([
        ("tool", "submit_plan", {"plans": [one_plan()]}, "先给一版思路"),
        ("answer", "给了 1 版计划。"),
    ])
    try:
        exec_cp = Checkpoint(run_id="r-exec", session_id="u:c", message="",
                             iteration=0, messages=[], scope={"planning": False})
        check(agent._resume_registry(exec_cp) is None,
              "执行轮快照续跑走全量表（剪辑节点必须可用，不塞规划表）")
        plain_cp = Checkpoint(run_id="r-plain", session_id="u:c", message="",
                              iteration=0, messages=[], scope={})
        check(agent._resume_registry(plain_cp) is None,
              "没有身份标记的旧快照同样走全量表（向后兼容）")

        # 没有可规划节点的进程：plan() 当初就退回了 handle，续跑也不该硬塞空表。
        await agent.plan(USER, CONV, "把采访剪成一条旅行精华", run_id=RUN_A)
        cp_a = await mgr.load(RUN_A)
        gate_backup = agent.plan_gate
        agent.plan_gate = None
        try:
            check(agent._resume_registry(cp_a) is None,
                  "计划门没接上时，规划轮快照续跑也不硬塞（退回全量表）")
        finally:
            agent.plan_gate = gate_backup
    finally:
        await storage.close()


async def main() -> int:
    await case_identity_persisted()
    await case_resume_keeps_planning_registry()
    await case_execution_resume_untouched()
    print("\n" + ("全部通过" if not pg._fails else f"有 {pg._fails} 项未通过"),
          flush=True)
    print(f"用例 {pg._checks} 条", flush=True)
    return 0 if not pg._fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
