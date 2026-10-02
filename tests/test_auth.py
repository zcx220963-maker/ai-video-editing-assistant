"""身份与鉴权（spec §7）在 HTTP / WS 面上的验证：user_id 由 token 反查，客户端说了不算。

覆盖：
  1. POST /register 签发：明文 token 只回一次，库里只有 sha256；device_name 可缺可给。
  2. 凭证矩阵：除 /health 与 /register 外每个端点缺凭证/错凭证一律 401。
  3. 反查生效：请求体与查询串里伪造的 user_id 一律无效，归属只认 token；
     GET /whoami 是这套反查的正面用法（前端 ca.user 已废弃，身份只问服务端）。
  4. 跨 owner：读别人的会话得到「未找到」（空）而不是 403，写别人名下的会话回 409，
     /sessions 只见自己的。
  5. WS 握手：无 token / 错 token 连不上；带 token 时回投用的 session_id 就是反查出的身份。
  6. 「换浏览器不丢历史」：同一个 token 交给另一个进程（新 app + 新 Agent）照样接得上。
  7. 会话面 API（取代前端 ca.convs）：/convs 列表、/convs/{id}/messages 历史回填（含附件展示）、
     /convs/rename 改名、DELETE /convs/{id} 删会话——全部只按 token 反查出的 owner 过滤。
  8. 不留兼容位：create_app 不给 storage 直接构造失败。
  9. 身份来源收敛（B5-6）：users.ensure 已删除；启动期登记内部身份（default/cron，
     有身份行但不可登录、幂等）；会话历史 / 素材入库 / 记忆三条写入路径都不再自建用户行。

运行：  python tests/test_auth.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import hashlib
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework import server as server_module
from agent_framework.agent import Agent, AgentConfig
from agent_framework.auth import bearer_token
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.identity import use_identity
from agent_framework.ingest import ingest_bytes
from agent_framework.llm import ScriptedLLM
from agent_framework.memory import MemoryStore
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import INTERNAL_IDENTITIES, build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def make_storage(tmp: str):
    return build_storage("memory", cache_root=Path(tmp) / "cache",
                         workspace_root=Path(tmp) / "ws")


def make_agent(storage, answers=("答复",)) -> Agent:
    """装配一个真会往 messages 表写历史的 Agent（鉴权判据要看表，不能只看返回值）。"""
    return Agent(
        llm=ScriptedLLM(steps=[("answer", a) for a in answers]),
        registry=ToolRegistry(),
        session_manager=SessionManager(storage),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=2),
        storage=storage,
    )


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: TestClient, device: str = "") -> tuple[str, str]:
    body = {"device_name": device} if device else {}
    j = client.post("/register", json=body).json()
    return j["user_id"], j["token"]


def case_issue() -> None:
    print("\n[1] /register 签发与库内哈希")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("ok",)), InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as c:
            check(c.get("/health").status_code == 200, "GET /health 不鉴权也能探活")
            uid, tok = register(c, "dev-A")
            check(uid.startswith("u-") and len(tok) == 43,
                  f"服务端签发身份：user_id={uid}、token 是 32 字节 urlsafe（{len(tok)} 字符）")
            again_uid, again_tok = register(c)
            check(again_uid != uid and again_tok != tok, "device_name 可缺省，重复注册是新身份而非覆盖")
            rows = asyncio.run(storage.db.select("users"))
            mine = next(r for r in rows if r["id"] == uid)
            check(mine["token_hash"] == hashlib.sha256(tok.encode()).hexdigest(),
                  "库里存的是 sha256(明文 token)")
            check(tok not in str(rows) and again_tok not in str(rows),
                  "整张 users 表里找不到任何明文 token")

            async def _repo_side() -> None:
                check(await storage.users.verify(tok) == uid, "token 反查回原 user_id")
                check(await storage.users.verify(tok + "x") is None, "改一个字符就反查不到")
                check(await storage.users.verify("") is None, "空 token 反查不到")
            asyncio.run(_repo_side())
            check(bearer_token("Bearer  " + tok) == tok, "bearer_token 解析容忍大小写与多余空白")
            check(bearer_token("Basic abc") == "" and bearer_token(None) == "",
                  "非 Bearer 方案视同没带凭证")


def case_credential_matrix() -> None:
    print("\n[2] 凭证矩阵：除 /health 与 /register 外全部要凭证")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("ok",)), InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as c:
            _, tok = register(c)
            bad = {"Authorization": "Bearer not-a-real-token"}
            probes = [
                ("POST /chat", lambda h=None: c.post("/chat", json={"conversation_id": "c1",
                                                                    "message": "hi"}, headers=h or {})),
                ("POST /chat/sync", lambda h=None: c.post("/chat/sync", json={
                    "conversation_id": "c1", "message": "hi"}, headers=h or {})),
                ("GET /sessions", lambda h=None: c.get("/sessions", headers=h or {})),
                ("GET /whoami", lambda h=None: c.get("/whoami", headers=h or {})),
                ("POST /upload", lambda h=None: c.post(
                    "/upload?conversation_id=c1&filename=a.mp4", content=b"BYTES",
                    headers=h or {})),
                ("POST /fetch_media", lambda h=None: c.post("/fetch_media", json={
                    "conversation_id": "c1", "url": "https://example.com/a.mp4"}, headers=h or {})),
                ("GET /convs", lambda h=None: c.get("/convs", headers=h or {})),
                ("GET /convs/{id}/messages", lambda h=None: c.get(
                    "/convs/c1/messages", headers=h or {})),
                ("POST /convs/rename", lambda h=None: c.post(
                    "/convs/rename", json={"conversation_id": "c1", "title": "t"},
                    headers=h or {})),
                ("DELETE /convs/{id}", lambda h=None: c.delete("/convs/c1", headers=h or {})),
            ]
            for label, probe in probes:
                r_none, r_bad, r_wrong_scheme = probe(), probe(bad), probe(
                    {"Authorization": "Basic YWxhZGRpbjpvcGVuc2VzYW1l"})
                check(r_none.status_code == 401 and r_bad.status_code == 401
                      and r_wrong_scheme.status_code == 401,
                      f"{label}：无凭证 / 错 token / 非 Bearer 方案都 401"
                      f"（{r_none.status_code}/{r_bad.status_code}/{r_wrong_scheme.status_code}）")
            check(c.get("/sessions", headers=hdr(tok)).status_code == 200, "带对 token 就放行")
            check(c.get("/health").status_code == 200,
                  "/health 仍不需要凭证（网关探活不能被 401 挡住）")


def case_owner_from_token() -> None:
    print("\n[3] 归属只认 token：伪造 user_id 无效")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("第一轮", "第二轮")), InMemoryMessageQueue(),
                         storage=storage)
        with TestClient(app) as c:
            uid, tok = register(c)
            victim = "u-victim"

            w = c.get("/whoami", headers=hdr(tok))
            check(w.status_code == 200 and w.json() == {"user_id": uid},
                  f"GET /whoami 只凭 token 问出身份：{w.json()}")

            # ChatRequest 里没有 user_id 这个字段：多塞的键被丢掉，到不了 Agent
            r = c.post("/chat/sync", json={"conversation_id": "c1", "message": "我的话",
                                           "user_id": victim}, headers=hdr(tok))
            check(r.status_code == 200, "/chat/sync 带 token 正常应答")
            hist = asyncio.run(storage.messages.history(uid, "c1"))
            check([h["role"] for h in hist] == ["user", "assistant"]
                  and hist[0]["content"] == "我的话",
                  f"历史落在 token 反查出的用户名下：{[(h['role'], h['content']) for h in hist]}")
            check(asyncio.run(storage.messages.history(victim, "c1")) == [],
                  "被伪造的 user_id 名下什么也没有（伪造不产生任何归属）")

            up = c.post("/upload?conversation_id=c1&filename=海边日落.mp4&user_id=" + victim,
                        content=b"FAKE-VIDEO-BYTES", headers=hdr(tok))
            check(up.status_code == 200, f"/upload 带 token 200（{up.status_code} {up.text[:120]}）")
            key = up.json()["object_key"]
            check(key.startswith(f"users/{uid}/convs/c1/"),
                  f"对象键用反查出的身份：{key}")
            row = asyncio.run(storage.materials.get(up.json()["material_id"]))
            check(row["owner_user_id"] == uid, "materials.owner_user_id 是 token 用户，不是查询串里的")

            # /fetch_media 同理：把取料实现换成替身，看端点交给它的 user_id 是谁
            seen: dict[str, str] = {}
            real = server_module.fetch_media

            async def fake(_storage, url, *, user_id, conversation_id, policy=None):  # noqa: ANN001
                seen["user_id"] = user_id
                seen["conv"] = conversation_id
                return {"material_id": "mat-fake", "object_key": f"users/{user_id}/convs/{conversation_id}/mat-fake.mp4"}
            server_module.fetch_media = fake
            try:
                fm = c.post("/fetch_media", json={"conversation_id": "c1", "url": "https://x/y.mp4",
                                                  "user_id": victim}, headers=hdr(tok))
            finally:
                server_module.fetch_media = real
            check(fm.status_code == 200 and seen["user_id"] == uid,
                  f"/fetch_media 拿到的是反查身份：{seen}")

            as_list = asyncio.run(storage.materials.list_visible(uid, "c1"))
            check(any(m["id"] == up.json()["material_id"] for m in as_list),
                  "本人按自己身份看得见刚上传的素材")


def case_cross_owner() -> None:
    print("\n[4] 跨 owner：读不到而不是告诉你读不到")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("A 的答复",)), InMemoryMessageQueue(),
                         storage=storage)
        with TestClient(app) as c:
            uid_a, tok_a = register(c, "A")
            uid_b, tok_b = register(c, "B")
            c.post("/chat/sync", json={"conversation_id": "c-shared", "message": "A 的话"},
                   headers=hdr(tok_a))
            rb = c.post("/chat/sync", json={"conversation_id": "c-shared", "message": "B 的话"},
                        headers=hdr(tok_b))
            check(rb.status_code == 409,
                  f"B 复用别人的 conversation_id 被端点挡下（HTTP {rb.status_code}）")
            check([h["content"] for h in asyncio.run(storage.messages.history(uid_a, "c-shared"))]
                  == ["A 的话", "A 的答复"], "A 的会话历史没被 B 污染")
            check(asyncio.run(storage.messages.history(uid_b, "c-shared")) == [],
                  "B 按自己的身份查同一 conv_id 得到空（不是 403，不泄露存在性）")
            sa = c.get("/sessions", headers=hdr(tok_a)).json()["sessions"]
            sb = c.get("/sessions", headers=hdr(tok_b)).json()["sessions"]
            check(all(s.startswith(uid_a + ":") for s in sa) and sa,
                  f"/sessions 只列自己的：A={sa}")
            check(all(s.startswith(uid_b + ":") for s in sb), f"/sessions 只列自己的：B={sb}")

            up = c.post("/upload?conversation_id=c-x&filename=a.mp4", content=b"BYTES",
                        headers=hdr(tok_a))
            mid = up.json()["material_id"]
            check(up.status_code == 200 and up.json()["object_key"].startswith(f"users/{uid_a}/"),
                  "A 上传的素材记在 A 名下")
            ok, denied = asyncio.run(storage.materials.resolve([mid], user_id=uid_b,
                                                               conv_id="c-x"))
            check(not ok and denied == [mid],
                  f"B 报同一个 conv_id 也 resolve 不到 A 的素材：ok={ok} denied={denied}")
            ok2, _ = asyncio.run(storage.materials.resolve([mid], user_id=uid_a, conv_id="c-x"))
            check([m["id"] for m in ok2] == [mid], "本人照样解析得到（拒绝的是越权，不是功能）")


def case_websocket() -> None:
    print("\n[5] WebSocket 凭证：?token= 决定这条连接是谁的")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("ok",)), InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as c:
            uid_a, tok_a = register(c, "A")
            uid_b, tok_b = register(c, "B")
            for url in ("/ws/c1", "/ws/c1?token=", "/ws/c1?token=wrong", "/ws/A/c1"):
                try:
                    with c.websocket_connect(url) as ws:
                        frame = ws.receive_json()
                    got: str | None = str(frame)
                except Exception as exc:  # noqa: BLE001 - 握手被拒就是异常，类型随 starlette 版本
                    got = None
                    check("Disconnect" in type(exc).__name__ or "WebSocket" in type(exc).__name__,
                          f"{url} 被拒（{type(exc).__name__}）")
                else:
                    check(False, f"{url} 竟被接受：{got}")
            with c.websocket_connect("/ws/c1?token=" + tok_a) as ws:
                frame = ws.receive_json()
            check(frame == {"type": "connected", "session_id": f"{uid_a}:c1"},
                  f"路径里没有 user_id，回投键由 token 反查：{frame}")
            with c.websocket_connect("/ws/c1?token=" + tok_b) as ws:
                frame_b = ws.receive_json()
            check(frame_b["session_id"] == f"{uid_b}:c1" and frame_b != frame,
                  f"同一个 conv_id 两个身份各连各的：{frame_b['session_id']}")


def case_token_survives_new_client() -> None:
    print("\n[6] 换浏览器 / 换进程：token 还认，历史还在")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app1 = create_app(make_agent(storage, ("旧进程的答复",)), InMemoryMessageQueue(),
                          storage=storage)
        with TestClient(app1) as c1:
            uid, tok = register(c1, "浏览器甲")
            c1.post("/chat/sync", json={"conversation_id": "c1", "message": "第一轮"},
                    headers=hdr(tok))
        # 新进程 + 新客户端实例：只有那张纸上记的 token，别的什么都没带走
        app2 = create_app(make_agent(storage, ("新进程的答复",)), InMemoryMessageQueue(),
                          storage=storage)
        with TestClient(app2) as c2:
            check(c2.get("/whoami", headers=hdr(tok)).json() == {"user_id": uid},
                  "新进程只凭 token 就问回同一个 user_id（本地不存身份）")
            r = c2.post("/chat/sync", json={"conversation_id": "c1", "message": "第二轮"},
                        headers=hdr(tok))
            check(r.status_code == 200, "旧 token 在新进程里照样认")
            hist = asyncio.run(storage.messages.history(uid, "c1"))
            check([h["content"] for h in hist]
                  == ["第一轮", "旧进程的答复", "第二轮", "新进程的答复"],
                  f"历史接得上：{[h['content'] for h in hist]}")
            check(c2.get("/sessions", headers=hdr(tok)).json()["sessions"],
                  "新进程里 /sessions 仍列出本人的会话")


def case_conv_apis() -> None:
    print("\n[7] 会话面：/convs 列表、历史回填、改名、删除都按 owner 走")
    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        app = create_app(make_agent(storage, ("A 的答复", "A 的第二轮")),
                         InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as c:
            uid_a, tok_a = register(c, "A")
            uid_b, tok_b = register(c, "B")

            # 第一条消息之前也可能先起名（前端新建会话就调 /convs/rename）
            rn = c.post("/convs/rename", json={"conversation_id": "ca-2", "title": "先起的名"},
                        headers=hdr(tok_a))
            check(rn.status_code == 200, f"空会话也能先起名（{rn.status_code}）")
            listed = {(x["conversation_id"], x["title"])
                      for x in c.get("/convs", headers=hdr(tok_a)).json()["conversations"]}
            check(("ca-2", "先起的名") in listed, f"/convs 里能看到刚起的名字：{listed}")

            mid = c.post("/upload?conversation_id=ca-1&filename=海边日落.mp4",
                         content=b"FAKE-VIDEO-BYTES", headers=hdr(tok_a)).json()["material_id"]
            c.post("/chat/sync", json={"conversation_id": "ca-1", "message": "帮我剪海边的",
                                       "attachments": [mid]}, headers=hdr(tok_a))
            rows = c.get("/convs", headers=hdr(tok_a)).json()["conversations"]
            check({r["conversation_id"] for r in rows} == {"ca-1", "ca-2"},
                  f"聊过天的会话自动出现在列表里：{[r['conversation_id'] for r in rows]}")
            check(all(r["updated_at"] for r in rows), "列表带回更新时间（供前端排序）")

            h = c.get("/convs/ca-1/messages", headers=hdr(tok_a)).json()["messages"]
            check([m["role"] for m in h] == ["user", "assistant"]
                  and h[0]["text"] == "帮我剪海边的",
                  f"历史回填读出本人这轮对话：{[(m['role'], m['text']) for m in h]}")
            att = h[0]["attachments"]
            check(len(att) == 1 and att[0]["material_id"] == mid
                  and att[0]["filename"] == "海边日落.mp4" and att[0]["kind"] == "video"
                  and att[0]["bytes"] == len(b"FAKE-VIDEO-BYTES") and att[0]["url"],
                  f"附件回成可展示的样子（含 presigned URL）：{att}")
            check(all("attachments" in m for m in h) and h[1]["attachments"] == [],
                  "assistant 那条没有附件也是空列表，不是缺字段")

            c.post("/convs/rename", json={"conversation_id": "ca-1", "title": "海边 vlog"},
                   headers=hdr(tok_a))
            c.post("/chat/sync", json={"conversation_id": "ca-1", "message": "再来一轮"},
                   headers=hdr(tok_a))
            after = {r["conversation_id"]: r["title"]
                     for r in c.get("/convs", headers=hdr(tok_a)).json()["conversations"]}
            check(after["ca-1"] == "海边 vlog",
                  f"改名之后继续聊天不会把标题冲回「新对话」：{after}")

            long = c.post("/convs/rename", json={"conversation_id": "ca-1", "title": "长" * 80},
                          headers=hdr(tok_a))
            cut = {r["conversation_id"]: r["title"]
                   for r in c.get("/convs", headers=hdr(tok_a)).json()["conversations"]}["ca-1"]
            check(long.status_code == 200 and cut == "长" * 60, f"标题截到 60 字：{len(cut)} 字")

            check(c.get("/convs", headers=hdr(tok_b)).json()["conversations"] == [],
                  "B 的列表是空的（看不见 A 的会话）")
            check(c.get("/convs/ca-1/messages", headers=hdr(tok_b)).json() == {"messages": []},
                  "B 读 A 的会话历史得到空（不是 403）")
            check(c.post("/convs/rename", json={"conversation_id": "ca-1", "title": "抢过来"},
                         headers=hdr(tok_b)).status_code == 409,
                  "B 改 A 的会话标题回 409")
            check(c.delete("/convs/ca-1", headers=hdr(tok_b)).status_code == 409,
                  "B 删 A 的会话回 409")
            check(asyncio.run(storage.messages.history(uid_a, "ca-1")),
                  "A 的历史没被 B 动过")

            drop = c.delete("/convs/ca-1", headers=hdr(tok_a))
            check(drop.status_code == 200, f"本人删自己的会话 200（{drop.status_code} {drop.text[:80]}）")
            check(c.get("/convs/ca-1/messages", headers=hdr(tok_a)).json() == {"messages": []},
                  "删完历史就没了")
            check({r["conversation_id"] for r in c.get("/convs", headers=hdr(tok_a)).json()["conversations"]}
                  == {"ca-2"}, "列表里也不再出现")


def case_no_compat() -> None:
    print("\n[8] 不留兼容位")
    with tempfile.TemporaryDirectory() as td:
        try:
            create_app(make_agent(make_storage(td), ("ok",)), InMemoryMessageQueue())
        except TypeError as exc:
            check("storage" in str(exc), f"create_app 不给 storage 直接构造失败：{exc}")
        else:
            check(False, "create_app 仍可在没有存储层时构造（鉴权无处落脚，应改为必填）")


def case_identity_provisioning() -> None:
    """B5-6：用户行的来路只剩两条——/register 签发、启动期登记内部身份。"""
    print("\n[9] 身份来源收敛：写入路径不再自建用户行")

    async def _count_users(s) -> int:
        return await s.db.count("users")

    with tempfile.TemporaryDirectory() as td:
        storage = make_storage(td)
        check(not hasattr(storage.users, "ensure"),
              "users.ensure 过渡位已删除（客户端自带的 id 无法自我加冕）")

        got = asyncio.run(storage.provision_internal())
        check(got == list(INTERNAL_IDENTITIES), f"启动期登记内部身份：{got}")
        for uid in INTERNAL_IDENTITIES:
            row = asyncio.run(storage.users.get(uid))
            check(row is not None and len(row["token_hash"]) == 64,
                  f"内部身份 {uid} 有行、库里只有 64 位哈希")
            check(asyncio.run(storage.users.verify(row["token_hash"])) is None,
                  f"内部身份 {uid} 存在但不可登录（占位哈希反查不到）")
        before = asyncio.run(_count_users(storage))
        asyncio.run(storage.provision_internal())
        check(asyncio.run(_count_users(storage)) == before, "重启再登记不插新行（幂等）")

        # 三条写入路径各自跑一遍：历史落库了，但 users 表一个字节都没多
        stranger, conv_s = "u-stranger", "c-stranger"
        n0 = asyncio.run(_count_users(storage))
        agent = make_agent(storage, ("答复",))
        asyncio.run(agent.handle(stranger, conv_s, "我的话"))
        hist = asyncio.run(storage.messages.history(stranger, conv_s))
        check([h["role"] for h in hist] == ["user", "assistant"],
              "未登记身份的一轮对话仍落库（离线替身无外键；真 PG 上由外键拒绝）")
        check(asyncio.run(_count_users(storage)) == n0, "会话写入路径不再补 users 行")

        async def _chunks():
            yield b"FAKE-VIDEO-BYTES"

        asyncio.run(ingest_bytes(storage, _chunks(), filename="a.mp4",
                                 user_id=stranger, conversation_id=conv_s))
        check(asyncio.run(storage.materials.list_visible(stranger, conv_s)),
              "素材入库路径本身仍工作（拒的是没登记过的身份，不是新功能）")
        check(asyncio.run(_count_users(storage)) == n0, "素材入库路径不再补 users 行")

        store = MemoryStore(storage)
        with use_identity("u-memo", "c-memo"):
            asyncio.run(store.write("user", "喜欢海边"))
            got_memo = asyncio.run(store.read("user"))
        check(asyncio.run(_count_users(storage)) == n0, "记忆写入不再补 users 行")
        check(got_memo == "喜欢海边", "记忆仍按当前身份读写")
        check(asyncio.run(store.read("user")) == "", "退出身份上下文后回到缺省作用域（读不到他人记忆）")


def main() -> None:
    for fn in (case_issue, case_credential_matrix, case_owner_from_token,
               case_cross_owner, case_websocket, case_token_survives_new_client,
               case_conv_apis, case_no_compat, case_identity_provisioning):
        fn()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
