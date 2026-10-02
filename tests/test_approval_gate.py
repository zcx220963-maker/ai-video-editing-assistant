# -*- coding: utf-8 -*-
"""HITL 审批断点的端到端离线验证（不联网、不起服务）。

钉的链子形状：
① 工具声明 requires_approval（或被 --approve-tools 按名覆盖）→ 执行到它时整批挂起，
   run 返回暂停话术，checkpoint 落 awaiting_approval 并记下 pending_calls；
② approve("approve") → 从断点续跑，工具真执行，run 正常收尾 completed；
③ approve("reject")  → 工具不执行，回喂拒绝结果，run 继续到下一轮 LLM 收尾；
④ 未标注审批的工具不挂起，照常执行；
⑤ 挂起期间 pending_approval(run_id) 能取回那条 checkpoint，clear 后取不回。

运行：  PYTHONPATH=. python tests/test_approval_gate.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun, _APPROVAL_PAUSE_ANSWER  # noqa: E402
from agent_framework.ask_user import AskUserTool  # noqa: E402
from agent_framework.checkpoint import (  # noqa: E402
    CheckpointManager, STATUS_AWAITING_APPROVAL, STATUS_COMPLETED,
)
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
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


class RecorderTool(Tool):
    """记下执行次数的替身工具；approval 控制是否在执行前挂起。"""

    def __init__(self, name: str, *, requires_approval: bool = False) -> None:
        self._name = name
        self._requires = requires_approval
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "替身工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    @property
    def requires_approval(self) -> bool:
        return self._requires

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return f"{self._name}-ok#{self.calls}"


def _build(reg: ToolRegistry, steps: list[Any], storage) -> AgentOnceRun:
    mgr = CheckpointManager(storage)
    return AgentOnceRun(
        ScriptedLLM(steps), reg, ContextBuilder("BASE"),
        hooks=CompositeHook([]), config=AgentConfig(max_iterations=8),
        checkpoint=mgr,
    ), mgr


async def case_a_approve_continues() -> None:
    print("\n=== ①② 批准：挂起 → approve → 工具真执行 → 收尾 completed ===")
    storage = build_storage("memory")
    await storage.start()
    gated = RecorderTool("render_video", requires_approval=True)
    reg = ToolRegistry()
    reg.register(gated)
    runner, mgr = _build(reg, [("tool", "render_video", {}), ("answer", "渲染完成")], storage)
    sess = Session(user_id="u", conversation_id="c_appr_a")
    out = await runner.run(sess, "渲染一下", run_id="run-a")

    check(out == _APPROVAL_PAUSE_ANSWER, f"① 撞上审批工具时返回暂停话术：{out}")
    check(gated.calls == 0, f"① 挂起时工具尚未执行：{gated.calls} 次")
    cp = await mgr.pending_approval("run-a")
    check(cp is not None and cp.status == STATUS_AWAITING_APPROVAL,
          f"① checkpoint 落 awaiting_approval：{cp.status if cp else None}")
    pending = (cp.approval or {}).get("pending_calls") or []
    check(len(pending) == 1 and pending[0]["name"] == "render_video",
          f"① pending_calls 记下挂起的那批：{pending}")

    out2 = await runner.approve(cp, sess, decision="approve")
    check(out2 == "渲染完成", f"② 批准后续跑到最终答复：{out2}")
    check(gated.calls == 1, f"② 批准后工具真执行了一次：{gated.calls} 次")
    done = await mgr.load("run-a")
    check(done is not None and done.status == STATUS_COMPLETED,
          f"② 收尾标记 completed：{done.status if done else None}")
    check(await mgr.pending_approval("run-a") is None,
          "② 审批清掉后不再有挂起记录")
    await storage.close()


async def case_b_reject_skips() -> None:
    print("\n=== ③ 拒绝：工具不执行，回喂拒绝结果，run 继续收尾 ===")
    storage = build_storage("memory")
    await storage.start()
    gated = RecorderTool("delete_clip", requires_approval=True)
    reg = ToolRegistry()
    reg.register(gated)
    runner, mgr = _build(reg, [("tool", "delete_clip", {}), ("answer", "已跳过删除")], storage)
    sess = Session(user_id="u", conversation_id="c_appr_b")
    out = await runner.run(sess, "删一下", run_id="run-b")
    check(out == _APPROVAL_PAUSE_ANSWER, f"③ 先挂起：{out}")
    cp = await mgr.pending_approval("run-b")
    check(cp is not None, "③ 挂起 checkpoint 存在")

    out2 = await runner.approve(cp, sess, decision="reject")
    check(out2 == "已跳过删除", f"③ 拒绝后 run 继续到下一轮收尾：{out2}")
    check(gated.calls == 0, f"③ 拒绝时工具一次都没执行：{gated.calls} 次")
    last = runner.llm.calls[-1]
    rejected = [m for m in last if m.get("role") == "tool"
                and "拒绝" in str(m.get("content") or "")]
    check(len(rejected) == 1, f"③ 拒绝结果作为 tool 回执喂回 LLM：{len(rejected)} 条")
    done = await mgr.load("run-b")
    check(done is not None and done.status == STATUS_COMPLETED,
          f"③ 拒绝后也正常收尾 completed：{done.status if done else None}")
    await storage.close()


async def case_c_no_approval_runs_through() -> None:
    print("\n=== ④ 未标注审批的工具不挂起，照常执行 ===")
    storage = build_storage("memory")
    await storage.start()
    plain = RecorderTool("list_clips", requires_approval=False)
    reg = ToolRegistry()
    reg.register(plain)
    runner, mgr = _build(reg, [("tool", "list_clips", {}), ("answer", "列完了")], storage)
    sess = Session(user_id="u", conversation_id="c_appr_c")
    out = await runner.run(sess, "列一下", run_id="run-c")
    check(out == "列完了", f"④ 未标注审批直接跑完：{out}")
    check(plain.calls == 1, f"④ 工具正常执行了一次：{plain.calls} 次")
    check(await mgr.pending_approval("run-c") is None,
          "④ 从未挂起，无 pending_approval 记录")
    await storage.close()


async def case_d_set_approval_by_name() -> None:
    print("\n=== ⑤ set_approval 按名覆盖：自声明 False 也能被挂起 ===")
    storage = build_storage("memory")
    await storage.start()
    plain = RecorderTool("export_video", requires_approval=False)
    reg = ToolRegistry()
    reg.register(plain)
    reg.set_approval(["export_video"])  # 装配方按名打开审批
    runner, mgr = _build(reg, [("tool", "export_video", {}), ("answer", "导出完成")], storage)
    sess = Session(user_id="u", conversation_id="c_appr_d")
    out = await runner.run(sess, "导出", run_id="run-d")
    check(out == _APPROVAL_PAUSE_ANSWER, f"⑤ 按名覆盖后也挂起：{out}")
    check(plain.calls == 0, f"⑤ 挂起时未执行：{plain.calls} 次")
    cp = await mgr.pending_approval("run-d")
    check(cp is not None and cp.status == STATUS_AWAITING_APPROVAL,
          f"⑤ 落 awaiting_approval：{cp.status if cp else None}")
    out2 = await runner.approve(cp, sess, decision="approve")
    check(out2 == "导出完成", f"⑤ 批准后续跑收尾：{out2}")
    check(plain.calls == 1, f"⑤ 批准后执行了一次：{plain.calls} 次")
    await storage.close()


async def case_e_confirm_executes_pending() -> None:
    """⑥ 渲染确认门：点「确认渲染」要**直接执行**挂起的那批，不是回喂一句话。

    为什么专门钉这一条：只回喂一句「用户已确认」时，模型会**再调一次**渲染，
    于是又被门拦住、弹出第二张一模一样的卡——真机实测就这样循环下去。
    正确行为是「用户点一下，那批发起渲染的调用就执行掉」。
    """
    print("\n=== ⑥ 渲染确认门：确认后直接执行待批调用（不再弹第二张卡）===")
    storage = build_storage("memory")
    await storage.start()
    gated = RecorderTool("render_video", requires_approval=False)
    reg = ToolRegistry()
    reg.register(gated)
    # 两轮脚本：第一轮要渲染（被门拦住），第二轮本不该发生（若回喂就会再调一次）
    runner, mgr = _build(reg, [
        ("tool", "render_video", {}),
        ("tool", "render_video", {}),      # 回喂策略下模型会走到这里
        ("answer", "已提交渲染"),
    ], storage)
    sess = Session(user_id="u", conversation_id="c_appr_e")
    out = await runner.run(sess, "渲染", run_id="run-e")
    check(out == _APPROVAL_PAUSE_ANSWER, f"⑥ 要渲染时先挂起：{out}")
    check(gated.calls == 0, f"⑥ 未确认前一次都没渲：{gated.calls} 次")
    cp = await mgr.pending_approval("run-e")
    check(cp is not None and cp.status == STATUS_AWAITING_APPROVAL,
          f"⑥ 落 awaiting_approval：{cp.status if cp else None}")

    from agent_framework.render_gate import CONFIRM_OPTION  # noqa: E402
    out2 = await runner.approve(cp, sess, decision=CONFIRM_OPTION)
    check(gated.calls == 1, f"⑥ 确认后只执行一次：{gated.calls} 次")
    check(out2 == "已提交渲染", f"⑥ 确认后正常收尾：{out2}")
    await storage.close()


async def case_f_repair_keeps_pending() -> None:
    """⑦ 续跑前修孤儿消息时，必须保住「还没回执」的待批调用。

    这是那条真机失败（整条 run failed）的根因：挂起点上待批的 tool_calls 本来就没有
    tool 结果，被判成孤儿剥掉 → 续跑补回执时变成孤儿 tool 消息 → LLM API 400。
    """
    print("\n=== ⑦ 待批 tool_calls 不会被当成孤儿剥掉 ===")
    from agent_framework.compress import _repair_orphans  # noqa: E402
    pending = [{"id": "call_pending", "type": "function",
                "function": {"name": "render_video", "arguments": "{}"}}]
    at_gate = [
        {"role": "user", "content": "剪一条"},
        {"role": "assistant", "content": "开始渲染。", "tool_calls": pending},
    ]
    dropped = _repair_orphans(list(at_gate))
    check(not (dropped[-1] or {}).get("tool_calls"),
          "⑦ 复现旧行为：不传 keep_ids 时确实被剥掉（这就是那个 bug）")
    kept = _repair_orphans(list(at_gate), keep_ids=["call_pending"])
    check(bool((kept[-1] or {}).get("tool_calls")), "⑦ 传 keep_ids 后保住")
    # 补上回执后两边 id 对得上，才是合法结构
    settled = [*at_gate, {"role": "tool", "tool_call_id": "call_pending",
                          "name": "render_video", "content": "确认渲"}]
    after = _repair_orphans(settled, keep_ids=["call_pending"])
    declared = set().union(*[{tc.get("id") for tc in (m.get("tool_calls") or [])}
                            for m in after if m.get("role") == "assistant"])
    got = {m.get("tool_call_id") for m in after if m.get("role") == "tool"}
    check(declared == got == {"call_pending"}, "⑦ 续跑后声明与回执对得上（不再是孤儿）")
    # 真孤儿仍须清掉，不能因为这次修复就手软
    lone = [{"role": "assistant", "content": "调了个工具", "tool_calls": [
        {"id": "call_gone", "type": "function",
         "function": {"name": "asr", "arguments": "{}"}}]}]
    check(not (_repair_orphans(list(lone))[0] or {}).get("tool_calls"),
          "⑦ 真孤儿（没回执且不在 keep 里）仍然清掉")


async def case_g_ask_user_no_duplicate_result() -> None:
    """⑧ 主动提问（ask_user）续跑时，不能给同一个 tool_call 补第二条回执。

    为什么专门钉：ask_user 在挂起**之前**已经执行过，它的 tool 回执已在 messages 里。
    续跑时若再补一条同 tool_call_id 的结果，就变成「一个 id 两条回执」——
    LLM API 直接 400，整条 run failed（真机实测到过，会话 ask-*）。
    渲染确认门那一路是先拦后执行，所以它确实需要补；两种情形必须区分对待。
    """
    print("\n=== ⑧ 主动提问续跑：同一 tool_call 不补第二条回执 ===")
    storage = build_storage("memory")
    await storage.start()
    reg = ToolRegistry()
    ask = AskUserTool()
    reg.register(ask)
    # 第一轮：模型提问（工具会执行并留下回执）→ 循环挂起
    runner, mgr = _build(reg, [
        ("tool", "ask_user", {"title": "要多长？", "options": [
            {"key": "s60", "label": "60 秒", "recommended": True},
            {"key": "s90", "label": "90 秒"}]}),
        ("answer", "好的，按 60 秒来"),
    ], storage)
    sess = Session(user_id="u", conversation_id="c_appr_g")
    out = await runner.run(sess, "帮我剪一条", run_id="run-g")
    check(out == _APPROVAL_PAUSE_ANSWER, f"⑧ 模型提问后挂起：{out}")
    cp = await mgr.pending_approval("run-g")
    check(cp is not None and cp.status == STATUS_AWAITING_APPROVAL, "⑧ 落 awaiting_approval")
    check(bool((cp.approval or {}).get("ask")), "⑧ ask 结构已落盘（刷新后能重弹）")

    out2 = await runner.approve(cp, sess, decision="s60", note="60 秒左右")
    check(out2 == "好的，按 60 秒来", f"⑧ 续跑正常收尾（没被 API 拒）：{out2}")

    # 关键断言：同一个 tool_call_id 只能有一条 tool 回执
    done = await mgr.load("run-g")
    tools = [m for m in (done.messages or []) if m.get("role") == "tool"]
    ids = [m.get("tool_call_id") for m in tools]
    dupes = {i for i in ids if ids.count(i) > 1}
    check(not dupes, f"⑧ 没有重复回执（重复的 id：{dupes or '无'}）")
    # 用户的选择要真的进了上下文
    joined = " ".join(str(m.get("content") or "") for m in (done.messages or []))
    check("60" in joined, "⑧ 用户的选择进了上下文")
    await storage.close()


async def case_h_ask_answer_enters_as_user() -> None:
    """⑨ 主动提问续跑：用户那句答案必须以 **user 角色**进链。

    为什么专门钉，且为什么⑧那样的断言不够：ask_user 的回执（「问题已发给用户，
    本轮到此为止」）在挂起**之前**就写好了，续跑时 ``_settle_pending`` 发现
    「这批调用都已有回执」，原先两条分支在这个前提下都不写入任何东西 ——
    模型于是永远等不到答案，只能一遍遍重问同一道题（真机实测：连问 6 题，
    第 6 题还在重问第 1 题）。⑧ 里那句「'60' 在链上」旧实现也能过，
    因为回执文本自身就带着选项里的「60 秒」。所以这里按**角色**判：
    要的是新出现的那条 role=user，不是 tool 回执里的字符串。
    """
    print("\n=== ⑨ 提问续跑：用户答案以 user 角色进链（旧实现丢答案 → 无限重问）===")
    storage = build_storage("memory")
    await storage.start()
    reg = ToolRegistry()
    reg.register(AskUserTool())
    runner, mgr = _build(reg, [
        ("tool", "ask_user", {"title": "要多长？", "options": [
            {"key": "s60", "label": "60 秒", "recommended": True},
            {"key": "s90", "label": "90 秒"}]}),
        ("answer", "好，按 60 秒来做"),
    ], storage)
    sess = Session(user_id="u", conversation_id="c_appr_h")
    out = await runner.run(sess, "帮我剪一条", run_id="run-h")
    check(out == _APPROVAL_PAUSE_ANSWER, f"⑨ 提问后挂起：{out}")
    cp = await mgr.pending_approval("run-h")
    check(cp is not None, "⑨ 挂起 checkpoint 存在")

    before = len((await mgr.load("run-h")).messages or [])
    await runner.approve(cp, sess, decision="s60", note="60 秒左右")

    # 续跑那一轮 LLM 真正看到的消息里必须有那句答案（模型侧生效，不只是落库）
    seen = runner.llm.calls[-1]
    got = [m for m in seen if m.get("role") == "user"
           and "60 秒左右" in str(m.get("content") or "")]
    check(len(got) == 1, f"⑨ 续跑时模型看到 user 角色的答案：{len(got)} 条")

    done = await mgr.load("run-h")
    saved = [m for m in (done.messages or []) if m.get("role") == "user"
             and "60 秒左右" in str(m.get("content") or "")]
    check(len(saved) == 1, f"⑨ 答案进了 checkpoint 消息链：{len(saved)} 条")
    check(len(done.messages or []) == before + 2,
          f"⑨ 只多了一条 user + 一条 assistant（没重复补回执）："
          f"{before} → {len(done.messages or [])}")
    await storage.close()


async def case_i_no_false_nudge_on_resume() -> None:
    """⑩ 从提问断点续跑后，不该再吃一条「你没有调用 ask_user」的假打回。

    为什么专门钉：ask_gate 的判据是「本轮没调过 ask_user + 收尾文本像在提问」。
    续跑开的是**新的** ``_drive``，``asked_this_run`` 从 False 起步，而这条 run
    明明就是被 ask_user 弹出去的。真机的 checkpoint 链里每个偶数 seq 都是一条
    这种 system 打回——模型确实调过工具，却被服务端反复指责「没用弹窗」。
    """
    print("\n=== ⑩ 提问续跑：不再注入「你没调用 ask_user」的假打回 ===")
    storage = build_storage("memory")
    await storage.start()
    reg = ToolRegistry()
    reg.register(AskUserTool())
    runner, mgr = _build(reg, [
        ("tool", "ask_user", {"title": "要多长？", "options": [
            {"key": "s60", "label": "60 秒", "recommended": True},
            {"key": "s90", "label": "90 秒"}]}),
        # 续跑后模型先顺着答一句、但文本里带着提问措辞：这不该被判成「提问却没弹窗」
        ("answer", "好，那你想用哪种风格？"),
        ("answer", "高光金句版做完了"),
    ], storage)
    sess = Session(user_id="u", conversation_id="c_appr_i")
    await runner.run(sess, "帮我剪一条", run_id="run-i")
    cp = await mgr.pending_approval("run-i")
    check(cp is not None, "⑩ 提问后确实挂起了")

    out2 = await runner.approve(cp, sess, decision="s60", note="60 秒左右")
    check(out2 == "好，那你想用哪种风格？", f"⑩ 续跑后的答复直接交付，没被打回：{out2}")
    check(len(runner.llm.calls) == 2,
          f"⑩ 续跑只问了一次 LLM（被打回会变成两次）：{len(runner.llm.calls)} 次")
    nudges = [m for call in runner.llm.calls for m in call
              if m.get("role") == "system" and "没有调用 ask_user" in str(m.get("content") or "")]
    check(not nudges, f"⑩ 没有假打回注入：{len(nudges)} 条")
    await storage.close()


async def main() -> int:
    await case_a_approve_continues()
    await case_b_reject_skips()
    await case_c_no_approval_runs_through()
    await case_d_set_approval_by_name()
    await case_e_confirm_executes_pending()
    await case_f_repair_keeps_pending()
    await case_g_ask_user_no_duplicate_result()
    await case_h_ask_answer_enters_as_user()
    await case_i_no_false_nudge_on_resume()
    print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
    print(f"用例 {CHECKS} 条", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))