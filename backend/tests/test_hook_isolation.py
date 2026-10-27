# -*- coding: utf-8 -*-
"""R9：Hook 生命周期五方法 + finalize_content 异常隔离（单个 Hook 抛错不中断收尾链）。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from dataclasses import dataclass, field

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.hooks import AgentHook, CompositeHook  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class Recorder(AgentHook):
    def __init__(self, tag: str, *, boom: bool = False) -> None:
        self.tag = tag
        self.boom = boom
        self.calls: list[str] = []

    async def before_iteration(self, context):
        self.calls.append("before_iteration")

    async def after_execute_tools(self, context):
        self.calls.append("after_execute_tools")

    def finalize_content(self, context, content):
        if self.boom:
            raise RuntimeError(f"{self.tag} boom")
        return f"{content}|{self.tag}"


@dataclass
class Ctx:
    messages: list = field(default_factory=list)
    iteration: int = 0


async def main() -> None:
    a, b, c = Recorder("a"), Recorder("b", boom=True), Recorder("c")
    comp = CompositeHook([a, b, c])

    # ---- 五方法都在 ----
    for m in ("before_iteration", "on_stream", "on_stream_end",
              "after_execute_tools", "finalize_content"):
        check(hasattr(comp, m), f"CompositeHook 缺 {m}")

    # ---- async 节点：单个抛错不影响其他 ----
    await comp.before_iteration(Ctx())
    check(a.calls == ["before_iteration"] and c.calls == ["before_iteration"],
          "before_iteration 三个都执行")

    # ---- finalize_content：中间那个抛错，后面的仍然执行 ----
    out = comp.finalize_content(Ctx(), "base")
    check(out == "base|a|c", f"收尾链没被 b 打断（实际 {out!r}）")
    check(comp._finalize_failures == 1, "失败被计数（不静默吞掉）")

    # ---- 全部正常时按顺序串接 ----
    comp2 = CompositeHook([Recorder("x"), Recorder("y")])
    check(comp2.finalize_content(Ctx(), "v") == "v|x|y", "正常时按注册顺序串接")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
