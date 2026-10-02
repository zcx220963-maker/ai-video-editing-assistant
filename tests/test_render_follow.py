# -*- coding: utf-8 -*-
"""#26「渲染未达终态不许收尾」硬保证的离线验证（不联网、不起服务）。

钉住五件事：
① 模型拿到 ``queued`` 就收尾时，Agent 循环自己轮 ``render_status`` 到 ``done``，
   终态结果进上下文、成片卡片回投 MQ、持久链接进 qa.parts 挂起缓冲——三处出口
   全部与「模型自己查」时同形；
② 中间态（queued/running）只发进度帧，不拼进 messages：一次长渲染按秒级轮询
   也不该撑爆上下文；
③ 预算用尽仍未达终态：如实追加 system 说明「不要声称成片已完成」，且**不**publish
   任何 media 卡片（宁缺不假）；
④ Registry 里没有 render_status（无剪辑装配）时行为与改动前一致；
⑤ rerun_from 分叉交接那一轮不追旧作用域的在途渲染。

运行：  python tests/test_render_follow.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun, _inflight_render_id
from agent_framework.context import ContextBuilder
from agent_framework.hooks import (
    MediaCardHook,
    AgentHookContext,
    CompositeHook,
)
from agent_framework.connection_manager import OUTBOUND_TOPIC
from agent_framework.llm import ScriptedLLM
from agent_framework.media_replay import drain_rendered_media, reset_rendered_media
from agent_framework.messages import ToolCall
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.session import Session
from agent_framework.tool import Tool, ToolRegistry

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


ART = "art_render_1"


def queued_view(art: str = ART) -> str:
    """``render_jobs.tool_view`` 非终态输出的形状（提交后台渲染后立刻返回的那一版）。"""
    return json.dumps({
        "node": "render_video", "artifact_id": art, "output": None,
        "render": {"status": "queued", "stage": "queued", "percent": 0},
        "hint": f"渲染仍在进行。请调用 render_status（artifact_id={art!r}）继续查询",
    }, ensure_ascii=False)


def running_view(art: str = ART) -> str:
    return json.dumps({
        "node": "render_video", "artifact_id": art, "output": None,
        "render": {"status": "running", "stage": "compositing", "percent": 62},
        "hint": "请调用 render_status 继续查询",
    }, ensure_ascii=False)


def done_view(art: str = ART, *, url: str = "") -> str:
    """终态成功视图：每张成片有自己的直链（MediaCardHook 按 URL 去重，共用会让断言失真）。"""
    return json.dumps({
        "node": "render_video", "artifact_id": art,
        "output": {"media_url": url or f"http://minio.local/renders/{art}.mp4?X-Amz-Sig=1",
                   "video": f"renders/{art}.mp4",
                   "title": "成片", "duration": 12.5},
        "render": {"status": "done", "percent": 100},
    }, ensure_ascii=False)


def failed_view(art: str = ART) -> str:
    return json.dumps({
        "node": "render_video", "artifact_id": art, "output": None,
        "render": {"status": "failed", "error": "ffmpeg 退出码 1"},
    }, ensure_ascii=False)


class RenderVideoTool(Tool):
    """提交渲染：注册即返回 queued 视图（与 submit+poll 落地后的真工具同形）。"""

    def __init__(self, view: str | None = None) -> None:
        self.view = view or queued_view()
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "render_video"

    @property
    def description(self) -> str:
        return "提交渲染并立刻返回在途视图"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.view


class RenderStatusTool(Tool):
    """查渲染状态：按 statuses 依次吐视图，用尽后重复最后一个（便于断言轮询次数）。"""

    def __init__(self, statuses: list[str], *, node_prefix: str = "") -> None:
        self.statuses = list(statuses)
        self.calls: list[dict[str, Any]] = []
        self._name = f"{node_prefix}render_status" if node_prefix else "render_status"

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "查询渲染状态"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, artifact_id: str = "") -> str:
        self.calls.append({"artifact_id": artifact_id})
        i = min(len(self.calls) - 1, len(self.statuses) - 1)
        view = self.statuses[i]
        if view == "queued":
            return queued_view(artifact_id)
        if view == "running":
            return running_view(artifact_id)
        if view == "done":
            return done_view(artifact_id)
        if view == "failed":
            return failed_view(artifact_id)
        return view


def build_agent(
    steps: list[Any], *, tools: list[Tool], mq: InMemoryMessageQueue,
    config: AgentConfig | None = None,
) -> AgentOnceRun:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return AgentOnceRun(
        ScriptedLLM(steps), reg,
        context_builder=ContextBuilder("BASE"),
        hooks=CompositeHook([MediaCardHook(mq)]),
        config=config or AgentConfig(max_iterations=6, render_poll_sec=0.1,
                                     render_follow_max_sec=5.0),
    )


def media_frames(seen: list[dict]) -> list[dict]:
    return [p for p in seen if p.get("type") == "media"]


def tool_msgs(messages: list[dict], name: str) -> list[dict]:
    return [m for m in messages if m.get("role") == "tool" and m.get("name") == name]


async def case_a_reaches_terminal() -> None:
    """① + ②：模型收尾即轮询，终态进上下文并落两处成片出口。"""
    reset_rendered_media()
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, "test-follow", lambda p: seen.append(p))
    await mq.start()

    status = RenderStatusTool(["running", "running", "done"])
    agent = build_agent(
        [("tool", "render_video", {}), ("answer", "成片已完成")],
        tools=[RenderVideoTool(), status], mq=mq)
    out = await agent.run(Session(user_id="u", conversation_id="c_a"), "出片")

    await mq.drain()
    await mq.stop()

    check(out == "成片已完成", "① 循环照常返回最终答复")
    check(len(status.calls) == 3, f"① 由循环发起 3 次 render_status 直到 done：{len(status.calls)} 次")
    check([c["artifact_id"] for c in status.calls] == [ART] * 3,
          f"① 轮询带的是渲染返回的 artifact_id：{status.calls}")

    llm_messages = agent.llm.calls[-1]
    polls = tool_msgs(llm_messages, "render_status")
    check(len(polls) == 1,
          f"② 只有终态那一次进上下文（中间态不进）：{len(polls)} 条")
    check(bool(polls) and '"media_url"' in str(polls[0].get("content")),
          "② 进上下文的那条是 done 视图（含 media_url）")
    check(bool(polls) and '"running"' not in str(polls[0].get("content")),
          "② 上下文里没有 running 中间态")

    check(len(media_frames(seen)) == 1,
          f"① 成片卡片回投 OutBound 一次：{media_frames(seen)}")
    card = (media_frames(seen) or [{}])[0]
    check(card.get("media_url", "").startswith("http://minio.local/renders/"),
          f"① 卡片带可播直链：{card.get('media_url')}")

    persisted = drain_rendered_media()
    check(len(persisted) == 1 and persisted[0].get("artifact_id") == ART
          and persisted[0].get("video_object_key") == f"renders/{ART}.mp4",
          f"① 持久链接已登记，供本轮 assistant 行 drain：{persisted}")


async def case_b_budget_exhausted() -> None:
    """③：预算用尽不假称成功——如实说明、不发卡片。"""
    reset_rendered_media()
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, "test-follow", lambda p: seen.append(p))
    await mq.start()

    status = RenderStatusTool(["running"])
    agent = build_agent(
        [("tool", "render_video", {}), ("answer", "好的")],
        tools=[RenderVideoTool(), status], mq=mq,
        config=AgentConfig(max_iterations=6, render_poll_sec=0.1,
                           render_follow_max_sec=0.25))
    out = await agent.run(Session(user_id="u", conversation_id="c_b"), "出片")
    await mq.drain()
    await mq.stop()

    check(out == "好的", "③ 预算用尽后循环仍正常收尾")
    check(len(status.calls) >= 1, f"③ 预算内确实轮询过：{len(status.calls)} 次")
    last = agent.llm.calls[-1]
    notes = [m for m in last if m.get("role") == "system" and "仍未达终态" in str(m.get("content"))]
    check(len(notes) == 1, f"③ 追加一条如实说明的 system：{notes}")
    check(bool(notes) and "不要声称成片已完成" in str(notes[0]["content"])
          and ART in str(notes[0]["content"]),
          "③ 说明里给出 artifact_id 并明确要求不声称完成")
    check(not tool_msgs(last, "render_status"), "③ 未终态的轮询不进上下文")
    check(media_frames(seen) == [], "③ 没有发布任何成片卡片")
    check(drain_rendered_media() == [], "③ 没有登记任何持久成片链接")


async def case_c_no_render_status_unchanged() -> None:
    """④：无剪辑装配（Registry 里没有 render_status）时行为与改动前一致。"""
    reset_rendered_media()
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, "test-follow", lambda p: seen.append(p))
    await mq.start()

    agent = build_agent(
        [("tool", "render_video", {}), ("answer", "已提交")],
        tools=[RenderVideoTool()], mq=mq)
    out = await agent.run(Session(user_id="u", conversation_id="c_c"), "出片")
    await mq.drain()
    await mq.stop()

    check(out == "已提交", "④ 没有 render_status 时循环照常返回")
    last = agent.llm.calls[-1]
    check(not any(m.get("role") == "system" and "仍未达终态" in str(m.get("content"))
                  for m in last),
          "④ 也不追加预算耗尽说明（无可轮对象）")
    check(media_frames(seen) == [], "④ queued 视图不发卡片")


async def case_e_two_renders_and_failed() -> None:
    """⑤ 前置：一轮里两条渲染各自轮到终态；failed 也算终态，不空转。"""
    reset_rendered_media()
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, "test-follow", lambda p: seen.append(p))
    await mq.start()

    render_a = RenderVideoTool(queued_view("art_a"))
    render_b = RenderVideoTool(queued_view("art_b"))
    status = RenderStatusTool(["running", "done"])   # 每条渲染各查 2 次
    agent = build_agent(
        [("tool", "render_video", {}), ("answer", "两条都完了")],
        tools=[render_a, render_b, status], mq=mq)
    ctx = AgentHookContext(session=Session(user_id="u", conversation_id="c_e"), messages=[])
    await agent._follow_inflight_renders(
        ctx, [("render_video", render_a.view), ("render_video", render_b.view)])
    await mq.drain()
    await mq.stop()

    check({c["artifact_id"] for c in status.calls} == {"art_a", "art_b"},
          f"⑤ 两条在途渲染都被追：{[c['artifact_id'] for c in status.calls]}")
    check(len(tool_msgs(ctx.messages, "render_status")) == 2,
          "⑤ 两条终态结果各进上下文一次")
    check(len(media_frames(seen)) == 2, f"⑤ 两张卡片各回投一次：{len(media_frames(seen))}")
    check(len(drain_rendered_media()) == 2, "⑤ 两条持久链接都登记了")

    # failed 也是终态：进上下文一次即止，不耗到预算
    reset_rendered_media()
    s2 = RenderStatusTool(["failed"])
    a2 = build_agent([("answer", "x")], tools=[RenderVideoTool(), s2], mq=mq)
    ctx2 = AgentHookContext(session=Session(user_id="u", conversation_id="c_f"), messages=[])
    await a2._follow_inflight_renders(ctx2, [("render_video", queued_view())])
    check(len(s2.calls) == 1 and len(tool_msgs(ctx2.messages, "render_status")) == 1,
          "⑤ failed 视为终态：一次查询即止并如实进上下文")
    check(media_frames(seen) and len(media_frames(seen)) == 2,
          "⑤ failed 结果不含 media_url，不新增卡片")


async def case_f_handover_skips() -> None:
    """⑤：rerun_from 分叉交接那一轮不追旧作用域的在途渲染。"""
    reset_rendered_media()
    mq = InMemoryMessageQueue()
    seen: list[dict] = []
    mq.subscribe(OUTBOUND_TOPIC, "test-follow", lambda p: seen.append(p))
    await mq.start()

    status = RenderStatusTool(["done"])
    agent = build_agent([("answer", "x")], tools=[RenderVideoTool(), status], mq=mq)
    ctx = AgentHookContext(session=Session(user_id="u", conversation_id="c_h"), messages=[])
    ctx.extras["handover"] = object()
    await agent._execute_tool_calls(ctx, [ToolCall(id="t1", name="render_video", arguments={})])
    await mq.drain()
    await mq.stop()

    check(status.calls == [], "⑤ 交接轮不发起轮询（新 run 有自己的渲染）")
    check(media_frames(seen) == [], "⑤ 交接轮不回投旧作用域卡片")


def case_g_unit_inflight() -> None:
    """``_inflight_render_id`` 的判据边界：只认 queued/running。"""
    check(_inflight_render_id(queued_view("a1")) == "a1", "queued 字符串 → artifact_id")
    check(_inflight_render_id(json.loads(running_view("a2"))) == "a2", "dict 也认")
    check(_inflight_render_id(done_view("a3")) is None, "done → 终态，不轮")
    check(_inflight_render_id(failed_view("a4")) is None, "failed → 终态，不轮")
    check(_inflight_render_id('{"render": {"status": "none"}}') is None,
          "none（还没提交过）→ 不轮")
    check(_inflight_render_id("not json") is None, "非 JSON 文本 → 不轮")
    check(_inflight_render_id(None) is None, "None → 不轮")
    check(_inflight_render_id({"artifact_id": "", "render": {"status": "queued"}}) == "_default",
          "空 artifact_id 回落 _default（真服务的作用域默认产物）")


async def main() -> None:
    await case_a_reaches_terminal()
    await case_b_budget_exhausted()
    await case_c_no_render_status_unchanged()
    await case_e_two_renders_and_failed()
    await case_f_handover_skips()
    case_g_unit_inflight()
    print(f"\n通过 {CHECKS - FAILS}/{CHECKS}" + ("" if not FAILS else f"，失败 {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
