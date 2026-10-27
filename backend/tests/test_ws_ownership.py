# -*- coding: utf-8 -*-
"""R3：WS 归属校验——is_owned 三态 + 真 WebSocket 上的 4403 / 多标签 / 未建行。

`is_owned` 三态用子类覆盖 `get` 造，不碰数据库；但「关连接时浏览器到底收到什么码」
只有真握手才算数，所以后半段起一个 TestClient：

  R3-a  拿 B 的 token 打开 A 的会话 → 关闭码 4403（不是 403，也不是静默断）；
  R3-b  同一会话两个标签页都收到流式结果；
  R3-c  未建行的 UUID 按原语义放行（首条 /chat 才落归属）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
from pathlib import Path

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from agent_framework.agent import Agent, AgentConfig  # noqa: E402
from agent_framework.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook, OutboundStreamHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.mq import InMemoryMessageQueue  # noqa: E402
from agent_framework.server import create_app  # noqa: E402
from agent_framework.session import SessionManager  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.storage.repositories import ConversationsRepo  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

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


def await_is(coro):
    import asyncio
    return asyncio.run(coro)


def build_agent(mq: InMemoryMessageQueue, answer: str) -> Agent:
    return Agent(
        llm=ScriptedLLM(steps=[("answer", answer)], stream_chunk_size=2),
        registry=ToolRegistry(),
        session_manager=SessionManager(),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        hooks=CompositeHook([OutboundStreamHook(mq)]),
        config=AgentConfig(max_iterations=3),
    )


def receive_until(sock, kind: str, deadline: float = 8.0) -> list[dict]:
    got: list[dict] = []
    stop = time.time() + deadline
    while time.time() < stop:
        msg = sock.receive_json()
        got.append(msg)
        if msg.get("type") == kind:
            break
    return got


def real_ws() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        mq = InMemoryMessageQueue()
        app = create_app(build_agent(mq, "归属校验真答复"), mq, storage=storage)
        with TestClient(app) as client:
            tok_a = client.post("/register", json={}).json()["token"]
            tok_b = client.post("/register", json={}).json()["token"]
            ha = {"Authorization": f"Bearer {tok_a}"}
            hb = {"Authorization": f"Bearer {tok_b}"}

            # A 先把归属落到行上：/chat 的 claim 只读、建行在 Agent 消费时异步发生，
            # 所以这里用 /convs/rename 同步建行（它同样 ensure user_id）。
            r = client.post("/convs/rename",
                            json={"conversation_id": "c-own", "title": "A 的会话"},
                            headers=ha)
            check(r.status_code == 200, f"A 认领会话成功（{r.status_code}）")

            # R3-a：B 的 token 打开 A 的会话 → 4403
            code = None
            try:
                with client.websocket_connect("/ws/c-own?token=" + tok_b) as ws:
                    ws.receive_json()
            except WebSocketDisconnect as e:
                code = e.code
            check(code == 4403, f"跨用户 WS 被 4403 关闭（实际 {code}）")

            # B 也不能把自己的 token 用到 A 的会话上拿流：/chat 直接 409
            r = client.post("/chat", json={"conversation_id": "c-own", "message": "偷看"},
                            headers=hb)
            check(r.status_code in (403, 409), f"跨用户 /chat 被拒（实际 {r.status_code}）")

            # R3-c：未建行的 UUID 放行
            with client.websocket_connect("/ws/brand-new?token=" + tok_a) as ws:
                hello = ws.receive_json()
            check(hello.get("type") == "connected", "未建行的会话按原语义放行")

            # R3-b：同一会话两个标签页都收到流式结果。先连 WS 再发消息（真实前端顺序），
            # 会话此时还没建行，顺带再验一次 R3-c 的放行。
            with client.websocket_connect("/ws/c-tabs?token=" + tok_a) as t1, \
                    client.websocket_connect("/ws/c-tabs?token=" + tok_a) as t2:
                check(t1.receive_json().get("type") == "connected", "标签页 1 建连")
                check(t2.receive_json().get("type") == "connected", "标签页 2 建连")
                r = client.post("/chat", json={"conversation_id": "c-tabs", "message": "推送"},
                                headers=ha)
                run_id = r.json()["run_id"]
                got1 = receive_until(t1, "answer")
                got2 = receive_until(t2, "answer")
            check(got1 and got1[-1].get("answer") == "归属校验真答复",
                  "标签页 1 收到 answer")
            check(got2 and got2[-1].get("answer") == "归属校验真答复",
                  "标签页 2 收到 answer")
            check(all(m.get("run_id") in (None, run_id) for m in got1),
                  "标签页 1 的帧都属于本轮 run")
            check(any(m.get("type") == "delta" for m in got1)
                  and any(m.get("type") == "delta" for m in got2),
                  "两个标签页都收到 delta（不是只投给其中一条连接）")

            # A 自己回连：正常
            with client.websocket_connect("/ws/c-own?token=" + tok_a) as ws:
                check(ws.receive_json().get("type") == "connected", "本人回连正常")


def main() -> None:
    repo = StubRepo({"c1": {"id": "c1", "user_id": "u1"}})

    # ---- 三态 ----
    check(await_is(repo.is_owned("u1", "c1")) is True, "本人会话 → True")
    check(await_is(repo.is_owned("u2", "c1")) is False, "别人会话 → False")
    check(await_is(repo.is_owned("u2", "cX")) is None, "未建行 → None（不是 False）")

    # ---- claim 仍然抛错（写入侧口径）----
    try:
        await_is(repo.claim("u2", "c1"))
        check(False, "claim 对别人的会话抛错")
    except Exception:
        check(True, "claim 对别人的会话抛错")

    # ---- WS 端点顺序：先 accept 再 is_owned 再 close(4403) ----
    src = (BACKEND / "agent_framework" / "server.py").read_text(encoding="utf-8")
    check("is_owned(" in src, "WS 端点调用 is_owned")
    check("4403" in src, "不匹配时 close(4403)")
    i_accept = src.index('await websocket.accept()', src.index("ws_endpoint"))
    i_owned = src.index("is_owned(", i_accept)
    check(i_accept < i_owned,
          "先 accept 再判归属（握手期 close 只会变成 HTTP 403，4403 传不到浏览器）")

    # ---- 真 WS ----
    real_ws()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
