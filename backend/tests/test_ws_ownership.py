# -*- coding: utf-8 -*-
"""R3：WS 归属校验——is_owned 三态 + WS 用 4403 关连接。

`is_owned` 只依赖 `self.get()`，所以这里用子类覆盖 `get` 造三态，不碰数据库。
"""

from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.storage.repositories import ConversationsRepo  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class StubRepo(ConversationsRepo):
    """只实现 get：None=未建行，{user_id}=已有行。"""

    def __init__(self, rows: dict):
        self._rows = rows          # 不走 _Repo.__init__（这里不需要 db）

    async def get(self, conv_id):
        return self._rows.get(conv_id)


def main() -> None:
    repo = StubRepo({"c1": {"id": "c1", "user_id": "u1"}})

    # ---- 三态 ----
    check(await_is(repo.is_owned("u1", "c1")) is True, "本人会话 → True")
    check(await_is(repo.is_owned("u2", "c1")) is False, "别人会话 → False")
    check(await_is(repo.is_owned("u2", "cX")) is None, "未建行 → None（不是 False）")

    # ---- claim 仍然抛错（写入侧口径）----
    try:
        import asyncio
        asyncio.get_event_loop().run_until_complete(repo.claim("u2", "c1"))
        check(False, "claim 对别人的会话抛错")
    except Exception:
        check(True, "claim 对别人的会话抛错")

    # ---- WS 端点：先 accept 再 is_owned 再 close(4403) ----
    src = (BACKEND / "agent_framework" / "server.py").read_text(encoding="utf-8")
    check("is_owned(" in src, "WS 端点调用 is_owned")
    check("4403" in src, "不匹配时 close(4403)")
    i_accept = src.index('await websocket.accept()', src.index("ws_endpoint"))
    i_owned = src.index("is_owned(", i_accept)
    check(i_accept < i_owned,
          "先 accept 再判归属（握手期 close 只会变成 HTTP 403，4403 传不到浏览器）")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


def await_is(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    main()
