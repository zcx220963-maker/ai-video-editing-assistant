# -*- coding: utf-8 -*-
"""预览是自查，不是交付：连续预览到硬线就不再渲静帧。

真机 2026-10-19 菊花镜（run ``32cd75d71fee``）：一镜 10 秒，模型写了 13 版代码、
打了 10 枪 ``preview_shot``，每一次参数都不同——`_stuck_rounds` 按「同一份调用」判
打转，结构上接不到它；`preview_shot` 回执末尾的 ``next`` 模型看见了也不照做。
整条 run 停在「执行中…」的观感上，用户原话「又怎么了，你到底行不行」。

这里钉三件事：

① 软线（``_PREVIEW_SOFT``）只提醒一句，不封路；
② 硬线（``_PREVIEW_HARD``）起 ``preview_shot`` 不再执行，回执给出路（交给片子的下一步）；
③ 交付调用（``submit_plan``）把计数清零——下一镜/下一轮重新拥有完整的预览额度。
   零素材与剪辑两条线的「交出去」都发生在 ``submit_plan`` 之后，所以清零只认它。

跑法：``cd backend && python tests/test_preview_delivery_gate.py``
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from test_tool_failure_guard import (  # noqa: E402
    build_agent, chain_violations)

from agent_framework.agent import (  # noqa: E402
    _PREVIEW_HARD, _PREVIEW_SOFT)
from agent_framework.messages import Message  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.tool import Tool  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class OkStub(Tool):
    """任意工具名的替身：每枪都成功，记下真执行的次数。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} 替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object",
                "properties": {"v": {"type": "string"}},
                "required": []}

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return f"{self._name} 完成 {kwargs.get('v', '')}"


def _tool_msgs(messages: list[Message]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


def _system_msgs(messages: list[Message]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "system"]


async def case_a_hard_line() -> int:
    print(f"=== ① 预览 {_PREVIEW_HARD} 枪之后不再渲；软线 {_PREVIEW_SOFT} 枪只说一句 ===")
    n = _PREVIEW_HARD + 4
    probe = OkStub("preview_shot")
    steps = [("tool", "preview_shot", {"v": f"v{i}"}) for i in range(n)]
    agent = build_agent(steps + [("answer", "交给 reshoot_shot")], tools=[probe])
    agent.config.max_iterations = n + 3
    await agent.run(Session(user_id="u", conversation_id="c_prev_a"), "做一镜")
    last = agent.llm.calls[-1]

    check(len(probe.calls) == _PREVIEW_HARD,
          f"① 真渲的静帧停在硬线：{len(probe.calls)}/{n}")
    receipts = _tool_msgs(last)
    refused = [t for t in receipts if "这次不再渲静帧" in t]
    check(len(refused) == n - _PREVIEW_HARD,
          f"① 超出的每一枪都收到拒绝回执：{len(refused)}/{n - _PREVIEW_HARD}")
    check(all("reshoot_shot" in t and "render_short_film" in t for t in refused),
          "① 拒绝回执给出路：零素材线用 reshoot_shot 改一镜 / render_short_film 重渲")
    notes = [t for t in _system_msgs(last) if "已经连续预览了" in t]
    check(len(notes) == 1,
          f"① 软线只说**一句** system 提醒：{len(notes)} 条")
    check(bool(notes) and f"{_PREVIEW_SOFT} 次" in notes[0],
          f"① 那句话说的是越过软线那一刻的真数（第 {_PREVIEW_SOFT} 枪）")
    check(bool(notes) and f"{_PREVIEW_HARD} 次，预览通道会先关上" in notes[0],
          "① 连硬线一起写明（模型知道还能走多远）")
    check(chain_violations(last) == [],
          f"① 链子收口：{chain_violations(last)}")
    return 0


async def case_b_delivery_resets() -> int:
    print("\n=== ② 交付调用清零：交一镜之后重新拥有完整额度 ===")
    probe = OkStub("preview_shot")
    plan = OkStub("submit_plan")
    steps = ([("tool", "preview_shot", {"v": f"a{i}"}) for i in range(_PREVIEW_SOFT + 1)]
             + [("tool", "submit_plan", {"v": "s01"})]
             + [("tool", "preview_shot", {"v": f"b{i}"}) for i in range(_PREVIEW_SOFT + 1)]
             + [("answer", "完成")])
    agent = build_agent(steps, tools=[probe, plan])
    agent.config.max_iterations = len(steps) + 3
    await agent.run(Session(user_id="u", conversation_id="c_prev_b"), "做两镜")
    last = agent.llm.calls[-1]

    check(len(plan.calls) == 1, f"② 交付调用真执行：{len(plan.calls)}")
    check(len(probe.calls) == 2 * (_PREVIEW_SOFT + 1),
          f"② 交付之后重新计数，两段预览都真渲：{len(probe.calls)}")
    notes = [t for t in _system_msgs(last) if "已经连续预览了" in t]
    check(len(notes) == 2,
          f"② 每段各自越过软线各说一句：{len(notes)} 条")
    check(chain_violations(last) == [],
          f"② 链子收口：{chain_violations(last)}")
    return 0


async def main() -> int:
    await case_a_hard_line()
    await case_b_delivery_resets()
    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
