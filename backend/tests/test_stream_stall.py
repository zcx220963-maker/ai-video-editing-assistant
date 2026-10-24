# -*- coding: utf-8 -*-
"""流式调用「连接开着、内容不来」的兜底：每块都有上限，别让 run 无限期挂着。

真机 2026-10-21 run ``270be6a9866b``：第 7 轮工具回执落库之后，应用进程 CPU 0.15%、
状态 ``S (sleeping)``，21 分钟没有下一条记录——界面上停在「分镜」，用户以为在
写分镜，其实在等一个永远不会吐字的流。SDK 的 60 秒读超时按「有字节」算，供应商
心跳一直发，它就一直不响。

这条用例钉两件事：

① **一个 delta 都没吐就卡住** → 判可重发，一共打 3 枪（1 + ``_STREAM_RETRIES``）；
② **已经吐过字再卡住** → 不重发（否则界面上同一段回答出现两遍），直接报错。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_stream_stall.py``
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import agent as agent_mod  # noqa: E402
from agent_framework.llm import LLMResponse, ScriptedLLM, StreamChunk  # noqa: E402
from test_tool_failure_guard import build_agent  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class StallLLM(ScriptedLLM):
    """流式接口的替身：按脚本给一段文本（或什么都不给），然后**永远不再吐**。

    ``emit`` 决定第一枪吐不吐字——这正是重发规则的分界线。
    """

    def __init__(self, emit: str | None) -> None:
        super().__init__([("answer", "")])
        self.emit = emit
        self.stream_calls = 0

    async def complete_stream(self, messages: list[Any], tools: list[Any] | None = None):
        # 与真实现同形：**异步生成器函数**（调用即得可迭代对象，不是协程）。
        self.stream_calls += 1
        self.calls.append(messages)
        if self.emit:
            yield StreamChunk(delta=self.emit)
        await asyncio.sleep(3600)          # 连接还在，内容不来
        yield StreamChunk(delta="永远不会到这里")


async def main() -> None:
    # 把 120 秒的窗口缩到 0.15 秒：判据是「有没有上限」，不是这个数。
    agent_mod._STREAM_STALL_SEC = 0.15
    agent_mod._STREAM_RETRY_SLEEP = 0.05   # 重发间隔同样缩：判据是「有上限」

    print("\n=== ① 一个 delta 都没吐：判可重发，打满 3 枪 ===")
    llm = StallLLM(emit=None)
    agent = build_agent([], tools=[])
    agent.llm = llm
    t0 = time.monotonic()
    err = None
    try:
        await agent.run(Session(user_id="u", conversation_id="c_stall_a"), "来",
                         stream=True)
    except Exception as exc:  # noqa: BLE001 —— 这里就是要它抛出来
        err = exc
    took = time.monotonic() - t0
    check(err is not None, f"① 卡死的流最终报错，而不是一直挂着（{type(err).__name__}）")
    check(llm.stream_calls == 1 + agent_mod._STREAM_RETRIES,
          f"① 一共打了 1+{agent_mod._STREAM_RETRIES} 枪（实得 {llm.stream_calls}）")
    check(took < 5.0, f"① 总耗时在窗口的几倍之内（{took:.2f}s）")

    print("\n=== ② 已经吐过字再卡住：不重发，直接报错 ===")
    llm2 = StallLLM(emit="半句")
    agent2 = build_agent([], tools=[])
    agent2.llm = llm2
    err2 = None
    try:
        await agent2.run(Session(user_id="u", conversation_id="c_stall_b"), "来",
                          stream=True)
    except Exception as exc:  # noqa: BLE001
        err2 = exc
    check(err2 is not None, f"② 同样是报错收尾（{type(err2).__name__}）")
    check(llm2.stream_calls == 1,
          f"② 吐过字就不重发（实得 {llm2.stream_calls} 枪）")
    check(isinstance(err2, TimeoutError),
          f"② 错因是超时，不是别的（实得 {type(err2).__name__}）")

    print(f"\n{'全部通过' if not _fails else f'{_fails} 项失败'}")
    print(f"用例 {_checks} 条")
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    from agent_framework.session import Session
    asyncio.run(main())
