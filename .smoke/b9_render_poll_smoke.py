# -*- coding: utf-8 -*-
"""渲染轮询的**模型侧**真机冒烟：把 render_grace_sec 关成 0，逼出「提交即回句柄」，看模型是否真去 render_status 追终态。

跑法（需 docker compose up -d，.env 五项已填，库里已有任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b9_render_poll_smoke.py

为什么单独一条：b3 直连冒烟钉的是服务端那两个口的形状，b7 钉的是整链出片与分叉；
但「模型收到 queued + hint 之后会不会继续轮询」是**模型行为**，只有把内联等待关掉才验得出来
——5～9 秒的短片在 20s grace 内就 done，那条路等于从没被走过。

自己起自己的端口与工作区，不动常驻服务；token 与密钥只按存在/长度报告，绝不打印值；
跑完把自己造的行删干净并复核零残留（共享曲库身份 u-bgm-library 只回删自己那几条音轨）。
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
from b6_fork_smoke import Service, bearer, borrow_key, free_port  # noqa: E402
from b7_bgm_rerun_smoke import BGM_USER, make_clip, make_tone, put_bgm  # noqa: E402
from storyline_server import mediaops  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b9_{STAMP}"
TRACK = f"轮询冒烟配乐_{STAMP}.wav"
RUN_WAIT = 1200.0
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


async def tool_results_in_chain(st, run_ids: list[str], suffix: str) -> list[dict]:
    """模型真正收到的那些返回值：checkpoint 链上 tool 消息的全文（不截断）。

    「模型有没有去轮询、轮询到的那一版有没有成片」是模型侧行为，判据必须取它读到的
    原文；WS 帧只是同一事件的展示副本。
    """
    out: list[dict] = []
    for rid in [r for r in run_ids if r]:
        for m in rebuild(await st.checkpoints.load_entries(rid)):
            if _field(m, "role") != "tool" or not str(_field(m, "name") or "").endswith(suffix):
                continue
            raw = _field(m, "content")
            try:
                raw = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                raw = {}
            out.append(raw if isinstance(raw, dict) else {})
    return out


async def runs_of(c, tok) -> list[dict]:
    r = await c.get(f"/convs/{CONV}/runs", headers=bearer(tok))
    return (r.json() or {}).get("runs") or []


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
    """等这条会话彻底没有在途 run（模型中途分叉时，发起那条会先到终态而子 run 还在跑）。"""
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
    tmp = Path(tempfile.mkdtemp(prefix="b9_poll_"))
    port_main, port_story = free_port(), free_port()
    base = f"http://127.0.0.1:{port_main}"

    # 临时 Storyline：换端口 + 临时工作区/缓存 + 把内联等待关成 0（这一步是本冒烟的全部前提）
    src_cfg = (REPO / "examples" / "storyline" / "config.toml").read_text(encoding="utf-8")
    cfg = re.sub(r"(?m)^port\s*=\s*\d+\s*$", f"port = {port_story}", src_cfg)
    cfg = re.sub(r'(?m)^workspace_root\s*=.*$',
                 f'workspace_root = "{(tmp / "ws").as_posix()}"', cfg)
    cfg = re.sub(r'(?m)^cache_root\s*=.*$',
                 f'cache_root = "{(tmp / "cache").as_posix()}"', cfg)
    # 只认独立成行的节名：注释里也写着 “[capabilities]”，无锚定的替换会把开关插进注释。
    cfg = re.sub(r"(?m)^\[capabilities\]\s*$",
                 "[capabilities]\nrender_grace_sec = 0", cfg, count=1)
    cfg_path = tmp / "storyline.toml"
    cfg_path.write_text(cfg, encoding="utf-8")
    # 判据取解析结果而不是字符串包含：插错位置（注释里）字符串照样“包含”，服务却起不来。
    import tomllib
    got = tomllib.loads(cfg).get("capabilities", {}).get("render_grace_sec")
    if not check(got == 0, f"临时 config 把 render_grace_sec 关成 0（提交即回句柄，"
                           f"没有内联等待）：实得 {got!r}"):
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
              "主服务接上了剪辑能力（Storyline 已接入，非「本服务无剪辑」降级）")

        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            reg = (await c.post("/register", json={"device_name": "b9"})).json()
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
            clip = make_clip(tmp / "轮询冒烟.mp4", speech, dur)
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
                                "标题就叫「轮询冒烟」。"
                                "调用 render_video 之后如果它回的是 queued 或 running，"
                                "就继续调用 render_status 查到 done 再收尾。"),
                    "attachments": [mid]})
                q.raise_for_status()
                run_id = q.json()["run_id"]
                row = await wait_run(c, token, run_id)
                await wait_turn_idle(c, token)
                await asyncio.sleep(1.0)      # 让尾部帧落地
                stop.set()
                try:
                    await asyncio.wait_for(task, timeout=15)
                except Exception:  # noqa: BLE001
                    task.cancel()
                all_runs = [str(r.get("run_id") or "") for r in await runs_of(c, token)]

        short = [str(f.get("tool") or "").replace("storyline_", "")
                 for f in frames if f.get("type") == "tool_call"]
        print(f"  [本轮 run] {run_id} → {(row or {}).get('status')}；[工具序] {short}",
              flush=True)
        check("render_video" in short, "模型提交了渲染（render_video 在工具序里）")

        submitted = await tool_results_in_chain(st, all_runs, "render_video")
        first = submitted[0] if submitted else {}
        first_render = first.get("render") or {}
        head = f"render.status={first_render.get('status')!r}"
        check(first.get("output") in (None, {}, "")
              and first_render.get("status") in ("queued", "running"),
              f"grace=0 生效：提交那次不回成片，只回句柄（{head}）")
        check(bool("render_status" in str(first.get("hint") or "")),
              f"未终态的返回值把模型指向 render_status：{str(first.get('hint'))[:70]}")

        polls = await tool_results_in_chain(st, all_runs, "render_status")
        check(len(polls) >= 1, f"模型真的去轮询了（render_status 全文 {len(polls)} 条）")
        seq = [str((p.get("render") or {}).get("status")) for p in polls][:8]
        reached = [p for p in polls if (p.get("render") or {}).get("status") == "done"]
        check(bool(reached) and (reached[-1].get("output") or {}).get("video"),
              f"轮询里出现 done 且带回成片对象键：{seq}")
        check(any(f.get("type") == "media" for f in frames),
              f"WS 里出现成片播放卡（轮询到 done 那一轮回投的；本轮 {len(frames)} 帧）")

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
        art = str(latest.get("artifact_id") or "_default")

        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            h = (await c.get(f"/render_status?artifact_id={art}&conv_id={CONV}",
                             headers=bearer(token))).json()
            check(h.get("status") == "done" and "://" in str(h.get("media_url"))
                  and h.get("video") == latest.get("video_object_key"),
                  f"GET /render_status 与工具同形状（直链只报长度 "
                  f"{len(str(h.get('media_url')))}，值不打印）")
            other = (await c.post("/register", json={"device_name": "b9-other"})).json()
            other_user = other.get("user_id") or ""
            r404 = await c.get(f"/render_status?artifact_id={art}&conv_id={CONV}",
                               headers=bearer(other["token"]))
            check(r404.status_code == 404, f"别人的渲染查不到：{r404.status_code}")
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
    """收干净自己造的东西并复核零残留；共享曲库身份只回删自己那条音轨，不动它的会话。"""
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
            f"素材 {left_mats} / 身份 "
            f"{'未清' if user and await st.db.get_by_pk('users', {'id': user}) else '已清'}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
