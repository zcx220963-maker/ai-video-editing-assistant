# -*- coding: utf-8 -*-
"""R12：记忆文件后端——User.md/Tool.md 落盘、全量覆盖、CAS、每轮注入幂等。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
from dataclasses import dataclass, field

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.memory import MemoryIterationHook  # noqa: E402
from agent_framework.memory_file import FileMemoryBackend  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


@dataclass
class Ctx:
    messages: list = field(default_factory=list)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        be = FileMemoryBackend(tmp)

        # ---- 文件名固定（设计点名 User.md / Tool.md）----
        await be.write("u1", "user", "喜欢快节奏")
        await be.write("u1", "tool", "导出优先 mp4")
        check((pathlib.Path(tmp) / "u1" / "User.md").exists(), "落成 User.md")
        check((pathlib.Path(tmp) / "u1" / "Tool.md").exists(), "落成 Tool.md")
        check(await be.read("u1", "user") == "喜欢快节奏", "读得回")

        # ---- 全量覆盖写 ----
        await be.write("u1", "user", "只留这一句")
        check(await be.read("u1", "user") == "只留这一句", "write 是全量覆盖")

        # ---- append ----
        merged = await be.append("u1", "user", "追加一句")
        check("只留这一句" in merged and "追加一句" in merged, "append 保留旧内容")

        # ---- CAS ----
        before = await be.read("u1", "user")
        check(await be.replace_if("u1", "user", before, "改过了") is True, "CAS 命中时写入")
        check(await be.replace_if("u1", "user", before, "又改") is False, "内容变了 CAS 失败")

        # ---- entries / stamps ----
        entries = await be.entries("u1")
        check(set(entries) == {"user", "tool"}, f"entries 列两分类（{sorted(entries)}）")
        stamps = await be.stamps("u1")
        check(set(stamps) == {"user", "tool"}, "stamps 给两分类的时间戳")

        # ---- 用户隔离 ----
        check(await be.read("u2", "user") == "", "别人的记忆读不到")

        # ---- 每轮注入：幂等（不堆 system 消息）----
        hook = MemoryIterationHook(_FakeStore())
        ctx = Ctx([{"role": "system", "content": "base"}, {"role": "user", "content": "hi"}])
        await hook.before_iteration(ctx)
        await hook.before_iteration(ctx)
        marks = [m for m in ctx.messages
                 if m.get("role") == "system" and MemoryIterationHook.MARKER in m["content"]]
        check(len(marks) == 1, f"重复调用只留一条记忆段（实际 {len(marks)}）")
        check(ctx.messages[1] is marks[0], "记忆段插在 system 之后")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


class _FakeStore:
    async def read(self, category: str) -> str:
        return f"{category} 的记忆"


if __name__ == "__main__":
    asyncio.run(main())
