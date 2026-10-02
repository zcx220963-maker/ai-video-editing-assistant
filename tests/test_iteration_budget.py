# -*- coding: utf-8 -*-
"""「继续」必须能真的继续——撞上限续跑要**再给一轮**，不能一进循环就退出。

真机事故（用户原话「[已达最大迭代次数 40，提前结束]…还是不行啊」）：
撞上限那条 run 的 ``iteration`` 等于 ``max_iterations``，用户点「继续」时从
``cp.iteration`` 续跑，而循环条件写的是 ``while r.iteration < max_iterations``
→ **一进循环就为假**，一个字都没干又打印一次同样的话。
用户看到的就是同一句提示反复出现、"继续"永远无效。

这条用例钉住：续跑点已经用光预算时，有效上限要**往后延**，循环真的能跑起来。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class Counter(Tool):
    """记一次调用，供断言「续跑真的干了活」。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "noop"

    @property
    def description(self) -> str:
        return "空工具"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kw):
        self.calls += 1
        return "ok"


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()
    try:
        tool = Counter()
        reg = ToolRegistry()
        reg.register(tool)          # type: ignore[arg-type]
        mgr = CheckpointManager(storage)
        # max_iterations=2：第一轮跑两个工具轮就用光预算。
        # 脚本要留**续跑之后**用的响应：noop, noop（第一轮用掉）→ noop, answer（续跑用）
        llm = ScriptedLLM([("tool", "noop", {}), ("tool", "noop", {}),
                           ("tool", "noop", {}), ("answer", "继续之后干的活")])
        runner = AgentOnceRun(
            llm, reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=2), checkpoint=mgr, storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_cont")
        out1 = await runner.run(sess, "做点事", run_id="run-cont")
        print(f"    第一次收尾：{out1[:60]!r}")
        cp = await mgr.load("run-cont")
        it = getattr(cp, "iteration", 0)
        print(f"    撞上限后 iteration={it} status={getattr(cp, 'status', '?')}")
        check(it >= 2, f"第一轮确实用光了预算（iteration={it}）")

        calls_before = tool.calls
        # 用户点「继续」→ 从断点续跑
        out2 = await runner.resume(cp, sess)
        print(f"    继续之后：{out2[:60]!r}  （工具调用 {calls_before}→{tool.calls}）")
        check("已达最大迭代次数" not in out2,
              "「继续」不再立刻打印「已达最大迭代次数」")
        check(tool.calls > calls_before,
              f"「继续」真的干活了（工具再被调用 {tool.calls - calls_before} 次）")
        check("继续之后干的活" in out2 or tool.calls > calls_before,
              "续跑推进到了新的工作，而不是原地不动")

        print()
        print("全部通过" if not _fails else f"有 {_fails} 项未通过")
        print(f"用例 {_checks} 条")
        return 0 if not _fails else 1
    finally:
        await storage.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
