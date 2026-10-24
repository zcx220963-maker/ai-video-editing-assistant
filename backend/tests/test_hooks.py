"""Hook 可观测机制验证（不联网）：5 节点触发顺序、异常隔离、finalize 链式改写、内置观测 Hook。

运行：  python tests/test_hooks.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import logging
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentOnceRun, AgentConfig
from agent_framework.context import ContextBuilder
from agent_framework.hooks import (
    AgentHook,
    AgentHookContext,
    CompositeHook,
    LoggingHook,
    MetricsHook,
    ToolTraceHook,
)
from agent_framework.llm import ScriptedLLM
from agent_framework.session import Session
from agent_framework.team_tools import FunctionTool
from agent_framework.tool import EchoTool, ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class RecorderHook(AgentHook):
    """把每个生命周期节点按发生顺序记进 events，用于断言接线。"""

    def __init__(self, tag: str = "r", sink: list | None = None) -> None:
        self.tag = tag
        self.events = sink if sink is not None else []

    async def before_iteration(self, context: AgentHookContext) -> None:
        self.events.append(f"before_iteration:{context.iteration}")

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        self.events.append(f"on_stream:{delta}")

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        self.events.append(f"on_stream_end:{resuming}")

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        self.events.append("after_execute_tools")

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        self.events.append("finalize_content")
        return content


class BoomHook(AgentHook):
    """所有异步节点都抛异常，用于验证 CompositeHook 的异常隔离。"""

    async def before_iteration(self, context: AgentHookContext) -> None:
        raise RuntimeError("boom-before")

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        raise RuntimeError("boom-after")

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        raise RuntimeError("boom-stream")


def _agent(steps, hooks, *, stream_cfg=3):
    reg = ToolRegistry()
    reg.register(EchoTool())
    llm = ScriptedLLM(steps, stream_chunk_size=stream_cfg)
    return AgentOnceRun(
        llm, reg, context_builder=ContextBuilder("BASE"),
        hooks=hooks, config=AgentConfig(max_iterations=5),
    )


async def main() -> None:
    # ---- 1. 非流式：一次带工具调用的完整执行，五个节点里应有的都被回调 ----
    events: list[str] = []
    agent = _agent(
        [("tool", "echo", {"text": "你好"}), ("answer", "完成")],
        CompositeHook([RecorderHook(sink=events)]),
    )
    out = await agent.run(Session(user_id="u", conversation_id="c"), "测一下")
    check(out == "完成", "非流式跑通并返回最终答复")
    kinds = {e.split(":")[0] for e in events}
    check("before_iteration" in kinds, "before_iteration 被回调")
    check("after_execute_tools" in kinds, "after_execute_tools 在工具执行后被回调")
    check("finalize_content" in kinds, "finalize_content 在收尾被回调")
    check(not {"on_stream", "on_stream_end"} & kinds, "非流式不触发 on_stream/on_stream_end")
    # 两轮：迭代 0（发工具）+ 迭代 1（给答复）。
    check(events.count("before_iteration:0") == 1 and events.count("before_iteration:1") == 1,
          f"每轮各触发一次 before_iteration：{events}")

    # ---- 2. 流式：on_stream 逐块、on_stream_end 收尾，均带 resuming 标志 ----
    sevents: list[str] = []
    sagent = _agent([("answer", "流式答复")], CompositeHook([RecorderHook(sink=sevents)]))
    sout = await sagent.run(Session(user_id="u", conversation_id="s"), "流式", stream=True)
    deltas = [e for e in sevents if e.startswith("on_stream:")]
    check(sout == "流式答复", "流式聚合出完整答复")
    check(len(deltas) >= 1 and any(e == "on_stream_end:False" for e in sevents),
          f"on_stream 逐块 + on_stream_end(resuming=False) 收尾：{deltas}")
    # resume 路径：resuming 应为 True（此处仅验证标志能透传，直接手动触发一次）。
    ctx = AgentHookContext(session=Session(user_id="u", conversation_id="s"), messages=[])
    rec = RecorderHook()
    await rec.on_stream_end(ctx, resuming=True)
    check("on_stream_end:True" in rec.events, "on_stream_end 透传 resuming=True")

    # ---- 3. finalize_content 链式改写：后一个 Hook 收到前一个的输出 ----
    class Appender(AgentHook):
        def __init__(self, suffix: str) -> None:
            self.suffix = suffix

        def finalize_content(self, context, content):
            return (content or "") + self.suffix

    fagent = _agent([("answer", "正文")], CompositeHook([Appender(" A"), Appender(" B")]))
    fout = await fagent.run(Session(user_id="u", conversation_id="f"), "改我")
    check(fout == "正文 A B", f"finalize 依串接顺序改写：{fout!r}")

    # ---- 4. CompositeHook 异常隔离：坏 Hook 抛错，好 Hook 照常执行、循环不中断 ----
    good_events: list[str] = []
    isolated = CompositeHook([BoomHook(), RecorderHook(sink=good_events)])
    with _mute_logger():
        gagent = _agent([("tool", "echo", {"text": "x"}), ("answer", "OK")], isolated)
        gout = await gagent.run(Session(user_id="u", conversation_id="g"), "隔离测试")
    check(gout == "OK", "坏 Hook 抛错不影响 Agent 正常产出")
    kinds = {e.split(":")[0] for e in good_events}
    check({"before_iteration", "after_execute_tools", "finalize_content"} <= kinds,
          f"好 Hook 的三个节点仍被回调：{sorted(kinds)}")

    # 单元级：CompositeHook 吞掉 async 节点异常并继续转发给后续 Hook。
    calls: list[str] = []
    comp = CompositeHook([BoomHook(), _Sink(calls)])
    with _mute_logger():
        await comp.before_iteration(AgentHookContext(session=Session(user_id="u", conversation_id="z"), messages=[]))
    check(calls == ["before_iteration"], "CompositeHook 转发遇异常后继续到下一个 Hook")

    # ---- 5. 内置 LoggingHook 真实写日志 ----
    logger = logging.getLogger("test_hooks_capture")
    logger.handlers.clear()
    buf = _ListHandler()
    logger.addHandler(buf)
    logger.setLevel(logging.DEBUG)
    lh = LoggingHook(logger_=logger)
    await lh.before_iteration(AgentHookContext(session=Session(user_id="u", conversation_id="L"), messages=[]))
    await lh.after_execute_tools(AgentHookContext(session=Session(user_id="u", conversation_id="L"), messages=[]))
    check(any("iter" in m for m in buf.messages) and any("tools done" in m for m in buf.messages),
          "LoggingHook 输出结构化日志记录")

    # ---- 6. 内置 MetricsHook 累计指标 + snapshot ----
    mhook = MetricsHook()
    cagent = _agent(
        [("tool", "echo", {"text": "m"}), ("answer", "done")], CompositeHook([mhook])
    )
    await cagent.run(Session(user_id="u", conversation_id="m"), "计数")
    snap = mhook.snapshot()
    check(snap["iterations"] == 2 and snap["tool_rounds"] == 1,
          f"MetricsHook：2 轮迭代、1 次工具回合：{snap}")

    # ---- 7. ToolTraceHook：call_id 配对 + invoked 两位 ----
    mq = _CollectMq()
    tagent = _agent([("tool", "echo", {"text": "t"}), ("answer", "好")],
                    CompositeHook([ToolTraceHook(mq)]))
    await tagent.run(Session(user_id="u", conversation_id="trace"), "追一下")
    calls = [f for f in mq.frames if f["type"] == "tool_call"]
    results = [f for f in mq.frames if f["type"] == "tool_result"]
    check(len(calls) == 1 and calls[0]["call_id"],
          f"tool_call 帧带上模型给的那次调用编号：{calls}")
    check(len(results) == 1 and results[0]["call_id"] == calls[0]["call_id"]
          and results[0]["invoked"] is True,
          f"结果帧认得出同一次调用且 invoked=True：{results[0].get('invoked')}")

    mq2 = _CollectMq()
    uagent = _agent([("tool", "split_shots", {}), ("answer", "换个说法")],
                    CompositeHook([ToolTraceHook(mq2)]))
    await uagent.run(Session(user_id="u", conversation_id="unknown"), "试试剪辑节点")
    ur = [f for f in mq2.frames if f["type"] == "tool_result"]
    check(len(ur) == 1 and ur[0]["invoked"] is False,
          f"注册表里没有的工具：调用从未发生，invoked=False（前端不把没跑的步记成受阻）")

    # ---- 8. 渲染视图单独挂车：result 截断不许削掉进度条起步那几行 ----
    long_view = (
        '{"node": "render_video", "artifact_id": "_default", "output": '
        '{"video": "renders/' + "x" * 700 + '.mp4", '
        '"media_url": "http://127.0.0.1:9000/creation-assets/' + "y" * 200 + '"}, '
        '"render": {"status": "running", "stage": "encoding", "percent": 42}}')

    async def _fake_render(**kwargs):
        return long_view

    rreg = ToolRegistry()
    rreg.register(FunctionTool("render_video", "渲染成片", {}, _fake_render))
    rmq = _CollectMq()
    ragent = AgentOnceRun(
        ScriptedLLM([("tool", "render_video", {}), ("answer", "好了")], stream_chunk_size=3),
        rreg, context_builder=ContextBuilder("BASE"),
        hooks=CompositeHook([ToolTraceHook(rmq)]), config=AgentConfig(max_iterations=5))
    await ragent.run(Session(user_id="u", conversation_id="render"), "出片")
    rr = [f for f in rmq.frames if f["type"] == "tool_result"]
    check(len(rr) == 1 and rr[0]["result"].endswith("…")
          and '"status"' not in rr[0]["result"],
          "结果文本照旧截断（进度块落在截断线之后——真机就是这样让进度条消失的）")
    check(rr[0]["render"] == {"artifact_id": "_default", "status": "running",
                              "stage": "encoding", "percent": 42},
          f"帧上单独挂的 render 视图完整：{rr[0].get('render')}")
    check(ur[0].get("render") is None,
          "没有 render 块的工具：帧上 render=None，前端继续走原有的文本解析")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


class _CollectMq:
    """假 MQ：只把回投帧按顺序收下，供断言帧上的字段。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def publish(self, topic: str, key: str, payload: dict) -> None:
        self.frames.append(payload)


class _Sink(AgentHook):
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def before_iteration(self, context: AgentHookContext) -> None:
        self.calls.append("before_iteration")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _mute_logger:
    """临时静音 agent_framework logger 的 exception 输出，保持测试日志干净。"""

    def __enter__(self):
        self._logger = logging.getLogger("agent_framework")
        self._prev = self._logger.level
        self._logger.setLevel(logging.CRITICAL)
        return self

    def __exit__(self, *exc):
        self._logger.setLevel(self._prev)
        return False


if __name__ == "__main__":
    asyncio.run(main())
