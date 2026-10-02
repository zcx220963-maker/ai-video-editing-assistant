# -*- coding: utf-8 -*-
"""B5 真机冒烟（spec §7）：身份只由 /register 签发，user_id 一律从 token 反查，会话面按 owner 过滤。

跑法（需 docker compose up -d 且 .env 已填 PG_DSN / MINIO_* 五项，模型密钥按项目唯一入口配好）：
    PYTHONPATH=. python .smoke/b5_auth_smoke.py

自己起两个 run_server 子进程（--storage pg_minio，不接 MCP/Storyline/前端，端口现取），
不动开发者正在用的 :8000。第二个进程只带着第一个进程签下的那张 token 访问——
这就是「换浏览器不丢历史」在服务端的真形：本地什么都没存，历史全从 PG 读回来。
判据对着真 PG 行、真 MinIO 字节、真 WS 帧、真模型回复钉。
token 与密钥一律只按长度/是否存在报告，任何情况下都不打印值。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx
import websockets

from agent_framework.llm_openai import get_default_llm
from agent_framework.storage import build_storage, in_

REPO = Path(__file__).resolve().parents[1]
CONV = f"c_b5_{int(time.time())}"
FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


def llm_ready() -> bool:
    """模型密钥由项目唯一入口判定；只报是否存在，不报值。"""
    try:
        get_default_llm()
    except RuntimeError as exc:
        print(f"缺少模型密钥：{exc}")
        return False
    print("模型密钥已配置（值未打印）")
    return True


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    """一个 run_server 子进程（不带前端静态目录，纯 API 面）。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.log = ""
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_base(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "run_server.py", "--storage", "pg_minio",
             "--port", str(self.port), "--no-mcp", "--no-storyline", "--static-dir", ""],
            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.log += line

    async def wait_ready(self, timeout_sec: float = 90.0) -> None:
        async with httpx.AsyncClient(base_url=self.base, timeout=3) as c:
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                try:
                    if (await c.get("/health")).status_code == 200:
                        return
                except Exception:  # noqa: BLE001 - 还没起来，继续等
                    pass
                if self.proc.poll() is not None:
                    raise RuntimeError(f"服务子进程提前退出：\n{self.log[-2000:]}")
                await asyncio.sleep(0.5)
        raise RuntimeError(f"等待 /health 超时：\n{self.log[-2000:]}")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def wait_log(svc: "Service", needle: str, timeout_sec: float = 15.0) -> bool:
    """/health 通了不等于那行日志已被读管道线程攒进 svc.log —— 按文本等一会儿。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if needle in svc.log:
            return True
        await asyncio.sleep(0.2)
    return needle in svc.log


async def register(c: httpx.AsyncClient, device: str) -> tuple[str, str]:
    j = (await c.post("/register", json={"device_name": device})).json()
    return j["user_id"], j["token"]


async def wait_answer(ws, run_id: str, timeout_sec: float = 180.0) -> str:
    """等这一轮 run_id 的 answer 帧（真模型 + 真 MQ + 真 Connection Manager）。"""
    loop = asyncio.get_running_loop()
    until = loop.time() + timeout_sec
    seen: list[str] = []
    while loop.time() < until:
        try:
            fr = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        except asyncio.TimeoutError:
            print(f"  … 等待回投中（已收帧型 {seen[-6:]}）", flush=True)
            continue
        seen.append(fr.get("type", "?"))
        if fr.get("type") == "error":
            raise RuntimeError(f"链路回了 error：{fr}")
        if fr.get("type") == "answer" and fr.get("run_id") == run_id:
            return str(fr.get("answer") or "")
    raise RuntimeError(f"等待 answer 超时（帧型 {seen[-12:]}）")


async def main() -> None:
    storage = build_storage("pg_minio")          # 先构造：它会把 .env 五项落进环境变量
    if not llm_ready():
        sys.exit(2)
    await storage.start()
    print(f"存储层已连通：backend={storage.backend}")

    svc_a, svc_b = Service(), Service()
    ids: list[str] = []
    obj_keys: list[str] = []
    try:
        svc_a.start()
        await svc_a.wait_ready()
        check(await wait_log(svc_a, "已连通并完成建表校验"),
              "进程 A 启动日志：存储层校验通过")

        async with httpx.AsyncClient(base_url=svc_a.base, timeout=180) as c:
            # ---------- 1. 没有凭证什么都干不了（/health 与 /register 除外）----------
            probes = {
                "POST /chat": ("post", "/chat", {"conversation_id": CONV, "message": "x"}),
                "POST /chat/sync": ("post", "/chat/sync",
                                    {"conversation_id": CONV, "message": "x"}),
                "GET /whoami": ("get", "/whoami", None),
                "GET /sessions": ("get", "/sessions", None),
                "GET /convs": ("get", "/convs", None),
                f"GET /convs/{{id}}/messages": ("get", f"/convs/{CONV}/messages", None),
                "POST /convs/rename": ("post", "/convs/rename",
                                       {"conversation_id": CONV, "title": "t"}),
                f"DELETE /convs/{{id}}": ("delete", f"/convs/{CONV}", None),
                "POST /upload": ("post", f"/upload?conversation_id={CONV}&filename=a.mp4", None),
                "POST /fetch_media": ("post", "/fetch_media",
                                      {"conversation_id": CONV, "url": "https://x/y.mp4"}),
            }
            codes = {}
            for label, (verb, path, body) in probes.items():
                kw = {"json": body} if body is not None else {}
                codes[label] = (await getattr(c, verb)(path, **kw)).status_code
            check(all(v == 401 for v in codes.values()),
                  f"真服务上 10 个端点无凭证一律 401：{sorted(set(codes.values()))} {codes}")
            check((await c.get("/health")).status_code == 200
                  and (await c.post("/register", json={})).status_code == 200,
                  "/health 与 /register 仍不鉴权（探活与新客户端取第一份凭证）")

            rejected = False
            try:
                async with websockets.connect(f"{svc_a.ws_base}/ws/{CONV}"):
                    pass
            except Exception:  # noqa: BLE001 - 握手被拒就是异常
                rejected = True
            check(rejected, "WS 不带 ?token= 连不上（服务端不 accept 无凭证连接）")

            # ---------- 2. 签发：库里只有 sha256，明文只在响应里 ----------
            uid_a, tok_a = await register(c, "浏览器甲")
            uid_b, tok_b = await register(c, "浏览器乙")
            ids += [uid_a, uid_b]
            check(uid_a.startswith("u-") and len(tok_a) == 43 and uid_a != uid_b,
                  f"两个身份各签一张：{uid_a} / {uid_b}（token 长度 {len(tok_a)}）")
            rows = {r["id"]: r for r in await storage.db.select("users",
                                                                where={"id": in_(ids)})}
            check(rows[uid_a]["token_hash"] == hashlib.sha256(tok_a.encode()).hexdigest(),
                  "PG users.token_hash 就是 sha256(明文)")
            check(tok_a not in str(rows) and tok_b not in str(rows),
                  "整张 users 表里没有明文 token")
            who = (await c.get("/whoami", headers=bearer(tok_a))).json()
            check(who == {"user_id": uid_a}, f"/whoami 只凭 token 问出身份：{who}")

            # ---------- 3. 上传 + 带附件的一轮真对话（MQ → Agent → WS 回投）----------
            up = await c.post(f"/upload?conversation_id={CONV}&filename=海边日落.mp4",
                              content=b"B5-SMOKE-FAKE-VIDEO-BYTES",
                              headers=bearer(tok_a))
            uj = up.json()
            mid = uj.get("material_id", "")
            obj_keys = [uj["object_key"]]
            check(up.status_code == 200 and mid and uj["object_key"].startswith(
                f"users/{uid_a}/convs/{CONV}/"),
                f"/upload 归到 token 反查出的名下：{uj.get('object_key')}")
            back = await c.get(uj["url"])
            check(back.status_code == 200 and back.content == b"B5-SMOKE-FAKE-VIDEO-BYTES",
                  f"presigned 直链读回原字节（{back.status_code} {len(back.content)}B）")

            async with websockets.connect(
                    f"{svc_a.ws_base}/ws/{CONV}?token={tok_a}", max_size=None) as ws:
                hello = json.loads(await ws.recv())
                check(hello.get("type") == "connected"
                      and hello.get("session_id") == f"{uid_a}:{CONV}",
                      f"回投键的 user 段由 token 反查：{hello}")
                q = await c.post("/chat", json={
                    "conversation_id": CONV,
                    "message": f"只看附件 {mid} 的文件名，不要调用任何工具，"
                               "回一句里带上那个文件名。",
                    "attachments": [mid]}, headers=bearer(tok_a))
                run_id = q.json().get("run_id", "")
                check(q.status_code == 200 and q.json().get("status") == "queued",
                      f"/chat 入队：{q.json()}")
                answer = await wait_answer(ws, run_id)
                check("海边日落" in answer and bool(answer.strip()),
                      f"真模型按上下文里的附件事实作答：{answer[:80]}")

            # ---------- 4. 会话面：列表 / 历史回填 / 改名 / 跨 owner ----------
            convs_a = (await c.get("/convs", headers=bearer(tok_a))).json()["conversations"]
            check([x["conversation_id"] for x in convs_a] == [CONV]
                  and convs_a[0]["title"] == "新对话" and convs_a[0]["updated_at"],
                  f"/convs 只列本人这册：{convs_a}")

            hist = (await c.get(f"/convs/{CONV}/messages",
                                headers=bearer(tok_a))).json()["messages"]
            check([h["role"] for h in hist] == ["user", "assistant"],
                  f"历史回填按 seq 读出这一轮：{[h['role'] for h in hist]}")
            att = hist[0]["attachments"]
            check(len(att) == 1 and att[0]["material_id"] == mid
                  and att[0]["filename"] == "海边日落.mp4" and att[0]["url"].startswith("http"),
                  f"附件回成展示形状（含 presigned URL）：{[{k: v for k, v in a.items() if k != 'url'} for a in att]}")
            played = await c.get(att[0]["url"]) if att else None
            check(played is not None and played.status_code == 200,
                  f"回填出来的回放链接真能播：HTTP {played.status_code if played else '—'}")

            rn = await c.post("/convs/rename", json={"conversation_id": CONV,
                                                     "title": "海边 vlog"},
                              headers=bearer(tok_a))
            check(rn.status_code == 200, f"改名 200（{rn.status_code}）")
            again = await c.post("/chat/sync", json={
                "conversation_id": CONV, "message": "只回两个字：继续"},
                headers=bearer(tok_a))
            titled = (await c.get("/convs", headers=bearer(tok_a))).json()["conversations"]
            check(again.status_code == 200
                  and titled[0]["title"] == "海边 vlog",
                  f"继续对话不会把标题冲回「新对话」：{titled}")

            # B 视角：A 的会话既看不见也改不动删不掉
            check((await c.get("/convs", headers=bearer(tok_b))).json()["conversations"] == [],
                  "B 的 /convs 是空的（看不见 A 的会话）")
            check((await c.get(f"/convs/{CONV}/messages",
                               headers=bearer(tok_b))).json() == {"messages": []},
                  "B 读 A 的会话历史得到空（不是 403，不泄露存在性）")
            check((await c.post("/convs/rename", json={"conversation_id": CONV, "title": "抢"},
                                headers=bearer(tok_b))).status_code == 409,
                  "B 改 A 的会话标题回 409")
            check((await c.delete(f"/convs/{CONV}",
                                  headers=bearer(tok_b))).status_code == 409,
                  "B 删 A 的会话回 409")
            up_b = await c.post(f"/upload?conversation_id={CONV}&filename=a.mp4",
                                content=b"x", headers=bearer(tok_b))
            check(up_b.status_code == 409,
                  f"B 往 A 的会话上传素材回 409（{up_b.status_code}）")
            async with websockets.connect(
                    f"{svc_a.ws_base}/ws/{CONV}?token={tok_b}", max_size=None) as ws_b:
                hb = json.loads(await ws_b.recv())
            check(hb.get("session_id") == f"{uid_b}:{CONV}",
                  f"B 连同名 conv 也只在自己的回投键上：{hb}")

        # ---------- 5. 换浏览器 / 换进程：只带那张 token，历史全从 PG 回来 ----------
        svc_a.kill()
        check(svc_a.proc.poll() is not None, "进程 A 已停（会话与历史都留在 PG）")
        svc_b.start()
        await svc_b.wait_ready()
        async with httpx.AsyncClient(base_url=svc_b.base, timeout=180) as c2:
            check((await c2.get("/whoami", headers=bearer(tok_a))).json() == {"user_id": uid_a},
                  "新进程只凭 token 就问回同一个身份（本地没存身份）")
            listed = (await c2.get("/convs", headers=bearer(tok_a))).json()["conversations"]
            check([x["conversation_id"] for x in listed] == [CONV]
                  and listed[0]["title"] == "海边 vlog",
                  f"换进程后 /convs 接得上：{listed}")
            hist2 = (await c2.get(f"/convs/{CONV}/messages",
                                  headers=bearer(tok_a))).json()["messages"]
            check([h["role"] for h in hist2] == ["user", "assistant", "user", "assistant"],
                  f"历史一条不丢：{[(h['role'], h['text'][:14]) for h in hist2]}")
            r3 = await c2.post("/chat/sync", json={"conversation_id": CONV,
                                                   "message": "只回三个字：接得上"},
                               headers=bearer(tok_a))
            check(r3.status_code == 200 and bool(r3.json().get("answer", "").strip()),
                  f"旧 token 在新进程里继续写同一条历史：{r3.json().get('answer', '')[:30]}")
            hist3 = (await c2.get(f"/convs/{CONV}/messages",
                                  headers=bearer(tok_a))).json()["messages"]
            check(len(hist3) == 6, f"新进程写的那轮也进了同一会话：{len(hist3)} 行")
            check((await c2.get("/convs", headers=bearer(tok_b))).json()["conversations"] == [],
                  "换进程后 B 仍然看不见 A 的任何东西")

            # ---------- 6. 前端契约：DELETE 之后服务端真的没有这册 ----------
            dl = await c2.delete(f"/convs/{CONV}", headers=bearer(tok_a))
            check(dl.status_code == 200
                  and (await c2.get("/convs", headers=bearer(tok_a))).json()["conversations"] == [],
                  f"本人删自己的会话后列表为空（{dl.status_code}）")
            check(await storage.messages.history(uid_a, CONV) == [],
                  "删会话连带清掉了 messages 行")

        leftovers = await storage.db.select("materials", where={"id": mid})
        check(len(leftovers) == 1 and leftovers[0]["conv_id"] is None,
              f"素材行按 schema 留档但脱离会话（conv_id={leftovers[0]['conv_id'] if leftovers else '—'}）")
    finally:
        for svc in (svc_a, svc_b):
            try:
                svc.kill()
            except Exception as exc:  # noqa: BLE001 - 收尾阶段进程已不在无所谓
                print(f"（收尾：{exc}）")
        for key in obj_keys:
            await storage.objects.delete(key)
        if ids:
            await storage.db.delete("users", where={"id": in_(ids)})   # CASCADE 带走会话/历史/素材
        left = await storage.db.select("users", where={"id": in_(ids)}) if ids else []
        await storage.close()
        print(f"收尾：冒烟身份与其名下数据已收回（残留 {len(left)} 行）")

    print("\n全部通过：B5 身份与鉴权在真服务上成立" if FAILS == 0 else f"\n失败 {FAILS} 项")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
