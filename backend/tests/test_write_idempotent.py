# -*- coding: utf-8 -*-
"""写文件是幂等的：同样的路径与内容再写一遍，不能被打回。

真机 2026-10-19 run ``ff8b2d9ed640``：模型把同一份内容第二次写进
``shots/s01.tsx``，撞上「参数与返回结果逐字相同」闸；它没有停下来，而是换名
新建 ``shots/shot01.tsx``，再换 ``final01.tsx``、``v2_01.tsx``——一条 run 留下
四套镜头文件，真正该渲染的那一套反而停在旧版本。

写文件与读接口不同：重复写的代价是**一次本地覆盖**，回报却是「幂等、状态没变」。
拦下它只会把模型推去改文件名（副作用更大）。所以成功写重复放行；真正的打转由
``_stuck_rounds`` 兜底（同样的写连续 6 轮才停）。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_write_idempotent.py``
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

from test_tool_failure_guard import build_agent, chain_violations  # noqa: E402

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


class WriteStub(Tool):
    """``write_file`` 的替身：每枪都成功，返回一句与路径/内容相关的话。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "写文件替身"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object",
                "properties": {"path": {"type": "string"},
                               "content": {"type": "string"}},
                "required": ["path", "content"]}

    async def execute(self, path: str, content: str) -> str:
        self.calls.append((path, len(content)))
        return f"已创建文件 {path}（{len(content)} 字符）"


def _tool_msgs(messages: list[Message]) -> list[str]:
    return [str(m.get("content") or "") for m in messages if m.get("role") == "tool"]


async def main() -> int:
    print("=== 同一份内容写四次：四次都真执行，不换文件名 ===")
    probe = WriteStub()
    code = "const Shot = () => null;\n"
    write = {"path": "shots/s01.tsx", "content": code}
    steps = [("tool", "write_file", write) for _ in range(4)] + [("answer", "完成")]
    agent = build_agent(steps, tools=[probe])
    agent.config.max_iterations = len(steps) + 2
    out = await agent.run(Session(user_id="u", conversation_id="c_write"), "做一条片")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "完成", f"run 正常收尾：{out}")
    check(len(probe.calls) == 4,
          f"四次同样的写都真的落到工具上：{len(probe.calls)} 枪")
    blocked = [t for t in _tool_msgs(last) if "参数已经真跑过" in t]
    check(not blocked, f"没有「重复、不执行」回执：{blocked[:1]}")
    warned = [t for t in _tool_msgs(last) if "不会再执行" in t]
    check(not warned, f"也没有「下一次不会再执行」的假承诺：{warned[:1]}")
    check(chain_violations(last) == [], f"链子收口：{chain_violations(last)}")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
