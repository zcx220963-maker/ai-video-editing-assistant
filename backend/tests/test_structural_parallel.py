"""决定性验证：模型把 asr / split_shots 分成两轮时，执行器会不会自己并批。

构造一个**只单独调用 asr** 的模型（真机就是这种写法），断言：
  · split_shots 被自动补进同一批（与 asr 同批 gather）→ 并行；
  · 且补进来的调用**不会**往消息链里写 tool 消息（否则 API 400）。

判据看的是「实际并发」而不是「提示词有没有写」。
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

sys.path.insert(0, ".")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.editing_contract import EditingContract, NodeContract  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import LLMResponse, ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402

FAIL: list[str] = []
TRACE: list[tuple[str, float, float]] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAIL.append(label)


class NodeTool(Tool):
    """最简剪辑节点替身：读写集按契约声明，执行时睡一会儿以便观察并发。"""

    def __init__(self, name: str, requires: tuple[str, ...], sleep: float) -> None:
        self._name = name
        self._requires = requires
        self._sleep = sleep

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name}（需先完成：{', '.join(self._requires) or '无'}）"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    @property
    def reads(self) -> frozenset[str]:
        return frozenset(f"store:{r}" for r in self._requires)

    @property
    def writes(self) -> frozenset[str]:
        return frozenset({f"store:{self._name}"})

    async def execute(self, **kw: Any) -> str:
        t0 = time.monotonic()
        await asyncio.sleep(self._sleep)
        TRACE.append((self._name, t0, time.monotonic()))
        return f"{self._name}-ok"


class Plan:
    """假计划门：只提供契约。"""

    def __init__(self, contract: EditingContract) -> None:
        self.contract = contract


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()
    try:
        contract = EditingContract(nodes={
            "load_media": NodeContract("load_media"),
            "asr": NodeContract("asr", requires=("load_media",)),
            "split_shots": NodeContract("split_shots", requires=("load_media",)),
            "understand_clips": NodeContract("understand_clips",
                                             requires=("split_shots",)),
        })
        reg = ToolRegistry()
        reg.register(NodeTool("load_media", (), 0.0))
        reg.register(NodeTool("asr", ("load_media",), 1.2))
        reg.register(NodeTool("split_shots", ("load_media",), 1.2))
        reg.register(NodeTool("understand_clips", ("split_shots",), 0.2))

        # 模型**分开写**：先只调 asr，再只调 understand_clips
        # （真机就是先 asr、拿到结果才 split_shots；这里用 understand_clips 收尾）
        llm = ScriptedLLM([
            ("tool", "load_media", {}),
            ("tool", "asr", {}),
            ("tool", "understand_clips", {}),
            ("answer", "完成"),
        ])
        runner = AgentOnceRun(
            llm, reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=8),
            checkpoint=CheckpointManager(storage), storage=storage,
        )
        runner.plan_gate = Plan(contract)          # type: ignore[attr-defined]

        # 让产物表知道 load_media 已完成（真实路径里由 Storyline 写入）
        class _Repo:
            async def executed(self) -> list[str]:
                return ["load_media"]

            async def snapshot(self) -> dict[str, Any]:
                return {}

        orig_artifacts = storage.artifacts
        storage.artifacts = lambda *a, **k: _Repo()   # type: ignore[assignment]

        # 计划节点（execute_plan 会填；这里直接给）
        runner._exec_plan_nodes = ["load_media", "asr", "split_shots",
                                   "understand_clips"]  # type: ignore[attr-defined]

        sess = Session(user_id="u", conversation_id="c_par")
        out = await runner.run(sess, "剪一条", run_id="run-par")
        storage.artifacts = orig_artifacts            # type: ignore[assignment]

        print("执行时间线：")
        for name, t0, t1 in TRACE:
            print(f"    {name:<18} {t0:.2f} -> {t1:.2f}  （{t1 - t0:.2f}s）")

        print()
        print("① 只单独调了 asr，split_shots 被自动补进同一批吗")
        names = [n for n, _, _ in TRACE]
        check("asr" in names, "asr 执行了")
        check("split_shots" in names,
              "split_shots **被自动补进来了**（模型没调它，执行器按图补的）")

        print("\n② 它们真的并发了吗（看时间窗是否重叠）")
        d = {n: (t0, t1) for n, t0, t1 in TRACE}
        if "asr" in d and "split_shots" in d:
            (a0, a1), (s0, s1) = d["asr"], d["split_shots"]
            overlap = min(a1, s1) - max(a0, s0)
            check(overlap > 0.3,
                  f"两个调用时间窗重叠 {overlap:.2f}s（真并发；串行会是负数）")
            span = max(a1, s1) - min(a0, s0)
            check(span < 2.0,
                  f"总耗时 {span:.2f}s < 2.0s（各 1.2s：并发≈1.2s，串行≈2.4s）")
        else:
            check(False, "缺 one of asr / split_shots，无法判并发")

        print("\n③ 补进来的调用不回填消息链（否则 API 400）")
        cp = await CheckpointManager(storage).load("run-par")
        msgs = list(getattr(cp, "messages", None) or [])
        tool_ids = [m.get("tool_call_id") for m in msgs
                    if isinstance(m, dict) and m.get("role") == "tool"]
        check(not any(str(i).startswith("auto-") for i in tool_ids),
              f"消息链里没有 auto- 的 tool 回执：{tool_ids}")

        print()
        print("全部通过" if not FAIL else f"有 {len(FAIL)} 项未通过")
        return 0 if not FAIL else 1
    finally:
        await storage.close()


if __name__ == "__main__":
    # 入口必须放在 __main__ 里：pytest 的 Collector 会 **import** 这份脚本
    # （见 pytest.ini 与 tests/conftest.py），顶层 sys.exit 会在收集阶段就炸掉整轮。
    raise SystemExit(asyncio.run(main()))
