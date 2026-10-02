# -*- coding: utf-8 -*-
"""多副本回投广播的真机冒烟：帧产生在副本 A、浏览器 WS 挂在副本 B。

跑法（需 docker compose up -d redis，.env 五项已填，库里已有任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b11_broadcast_smoke.py

为什么必须真机再跑一遍：离线用例（tests/test_outbound_broadcast.py）里 broker 是假的。
「同一 topic+group 只能有一个订阅者」「Redis 频道真扇出到多个进程」「帧只被一个实例消费
掉」这三件事只有在两个真进程 + 一个真 broker 上才同时成立。

同一个场景跑两段对照：
  ① 开广播（--broadcast-redis-url）：B 的 WS 收到 A 那一轮产出的 delta/stream_end/answer，
     且挂着连接的 B 攒下环形缓冲，重连能补看进度。
  ② 不开广播（单副本直连）：同一拓扑下 B 一条执行帧也收不到——而那一轮确实在 A 上跑完了、
     答案也真进了共享存储（B 的 HTTP 口读得到）。差距是真的，①里补上它的就是广播那一跳。
     「那轮真跑了」必须先钉住（/chat 200 且带回 run_id → 终态 completed → 共享存储读得到正文），
     否则「B 收不到帧」可能只是那轮根本没跑：第一版就被自己的 conv 复用骗过一次——两段共用一个
     会话 id，而会话归一个用户所有，对照段的 /chat 直接吃了 409。所以每段各用自己的 conv。

自己起自己的端口，不动常驻的 :8000/:8001；token 与密钥只按存在/长度报告，绝不打印值；
跑完把自己造的行删干净并复核零残留。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".smoke"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import websockets

from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from b6_fork_smoke import Service, bearer, borrow_key, free_port  # noqa: E402

STAMP = int(time.time())
REDIS_URL = os.getenv("BROADCAST_REDIS_URL", "redis://localhost:6379/0")
ANSWER_MARK = "跨实例广播"
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


def argv(port: int, broadcast: bool) -> list[str]:
    """一个副本的启动命令：无剪辑链路、无常驻恢复，只留「一句话回答」这条最短执行路径。"""
    out = ["run_server.py", "--storage", "pg_minio", "--port", str(port),
           "--no-mcp", "--no-storyline", "--static-dir", "", "--no-resume",
           "--max-iterations", "3"]
    if broadcast:
        out += ["--broadcast-redis-url", REDIS_URL]
    return out


async def redis_alive() -> bool:
    import redis.asyncio as aioredis
    try:
        r = aioredis.from_url(REDIS_URL, socket_connect_timeout=3)
        pong = await r.ping()
        await r.aclose()
        return bool(pong)
    except Exception as exc:  # noqa: BLE001
        print(f"[redis] {REDIS_URL} 连不上：{exc}", flush=True)
        return False


async def scenario(st, broadcast: bool) -> dict[str, Any]:
    """起两个副本 → 注册身份 → WS 挂 B → 消息投 A → 收 B 的帧与库里的终态。"""
    tag = "① 开广播" if broadcast else "② 不开广播（对照）"
    print(f"\n=== {tag} ===", flush=True)
    # 会话归一个用户所有：两段各用自己那份 conv，否则对照段的 /chat 会被 409 拒在门外，
    # 「B 收不到帧」就成了假阳性——收不到是因为那轮根本没跑。
    conv = f"c_b11_{'on' if broadcast else 'off'}_{STAMP}"
    port_a, port_b = free_port(), free_port()
    base_a, base_b = f"http://127.0.0.1:{port_a}", f"http://127.0.0.1:{port_b}"
    svc_a = Service(argv(port_a, broadcast), f"A-{'' if broadcast else 'ctrl-'}{port_a}")
    svc_b = Service(argv(port_b, broadcast), f"B-{'' if broadcast else 'ctrl-'}{port_b}")
    obs: dict[str, Any] = {"user": "", "frames": [], "replay": [], "run_status": "",
                           "history": [], "answer_text": "", "sid": "", "run_id": "",
                           "chat_http": "", "run_http": "", "run_body": "",
                           "hist_http": "", "log_a": "", "log_b": "", "conv": conv}
    svc_a.start(); svc_b.start()
    try:
        if not (await svc_a.wait_ready(base_a) and await svc_b.wait_ready(base_b)):
            check(False, f"{tag}：两个副本都起来并 /health 通过")
            return obs
        hb_rows = await st.db.select("scheduled_jobs", where={"name": "heartbeat"})
        check(len(hb_rows) == 1,
              f"{tag}：两个副本起来后 heartbeat 行**恰好**一条（既不被人删掉也不各建一条）："
              f"{len(hb_rows)} 条")
        a_on, b_on = ("回投广播: 经 Redis" in svc_a.log), ("回投广播: 经 Redis" in svc_b.log)
        if broadcast:
            check(a_on and b_on, f"{tag}：两个副本的启动日志都宣告走广播回投")
        else:
            check("回投广播: 关闭" in svc_a.log and "回投广播: 关闭" in svc_b.log,
                  f"{tag}：两个副本都宣告单副本直连（不订阅频道）")

        async with httpx.AsyncClient(base_url=base_a, timeout=60) as ca, \
                httpx.AsyncClient(base_url=base_b, timeout=60) as cb:
            reg = (await ca.post("/register", json={"device_name": "b11"})).json()
            obs["user"], token = reg["user_id"], reg["token"]
            key = await borrow_key(st)
            if not check(bool(key), f"{tag}：借到一把模型密钥（值不打印，只记长度 {len(key)}）"):
                return obs
            check((await ca.post("/settings/api-key", headers=bearer(token),
                                 json={"api_key": key})).status_code == 200,
                  f"{tag}：页面同款写入口配上密钥")

            frames: list[dict] = []
            ws_url = f"ws://127.0.0.1:{port_b}/ws/{conv}?token={token}"
            async with websockets.connect(ws_url, max_size=None) as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), 20))
                check(first.get("type") == "connected", f"{tag}：浏览器连上副本 B")
                frames.append(first)
                obs["sid"] = first.get("session_id")

                stop = asyncio.Event()

                async def pump():
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:  # noqa: BLE001 - 连接随场景结束一起关
                            return
                        frames.append(json.loads(raw))

                task = asyncio.create_task(pump())
                r = await ca.post("/chat", headers=bearer(token), json={
                    "conversation_id": conv,
                    "message": f"不要调用任何工具，只回一句中文：{ANSWER_MARK}"})
                obs["chat_http"] = f"{r.status_code} {r.text[:200]}"
                obs["run_id"] = r.json().get("run_id") or ""

                deadline = time.time() + 90
                while time.time() < deadline and not any(f.get("type") == "answer" for f in frames):
                    await asyncio.sleep(0.5)
                stop.set()
                await task
                obs["frames"] = list(frames)

                # 不管 B 收没收到，都在 A 上把这轮跑到终态——对照段的判据要拿它当证据
                deadline = time.time() + 180
                while time.time() < deadline:
                    pr = await ca.get(f"/runs/{obs['run_id']}", headers=bearer(token))
                    obs["run_http"] = str(pr.status_code)
                    row = pr.json() or {}
                    obs["run_status"] = ((row.get("run") or {}).get("status")) or ""
                    obs["run_body"] = str(row)[:300]
                    if obs["run_status"] in ("completed", "failed", "superseded"):
                        break
                    await asyncio.sleep(2)
                hr = await cb.get(f"/convs/{conv}/messages", headers=bearer(token))
                obs["hist_http"] = f"{hr.status_code} {hr.text[:200]}"
                hist = hr.json() or {}
                obs["history"] = hist.get("messages") or []
                # 历史行的正文字段就叫 text（content 是消息里另一回事），只读 content 会拿到空串
                obs["answer_text"] = str(next(
                    (m.get("text") or m.get("content") or "" for m in reversed(obs["history"])
                     if m.get("role") == "assistant"), ""))

            if broadcast:
                async with websockets.connect(ws_url, max_size=None) as ws2:
                    got: list[dict] = []
                    deadline = time.time() + 10
                    while time.time() < deadline:
                        try:
                            got.append(json.loads(await asyncio.wait_for(ws2.recv(), timeout=1.5)))
                        except asyncio.TimeoutError:
                            break
                        except Exception:  # noqa: BLE001
                            break
                    obs["replay"] = got[1:]      # 首帧是 connected，其后都是补看的缓冲
    finally:
        obs["log_a"] = svc_a.log[-3000:]
        obs["log_b"] = svc_b.log[-1000:]
        svc_a.kill(); svc_b.kill()
    return obs


async def _cleanup(st, runs: list[dict[str, Any]]) -> str:
    """删掉自己造的身份（CASCADE 带走会话/消息/密钥），再复核残留。"""
    for obs in runs:
        u, conv = obs.get("user") or "", obs.get("conv") or ""
        if not u:
            continue
        try:
            if conv:
                await st.conversations.drop(u, conv)
            await st.secrets.drop(u, API_KEY_NAME)
            await st.db.delete("users", where={"id": u})
        except Exception as exc:  # noqa: BLE001
            print(f"[清理] {u} 删除失败：{exc}", flush=True)
    left_users = 0
    for obs in runs:
        u = obs.get("user") or ""
        if u and await st.db.get_by_pk("users", {"id": u}):
            left_users += 1
    left_convs = 0
    for obs in runs:
        u = obs.get("user") or ""
        if u and await st.db.count("conversations", where={"user_id": u}):
            left_convs += 1
    return f"身份残留 {left_users} / 会话残留 {left_convs}"


async def main() -> int:
    if not await redis_alive():
        print(f"Redis 不可达（{REDIS_URL}）：多副本广播没法真机验，先 docker compose up -d redis。")
        return 2
    st = build_storage("pg_minio")
    await st.start()
    runs: list[dict[str, Any]] = []
    try:
        # 前提：两个副本同时冷启动——各自跑一遍 ensure_schema。
        # b11 第一版在这里露出过一处单实例口径：migrations.sql 的 DROP+ADD 成对语句在并发下
        # 撞 DuplicateObjectError，第二个实例启动直接失败。（心跳那条另算——它由服务 lifespan
        # 登记，不在这两个裸存储句柄的启动路径里，所以放到场景段去核对。）
        pair = [build_storage("pg_minio"), build_storage("pg_minio")]
        try:
            await asyncio.gather(*(s.start() for s in pair))
            cold_err = ""
        except Exception as exc:  # noqa: BLE001
            cold_err = f"{type(exc).__name__}: {exc}"
        for s in pair:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
        check(not cold_err, f"两个副本并发跑 ensure_schema 都不抛穿（{cold_err or '无异常'}）")

        on = await scenario(st, broadcast=True)
        runs.append(on)
        types = [f.get("type") for f in on["frames"]]
        check(types[:1] == ["connected"], f"开广播：B 侧首帧是 connected（{types}）")
        check("delta" in types, "开广播：A 产出的 delta 到了挂在 B 的浏览器")
        check("stream_end" in types and types[-1:] == ["answer"],
              "开广播：stream_end 与收尾 answer 同样跨实例到达")
        ans = next((f for f in on["frames"] if f.get("type") == "answer"), {})
        check(ANSWER_MARK in str(ans.get("answer") or ""),
              "B 收到的 answer 就是 A 那一轮真跑出来的答复")
        check(all(f.get("session_id") == on.get("sid") for f in on["frames"]),
              f"跨实例投递没改回投键：每帧仍带 {on.get('sid')}")
        check(on.get("run_status") == "completed", f"开广播：A 上这轮跑到终态 {on.get('run_status')}")
        check(ANSWER_MARK in str(on.get("answer_text")),
              f"开广播：同一份答案也在共享存储里，B 的 HTTP 口读得到（{on.get('answer_text')[:40]}…）")
        check(len(on.get("replay") or []) >= 2,
              f"开广播：B 攒了环形缓冲，重连补看 {len(on.get('replay') or [])} 帧")

        off = await scenario(st, broadcast=False)
        runs.append(off)
        off_types = [f.get("type") for f in off["frames"]]
        # 先钉「那轮真跑了」：上一版这里被自己的 conv 复用骗过——/chat 409 回绝，
        # B 自然一条帧也收不到，对照就变成假阳性。
        check(off["chat_http"].startswith("200") and bool(off.get("run_id")),
              f"对照：/chat 在 A 上真被受理并起了 run（不是被拒才‘收不到’）"
              f"｜ {off['chat_http'][:40]} run={off.get('run_id') or '空'}")
        check("answer" not in off_types and off_types == ["connected"],
              f"对照（不开广播）：B 的浏览器只拿到 connected，一条执行帧也没收到（{off_types}）")
        ok_exec = (off.get("run_status") == "completed"
                   and ANSWER_MARK in str(off.get("answer_text")))
        check(ok_exec,
              f"对照：那一轮确实在 A 上跑完、答案也真进了共享存储（丢的是回投不是执行）"
              f"｜ run={off.get('run_id') or '空'}({off.get('run_status') or '无终态'}, "
              f"/runs {off.get('run_http')}) chat={off.get('chat_http')} "
              f"历史={len(off.get('history') or [])}条 读={off.get('hist_http')[:60]}")
        check(len(off.get("history") or []) >= 2,
              f"对照：从副本 B 的 HTTP 口读得到同一份会话历史"
              f"（{len(off.get('history') or [])} 条，读回 {off.get('hist_http')[:80]}）")
        if not ok_exec:
            print("\n—— 对照段证据：副本 A 日志尾部 ——\n" + str(off.get("log_a")), flush=True)
    finally:
        residue = await _cleanup(st, runs)
        await st.close()
    ok = not FAILS
    print("\n" + ("SMOKE PASSED" if ok else f"SMOKE FAILED：{FAILS}"), flush=True)
    print(f"自建数据残留复核：{residue}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
