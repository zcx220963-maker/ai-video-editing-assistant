# -*- coding: utf-8 -*-
"""渲染代查的**真机**冒烟：模型被告知不要轮询，成片卡仍要如期落地。

跑法（需 docker compose up -d，.env 五项已填，库里已有任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b15_render_follow_smoke.py

为什么需要这一条：b9 钉的是「模型收到 queued + hint 之后会不会自己去查」——那是模型行为，
换提示词或换情绪就塌。#26 把同一件事改成硬保证：``AgentOnceRun._follow_inflight_renders``
在工具回合结束后**自己**轮 ``render_status`` 到终态。于是判据反过来才有效：**明令模型别查**，
看卡片是不是照样出现、出现的那次轮询是不是循环发起的（工具调用 id 前缀 ``render_follow_``）。

自己起自己的端口与工作区，不动常驻服务；token 与密钥只按存在/长度报告，绝不打印值；
跑完把自己造的行删干净并复核零残留（共享曲库身份只回删自己那几条音轨）。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
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

from agent_framework.checkpoint import rebuild  # noqa: E402
from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from b6_fork_smoke import (Service, bearer, borrow_key, credit_gate,  # noqa: E402
                           free_port)
from b7_bgm_rerun_smoke import BGM_USER, make_clip, make_tone, put_bgm  # noqa: E402
from storyline_server import mediaops  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b15_{STAMP}"
TRACK = f"代查冒烟配乐_{STAMP}.wav"
RUN_WAIT = 1500.0
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


def sid_of(user: str) -> str:
    return f"u:{user}:c:{CONV}"


def _field(m: Any, key: str) -> Any:
    return m.get(key) if isinstance(m, dict) else getattr(m, key, None)


def _loads(raw: Any) -> dict:
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) else {}


async def render_tool_msgs(st, run_ids: list[str]) -> list[dict]:
    """链上所有 render_status / render_video 的 tool 消息：带调用 id，用来分清谁发起的。"""
    out: list[dict] = []
    for rid in [r for r in run_ids if r]:
        for m in rebuild(await st.checkpoints.load_entries(rid)):
            if _field(m, "role") != "tool":
                continue
            name = str(_field(m, "name") or "")
            if not (name.endswith("render_video") or name.endswith("render_status")):
                continue
            cid = str(_field(m, "tool_call_id") or "")
            body = _loads(_field(m, "content"))
            out.append({"id": cid, "name": name, "body": body, "run": rid})
    return out


async def runs_of(c, tok) -> list[dict]:
    return ((await c.get(f"/convs/{CONV}/runs", headers=bearer(tok))).json() or {}).get("runs") or []


async def wait_run(c, tok, run_id, timeout=RUN_WAIT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await c.get(f"/runs/{run_id}", headers=bearer(tok))
        if r.status_code == 200:
            row = (r.json() or {}).get("run") or {}
            if row.get("status") in ("completed", "failed", "superseded"):
                return row
        await asyncio.sleep(2)
    return {}


async def wait_turn_idle(c, tok, timeout=RUN_WAIT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = await runs_of(c, tok)
        except Exception:  # noqa: BLE001 - 服务重启中的瞬时失败
            rows = []
        if not any(r.get("status") == "running" for r in rows):
            return True
        await asyncio.sleep(2)
    return False


async def main() -> int:
    # 模型额度是这些断言的前提：没额度时 run 会在 iteration=0 就 failed，
    # 十几条断言一起 FAIL，那是环境不是缺陷——先闸掉再说。
    if (code := await credit_gate()):
        return code
    tmp = Path(tempfile.mkdtemp(prefix="b15_follow_"))
    port_main, port_story = free_port(), free_port()
    base = f"http://127.0.0.1:{port_main}"

    # 临时 Storyline：换端口 + 临时工作区/缓存 + 内联等待关成 0（逼出「提交只回句柄」）
    src_cfg = (REPO / "examples" / "storyline" / "config.toml").read_text(encoding="utf-8")
    cfg = re.sub(r"(?m)^port\s*=\s*\d+\s*$", f"port = {port_story}", src_cfg)
    cfg = re.sub(r'(?m)^workspace_root\s*=.*$',
                 f'workspace_root = "{(tmp / "ws").as_posix()}"', cfg)
    cfg = re.sub(r'(?m)^cache_root\s*=.*$',
                 f'cache_root = "{(tmp / "cache").as_posix()}"', cfg)
    cfg = re.sub(r"(?m)^\[capabilities\]\s*$",
                 "[capabilities]\nrender_grace_sec = 0", cfg, count=1)
    cfg_path = tmp / "storyline.toml"
    cfg_path.write_text(cfg, encoding="utf-8")
    import tomllib
    got = tomllib.loads(cfg).get("capabilities", {}).get("render_grace_sec")
    if not check(got == 0, f"临时 config 把 render_grace_sec 关成 0：实得 {got!r}"):
        return 1

    story = Service(["run_storyline.py", "--config", str(cfg_path)], "storyline")
    main_svc = Service(["run_server.py", "--storage", "pg_minio", "--port", str(port_main),
                        "--no-mcp", "--storyline-config", str(cfg_path),
                        "--static-dir", "", "--no-resume", "--max-iterations", "40"], "main")
    story.start()
    st = build_storage("pg_minio")
    await st.start()
    user = token = ""
    run_id = ""
    all_runs: list[str] = []
    mat_ids: list[str] = []
    other_user = ""
    frames: list[dict] = []
    try:
        if not await story.wait_port(port_story):
            check(False, "临时 Storyline 起来了")
            return 1
        main_svc.start()
        if not await main_svc.wait_ready(base):
            check(False, "主服务起来了")
            return 1
        check("已接入" in main_svc.log and "未连通" not in main_svc.log,
              "主服务接上了剪辑能力（非「本服务无剪辑」降级）")

        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            reg = (await c.post("/register", json={"device_name": "b15"})).json()
            user, token = reg["user_id"], reg["token"]
            key = await borrow_key(st)
            if not check(bool(key), "借到一把模型密钥（值不打印）"):
                return 1
            check((await c.post("/settings/api-key", headers=bearer(token),
                                json={"api_key": key})).status_code == 200,
                  "页面同款写入口配上密钥")

            bgm = await put_bgm(st, make_tone(tmp / TRACK, 330))
            mat_ids.append(bgm["material_id"])
            import edge_tts
            speech = tmp / "口播.mp3"
            await edge_tts.Communicate("第一句开场，第二句铺垫，最后一句收尾。",
                                       "zh-CN-XiaoxiaoNeural").save(str(speech))
            dur = float(mediaops.probe(speech)["duration"]) + 1.0
            clip = make_clip(tmp / "代查冒烟.mp4", speech, dur)
            up = (await c.post(f"/upload?conversation_id={CONV}&filename={clip.name}",
                               content=clip.read_bytes(),
                               headers={**bearer(token), "content-type": "video/mp4"})).json()
            mid = up.get("material_id") or ""
            mat_ids.append(mid)
            if not check(bool(mid), f"素材上传入库：{mid}"):
                return 1

            ws_url = f"ws://127.0.0.1:{port_main}/ws/{CONV}?token={token}"
            async with websockets.connect(ws_url, max_size=None) as ws:
                check(json.loads(await ws.recv()).get("type") == "connected", "WS 已连接")
                stop = asyncio.Event()

                async def pump():
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:  # noqa: BLE001 - 连接随冒烟结束一起关
                            return
                        frames.append(json.loads(raw))

                task = asyncio.create_task(pump())
                q = await c.post("/chat", headers=bearer(token), json={
                    "conversation_id": CONV,
                    "message": (f"用附件 {mid} 完整剪一条短片并直接出片：保留原声、配字幕、"
                                f"背景音乐用《{TRACK.rsplit('.', 1)[0]}》，"
                                "调用 select_BGM 时把 query 显式填成这个完整歌名，"
                                "标题就叫「代查冒烟」。"
                                "最后一步调用 render_video；它若回 queued 或 running，"
                                "**不要**再调用 render_status，也不要重复调用 render_video，"
                                "直接用一句话告诉我已提交就收尾。"),
                    "attachments": [mid]})
                q.raise_for_status()
                run_id = q.json()["run_id"]
                row = await wait_run(c, token, run_id)
                await wait_turn_idle(c, token)
                await asyncio.sleep(1.0)
                stop.set()
                try:
                    await asyncio.wait_for(task, timeout=15)
                except Exception:  # noqa: BLE001
                    task.cancel()
                all_runs = [str(r.get("run_id") or "") for r in await runs_of(c, token)]

        short = [str(f.get("tool") or "").replace("storyline_", "")
                 for f in frames if f.get("type") == "tool_call"]
        print(f"  [本轮 run] {run_id} → {(row or {}).get('status')}；[工具帧序] {short}",
              flush=True)
        check("render_video" in short, "模型提交了渲染（render_video 在工具帧里）")

        msgs = await render_tool_msgs(st, all_runs)
        submitted = [m for m in msgs if m["name"].endswith("render_video")]
        first = (submitted or [{}])[0].get("body") or {}
        fr = first.get("render") or {}
        check(first.get("output") in (None, {}, "")
              and fr.get("status") in ("queued", "running"),
              f"提交那次只回句柄（render.status={fr.get('status')!r}）")

        followed = [m for m in msgs if m["id"].startswith("render_follow_")]
        by_model = [m for m in msgs if m["name"].endswith("render_status")
                    and not m["id"].startswith("render_follow_")]
        check(len(followed) >= 1,
              f"循环代查了 render_status（{len(followed)} 次，id 前缀 render_follow_）")
        check(by_model == [],
              f"模型确实没自己去查（它发起的 render_status {len(by_model)} 条）")
        seq = [str((m["body"].get("render") or {}).get("status")) for m in followed]
        done = [m for m in followed if (m["body"].get("render") or {}).get("status") == "done"]
        check(bool(done) and (done[-1]["body"].get("output") or {}).get("video"),
              f"代查轮到的终态是 done 且带回成片对象键：{seq}")
        check(all((m["body"].get("render") or {}).get("status") in ("done", "failed")
                  for m in followed),
              f"进上下文的只有终态（中间态不拼消息）：{seq}")

        media = [f for f in frames if f.get("type") == "media"]
        check(len(media) >= 1,
              f"成片卡在模型没轮询的这一步仍回投了（WS media 帧 {len(media)} 条）")

        # 持久链接：历史口把 qa.parts 里的 media 片段现签回可播卡片（与 WS 帧同形状）
        async with httpx.AsyncClient(base_url=base, timeout=60) as c2:
            items = ((await c2.get(f"/convs/{CONV}/messages",
                                   headers=bearer(token))).json() or {}).get("messages") or []
            views = [m for r in items for m in (r.get("media") or [])]
            check(bool(views) and "://" in str(views[-1].get("media_url")),
                  f"持久链接落进本轮 assistant 行，刷新后仍可回放（直链只报长度 "
                  f"{len(str((views or [{}])[-1].get('media_url') or ''))}，值不打印）")
            want_art = str((done[-1]["body"].get("artifact_id") or "") if done else "")
            check(bool(want_art) and any(str(v.get("artifact_id") or "") == want_art
                                         for v in views),
                  f"历史里那条指向代查终态的同一产物作用域：{want_art or '（无终态）'}")

        sid = sid_of(user)
        latest: dict = {}
        for _ in range(140):
            rows = await st.db.select("render_jobs", where={"session_id": sid})
            latest = (max(rows, key=lambda r: str(r.get("updated_at") or ""))
                      if rows else {})
            if latest.get("status") in ("done", "failed"):
                break
            await asyncio.sleep(3)
        check(latest.get("status") == "done" and bool(latest.get("video_object_key")),
              f"render_jobs 终态：{ {k: latest.get(k) for k in ('status', 'stage', 'percent')} }")
        return 0 if not FAILS else 1
    finally:
        main_svc.kill()
        story.kill()
        residue = await _cleanup(st, user, other_user, all_runs or [run_id], mat_ids)
        await st.close()
        print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
        print(f"自建数据残留复核：{residue}", flush=True)
    return 1


async def _cleanup(st, user: str, other_user: str, runs: list[str],
                   mat_ids: list[str]) -> str:
    for r in [x for x in runs if x]:
        try:
            await st.checkpoints.drop(r)
        except Exception:  # noqa: BLE001
            pass
    sid = sid_of(user) if user else ""
    for m in [x for x in mat_ids if x]:
        row = await st.db.get_by_pk("materials", {"id": m})
        if row and row.get("object_key"):
            try:
                await st.objects.delete(row["object_key"])
            except Exception:  # noqa: BLE001
                pass
        for owner in (user, BGM_USER):
            try:
                await st.materials.drop(owner, m)
            except Exception:  # noqa: BLE001
                pass
    if sid:
        await st.db.delete("artifacts", where={"session_id": sid})
        await st.db.delete("render_jobs", where={"session_id": sid})
    if user:
        await st.conversations.drop(user, CONV)
        await st.secrets.drop(user, API_KEY_NAME)
        await st.db.delete("users", where={"id": user})
    if other_user:
        await st.secrets.drop(other_user, API_KEY_NAME)
        await st.db.delete("users", where={"id": other_user})
    # 成片对象也自己收：桶里的键把作用域的 ':' 换成了 '_'，所以按会话 id 子串认。
    # 不删的话每跑一次就在桶里留一部片——库里查得到行、桶里查不到字节，比留行更难解释。
    left_objs: list[str] = []
    try:
        for o in await st.objects.list_prefix("renders/"):
            if CONV in o.key:
                try:
                    await st.objects.delete(o.key)
                except Exception:  # noqa: BLE001
                    left_objs.append(o.key)
    except Exception as e:  # noqa: BLE001
        print(f"  成片对象清理失败：{e}")
    left_runs = 0
    for r in [x for x in runs if x]:
        if await st.checkpoints.load(r) is not None:
            left_runs += 1
    left_mats = 0
    for m in [x for x in mat_ids if x]:
        if await st.db.get_by_pk("materials", {"id": m}):
            left_mats += 1
    return (f"run {left_runs} / 产物 "
            f"{await st.db.count('artifacts', where={'session_id': sid}) if sid else 0} / "
            f"渲染任务 "
            f"{await st.db.count('render_jobs', where={'session_id': sid}) if sid else 0} / "
            f"素材 {left_mats} / 成片对象 {len(left_objs)} / 身份 "
            f"{'未清' if user and await st.db.get_by_pk('users', {'id': user}) else '已清'}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
