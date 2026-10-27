# -*- coding: utf-8 -*-
"""R11：三级压缩各有独立阈值 + 工具元信息折叠后仍能按名取全量。"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.compress import ContextCompressor, fold_tool_schemas  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


async def main() -> None:
    # ---- 阈值独立：只超 drop 时不该调摘要 ----
    called = {"n": 0}

    async def summarizer(msgs):
        called["n"] += 1
        return "summary"

    body = [msg("system", "sys")] + [msg("user", "x" * 400) for _ in range(6)]
    comp = ContextCompressor(100, drop_at_tokens=50, summary_at_tokens=100_000,
                             summarizer=summarizer, token_counter=lambda m: len(m["content"]))
    out = await comp(body)
    check(called["n"] == 0, "只超 drop 阈值时不触发摘要（分级的意义）")
    check(len(out) < len(body), "旧消息被丢弃")

    # ---- 超过 summary 阈值才摘要 ----
    comp2 = ContextCompressor(100, drop_at_tokens=50, summary_at_tokens=51,
                              summarizer=summarizer, token_counter=lambda m: len(m["content"]))
    out2 = await comp2(body)
    check(called["n"] == 1, "超过 summary 阈值才摘要")
    check(any("<history_summary>" in str(m.get("content")) for m in out2), "摘要进了上下文")

    # ---- 工具元信息折叠：只压描述，不动参数结构 ----
    defs = [{
        "type": "function",
        "function": {
            "name": f"t{i}",
            "description": "很长的描述" * 40,
            "parameters": {"type": "object",
                           "properties": {"a": {"type": "string",
                                                "description": "很长的参数说明" * 30}}},
        },
    } for i in range(5)]
    folded = fold_tool_schemas(defs, keep={"t0"}, max_bytes=200)
    check(len(folded) == 5, "折叠不删工具")
    check(folded[0]["function"]["description"] == defs[0]["function"]["description"],
          "keep 里的工具保持完整")
    check(len(folded[1]["function"]["description"]) < 200, "其余工具描述被压短")
    check("a" in folded[1]["function"]["parameters"]["properties"],
          "**参数结构不动**（否则模型不知道该传什么）")
    total_before = sum(len(json.dumps(d, ensure_ascii=False)) for d in defs)
    total_after = sum(len(json.dumps(d, ensure_ascii=False)) for d in folded)
    check(total_after < total_before, "折叠确实变小了")

    # ---- 预算内不折叠 ----
    check(fold_tool_schemas(defs, max_bytes=10 ** 6) is defs, "预算内原样返回")

    # ---- 折叠后仍能按名取全量（兜底出口）----
    class T(Tool):
        name = "t1"
        description = "x"
        parameters = {"type": "object", "properties": {"a": {"type": "string"}}}

        async def execute(self, **kw):
            return "ok"

    reg = ToolRegistry()
    reg.register(T())
    full = reg.get_definitions(user_id=None)
    check(full and "properties" in full[0]["function"]["parameters"],
          "get_definitions 仍返回全量 schema")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
