# -*- coding: utf-8 -*-
"""配额闸的离线验证（不联网）：UsageHook 落库 + QuotaHook 超限拦截。

运行：  PYTHONPATH=. python tests/test_quota_gate.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook, QuotaExceeded, UsageHook, QuotaHook  # noqa: E402
from agent_framework.llm import LLMResponse, ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


class UsageLLM(ScriptedLLM):
    """ScriptedLLM 但在响应里带 usage。"""

    def __init__(self, steps: list[Any], usage: dict[str, int] | None = None) -> None:
        super().__init__(steps)
        self._usage = usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    async def complete(self, messages, tools=None) -> LLMResponse:
        r = await super().complete(messages, tools)
        r.usage = self._usage
        return r


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()

    print("\n=== ① UsageHook 把 token 用量落 token_usage 表 ===")
    usage_hook = UsageHook(storage.db, model="test-model")
    hooks = CompositeHook([usage_hook])
    llm = UsageLLM([("answer", "你好")],
                   usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150})
    runner = AgentOnceRun(llm, ToolRegistry(), ContextBuilder("S"),
                          hooks=hooks, config=AgentConfig(max_iterations=3))
    sess = Session(user_id="u_quota", conversation_id="c1")
    out = await runner.run(sess, "hi", run_id="run-q1")
    check(out == "你好", f"① 正常返回：{out}")
    rows = await storage.db.select("token_usage", where={"user_id": "u_quota"})
    check(len(rows) == 1, f"① token_usage 落了一行：{len(rows)} 行")
    if rows:
        r = rows[0]
        check(r["prompt_tokens"] == 120 and r["completion_tokens"] == 30,
              f"① token 数对得上：pt={r['prompt_tokens']} ct={r['completion_tokens']}")
        check(r["run_id"] == "run-q1", f"① run_id 记对：{r['run_id']}")

    print("\n=== ② QuotaHook 未超限时不拦截 ===")
    quota_hook = QuotaHook(storage.db, limit=10000, window=86400)
    hooks2 = CompositeHook([UsageHook(storage.db), quota_hook])
    llm2 = UsageLLM([("answer", "继续")],
                    usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
    runner2 = AgentOnceRun(llm2, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks2, config=AgentConfig(max_iterations=3))
    out2 = await runner2.run(Session(user_id="u_quota", conversation_id="c2"), "hi", run_id="run-q2")
    check(out2 == "继续", f"② 未超限正常返回：{out2}")

    print("\n=== ③ QuotaHook 超限时抛 QuotaExceeded ===")
    quota_hook3 = QuotaHook(storage.db, limit=400, window=86400)
    hooks3 = CompositeHook([UsageHook(storage.db), quota_hook3])
    llm3 = UsageLLM([("answer", "不该到这")],
                    usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
    runner3 = AgentOnceRun(llm3, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks3, config=AgentConfig(max_iterations=3))
    raised = False
    try:
        await runner3.run(Session(user_id="u_quota", conversation_id="c3"), "hi", run_id="run-q3")
    except QuotaExceeded as e:
        raised = True
        check(e.used >= 400, f"③ 报的已用量 ≥400：{e.used}")
        check(e.limit == 400, f"③ 报的上限=400：{e.limit}")
    check(raised, "③ 超限时抛 QuotaExceeded")

    print("\n=== ④ limit=0 不拦截（默认关闭）===")
    quota_hook4 = QuotaHook(storage.db, limit=0, window=86400)
    hooks4 = CompositeHook([UsageHook(storage.db), quota_hook4])
    llm4 = UsageLLM([("answer", "ok")])
    runner4 = AgentOnceRun(llm4, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks4, config=AgentConfig(max_iterations=3))
    out4 = await runner4.run(Session(user_id="u_quota", conversation_id="c4"), "hi", run_id="run-q4")
    check(out4 == "ok", f"④ limit=0 不拦截：{out4}")

    await storage.close()
    print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
    print(f"用例 {CHECKS} 条", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))