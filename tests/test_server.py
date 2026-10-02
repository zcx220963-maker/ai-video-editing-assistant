"""Web 接口层验证（FastAPI TestClient，不联网、不连 broker）：/health、/chat/sync、/chat 入队消费、
/upload 直写对象存储 + materials 登记（内存替身存储，全程不落本地文件）、
/fetch_media 按链接取料（假传输 + 假 DNS，端点与 /upload 同构）、
/chat attachments → MQ → messages 表 + 上下文附件事实。

身份一律走 B5 的凭证模型（`Authorization: Bearer <token>`，token 由 /register 签发）：
请求体与查询串里没有 user_id 这个字段，归属由服务端反查。鉴权矩阵本身由
test_auth.py 专测，这里只把端点功能跑在真实凭证上，外加两条最粗的边界探针。

运行：  python tests/test_server.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import hashlib
import socket
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework import media_fetch
from agent_framework.agent import Agent, AgentConfig
from agent_framework.checkpoint import CheckpointManager
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.editing_contract import ContractSlot, EditingContract, NodeContract
from agent_framework.identity import storyline_session_id
from agent_framework.llm import ScriptedLLM
from agent_framework.media_fetch import FetchPolicy, FetchRejected
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def storage_in(td: str):
    return build_storage("memory", cache_root=Path(td) / "cache",
                         workspace_root=Path(td) / "ws")


def put_object(storage, key: str, blob: bytes) -> None:
    """往对象存储塞一个成片字节：presign 不静默降级，缺对象就该抛。"""
    async def _run():
        async def chunks():
            yield blob
        await storage.objects.put(key, chunks(), content_type="video/mp4")
    asyncio.run(_run())


def make_agent(storage, answer: str = "创作助手已就绪", steps=1) -> Agent:
    return Agent(
        llm=ScriptedLLM(steps=[("answer", answer)] * steps),
        registry=ToolRegistry(),
        session_manager=SessionManager(storage),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        config=AgentConfig(max_iterations=3),
        storage=storage,
    )


def register(client: TestClient) -> tuple[str, dict[str, str]]:
    """签一个新身份，返回 (user_id, 可直接发给端点的凭证头)。"""
    j = client.post("/register", json={}).json()
    return j["user_id"], {"Authorization": f"Bearer {j['token']}"}


def wait_run(consumer, run_id: str, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run_id in consumer.processed:
            return True
        time.sleep(0.02)
    return False


def main() -> None:
    # ---- 聊天面：同步 / 异步入队 / 会话列表（都只认 token 反查出的身份）----
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        agent = make_agent(storage, steps=2)
        app = create_app(agent, InMemoryMessageQueue(), storage=storage)
        with TestClient(app) as client:
            uid, hdr = register(client)
            other, hdr_other = register(client)

            r = client.get("/health")
            check(r.status_code == 200 and r.json()["status"] == "ok", "GET /health → ok")
            paths = {getattr(rt, "path", "") for rt in app.routes}
            check(not any(p == "/media" or p.startswith("/media/") for p in paths),
                  "「/media/*」静态挂载已删除：媒体回放只剩对象存储 presigned 直链")
            check(client.post("/chat/sync", json={"conversation_id": "c1",
                                                 "message": "你好"}).status_code == 401,
                  "不带凭证的 /chat/sync 直接被拒（401）")

            # 同步端到端：直接 await Agent.handle
            r = client.post("/chat/sync", json={"conversation_id": "c1", "message": "你好"},
                            headers=hdr)
            check(r.status_code == 200, f"POST /chat/sync 200（{r.status_code} {r.text[:120]}）")
            body = r.json()
            check(body["answer"] == "创作助手已就绪", "/chat/sync 返回最终答复")
            check("run_id" in body, "/chat/sync 带 run_id")

            # 异步：入队 → 消费者驱动 Agent
            r = client.post("/chat", json={"conversation_id": "c2", "message": "帮我写文案"},
                            headers=hdr)
            check(r.status_code == 200 and r.json()["status"] == "queued", "POST /chat 入队返回 queued")
            check(wait_run(app.state.consumer, r.json()["run_id"]), "消费者异步处理了该 run_id")

            sess = agent.session_manager.get(f"{uid}:c2")
            check(sess is not None and len(sess.messages) >= 2, "被处理会话写入了历史")

            mine = client.get("/sessions", headers=hdr).json()["sessions"]
            check(f"{uid}:c1" in mine and f"{uid}:c2" in mine, f"GET /sessions 列出本人的活跃会话：{mine}")
            check(client.get("/sessions", headers=hdr_other).json()["sessions"] == [],
                  "另一个身份的 /sessions 看不到上面这些会话")

            forged = client.post("/chat/sync", json={"conversation_id": "c1", "message": "borrow"},
                                 headers=hdr_other)
            check(forged.status_code == 409,
                  f"他人复用已归属的 conversation_id 被挡下（{forged.status_code}）")
            check([h["content"] for h in asyncio.run(storage.messages.history(uid, "c1"))]
                  == ["你好", "创作助手已就绪"], "被挡下后本人历史没被污染")
            check(client.get("/runs/whatever", headers=hdr).status_code == 503,
                  "没配 checkpoint 时执行记录端点如实 503（不假装有一张空表）")
            check(client.get("/convs/c1/runs", headers=hdr).json() == {"runs": []},
                  "同一情形下执行清单退化成空")

    # ---- 执行记录 / 时间旅行面 ----------------------------------------------
    #
    # 这几个端点是「换 BGM 重渲染」的入口面：只读的两个负责让人挑分叉点，
    # fork / resume 不就地执行，而是投一帧带 action 的 MQ 消息，由消费者走回同一条链路。
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        agent_cp = Agent(
            llm=ScriptedLLM(steps=[("answer", "这一版已出片")] * 6),
            registry=ToolRegistry(),
            session_manager=SessionManager(storage),
            context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
            config=AgentConfig(max_iterations=3),
            storage=storage,
            checkpoint=CheckpointManager(storage),
        )
        # 分叉要按契约展开下游：这里给一段最小剪辑 DAG（select_BGM → plan_timeline）。
        slot = ContractSlot(EditingContract(nodes={
            "generate_script": NodeContract("generate_script"),
            "select_BGM": NodeContract("select_BGM", ("generate_script",)),
            "plan_timeline": NodeContract("plan_timeline", ("select_BGM",)),
        }))
        app_cp = create_app(agent_cp, InMemoryMessageQueue(), storage=storage,
                            editing_contract=slot)
        with TestClient(app_cp) as c:
            uid, hdr = register(c)
            other, hdr_other = register(c)

            run_id = c.post("/chat", json={"conversation_id": "ct", "message": "出一条片子"},
                            headers=hdr).json()["run_id"]
            check(wait_run(app_cp.state.consumer, run_id), "首条消息的 run 已被消费")

            runs = c.get("/convs/ct/runs", headers=hdr).json()["runs"]
            check([x["run_id"] for x in runs] == [run_id], f"/convs/*/runs 列出本次执行：{runs}")
            check(runs[0]["status"] == "completed" and runs[0]["forked_from"] is None
                  and "messages" not in runs[0] and runs[0]["head_seq"] >= 1,
                  f"清单只有指针行（head_seq={runs[0]['head_seq']}），不回传消息正文")
            check(c.get(f"/runs/{run_id}", headers=hdr).json()["run"]["session_id"]
                  == f"{uid}:ct", "GET /runs/{id} 按 token 反查的归属取行")

            pts = c.get(f"/runs/{run_id}/history", headers=hdr).json()["points"]
            check([p["seq"] for p in pts] == list(range(len(pts))) and len(pts) >= 2,
                  f"一致点链可枚举：{[(p['seq'], p['kind']) for p in pts]}")
            check(pts[0]["kind"] == "full" and all(p["kind"] == "delta" for p in pts[1:]),
                  "链首 full、其后 delta：每轮只写新增段")

            check(c.get(f"/runs/{run_id}", headers=hdr_other).status_code == 404,
                  "别人的 run 按不存在处理（404，不泄露存在性）")
            check(c.post(f"/runs/{run_id}/fork", json={"at_seq": 0},
                         headers=hdr_other).status_code == 404,
                  "他人不能对这条 run 分叉")

            fork_at = pts[-1]["seq"]
            fr = c.post(f"/runs/{run_id}/fork",
                        json={"at_seq": fork_at, "message": "换首BGM重新出片",
                              "rerun_nodes": ["select_BGM"]},
                        headers=hdr)
            child_id = fr.json()["run_id"]
            check(fr.status_code == 200 and child_id and child_id != run_id,
                  "fork 回一条新 run_id（父 run 原样留着）")
            check(wait_run(app_cp.state.consumer, child_id), "fork 帧经 MQ 被消费者接到")
            byid = {x["run_id"]: x
                    for x in c.get("/convs/ct/runs", headers=hdr).json()["runs"]}
            check(byid[run_id]["status"] == "superseded", "父 run 让位 superseded（不再被崩溃恢复重播）")
            check(byid[child_id]["forked_from"] == run_id
                  and byid[child_id]["forked_at_seq"] == fork_at,
                  f"子 run 记下来源：{byid[child_id]['forked_from']}@{byid[child_id]['forked_at_seq']}")
            check(byid[child_id]["status"] == "completed", "子 run 自己跑到 completed")
            check(byid[child_id]["artifact_id"],
                  f"分叉换了产物作用域：{byid[child_id]['artifact_id']}")

            # 在途 run：resume 语义下回给前端的必须是它，否则界面上是一个永不结束的幽灵 run。
            sid_cr = f"{uid}:cr"
            asyncio.run(agent_cp.runner.checkpoint.begin(
                sid_cr, "继续刚才那条", [{"role": "user", "content": "继续刚才那条"}],
                "inflight-1", scope={"storyline_session": sid_cr, "artifact_id": ""}))
            active = c.get("/convs/cr/runs/active", headers=hdr).json()["run"]
            check(active and active["run_id"] == "inflight-1", "/convs/*/runs/active 看到在途 run")
            rr = c.post("/chat", json={"conversation_id": "cr", "message": "继续",
                                       "resume": True}, headers=hdr)
            check(rr.json()["run_id"] == "inflight-1",
                  f"resume=true 返回在途 run_id：{rr.json()['run_id']}")
            check(wait_run(app_cp.state.consumer, "inflight-1"), "resume 帧按在途 run_id 被消费")
            check(c.get("/runs/inflight-1", headers=hdr).json()["run"]["status"] == "completed",
                  "resume 续跑并收尾 completed")
            new_run = c.post("/chat", json={"conversation_id": "cr", "message": "新想法"},
                             headers=hdr).json()["run_id"]
            check(new_run != "inflight-1", "不带 resume 的新消息开新 run（不劫持）")
            check(wait_run(app_cp.state.consumer, new_run), "新 run 被正常消费")

            sid_cs = f"{uid}:cs"
            asyncio.run(agent_cp.runner.checkpoint.begin(
                sid_cs, "分叉后想回看", [{"role": "user", "content": "分叉后想回看"}],
                "inflight-2", scope={"storyline_session": sid_cs, "artifact_id": ""}))
            rs = c.post("/runs/inflight-2/resume", json={}, headers=hdr)
            check(rs.status_code == 200 and rs.json()["run_id"] == "inflight-2",
                  "POST /runs/{id}/resume 入队并沿用该 run_id")
            check(wait_run(app_cp.state.consumer, "inflight-2"), "resume 帧被消费者接到")
            check(c.get("/runs/inflight-2", headers=hdr).json()["run"]["status"] == "completed",
                  "该 run 续跑完成")

    # 素材上传：POST /upload 原始字节流 → 对象存储 + materials 登记 → material_id + 可播放 URL
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        agent3 = make_agent(storage, answer="x", steps=1)
        app3 = create_app(agent3, InMemoryMessageQueue(), storage=storage, max_upload_mb=1)
        with TestClient(app3) as c3:
            uid, hdr = register(c3)
            url = "/upload?conversation_id=c9&filename=海边日落.mp4"
            first = c3.post(url, content=b"FAKE-VIDEO-BYTES", headers=hdr)
            dirty = c3.post("/upload?conversation_id=c9&filename=..%2F..%2Fevil%3Ashell.mp4",
                            content=b"x", headers=hdr)
            again = c3.post(url, content=b"SECOND", headers=hdr)      # 同名再传
            huge = c3.post(url, content=b"Z" * (1024 * 1024 + 10), headers=hdr)
            empty = c3.post(url, content=b"", headers=hdr)
            weird = c3.post("/upload?conversation_id=c9&filename=笔记.txt",
                            content=b"hello", headers=hdr)

            check(first.status_code == 200, f"POST /upload 200（{first.status_code} {first.text[:160]}）")
            j = first.json()
            check(j["material_id"].startswith("mat-") and j["id"] == j["material_id"],
                  "返回 material_id 作为素材唯一标识")
            check(j["object_key"] == f"users/{uid}/convs/c9/{j['material_id']}.mp4",
                  "对象键布局 users/{反查出的 uid}/convs/{c}/{material_id}{ext}")
            check(j["url"] and j["object_key"] in j["url"], "回 presigned URL 供前端直接播放")
            check(dirty.status_code == 200 and dirty.json()["filename"] == "evil_shell.mp4",
                  "文件名净化：穿越与非法字符被剥除")
            check("evil" not in dirty.json()["object_key"],
                  "对象键只含 material_id，不含用户文件名")
            check(again.status_code == 200 and again.json()["material_id"] != j["material_id"],
                  "同名再传是另一条素材（id 唯一，天然不覆盖）")
            check(huge.status_code == 413, "超过 max_upload_mb 上限回 413")
            check(empty.status_code == 400, "空文件回 400")
            check(weird.status_code == 415, "白名单外类型回 415")

            async def _storage_side() -> None:
                row = await storage.materials.get(j["material_id"])
                check(row is not None and row["object_key"] == j["object_key"],
                      "materials 行与响应同源")
                if row is None:
                    return
                check(row["owner_user_id"] == uid and row["conv_id"] == "c9",
                      "归属登记到 token 反查出的 user/conversation")
                check(row["kind"] == "video" and row["filename"] == "海边日落.mp4"
                      and row["mime"] == "video/mp4" and row["origin"] == "upload",
                      "kind/filename/mime/origin 入库正确")
                check(row["bytes"] == len(b"FAKE-VIDEO-BYTES")
                      and row["sha256"] == hashlib.sha256(b"FAKE-VIDEO-BYTES").hexdigest(),
                      "字节数与 sha256 由存储层边收流边算并落库")
                check("path" not in row, "数据里没有任何本地路径字段")
                info = await storage.objects.head(j["object_key"])
                check(info is not None and info.bytes == len(b"FAKE-VIDEO-BYTES"),
                      "字节已在对象存储里（本地磁盘未经手）")
                ok, denied = await storage.materials.resolve(
                    [j["material_id"]], user_id="u-intruder", conv_id="c9")
                check(not ok and denied == [j["material_id"]], "他人 resolve 不到该素材")
                check(await storage.users.get(uid) is not None
                      and await storage.conversations.get("c9") is not None,
                      "users 行来自 /register，conversations 行随上传补齐（外键前置）")

            asyncio.run(_storage_side())

            # ---- POST /fetch_media：按链接取料与上传共用一条入库路径（假网络，离线）----
            class _H:
                def __init__(self, kv):                # noqa: ANN001
                    self.kv = {k.lower(): v for k, v in kv.items()}

                def get(self, name, default=""):       # noqa: ANN001
                    return self.kv.get(name.lower(), default)

            class _R:
                def __init__(self, body, kv):          # noqa: ANN001
                    self.b, self.headers, self.closed = body, _H(kv), False

                def read(self, n=-1):                  # noqa: ANN001
                    out, self.b = (self.b, b"") if n is None or n < 0 else (self.b[:n], self.b[n:])
                    return out

                def close(self):
                    self.closed = True

            routes = {
                "https://media.example.com/sea/link.mp4":
                    (b"LINK-BYTES", {"Content-Type": "video/mp4",
                                     "Content-Length": str(len(b"LINK-BYTES"))}),
                "https://media.example.com/doc.pdf":
                    (b"%PDF-1.7", {"Content-Type": "application/pdf"}),
            }

            def fake_open(url, timeout):               # noqa: ANN001
                if url not in routes:
                    raise FetchRejected(502, f"假网络未登记：{url}")
                body, kv = routes[url]
                return _R(body, kv), url

            real_gai, old_open = socket.getaddrinfo, media_fetch._open
            socket.getaddrinfo = lambda host, port, *a, **kw: (
                [(2, 1, 6, "", ("93.184.216.34", 0))] if host == "media.example.com"
                else real_gai(host, port, *a, **kw))
            media_fetch._open = fake_open
            try:
                app_f = create_app(agent3, InMemoryMessageQueue(), storage=storage,
                                   fetch_policy=FetchPolicy(max_mb=8, timeout=5.0,
                                                            allow_ytdlp=False))
                with TestClient(app_f) as cf:
                    got = cf.post("/fetch_media", json={
                        "conversation_id": "c9",
                        "url": "https://media.example.com/sea/link.mp4"}, headers=hdr)
                    bad_type = cf.post("/fetch_media", json={
                        "conversation_id": "c9",
                        "url": "https://media.example.com/doc.pdf"}, headers=hdr)
                    internal = cf.post("/fetch_media", json={
                        "conversation_id": "c9",
                        "url": "http://127.0.0.1:8000/a.mp4"}, headers=hdr)
                check(got.status_code == 200, f"POST /fetch_media 200（{got.status_code} {got.text[:120]}）")
                fj = got.json()
                check(fj["material_id"].startswith("mat-") and fj["kind"] == "video"
                      and fj["origin"] == "url", "返回同构素材描述（material_id/kind/origin）")
                check(fj["object_key"] == f"users/{uid}/convs/c9/{fj['material_id']}.mp4"
                      and fj["url"], "对象键布局与 presigned URL 与 /upload 一致")
                check(bad_type.status_code == 415, "白名单外链接回 415")
                check(internal.status_code == 400, "内网链接回 400（SSRF 守卫在端点上生效）")

                async def _fetched_row() -> None:
                    row = await storage.materials.get(fj["material_id"])
                    check(row is not None and row["origin"] == "url"
                          and row["owner_user_id"] == uid and row["conv_id"] == "c9",
                          "materials 里确有这条爬来的素材")
                    info = await storage.objects.head(fj["object_key"])
                    check(info is not None and info.bytes == len(b"LINK-BYTES"),
                          "链接字节确实进了对象存储")
                asyncio.run(_fetched_row())
            finally:
                media_fetch._open, socket.getaddrinfo = old_open, real_gai

    # ---- 渲染轮询 HTTP 口：GET /render_status（前端进度条与脚本共用）----
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        app6 = create_app(make_agent(storage, answer="x", steps=1),
                          InMemoryMessageQueue(), storage=storage)
        with TestClient(app6) as c6:
            uid, hdr = register(c6)
            sid = storyline_session_id(uid, "c_poll")
            art = "artPoll"
            key = f"renders/{sid.replace(':', '_')}/{art}.mp4"
            q = c6.get(f"/render_status?artifact_id={art}&conv_id=c_poll", headers=hdr)
            check(q.status_code == 404, "没提交过的作用域 → 404（不区分不存在与无权）")

            asyncio.run(storage.render_jobs.enqueue(sid, art))
            q = c6.get(f"/render_status?artifact_id={art}&conv_id=c_poll", headers=hdr)
            body = q.json()
            check(q.status_code == 200 and body["status"] == "queued"
                  and "media_url" not in body and "video" not in body,
                  f"queued 只回进度、不编造成片：{ {k: body.get(k) for k in ('status', 'stage', 'percent')} }")

            asyncio.run(storage.render_jobs.progress(sid, art, "encoding", 40))
            body = c6.get(f"/render_status?artifact_id={art}&conv_id=c_poll",
                          headers=hdr).json()
            check((body["stage"], body["percent"]) == ("encoding", 40),
                  "进度写读一致（stage/percent 来自 render_jobs 行）")

            put_object(storage, key, b"MP4-BYTES")
            asyncio.run(storage.render_jobs.succeed(sid, art, key, 6.5,
                                                    {"video": key, "duration": 6.5,
                                                     "width": 640, "height": 360,
                                                     "title": "轮询口"}))
            body = c6.get(f"/render_status?artifact_id={art}&conv_id=c_poll",
                          headers=hdr).json()
            check(body["status"] == "done" and "://" in str(body.get("media_url"))
                  and body.get("title") == "轮询口" and body.get("width") == 640,
                  "done 平铺 result 并现签 media_url（与当轮卡片同形状）")

            other_uid, other_hdr = register(c6)
            q = c6.get(f"/render_status?artifact_id={art}&conv_id=c_poll", headers=other_hdr)
            check(q.status_code == 404,
                  f"作用域按 token 反查：别人的渲染查不到（{other_uid[:8]}… → {q.status_code}）")

    # ---- 附件贯通：/chat {attachments} → MQ → Consumer → Agent → messages 表 + 上下文 ----
    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        llm = ScriptedLLM(steps=[("answer", "已按附件开始剪辑"), ("answer", "同步路径收到")])
        agent5 = Agent(
            llm=llm,
            registry=ToolRegistry(),
            session_manager=SessionManager(storage),
            context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
            config=AgentConfig(max_iterations=3),
            storage=storage,
        )
        app5 = create_app(agent5, InMemoryMessageQueue(), storage=storage)
        with TestClient(app5) as c5:
            uid, hdr = register(c5)
            mid = c5.post("/upload?conversation_id=c9&filename=海边日落.mp4",
                          content=b"FAKE-BYTES", headers=hdr).json()["material_id"]
            r = c5.post("/chat", json={"conversation_id": "c9",
                                       "message": "把这段剪成 vlog",
                                       "attachments": [mid, "mat-nope"]}, headers=hdr)
            check(wait_run(app5.state.consumer, r.json()["run_id"]), "/chat 附件随 MQ 帧被消费者取到")
            sync = c5.post("/chat/sync", json={"conversation_id": "c10",
                                               "message": "同步也带附件",
                                               "attachments": [mid]}, headers=hdr)
            check(sync.status_code == 200 and sync.json()["answer"] == "同步路径收到",
                  "/chat/sync 同样接受 attachments")

        system = llm.calls[0][0]["content"]
        check(mid in system and "海边日落.mp4" in system and "<attachments>" in system,
              "LLM 上下文里是 material_id 事实，不是自由文本路径")
        check("mat-nope" in system and "无法使用" in system, "无法使用的附件 id 如实告知")
        check(str(Path(td)) not in system and "cache" not in system, "上下文里没有任何本地路径")
        check("把这段剪成 vlog" == llm.calls[0][-1]["content"], "user 消息仍是用户原话")

        async def _attach_side() -> None:
            hist = await storage.messages.history(uid, "c9")
            check([h["role"] for h in hist] == ["user", "assistant"],
                  "messages 表按 seq 落成 user/assistant 一对")
            if hist:
                check(hist[0]["attachments"] == [mid, "mat-nope"],
                      "user 行的 attachments 列存 material_id 列表")
                check(hist[0]["content"] == "把这段剪成 vlog", "user 行 content 是原话")
                check(hist[1]["content"] == "已按附件开始剪辑", "assistant 行存最终答复")
                check(isinstance(hist[1]["qa"], dict)
                      and hist[1]["qa"]["parts"][-1]["type"] == "answer",
                      "assistant 行 qa 带本轮 think/tool/answer 片段")
            hist2 = await storage.messages.history(uid, "c10")
            check(len(hist2) == 2 and hist2[0]["attachments"] == [mid],
                  "/chat/sync 路径同样落库（含 attachments）")
            check(await storage.messages.history("intruder", "c9") == [],
                  "他人读不到该会话历史")

        asyncio.run(_attach_side())

        # 未注入 storage 的 Agent：附件无法核验就如实报告，绝不静默按路径兜底
        llm6 = ScriptedLLM(steps=[("answer", "无存储")])
        agent6 = Agent(llm=llm6, registry=ToolRegistry(),
                       context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
                       config=AgentConfig(max_iterations=2))
        asyncio.run(agent6.handle(uid, "c11", "剪一下", attachments=[mid]))
        check("无法使用" in llm6.calls[0][0]["content"],
              "无存储句柄时附件全部报不可用（不猜路径）")

    # 静态托管：dist 目录存在时 / 返回前端页面，API 路由不被 mount 遮蔽
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "index.html").write_text("<html>frontend-app</html>", encoding="utf-8")
        storage7 = storage_in(td)
        app2 = create_app(make_agent(storage7, answer="x"), InMemoryMessageQueue(),
                          storage=storage7, static_dir=td)
        with TestClient(app2) as c2:
            r = c2.get("/")
            check(r.status_code == 200 and "frontend-app" in r.text, "GET / 返回托管的前端页面")
            check(c2.get("/health").json()["status"] == "ok", "静态 mount 后 API 仍先匹配")

    # ---- 中文名词表的 HTTP 出口（块 A）：默认响应类 + 报错通道共用同一个出口 ----
    from agent_framework.catalog import ToolCatalog, get_catalog, set_catalog
    from agent_framework.team_tools import FunctionTool

    with tempfile.TemporaryDirectory() as td:
        storage = storage_in(td)
        set_catalog(ToolCatalog({"split_shots": "镜头切分", "render_video": "成片渲染"}))
        try:
            reg8 = ToolRegistry()
            reg8.register(FunctionTool(
                "split_shots", "把素材按镜头切开，产物写进 split_shots 的表",
                {"type": "object", "properties": {}},
                lambda **kw: {"node": "split_shots"}, display_name="镜头切分"))
            agent8 = Agent(
                llm=ScriptedLLM(steps=[("answer", "已经跑完 split_shots，再看 render_video。")]),
                registry=reg8, session_manager=SessionManager(storage),
                context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
                config=AgentConfig(max_iterations=3), storage=storage,
                checkpoint=CheckpointManager(storage))
            app8 = create_app(agent8, InMemoryMessageQueue(), storage=storage)
            with TestClient(app8) as c8:
                uid, hdr = register(c8)
                groups = c8.get("/tools", headers=hdr).json()
                pd = groups["params_display"]
                check(pd.get("material_ids") == "素材" and len(pd) >= 60,
                      f"GET /tools 交出参数标签单源表（{len(pd)} 条），界面那张退为兜底")
                groups = groups["groups"]
                item = next(t for g in groups.values() for t in g
                            if t["name"] == "split_shots")
                check(item["name"] == "split_shots" and item["name_display"] == "镜头切分",
                      "GET /tools 双轨：name 仍是键，另挂 name_display")
                check("split_shots" not in item["desc"] and "镜头切分 的表" in item["desc"],
                      "工具说明里的机器名换成中文")

                sync = c8.post("/chat/sync", json={"conversation_id": "hz",
                                                   "message": "跑一遍"}, headers=hdr)
                check(sync.json()["answer"] == "已经跑完 镜头切分，再看 成片渲染。",
                      "POST /chat/sync 的回答过同一个出口")
                hist = asyncio.run(storage.messages.history(uid, "hz"))
                check("split_shots" in hist[-1]["content"],
                      "库里落的仍是原话（出口只改「发出去」的那一份）")

                bad = c8.get("/runs/split_shots/history", headers=hdr)
                check(bad.status_code == 404 and set(bad.json()) == {"detail"},
                      "报错形状与状态码不变（仍是 {\"detail\": …} + 404）")
                check(bad.json()["detail"] == "没有这个执行记录：镜头切分",
                      "报错文案与成功响应共用同一个出口（错误通道不是漏网口）")
        finally:
            set_catalog(ToolCatalog())

        # 词表为空（离线装配 / Storyline 未连通）：一个字都不改，也不挂旁路
        storage9 = storage_in(td)
        app9 = create_app(make_agent(storage9, answer="答案是 split_shots"),
                          InMemoryMessageQueue(), storage=storage9)
        with TestClient(app9) as c9:
            _, hdr9 = register(c9)
            check(not get_catalog(), "词表已复位为空")
            check(c9.post("/chat/sync", json={"conversation_id": "h9", "message": "x"},
                          headers=hdr9).json()["answer"] == "答案是 split_shots",
                  "空词表 → 直通，不静默改字")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    main()
