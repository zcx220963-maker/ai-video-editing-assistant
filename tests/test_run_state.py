# -*- coding: utf-8 -*-
"""跨挂起仍然成立的状态：收敛、落盘、续跑后对账不丢（不联网、不起服务）。

钉的链子形状（审计里的结构性问题 3：``ctx.extras`` 那批不进 checkpoint 的临时键
是「resume 丢对账」的根因）：

① 字段清单与序列化往返：``RunState`` 的每一位都过得了 JSON，脏数据不炸；
② 指针行往返：写进 ``cp.plan["state"]`` 再读回来，字段一字不差（含候选计划从
   ``cp.plan["candidates"]`` 那一路 hydrate 回来）；
③ **端到端**：执行轮带着批准的计划跑过一步 → 弹窗挂起 → 从落盘的断点续跑 →
   对账帧照旧发出，且挂起前跑过的那一步算**已履行**（改之前这里整帧消失）；
④ 去重位跨挂起：续跑后不会把同一版偏差再推一帧；
⑤ qa 片段跨挂起：挂起前那次工具调用气泡仍然在最后那条 assistant 行里；
⑥ 收尾后指针行只留「认过的承诺」，不留重复的 transcript；
⑦ 分叉出的子 run 继承承诺、但调用清单按子 run 上下文里真有的回执重建。

运行：  python tests/test_run_state.py
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun, _APPROVAL_PAUSE_ANSWER  # noqa: E402
from agent_framework.ask_user import AskUserTool  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.plan_gate import PlanReconcileHook  # noqa: E402
from agent_framework.run_state import STATE_FIELD, RunState, tool_names_in  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


class StubTool(Tool):
    """计划步骤的替身：只记被调用次数。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "计划步骤替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return f"{self._name}-ok"


class FakeMq:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        self.frames.append(payload)


def _plan() -> dict[str, Any]:
    return {"plan_id": "p1", "label": "两版方案",
            "steps": [{"seq": 1, "node": "load_media", "why": "读素材"},
                      {"seq": 2, "node": "select_bgm", "why": "选配乐"}]}


# ---- ① 字段清单与序列化往返 ------------------------------------------------

def case_round_trip() -> None:
    print("\n=== ① RunState 往返：字段一位不差、脏数据不炸 ===")
    st = RunState(approved_plan=_plan(), calls_attempted=["load_media", "boom"],
                  calls_executed=["load_media"], plan_candidates=[{"plan_id": "p1"}],
                  plan_warnings=["某参数缺省"], plan_card_pushed=True,
                  audit_pushed={"extra": [], "unfulfilled": ["select_bgm"]},
                  plan_audit={"extra": ["asr"]},
                  qa_parts=[{"type": "think", "content": "先读素材"}])
    raw = json.loads(json.dumps(st.to_json()))
    back = RunState.from_json(raw)
    check(back.approved_plan == st.approved_plan, "批准的计划往返一致")
    check(back.calls_attempted == st.calls_attempted
          and back.calls_executed == st.calls_executed, "两份调用清单往返一致")
    check(back.plan_card_pushed is True, "「卡已推过」这一位往返一致")
    check(back.audit_pushed == st.audit_pushed and back.plan_audit == st.plan_audit,
          "对账的去重位与结论往返一致")
    check(back.qa_parts == st.qa_parts, "qa 片段往返一致")
    # 候选与警告另有归处（cp.plan["candidates"]），state 里不该再存一份
    check("plan_candidates" not in raw and "plan_warnings" not in raw,
          "候选计划不在 state 里重复存一份：库里只有 cp.plan[candidates] 那一份")

    empty = RunState.from_json(None)
    check(empty.approved_plan is None and empty.calls_executed == [],
          "脏输入（None）→ 全新状态，不抛")
    dirty = RunState.from_json({"approved_plan": "不是字典", "calls_executed": [None, "a"],
                                "qa_parts": ["不是字典", {"type": "answer"}]})
    check(dirty.approved_plan is None and dirty.calls_executed == ["None", "a"],
          f"脏字段各自兜住：{dirty.calls_executed}")
    check(len(dirty.qa_parts) == 1, "qa 片段里非字典的条目丢掉")


