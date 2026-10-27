# -*- coding: utf-8 -*-
"""R19：文件收件箱——jsonl 落盘、已读即删、peek 不清、dedup 幂等。"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.team_files import FileInbox  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        inbox = FileInbox(tmp)

        # ---- send 落 jsonl ----
        await inbox.send("u1", "c1", "writer", {"content": "hello"}, sender="main")
        path = pathlib.Path(tmp) / "u1" / "c1" / "writer.jsonl"
        check(path.exists(), "收件箱落到 {user}/{conv}/{agent}.jsonl")
        check(len(path.read_text(encoding="utf-8").strip().splitlines()) == 1, "一行一条消息")

        # ---- peek 不清 ----
        got = await inbox.peek("u1", "c1", "writer")
        check(len(got) == 1 and got[0]["content"]["content"] == "hello", "peek 读到正文")
        check(len(await inbox.peek("u1", "c1", "writer")) == 1, "peek 不消费")

        # ---- read 即删 ----
        rows = await inbox.read("u1", "c1", "writer")
        check(len(rows) == 1, "read 拿到消息")
        check(rows[0]["id"], "消息带 id（消费方可去重）")
        check(await inbox.peek("u1", "c1", "writer") == [], "read 之后收件箱清空")

        # ---- dedup_key 幂等 ----
        await inbox.send("u1", "c1", "writer", {"content": "a"}, dedup_key="k1")
        again = await inbox.send("u1", "c1", "writer", {"content": "a"}, dedup_key="k1")
        check(again.get("_deduplicated") is True, "同键未消费消息不重复投递")
        check(len(await inbox.peek("u1", "c1", "writer")) == 1, "只留一条")

        # ---- agents 列名单 ----
        await inbox.send("u1", "c1", "editor", {"content": "x"})
        check(await inbox.agents("u1", "c1") == ["editor", "writer"], "agents 列全部收件箱")

        # ---- 作用域隔离 ----
        check(await inbox.peek("u2", "c1", "writer") == [], "别的用户看不到")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
