# -*- coding: utf-8 -*-
"""「向用户提问必须走弹窗」的硬保证验证（不联网、不起服务）。

钉的链子形状：
① 判据本身：强索取（含不带问号的「请把链接发我」）算提问；正常结论不算，
   尤其不能把「两点需要你知道的偏差」这种**告知**误判成提问；
② 集成：模型第一轮把问题写在正文里 → 循环**打回**并喂一条「用 ask_user 重问」的核对；
③ 打回后模型改用 ask_user → 本轮挂起、发出一条带 ask 的审批帧（前端据此弹窗）；
④ 打回预算用尽仍在提问 → 如实交付，但末尾附一句「这句本该是个弹窗」。

运行：  python tests/test_ask_gate.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun, _APPROVAL_PAUSE_ANSWER  # noqa: E402
from agent_framework.ask_gate import (  # noqa: E402
    NO_POPUP_NOTE, looks_like_asking_user, looks_like_plan_invite,
)
from agent_framework.ask_user import AskUserTool  # noqa: E402
from agent_framework.checkpoint import (  # noqa: E402
    CheckpointManager, STATUS_AWAITING_APPROVAL,
)
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import AgentHook, AgentHookContext, CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class AskSpy(AgentHook):
    """记下循环发出过哪些带 ask 的审批帧。"""

    def __init__(self) -> None:
        self.asks: list[dict] = []

    async def on_approval_required(self, context: AgentHookContext, calls: list[dict],
                                   reason: str = "", fallback_options=None,
                                   ask: dict | None = None) -> None:
        if ask:
            self.asks.append(ask)


def case_detector() -> None:
    print("\n=== ① 判据：该拦的拦、不该拦的不拦 ===")
    # 强索取：不带问号也要拦（用户点名的场景都是祈使句）
    for text in [
        "请把视频链接发我，或者告诉我素材在哪。",
        "麻烦你把视频链接贴给我，我来取。",
        "需要你上传一条更长的素材，不然凑不到 30 秒。",
        "你告诉我更想要哪种节奏，我按那个来。",
        "已经给你弹了两个选项卡片，麻烦点一下：1. 风格 2. 时长",
        "选一个时长。",
    ]:
        check(looks_like_asking_user(text), f"拦：{text[:26]}…")
    # 正常答复不能误伤
    for text in [
        "成片已渲染完成 ✅ 时长 90 秒 | 1920x1080 | 30fps",
        "当前链路没有去字幕能力，字幕是烧进画面的硬字幕，只能整段保留或整段放弃。",
        "为什么会这样？因为素材本身只有 10 秒，铺满 20 秒必然要循环。",
        "两点需要你知道的偏差：字幕没能完全去掉；末尾那句话被截断在 90s 处。",
        "用户的诉求是风景占 80%，我按这个排好了时间线。",
        "已按你的要求把出镜比例设成 20%，时间线排好了。",
        "",
    ]:
        check(not looks_like_asking_user(text), f"不拦：{text[:26] or '(空串)'}…")
    check(not looks_like_asking_user(None), "不拦：None")


async def case_prose_question_is_bounced() -> None:
    print("\n=== ②③ 正文提问被打回 → 改用 ask_user → 挂起并发出弹窗帧 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        reg = ToolRegistry()
        reg.register(AskUserTool())
        spy = AskSpy()
        runner = AgentOnceRun(
            # 第一轮：把问题写在正文里（不带工具）→ 应被打回
            # 第二轮：改用 ask_user → 应挂起
            ScriptedLLM([
                ("answer", "这条片子你想要多长？请你选一个时长，我再排时间线。"),
                ("tool", "ask_user", {
                    "title": "这条片子你想要多长？",
                    "options": [{"key": "s30", "label": "30 秒", "recommended": True},
                                {"key": "s60", "label": "60 秒"}],
                }),
                ("answer", "好的，按你选的来"),
            ]),
            reg, ContextBuilder("BASE"), hooks=CompositeHook([spy]),
            config=AgentConfig(max_iterations=6), checkpoint=CheckpointManager(storage),
            storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_ask_gate")
        out = await runner.run(sess, "帮我剪一条", run_id="run-askgate")

        check(out == _APPROVAL_PAUSE_ANSWER, f"②③ 最终挂起等弹窗：{out[:40]}")
        check(len(spy.asks) == 1, f"②③ 恰好发出一条带 ask 的审批帧（实得 {len(spy.asks)}）")
        if spy.asks:
            ask = spy.asks[0]
            check(bool(ask.get("title")), f"②③ 帧里有题面：{ask.get('title')}")
            check(len(ask.get("options") or []) >= 2, "②③ 帧里有选项（前端才能渲染选项卡）")
            check(any(o.get("recommended") for o in ask.get("options") or []),
                  "②③ 有推荐项")
        cp = await runner.checkpoint.pending_approval("run-askgate")
        check(cp is not None and cp.status == STATUS_AWAITING_APPROVAL,
              "②③ checkpoint 落 awaiting_approval")
        # 打回时喂进去的那条核对确实要求它用 ask_user
        joined = " ".join(str(m.get("content") or "") for m in (cp.messages or []))
        check("ask_user" in joined, "②③ 打回文案里点名了 ask_user")
        check("没有例外" in joined, "②③ 打回文案写明了「没有例外」")
    finally:
        await storage.close()


async def case_budget_exhausted_appends_note() -> None:
    print("\n=== ④ 打回用尽仍在提问 → 如实交付 + 附「本该是弹窗」的核对 ===")
    storage = build_storage("memory")
    await storage.start()
    try:
        reg = ToolRegistry()
        reg.register(AskUserTool())
        runner = AgentOnceRun(
            # 两轮都只在正文里问，从来不调 ask_user
            ScriptedLLM([
                ("answer", "请你选一个时长，我再继续。"),
                ("answer", "麻烦你把视频链接发我。"),
            ]),
            reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6, popup_question_nudges=1),
            checkpoint=CheckpointManager(storage), storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_ask_note")
        out = await runner.run(sess, "剪一条", run_id="run-note")
        check(NO_POPUP_NOTE.strip() in out, "④ 末尾补上了兜底核对")
        check("本来就该是弹窗" in NO_POPUP_NOTE or "本该" in NO_POPUP_NOTE,
              "④ 兜底核对说明这句本该是弹窗")
    finally:
        await storage.close()


async def case_flag_off() -> None:
    print("\n=== ⑤ 关掉这道保证后，正文提问原样交付（不砍老行为）===")
    storage = build_storage("memory")
    await storage.start()
    try:
        reg = ToolRegistry()
        reg.register(AskUserTool())
        runner = AgentOnceRun(
            ScriptedLLM([("answer", "请你选一个时长，我再继续。")]),
            reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=4, require_popup_questions=False),
            checkpoint=CheckpointManager(storage), storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_ask_off")
        out = await runner.run(sess, "剪一条", run_id="run-off")
        check(out == "请你选一个时长，我再继续。", f"⑤ 原样交付：{out}")
        check(NO_POPUP_NOTE.strip() not in out, "⑤ 没有多余的兜底核对")
    finally:
        await storage.close()


async def case_plan_must_be_card() -> None:
    """计划必须出卡：不能把方案写成正文再要用户"回复确认"。

    真机实测的形状（用户原话「为什么这步没有弹窗??我不想打字」）：
    模型弹窗问了时长、又问出镜分布，用户都点完，最后模型把整套方案写成
    正文，结尾「确认这个方案就回复我，我立刻开始跑」——既没有计划卡可点，
    又明确要用户打字。这条用例钉住：这种收尾会被打回，并喂一条要求出卡的核对。
    """
    print("\n=== ⑥ 方案写成正文 + 要用户回复确认 → 打回要求出卡 ===")
    prose = ("这一版会怎么做（3 分钟 · 金句出镜版）\n"
             "1. 加载素材…\n2. 采访转写…\n"
             "确认这个方案就回复我，我立刻开始跑。")
    storage = build_storage("memory")
    await storage.start()
    try:
        reg = ToolRegistry()
        reg.register(AskUserTool())
        mgr = CheckpointManager(storage)
        # 第一轮给散文方案，第二轮（被打回后）才交卡——用真实 SubmitPlanTool 太重，
        # 这里只验「打回发生过」，所以第二轮给一个普通收尾即可。
        runner = AgentOnceRun(
            ScriptedLLM([("answer", prose), ("answer", "方案已交。")]),
            reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr, storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_plan_card")
        # planning=True 才会认这条保证（规划轮才是该出卡的轮次）
        out = await runner.run(sess, "剪一条", run_id="run-pc", planning=True)
        # 证据从一致点链里取（真实来源，不猜内部属性）
        cp = await mgr.load("run-pc")
        msgs = list(getattr(cp, "messages", None) or []) if cp else []
        joined = "\n".join(str((m or {}).get("content") or "") for m in msgs)
        hit = ("submit_plan" in joined) or ("计划卡" in joined)
        check(hit, f"⑥ 打回时要求改用 submit_plan 出卡（链上消息 {len(msgs)} 条，"
                   f"末答：{out[:36]}）")
    finally:
        await storage.close()

    print("\n=== ⑦ 正常收尾不该被打回 ===")
    for text, tag in [("已按你的要求渲完了，成片在这里。", "交付说明"),
                      ("这条素材能用，1080p/25fps。", "纯咨询"),
                      ("我建议先切镜头再理解画面。", "普通叙述")]:
        check(not looks_like_plan_invite(text), f"⑦ 不误伤：{tag}")


async def main() -> int:
    case_detector()
    await case_prose_question_is_bounced()
    await case_budget_exhausted_appends_note()
    await case_flag_off()
    await case_plan_must_be_card()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