# ---- ② 指针行往返 ----------------------------------------------------------

async def case_persist_through_row() -> None:
    print("\n=== ② 写进指针行再读回来：候选计划从 candidates 那一路补回 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        mgr = CheckpointManager(storage)
        cp = await mgr.begin("u:c_rs_persist", "剪一条", [], "rs-persist")
        st = RunState(approved_plan=_plan(), calls_executed=["load_media"],
                      qa_parts=[{"type": "tool call", "name": "load_media"}])
        st.plan_candidates = [{"plan_id": "p1", "steps": []}]
        st.plan_warnings = ["某参数用了默认值"]
        st.persist(cp)
        check(cp.plan[STATE_FIELD]["calls_executed"] == ["load_media"],
              "persist 只写进指针行的 plan 列（不新增列、不迁移）")
        # submit_plan 在指针行上另有这两列：restore 要从这里读回候选，而不是 state 里再存一份
        cp.plan["candidates"] = st.plan_candidates
        cp.plan["warnings"] = st.plan_warnings
        await mgr.save(cp)

        got = await mgr.load("rs-persist")
        st2 = RunState.restore(got)
        check(st2.approved_plan == st.approved_plan, "批准的计划从库里读回")
        check(st2.calls_executed == ["load_media"], "已跑过的调用从库里读回")
        check([p.get("plan_id") for p in st2.plan_candidates] == ["p1"]
              and st2.plan_warnings == ["某参数用了默认值"],
              "候选计划与警告按 candidates/warnings 两列 hydrate 回来")
        check(st2.qa_parts == st.qa_parts, "qa 片段从库里读回")
        check(RunState.restore(None) is not None and RunState.restore(got).approved_plan,
              "无 checkpoint 时是全新状态；有则取得回")
    finally:
        await storage.close()


# ---- ③④⑤ 端到端：挂起一次之后对账仍然算得出来 -------------------------------

async def case_reconcile_survives_suspend() -> None:
    print("\n=== ③④⑤ 执行轮挂起→续跑：对账不丢、不重复推帧、qa 片段不断档 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        reg = ToolRegistry()
        reg.register(StubTool("load_media"))
        reg.register(StubTool("select_bgm"))
        reg.register(AskUserTool())
        mq = FakeMq()
        runner = AgentOnceRun(
            # 第 1 轮按计划跑第一步；第 2 轮弹问题卡 → 挂起；
            # 续跑后第 3 轮跑完第二步；第 4 轮收尾。
            ScriptedLLM([
                ("tool", "load_media", {}),
                ("tool", "ask_user", {
                    "title": "配乐想要哪种？",
                    "options": [{"key": "calm", "label": "舒缓", "recommended": True},
                                {"key": "up", "label": "欢快"}],
                }),
                ("tool", "select_bgm", {}),
                ("answer", "两步都按你选的排好了，接着往下走。"),
            ]),
            reg, ContextBuilder("BASE"),
            hooks=CompositeHook([PlanReconcileHook(mq)]),
            config=AgentConfig(max_iterations=10),
            checkpoint=CheckpointManager(storage), storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_rs_e2e")
        out = await runner.run(sess, "按确认的计划剪一条", run_id="rs-e2e",
                               approved_plan=_plan())
        check(out == _APPROVAL_PAUSE_ANSWER, f"问题卡挂起：{out[:24]}")

        audits = [f for f in mq.frames if f.get("type") == "plan reconciliation"]
        check(len(audits) == 1
              and audits[0]["unfulfilled"] == ["select_bgm"]
              and audits[0]["extra"] == [],
              f"挂起前那一帧：只剩第二步未履行（{audits[0] if audits else '无帧'}）")

        cp = await runner.checkpoint.pending_approval("rs-e2e")
        st = RunState.restore(cp)
        check(st.approved_plan == _plan() and "load_media" in st.calls_executed,
              f"断点上读得到承诺与已跑过的步骤：{st.calls_executed}")
        check("ask_user" in st.calls_executed
              and all("ask_user" not in (f.get("extra") or [])
                      for f in mq.frames if f.get("type") == "plan reconciliation"),
              "提问确实执行过（记在调用清单里），但不算「计划外一步」（角标只写创作步骤）")
        check(len(st.qa_parts) >= 2
              and any(p.get("name") == "load_media" for p in st.qa_parts),
              "挂起前那次工具调用已经在落盘的 qa 片段里")

        await runner.approve(cp, sess, decision="calm", note="舒缓")

        audits2 = [f for f in mq.frames if f.get("type") == "plan reconciliation"]
        check(len(audits2) >= 2,
              f"③ 续跑之后对账帧仍然发出（改之前这里停在 1 帧，整条链再无对账）"
              f"（实得 {len(audits2)}）")
        last = audits2[-1] if audits2 else {}
        check(last.get("unfulfilled") == [] and last.get("extra") == [],
              f"③ 挂起前跑过的那一步续跑后算已履行：{last}")
        check(len(audits2) == 2, f"④ 同一版偏差不重复推帧：{len(audits2)} 帧")

        rows = await storage.messages.history("u", "c_rs_e2e", limit=10)
        assistant = [r for r in rows if r.get("role") == "assistant"]
        check(bool(assistant), "续跑收尾把答复落回会话历史")
        parts = (assistant[-1].get("qa") or {}).get("parts") if assistant else []
        names = [p.get("name") for p in (parts or []) if p.get("type") == "tool call"]
        check("load_media" in names and "select_bgm" in names,
              f"⑤ 那条 assistant 行带着挂起前的调用气泡（不是只剩半截）：{names}")
        check(sum(1 for p in (parts or []) if p.get("type") == "prompt_fingerprint") == 1,
              "一次执行只记一个 prompt 指纹：续跑那半截不再补一条")
        kinds = [p.get("type") for p in (parts or [])]
        check("plan reconciliation" in kinds, "对账结论随终答进历史（历史里看得见角标）")

        done = await runner.checkpoint.row("rs-e2e")
        state = (done.get("plan") or {}).get(STATE_FIELD) or {}
        check(state.get("approved_plan") == _plan(), "⑥ 收尾后只留「认过的承诺」")
        check("qa_parts" not in state and "calls_executed" not in state,
              "⑥ transcript 与调用清单从指针行撤下（已在 assistant 行的 qa 字段里）")
        check((done.get("plan") or {}).get("audit", {}).get("plan_id") == "p1",
              "⑥ 终态对账结论仍留在 plan.audit（执行记录面板的数据源）")
    finally:
        await storage.close()


# ---- ⑦ 分叉：子 run 继承承诺、调用清单重建 -----------------------------------

async def case_fork_inherits_commitment() -> None:
    print("\n=== ⑦ 分叉出的子 run：认过的承诺跟过去，调用清单按回执重建 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        mgr = CheckpointManager(storage)
        from agent_framework.messages import ToolCall, assistant, tool_result, user
        msgs = [user("剪一条"),
                assistant(None, [ToolCall(id="t1", name="load_media", arguments={})]),
                tool_result("t1", "load_media", "ok")]
        cp = await mgr.begin("u:c_rs_fork", "剪一条", msgs, "rs-fork")
        st = RunState(approved_plan=_plan(), calls_executed=["load_media", "select_bgm"],
                      qa_parts=[{"type": "think", "content": "半截"}])
        st.persist(cp)
        await mgr.save(cp)

        child = await mgr.fork("rs-fork", cp.head_seq, message="换一版配乐")
        cst = RunState.restore(child)
        check(cst.approved_plan == _plan(), "子 run 继承同一份承诺（否则执行轮的保证与对账失效）")
        check(cst.qa_parts == [], "父 run 的 transcript 不跟：子 run 有自己的答复")
        check(tool_names_in(child.messages) == ["load_media"],
              f"清单按子 run 上下文里真有的回执重建：{tool_names_in(child.messages)}")
        check(cst.plan_audit is None and cst.audit_pushed is None,
              "父 run 的对账结论不跟（那是那一次的记录）")
    finally:
        await storage.close()


async def main() -> int:
    case_round_trip()
    await case_persist_through_row()
    await case_reconcile_survives_suspend()
    await case_fork_inherits_commitment()
    print("\n" + ("全部通过" if not FAILS else f"有 {FAILS} 项未通过"), flush=True)
    print(f"用例 {CHECKS} 条", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
