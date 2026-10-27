# -*- coding: utf-8 -*-
"""R12：记忆文件后端——User.md/Tool.md 落盘、全量覆盖、CAS、每轮刷新、隔离。

后半段起真 storage + 真 AgentOnceRun 跑一次：模型在中间用 update_memory 写下的
内容，**下一次 LLM 调用**的 system 里就要看得见（设计原文：「每次与 LLM 交互都会
携带记忆内容」）。刷新是原地替换装配期那段 <memory>，不新增系统消息、不绕过预算。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
import tempfile
from dataclasses import dataclass, field

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import LLMResponse  # noqa: E402
from agent_framework.memory import (  # noqa: E402
    MemoryContextSource,
    MemoryIterationHook,
    MemoryStore,
    UpdateMemoryTool,
    make_iteration_hook,
)
from agent_framework.memory_file import FileMemoryBackend  # noqa: E402
from agent_framework.messages import ToolCall  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

_fail = 0
_pass = 0

_MEM_RE = re.compile(r"<memory>.*?</memory>", re.S)


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


@dataclass
class Ctx:
    messages: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)


class _FakeSource:
    def __init__(self, body: str) -> None:
        self.body = body
        self.queries: list[str] = []

    async def render(self, query: str, **_kw) -> str | None:
        self.queries.append(query)
        return self.body


async def file_backend(tmp: str) -> None:
    be = FileMemoryBackend(tmp)

    # ---- 文件名固定（设计点名 User.md / Tool.md）----
    await be.write("u1", "user", "喜欢快节奏")
    await be.write("u1", "tool", "导出优先 mp4")
    check((pathlib.Path(tmp) / "u1" / "User.md").exists(), "落成 User.md")
    check((pathlib.Path(tmp) / "u1" / "Tool.md").exists(), "落成 Tool.md")
    check(await be.read("u1", "user") == "喜欢快节奏", "读得回")

    # ---- 全量覆盖写 ----
    await be.write("u1", "user", "只留这一句")
    check(await be.read("u1", "user") == "只留这一句", "write 是全量覆盖")

    # ---- append ----
    merged = await be.append("u1", "user", "追加一句")
    check("只留这一句" in merged and "追加一句" in merged, "append 保留旧内容")

    # ---- CAS ----
    before = await be.read("u1", "user")
    check(await be.replace_if("u1", "user", before, "改过了") is True, "CAS 命中时写入")
    check(await be.replace_if("u1", "user", before, "又改") is False, "内容变了 CAS 失败")

    # ---- entries / stamps ----
    entries = await be.entries("u1")
    check(set(entries) == {"user", "tool"}, f"entries 列两分类（{sorted(entries)}）")
    stamps = await be.stamps("u1")
    check(set(stamps) == {"user", "tool"}, "stamps 给两分类的时间戳")

    # ---- 用户隔离 ----
    check(await be.read("u2", "user") == "", "别人的记忆读不到")


def hook_switch() -> None:
    # ---- 默认挂（设计「每次交互都携带」），只有显式关才不挂 ----
    check(make_iteration_hook(source=_FakeSource("x")) is not None, "默认启用每轮刷新")
    check(make_iteration_hook(source=_FakeSource("x"), enabled=False) is None,
          "enabled=False 不挂")
    old = os.environ.get("MEMORY_EVERY_ITERATION")
    try:
        os.environ["MEMORY_EVERY_ITERATION"] = "0"
        check(make_iteration_hook(source=_FakeSource("x")) is None,
              "MEMORY_EVERY_ITERATION=0 关掉")
        os.environ["MEMORY_EVERY_ITERATION"] = "1"
        check(make_iteration_hook(source=_FakeSource("x")) is not None,
              "MEMORY_EVERY_ITERATION=1 打开")
    finally:
        if old is None:
            os.environ.pop("MEMORY_EVERY_ITERATION", None)
        else:
            os.environ["MEMORY_EVERY_ITERATION"] = old


async def hook_refresh() -> None:
    src = _FakeSource("新记忆：用户喜欢冷色调")
    hook = MemoryIterationHook(src)
    ctx = Ctx([{"role": "system", "content": "base\n\n<memory>\n旧记忆</memory>"},
               {"role": "user", "content": "hi"}])

    await hook.before_iteration(ctx)
    system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
    check(len(system_msgs) == 1, f"没有新增 system 消息（实际 {len(system_msgs)} 条）")
    check(len(ctx.messages) == 2, f"消息总数不变（实际 {len(ctx.messages)}）")
    check("新记忆：用户喜欢冷色调" in system_msgs[0]["content"], "刷新成新正文")
    check("旧记忆" not in system_msgs[0]["content"], "旧正文被替换掉")
    check(len(_MEM_RE.findall(system_msgs[0]["content"])) == 1, "只有一段 <memory>")
    check(system_msgs[0]["content"].startswith("base"), "system 其余部分保留")

    # ---- 幂等：内容没变时不改写 ----
    before = system_msgs[0]["content"]
    await hook.before_iteration(ctx)
    check(ctx.messages[0]["content"] == before, "重复调用不改写")
    check(len(_MEM_RE.findall(ctx.messages[0]["content"])) == 1, "重复调用仍只有一段")

    # ---- 装配期没有这一段、这一轮有记忆 → 追加进同一条 system ----
    ctx2 = Ctx([{"role": "system", "content": "base"},
                {"role": "user", "content": "hi"}])
    await MemoryIterationHook(_FakeSource("刚写下的记忆")).before_iteration(ctx2)
    check(len([m for m in ctx2.messages if m.get("role") == "system"]) == 1,
          "追加时也不新增 system 消息")
    check("刚写下的记忆" in ctx2.messages[0]["content"], "记忆段追加进 system")

    # ---- 记忆被删空 → 这一段跟着消失 ----
    await MemoryIterationHook(_FakeSource("")).before_iteration(ctx2)
    check("<memory>" not in ctx2.messages[0]["content"], "记忆清空时这一段被移除")

    # ---- 召回词：优先装配期那份，续跑退回最后一条用户消息 ----
    src3 = _FakeSource("x")
    ctx3 = Ctx([{"role": "system", "content": "base"},
                {"role": "user", "content": "用户原话"}],
               extras={"recall_query": "用户原话\n生成配音"})
    await MemoryIterationHook(src3).before_iteration(ctx3)
    check(src3.queries[-1] == "用户原话\n生成配音", f"用装配期召回词（{src3.queries[-1]!r}）")
    ctx4 = Ctx([{"role": "system", "content": "base"},
                {"role": "user", "content": "用户原话"}])
    await MemoryIterationHook(src3).before_iteration(ctx4)
    check(src3.queries[-1] == "用户原话", "续跑没有召回词时退回用户消息")

    # ---- 超预算的刷新直接放弃（不绕过 source 的预算）----
    ctx5 = Ctx([{"role": "system", "content": "base\n\n<memory>\n旧</memory>"},
                {"role": "user", "content": "hi"}])
    await MemoryIterationHook(_FakeSource("超" * 5000)).before_iteration(ctx5)
    check("旧" in ctx5.messages[0]["content"], "超预算的正文不写入")


def _tool_call(call_id: str, name: str, args: dict) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])


class _RecallLLM:
    def __init__(self) -> None:
        self.calls: list[list] = []
        self._i = 0

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        self._i += 1
        if self._i == 1:
            return _tool_call("m1", "update_memory", {
                "category": "user",
                "content": "用户讨厌片尾加字幕",
                "mode": "append",
            })
        return LLMResponse(content="好的")


async def every_llm_call() -> None:
    storage = build_storage("memory")
    await storage.start()
    store = MemoryStore(storage, user_id="u1")
    await store.write("user", "用户喜欢冷色调")
    source = MemoryContextSource(store)
    reg = ToolRegistry()
    reg.register(UpdateMemoryTool(store))
    llm = _RecallLLM()
    runner = AgentOnceRun(
        llm, reg, ContextBuilder("SYS", context_sources=[source]),
        hooks=CompositeHook([MemoryIterationHook(source)]),
        config=AgentConfig(max_iterations=4),
    )
    sess = Session(user_id="u1", conversation_id="mem-e2e")
    answer = await runner.run(sess, "开工")
    check(answer == "好的", f"执行完成（{answer!r}）")
    check(len(llm.calls) == 2, f"两轮 LLM 调用（实际 {len(llm.calls)}）")

    first, second = llm.calls[0], llm.calls[1]
    check("用户喜欢冷色调" in first[0]["content"], "第一次调用带装配期记忆")
    check("用户讨厌片尾加字幕" in second[0]["content"],
          "第二次调用带上了本轮刚写的记忆（每轮刷新）")
    blocks = _MEM_RE.findall(second[0]["content"])
    check(len(blocks) == 1, f"整次执行只有一段 <memory>（实际 {len(blocks)}）")
    check(len(blocks[0].encode("utf-8")) <= 2048,
          f"刷新后的记忆段不超预算（{len(blocks[0].encode('utf-8'))} B）")
    check("用户讨厌片尾加字幕" in await store.read("user"), "记忆真的落库了")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        await file_backend(tmp)
    hook_switch()
    await hook_refresh()
    await every_llm_call()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
