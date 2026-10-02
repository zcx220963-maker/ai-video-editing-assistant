#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线评测入口：加载 golden 基线 → 跑每条用例 → 检查断言 → 出报告。

不联网、不依赖真实 LLM——用 ScriptedLLM 按 golden 里声明的 llm_steps 逐轮返回，
所以结果只钉 prompt 路径的形状（工具调用轮次、最终答复内容），不钉模型质量。
模型质量评测走真机入口（--live，另接真实 LLM + 真实素材）。

用法：
  python scripts/run_eval.py                      # 跑全部 golden 用例
  python scripts/run_eval.py tests/golden/baseline.json   # 指定基线文件
  python scripts/run_eval.py --verbose            # 逐条详细输出

运行：  PYTHONPATH=. python scripts/run_eval.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.context import ContextBuilder, prompt_fingerprint  # noqa: E402
from agent_framework.hooks import CompositeHook, MetricsHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402


class StubTool(Tool):
    """评测用的无副作用替身工具。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"评测替身工具 {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        return f"{self._name}:ok"


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for n in ("list_materials", "load_media", "analyze_scene", "render_video",
              "select_BGM", "group_clips", "generate_script"):
        reg.register(StubTool(n))
    return reg


def check_assertion(assertion: dict, output: str, tool_call_count: int) -> tuple[bool, str]:
    """返回 (是否通过, 失败原因)。"""
    atype = assertion.get("type", "")
    if atype == "equals":
        ok = output == assertion["value"]
        return ok, "" if ok else f"期望「{assertion['value']}」，实得「{output}」"
    if atype == "contains":
        ok = assertion["value"] in output
        return ok, "" if ok else f"输出不含「{assertion['value']}」：{output}"
    if atype == "regex":
        ok = bool(re.search(assertion["value"], output))
        return ok, "" if ok else f"输出不匹配正则「{assertion['value']}」：{output}"
    if atype == "tool_calls":
        mn = assertion.get("min", 0)
        mx = assertion.get("max", float("inf"))
        ok = mn <= tool_call_count <= mx
        return ok, "" if ok else f"工具调用次数 {tool_call_count} 不在 [{mn}, {mx}]"
    return False, f"未知断言类型：{atype}"


async def run_case(case: dict, fingerprint: str) -> dict:
    """跑一条 golden 用例，返回 {name, passed, details, fingerprint}。"""
    steps = [tuple(s) for s in case.get("llm_steps", [])]
    llm = ScriptedLLM(steps)
    reg = build_registry()
    metrics = MetricsHook()
    runner = AgentOnceRun(
        llm, reg, ContextBuilder("BASE"),
        hooks=CompositeHook([metrics]),
        config=AgentConfig(max_iterations=10),
    )
    sess = Session(user_id="eval", conversation_id=f"eval_{case['name']}")
    output = await runner.run(sess, case["input"], run_id=f"eval_{case['name']}")
    tool_call_count = metrics.tool_rounds

    results = []
    all_passed = True
    for a in case.get("assertions", []):
        ok, reason = check_assertion(a, output, tool_call_count)
        results.append({"type": a["type"], "passed": ok, "reason": reason})
        if not ok:
            all_passed = False

    return {
        "name": case["name"],
        "description": case.get("description", ""),
        "passed": all_passed,
        "output": output,
        "tool_calls": tool_call_count,
        "fingerprint": fingerprint,
        "assertions": results,
        "metrics": metrics.snapshot(),
    }


async def main() -> int:
    verbose = "--verbose" in sys.argv
    golden_files = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
    if not golden_files:
        golden_files = [Path(__file__).resolve().parent.parent / "tests" / "golden" / "baseline.json"]

    fingerprint = prompt_fingerprint("BASE")
    all_results = []
    for gf in golden_files:
        cases = json.loads(gf.read_text(encoding="utf-8"))
        for case in cases:
            r = await run_case(case, fingerprint)
            all_results.append(r)

    passed = sum(1 for r in all_results if r["passed"])
    total = len(all_results)
    print(f"\n{'=' * 60}")
    print(f"评测报告：{passed}/{total} 通过 | prompt 指纹 {fingerprint}")
    print(f"{'=' * 60}")
    for r in all_results:
        status = "✓" if r["passed"] else "✗"
        print(f"  {status} {r['name']}: {r['description']}")
        if verbose or not r["passed"]:
            print(f"      输出: {r['output']}")
            print(f"      工具调用: {r['tool_calls']} 次")
            for a in r["assertions"]:
                if not a["passed"]:
                    print(f"      ✗ {a['type']}: {a['reason']}")
            if verbose:
                m = r["metrics"]
                print(f"      指标: 迭代 {m['iterations']} / 工具轮 {m['tool_rounds']} / token {m['total_tokens']}")
    print(f"\n{'PASSED' if passed == total else 'FAILED'}：{passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))