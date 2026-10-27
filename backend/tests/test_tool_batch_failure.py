# -*- coding: utf-8 -*-
"""R6：批内失败不连坐 + 独占工具真串行（真 Agent 走一遍 plan_batches）。

R6-a：一批并发里一个工具抛错，同批其他工具仍拿到结果（不再被 gather 取消）。
R6-b：不可并发（exclusive / 未声明读写集的写工具）独占一批——与同一条回复里的
      其他调用在时间轴上不重叠；两个只读工具则真的并起来跑（对照组，证明不是
      「所有批次都串行」）。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.agent import Agent, AgentConfig  # noqa: E402
from agent_framework.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder  # noqa: E402
from agent_framework.llm import LLMResponse  # noqa: E402
from agent_framework.messages import ToolCall  # noqa: E402
from agent_framework.session import SessionManager  # noqa: E402
from agent_framework.tool import (  # noqa: E402
    Tool,
    ToolError,
    ToolRegistry,
    is_tool_error,
)

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class Boom(Tool):
    name = "boom"
    description = "炸"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kw):
        raise RuntimeError("boom!")


class Fine(Tool):
    name = "fine"
    description = "正常"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kw):
        return "fine-result"


class TimedTool(Tool):
    """记录自己何时开始/结束；用于判断两个调用在时间轴上是否重叠。"""

    def __init__(self, name: str, *, read_only: bool = False,
                 exclusive: bool = False, delay: float = 0.08) -> None:
        self._name = name
        self._ro = read_only
        self._ex = exclusive
        self._delay = delay
        self.starts: list[float] = []
        self.ends: list[float] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"计时工具 {self._name}"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return self._ro

    @property
    def exclusive(self) -> bool:
        return self._ex

    async def execute(self, **kw):
        start = time.monotonic()
        self.starts.append(start)
        try:
            await asyncio.sleep(self._delay)
            return "ok"
        finally:
            self.ends.append(time.monotonic())


class TwoCallLLM:
    """第一次回复里同时给出两个工具调用，第二次给终答。"""

    def __init__(self, names: list[str]) -> None:
        self._names = names
        self.n = 0

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.n += 1
        if self.n == 1:
            return LLMResponse(tool_calls=[
                ToolCall(id=f"t{i}", name=n, arguments={})
                for i, n in enumerate(self._names)
            ])
        return LLMResponse(content="收工")


def make_agent(llm, reg: ToolRegistry) -> Agent:
    return Agent(
        llm=llm,
        registry=reg,
        session_manager=SessionManager(),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=5),
    )


async def exclusive_runs_alone() -> None:
    iso = TimedTool("iso_ex", exclusive=True)
    ro = TimedTool("iso_ro", read_only=True)
    reg = ToolRegistry()
    reg.register(iso)
    reg.register(ro)
    agent = make_agent(TwoCallLLM(["iso_ex", "iso_ro"]), reg)
    answer = await agent.handle("u", "c-ex", "跑两个")

    check(answer == "收工", "真 Agent 跑完整轮")
    check(len(iso.starts) == 1 and len(ro.starts) == 1, "两个工具都执行了一次")
    check(iso.ends and ro.starts and ro.starts[0] >= iso.ends[0] - 0.005,
          f"独占工具与只读工具不重叠（ro 起 {ro.starts[0]:.3f} / ex 止 {iso.ends[0]:.3f}）")

    # 对照组：两个只读工具同批并发（不是「所有批次都被串行化」）
    a = TimedTool("ro_a", read_only=True)
    b = TimedTool("ro_b", read_only=True)
    reg2 = ToolRegistry()
    reg2.register(a)
    reg2.register(b)
    agent2 = make_agent(TwoCallLLM(["ro_a", "ro_b"]), reg2)
    await agent2.handle("u", "c-ro", "跑两个")
    overlap = a.starts[0] < b.ends[0] and b.starts[0] < a.ends[0]
    check(overlap, "两个只读工具真并发（时间轴重叠）")


async def main() -> None:
    reg = ToolRegistry()
    reg.register(Boom())
    reg.register(Fine())

    # ---- 直接复现 agent 里的写法：gather(return_exceptions=True) + 转 ToolError ----
    async def one(name: str):
        return await reg.execute(name, {})

    got = await asyncio.gather(one("boom"), one("fine"), return_exceptions=True)
    check(len(got) == 2, "同批两个都有结果（没被取消）")
    check(not isinstance(got[1], BaseException) and got[1] == "fine-result",
          f"正常工具拿到了结果（{got[1]!r}）")
    check(is_tool_error(got[0]), "抛错的那个被转成 ToolError")

    # ---- 源码断言：agent 用的是 return_exceptions=True ----
    src = (BACKEND / "agent_framework" / "agent.py").read_text(encoding="utf-8")
    check("return_exceptions=True" in src, "agent 的 gather 带 return_exceptions")
    check("isinstance(r, BaseException)" in src, "异常被转成失败回执而不是抛出")

    # 注意：`reg.execute` 内部已经把工具异常兜成 ToolError，所以「不带
    # return_exceptions 就整批失败」在 Registry 这一层复现不出来——真正的取消发生在
    # agent 的 `_exec_one`（钩子/闸函数抛错）。这里只断言那层的写法，不伪造对照。

    # ---- 真 Agent：独占工具不和同批的只读工具重叠 ----
    await exclusive_runs_alone()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
