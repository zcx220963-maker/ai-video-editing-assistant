# -*- coding: utf-8 -*-
"""R6：一批并发里一个工具抛错，同批其他工具仍拿到结果（不再被 gather 取消）。"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.tool import Tool, ToolError, ToolRegistry, is_tool_error  # noqa: E402

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

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
