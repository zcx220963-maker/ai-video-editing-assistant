"""流式输出验证（不联网）：delta 逐块回调、tool_calls 分片聚合、无流式后端回退、写回会话。

运行：  python tests/test_stream.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from types import SimpleNamespace

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.checkpoint import CheckpointManager
from agent_framework.context import ContextBuilder
from agent_framework.hooks import AgentHook, AgentHookContext
from agent_framework.llm import ScriptedLLM
from agent_framework.llm_openai import OpenAICompatClient
from agent_framework.messages import ToolCall
from agent_framework.session import Session
from agent_framework.storage import build_storage
from agent_framework.tool import Tool, ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class RecordingHook(AgentHook):
    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.ends: list[bool] = []  # 记录每次 on_stream_end 的 resuming

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        self.deltas.append(delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        self.ends.append(resuming)


class UpperTool(Tool):
    @property
    def name(self) -> str:
        return "upper"

    @property
    def description(self) -> str:
        return "转大写"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, text: str) -> str:
        return text.upper()


class NoStreamLLM:
    """只实现 complete、没有 complete_stream 的后端。"""

    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, tools=None):
        from agent_framework.llm import LLMResponse
        return LLMResponse(content=self._content)


async def main() -> None:
    # ---- 1. 纯文本流式：逐块回调 + 聚合 + 写回会话（最简单情形）----
    reg = ToolRegistry()
    reg.register(UpperTool())
    hook = RecordingHook()
    llm = ScriptedLLM([("answer", "今天天气真不错")], stream_chunk_size=3)
    runner = AgentOnceRun(llm, reg, ContextBuilder("S"), hooks=hook, config=AgentConfig(max_iterations=3))
    sess = Session(user_id="u", conversation_id="s1")
    ans = await runner.run(sess, "外面天气如何", stream=True)

    check("".join(hook.deltas) == "今天天气真不错", "文本 delta 拼接等于完整答复")
    check(len(hook.deltas) == 3, f"按 chunk_size=3 切成 3 段(7字): {hook.deltas}")
    check(hook.ends == [False], "on_stream_end 触发一次，resuming=False")
    check(ans == "今天天气真不错", "run 返回聚合后的完整答复")
    check(sess.messages[-1]["role"] == "assistant" and sess.messages[-1]["content"] == "今天天气真不错",
          "流式答复写回会话（最简单情形）")

    # ---- 2. 流式 + 工具轮：工具轮无文本、答复盘逐块 ----
    hook2 = RecordingHook()
    llm2 = ScriptedLLM([("tool", "upper", {"text": "abc"}), ("answer", "结果是 ABC")], stream_chunk_size=2)
    sess2 = Session(user_id="u", conversation_id="s2")
    r2 = AgentOnceRun(llm2, reg, ContextBuilder("S"), hooks=hook2, config=AgentConfig(max_iterations=4))
    ans2 = await r2.run(sess2, "转大写", stream=True)
    check("".join(hook2.deltas) == "结果是 ABC", "工具轮之后的答复文本被逐块回调")
    check(hook2.ends == [False, False], f"两轮各触发一次 on_stream_end: {hook2.ends}")
    check(ans2 == "结果是 ABC" and len(sess2.messages) == 2, "含工具的流式执行最终写回会话")

    # ---- 3. 无流式后端回退：整段作为一次 delta ----
    hook3 = RecordingHook()
    r3 = AgentOnceRun(NoStreamLLM("一次性回复"), reg, ContextBuilder("S"), hooks=hook3)
    ans3 = await r3.run(Session(user_id="u", conversation_id="s3"), "hi", stream=True)
    check(hook3.deltas == ["一次性回复"] and hook3.ends == [False], "不支持流式的后端回退为单次 delta")
    check(ans3 == "一次性回复", "回退路径仍返回完整答复")

    # ---- 4. resuming 透传：从 checkpoint 恢复时 on_stream_end(resuming=True) ----
    storage = build_storage("memory")
    await storage.start()
    try:
        mgr = CheckpointManager(storage)
        # 迭代0 用工具、迭代1 崩溃 → 落一致点，恢复后走到答复
        hook4 = RecordingHook()
        crash_llm = ScriptedLLM([("tool", "upper", {"text": "x"}), ("answer", "恢复答复")], stream_chunk_size=4)
        r4 = AgentOnceRun(crash_llm, reg, ContextBuilder("S"), hooks=hook4,
                          config=AgentConfig(max_iterations=4), checkpoint=mgr)
        # 手动跑半程：直接 run 会跑完；这里用低层——改为先 run 完成即可验证 resuming 恒 False
        s4 = Session(user_id="u", conversation_id="s4")
        await r4.run(s4, "开工", run_id="r4", stream=True)
        check(all(e is False for e in hook4.ends), "常规 run 全程 resuming=False")

        # 构造一个未完成的 checkpoint 再 resume
        storage2 = build_storage("memory")
        await storage2.start()
        mgr2 = CheckpointManager(storage2)
        try:
            cp = await mgr2.begin("u:s5", "查询",
                                  [{"role": "system", "content": "S"},
                                   {"role": "user", "content": "查询"}], "r5")
            await mgr2.save_progress(cp, iteration=1, messages=cp.messages)
            hook5 = RecordingHook()
            r5 = AgentOnceRun(ScriptedLLM([("answer", "续跑答复")], stream_chunk_size=3), reg,
                              ContextBuilder("S"), hooks=hook5,
                              config=AgentConfig(max_iterations=4), checkpoint=mgr2)
            ans5 = await r5.resume(cp, Session(user_id="u", conversation_id="s5"), stream=True)
            check(ans5 == "续跑答复" and hook5.ends == [True], "resume 时 on_stream_end(resuming=True)")
        finally:
            await storage2.close()
    finally:
        await storage.close()

    # ---- 5. OpenAI 适配器流式：文本 delta + 分片 tool_calls 聚合 ----
    events = [
        _ev(content="Hel"),
        _ev(content="lo"),
    ]
    client = OpenAICompatClient(client=_FakeStreamClient(events), api_key="x")
    parts: list[str] = []
    async for c in client.complete_stream([{"role": "user", "content": "hi"}]):
        if c.delta:
            parts.append(c.delta)
    check("".join(parts) == "Hello", "OpenAI 流式文本 delta 聚合")

    # 分片工具调用：id/name/arguments 跨块到达
    tev = [
        _ev(tool_calls=[_tc(0, id="call_9", name="search", arguments=None)]),
        _ev(tool_calls=[_tc(0, name="", arguments='{"q":')]),
        _ev(tool_calls=[_tc(0, name="", arguments=' "北京"}')]),
    ]
    tclient = OpenAICompatClient(client=_FakeStreamClient(tev), api_key="x")
    final_calls = None
    async for chunk in tclient.complete_stream([{"role": "user", "content": "x"}]):
        if chunk.tool_calls is not None:
            final_calls = chunk.tool_calls
    check(final_calls and final_calls[0].id == "call_9" and final_calls[0].name == "search"
          and final_calls[0].arguments == {"q": "北京"},
          "跨块分片的 tool_calls 在客户端聚合还原")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


def _ev(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tc(index, id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments))


class _AsyncStream:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for it in self._items:
                yield it
        return gen()


class _FakeStreamClient:
    def __init__(self, events):
        self._events = events
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        return _AsyncStream(self._events)


if __name__ == "__main__":
    asyncio.run(main())
