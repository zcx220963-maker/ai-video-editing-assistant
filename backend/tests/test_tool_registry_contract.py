# -*- coding: utf-8 -*-
"""R5：Registry 九方法、enum 校验、错误提示只一次、可见性同口径。"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.tool import (  # noqa: E402
    _HINT, Tool, ToolError, ToolRegistry, is_tool_error, validate_params, with_hint)

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class DemoTool(Tool):
    name = "demo"
    description = "demo"
    parameters = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["fast", "slow"]},
            "n": {"type": "integer"},
        },
        "required": ["mode"],
    }

    async def execute(self, **kwargs):
        return "ok"


class PersonalTool(DemoTool):
    name = "mine"
    owner_user_id = "u1"


async def main() -> None:
    reg = ToolRegistry()
    reg.register(DemoTool())

    # ---- 九方法 ----
    for m in ("register", "unregister", "get", "has", "get_definitions",
              "execute", "tool_names", "__len__", "__contains__"):
        check(hasattr(reg, m), f"Registry 缺方法 {m}")

    # ---- enum 校验 ----
    r = await reg.execute("demo", {"mode": "turbo"})
    check(is_tool_error(r) and "只能是" in str(r), f"enum 非法值被拒：{r}")
    r2 = await reg.execute("demo", {"mode": "fast"})
    check(not is_tool_error(r2), "合法 enum 通过")

    # ---- 未知键 ----
    r3 = await reg.execute("demo", {"mode": "fast", "modee": 1})
    check(is_tool_error(r3) and "多余的键" in str(r3), f"未知键被拒：{r3}")

    # ---- 提示只出现一次 ----
    r4 = await reg.execute("demo", {"mode": "turbo"})
    text = str(r4)
    check(text.count(_HINT) == 1, f"错误提示只追加一次（实际 {text.count(_HINT)} 次）")
    check(with_hint(with_hint("x")) == with_hint("x"), "with_hint 幂等")

    # ---- 可见性同口径 ----
    reg.register(PersonalTool())
    check(reg.has("mine") is False, "has 默认看不到别人的个人工具")
    check(reg.has("mine", user_id="u1") is True, "has(user_id) 能看到自己的")
    check(("mine" in reg) is False, "__contains__ 与 has 同口径")
    check(reg.get("mine", user_id="u2") is None, "get 也按 user 过滤")

    # ---- 未知工具与参数错误都算失败，但类型不同 ----
    check(is_tool_error(await reg.execute("nope", {})), "未知工具返回 ToolError")
    check(validate_params(DemoTool(), {"mode": "fast"}) is None, "validate_params 放行合法参数")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
