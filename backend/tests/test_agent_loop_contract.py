# -*- coding: utf-8 -*-
"""R4：外层循环有名字 + 迭代上限 + 一次 run 只驱动一次 + 同会话串行（真 Agent）。

R4-a：一轮 run 里跑了几次迭代（工具 → 答复），`_drive` 都只被调用一次——
       外层不是「每次迭代新开一个 drive」。
R4-c：同一会话两个并发 handle 真串行（LLM 侧观测到的最大并发 = 1），
       不同会话可并行（= 2，证明锁是 per-session 而不是全局）。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.agent import (  # noqa: E402
    Agent,
    AgentConfig,
    AgentOnceRun,
)
from agent_framework.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder  # noqa: E402
from agent_framework.llm import LLMResponse  # noqa: E402
from agent_framework.messages import ToolCall  # noqa: E402
from agent_framework.session import SessionManager  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402
from agent_framework.worker import AgentWorker, StopReason  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class NoopTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "什么都不做，只记一次调用"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> str:
        self.calls += 1
        return "ok"


class CycleLLM:
    """奇数次调用要工具、偶数次调用给答复：每轮 run 都是两次迭代。"""

    def __init__(self) -> None:
        self.n = 0
        self.calls: list[list] = []

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append(messages)
        self.n += 1
        if self.n % 2 == 1:
            return LLMResponse(tool_calls=[ToolCall(id=f"c{self.n}", name="noop",
                                                    arguments={})])
        return LLMResponse(content="收工")


class SlowLLM:
    """睡一小会儿再答复；记录同时在跑的调用数（串行判据）。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.06)
            return LLMResponse(content="答")
        finally:
            self.active -= 1


def make_agent(llm, registry: ToolRegistry) -> Agent:
    return Agent(
        llm=llm,
        registry=registry,
        session_manager=SessionManager(),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=5),
    )


async def real_drive_once() -> None:
    tool = NoopTool()
    reg = ToolRegistry()
    reg.register(tool)
    llm = CycleLLM()
    agent = make_agent(llm, reg)

    orig = AgentOnceRun._drive
    count = [0]

    async def counted(self, *args, **kwargs):
        count[0] += 1
        return await orig(self, *args, **kwargs)

    AgentOnceRun._drive = counted
    try:
        a1 = await agent.handle("u", "c1", "第一次")
        a2 = await agent.handle("u", "c2", "第二次")
    finally:
        AgentOnceRun._drive = orig

    check(a1 == "收工" and a2 == "收工", "两轮 run 都正常交付")
    check(len(llm.calls) == 4, f"两轮各两次迭代（LLM 共 4 次，实际 {len(llm.calls)}）")
    check(tool.calls == 2, f"两轮各执行一次工具（实际 {tool.calls}）")
    check(count[0] == 2, f"两轮 run = 两次 _drive（实际 {count[0]}）")


async def same_session_serial() -> None:
    tool = NoopTool()
    reg = ToolRegistry()
    reg.register(tool)
    llm = SlowLLM()
    agent = make_agent(llm, reg)

    # 同一会话：两路并发 → 必须串行
    await asyncio.gather(
        agent.handle("u", "serial", "A"),
        agent.handle("u", "serial", "B"),
    )
    check(llm.calls == 2, f"同会话两条消息都被处理（实际 {llm.calls}）")
    check(llm.max_active == 1, f"同会话串行：LLM 最大并发 1（实际 {llm.max_active}）")

    # 不同会话：锁是 per-session，不是全局
    llm2 = SlowLLM()
    agent2 = make_agent(llm2, ToolRegistry())
    await asyncio.gather(
        agent2.handle("u", "s-a", "A"),
        agent2.handle("u", "s-b", "B"),
    )
    check(llm2.max_active == 2, f"异会话并行：LLM 最大并发 2（实际 {llm2.max_active}）")


def main() -> None:
    # ---- 迭代上限：默认 200（与运行期一致）----
    check(AgentConfig().max_iterations == 200,
          f"max_iterations 默认 200（实际 {AgentConfig().max_iterations}）")
    check(AgentConfig(max_iterations=3).max_iterations == 3, "可配")

    # ---- 停止原因枚举化 ----
    for reason in ("SHUTDOWN", "ERROR", "BROKER_DOWN", "CANCELLED"):
        check(hasattr(StopReason, reason), f"StopReason 有 {reason}")

    # ---- AgentWorker：订阅 + 生命周期 ----
    class FakeMQ:
        def __init__(self) -> None:
            self.subs = []
            self.started = 0

        def subscribe(self, topic, group, handler):
            self.subs.append((topic, group, handler))

        async def start(self):
            self.started += 1

        async def stop(self):
            pass

    mq = FakeMQ()
    worker = AgentWorker(mq, handler=lambda p: None, topic="chat", group="agent")
    worker.subscribe()
    check(mq.subs and mq.subs[0][0] == "chat", "subscribe 注册到 chat")

    # ---- broker 起不来 → BROKER_DOWN + 可动作的错误 ----
    class DeadMQ(FakeMQ):
        async def start(self):
            raise ConnectionError("no broker")

    dead = AgentWorker(DeadMQ(), handler=lambda p: None)
    try:
        asyncio.run(dead.start())
        check(False, "broker 不可达应抛错")
    except RuntimeError as exc:
        check(dead.stop_reason is StopReason.BROKER_DOWN, "停止原因记为 BROKER_DOWN")
        check("KAFKA_BOOTSTRAP_SERVERS" in str(exc) and "--mq memory" in str(exc),
              "错误信息可动作（给出两个排查方向）")

    check("AgentWorker" in (BACKEND / "agent_framework" / "server.py").read_text(
        encoding="utf-8"), "server 用 AgentWorker 订阅（外层循环有名字）")

    # ---- 真 Agent 上的两条判据 ----
    asyncio.run(real_drive_once())
    asyncio.run(same_session_serial())

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
