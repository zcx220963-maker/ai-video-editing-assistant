# -*- coding: utf-8 -*-
"""B10 真机冒烟：**跨轮**回溯——用户第二条消息不重剪，模型自己回到**上一轮那条执行**换 BGM 重出片。

跑法（需 docker compose up -d，.env 五项已填，库里已有任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b10_cross_run_rerun_smoke.py

b7 的 ③ 验的是「同一轮里模型自己分叉」；`rerun_from` 加了 `run_id` 之后，真正的用户诉求是
「回到上次那一版换个配乐」——那一步在**另一条 run** 上，而模型手上原本没有那个 run_id。
所以这条冒烟刻意不在提示词里给 run_id：让它要么先被如实回绝、从回绝文本列出的历史执行里
捞到 id 再调一次，要么一次就命中；两条路都必须真的跨到上一轮那条 run 上。

顺带钉两件事：跨轮回溯后**本轮那条被弃用的 run 也必须让位**（否则它永远停在 running，
崩溃恢复会重播一条没人要的分支）；以及上游产物（split_shots）在跨轮之间照样逐字节复用。

自己起自己的端口与工作区，不动常驻的 :8000/:8001；token 与密钥只报存在/长度，绝不打印值；
跑完把自建的 run/产物/渲染任务/曲库行/会话/身份收回并复核零残留。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".smoke"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import websockets

from agent_framework.checkpoint import STATUS_SUPERSEDED, rebuild  # noqa: E402
from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from b6_fork_smoke import Service, bearer, borrow_key, free_port  # noqa: E402
from b7_bgm_rerun_smoke import (  # noqa: E402
    BGM_USER, _scope, cleanup, make_clip, make_tone, node_payload, put_bgm, render_done,
    timeline_with,
)
from storyline_server import mediaops  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b10_{STAMP}"
# 歌名自带本次时间戳：与历次冒烟同名会让 select_BGM 检回旧行，素材 id 判据就失效了
TRACK_A = f"跨轮A轻快_{STAMP}.wav"
TRACK_B = f"跨轮B沉重_{STAMP}.wav"
RUN_WAIT = 1800.0
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


async def _collect(st, run_ids: list[str], suffix: str) -> list[dict]:
    """模型真正读到的那些返回值：checkpoint 链上 tool 消息的全文（不是 WS 帧的 600 字符副本）。"""
    out: list[dict] = []
    for rid in [r for r in run_ids if r]:
        for m in rebuild(await st.checkpoints.load_entries(rid)):
            d = m if isinstance(m, dict) else {"role": getattr(m, "role", None),
                                               "name": getattr(m, "name", None),
                                               "content": getattr(m, "content", None)}
            if d.get("role") != "tool" or not str(d.get("name") or "").endswith(suffix):
                continue
            raw = d.get("content")
            try:
                raw = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                raw = None
            out.append(raw if isinstance(raw, dict) else {"_raw": str(d.get("content"))})
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
        await asyncio.sleep(3)
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
        await asyncio.sleep(3)
    return False


async def descendant_of(c, tok, roots: set[str]) -> dict:
    """从 roots 分出来的那条（模型可能连分两次，顺链取尾）。

    跨轮回溯的子 run 记的是 `forked_from = 上一轮那条`，不是本轮发起那条——
    只按「本轮 run 的孩子」找会一条也找不到。
    """
    rows = await runs_of(c, tok)
    tip, frontier = {}, set(roots)
    for _ in range(8):
        kids = [r for r in rows if r.get("forked_from") in frontier]
        if not kids:
            return tip
        tip = max(kids, key=lambda r: int(r.get("created_at_ms") or 0))
        frontier = {str(tip["run_id"])}
    return tip


async def drive(c, tok, ws, message: str, frames: list[dict]) -> tuple[str, dict]:
    """投一句话进 MQ，边等 run 到终态边把 WS 帧收进调用方给的列表。"""
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
    q = await c.post("/chat", headers=bearer(tok),
                     json={"conversation_id": CONV, "message": message})
    q.raise_for_status()
    run_id = q.json()["run_id"]
    row = await wait_run(c, tok, run_id)
    await wait_turn_idle(c, tok)      # 中途分叉时发起那条先到终态，子 run 还在跑
    await asyncio.sleep(1.0)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=15)
    except Exception:  # noqa: BLE001
        task.cancel()
    return run_id, row


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="b10_smoke_"))
    port_main, port_story = free_port(), free_port()
    base = f"http://127.0.0.1:{port_main}"

    src_cfg = (REPO / "examples" / "storyline" / "config.toml").read_text(encoding="utf-8")
    cfg = re.sub(r"(?m)^port\s*=\s*\d+\s*$", f"port = {port_story}", src_cfg)
    cfg = re.sub(r'(?m)^workspace_root\s*=.*$',
                 f'workspace_root = "{(tmp / "ws").as_posix()}"', cfg)
    cfg = re.sub(r'(?m)^cache_root\s*=.*$',
                 f'cache_root = "{(tmp / "cache").as_posix()}"', cfg)
    cfg_path = tmp / "storyline.toml"
    cfg_path.write_text(cfg, encoding="utf-8")

    story = Service(["run_storyline.py", "--config", str(cfg_path)], "storyline")
    main_svc = Service(["run_server.py", "--storage", "pg_minio", "--port", str(port_main),
                        "--no-mcp", "--storyline-config", str(cfg_path),
                        "--static-dir", "", "--no-resume", "--max-iterations", "40"], "main")
    story.start()
    st = build_storage("pg_minio")
    await st.start()
    user = token = ""
    runs: list[str] = []
    bgm_ids: list[str] = []
    mat_ids: list[str] = []
    try:
        if not await story.wait_port(port_story):
            check(False, "临时 Storyline 起来了")
            return 1
        main_svc.start()
        if not await main_svc.wait_ready(base):
            check(False, "主服务起来了")
            return 1

        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            reg = (await c.post("/register", json={"device_name": "b10"})).json()
            user, token = reg["user_id"], reg["token"]
            key = await borrow_key(st)
            if not check(bool(key), "借到一把模型密钥（值不打印）"):
                return 1
            check((await c.post("/settings/api-key", headers=bearer(token),
                                json={"api_key": key})).status_code == 200,
                  "页面同款写入口配上密钥")

            a = await put_bgm(st, make_tone(tmp / TRACK_A, 392))
            b = await put_bgm(st, make_tone(tmp / TRACK_B, 98))
            bgm_ids += [a["material_id"], b["material_id"]]
            check(a["material_id"] != b["material_id"], "两条测试音轨进了曲库（跑完回删）")

            import edge_tts
            speech = tmp / "口播.mp3"
            await edge_tts.Communicate("第一句开场，第二句铺垫，最后一句收尾。",
                                       "zh-CN-XiaoxiaoNeural").save(str(speech))
            dur = float(mediaops.probe(speech)["duration"]) + 1.0
            clip = make_clip(tmp / "跨轮冒烟.mp4", speech, dur)
            up = (await c.post(f"/upload?conversation_id={CONV}&filename={clip.name}",
                               content=clip.read_bytes(),
                               headers={**bearer(token), "content-type": "video/mp4"})).json()
            mid = up.get("material_id") or ""
            mat_ids.append(mid)
            if not check(bool(mid), f"素材上传入库：{mid}"):
                return 1

            ws_url = f"ws://127.0.0.1:{port_main}/ws/{CONV}?token={token}"
            async with websockets.connect(ws_url, max_size=None) as ws:
                await ws.recv()      # connected

                # ---------- ① 第一版：走到 A 出片（这一轮的全部意义是给第二轮当历史）
                msg1 = (f"用附件 {mid} 剪一条 {int(dur)} 秒左右的短片：保留原声、配字幕、"
                        f"背景音乐必须用曲库里的「{TRACK_A.rsplit('.', 1)[0]}」，"
                        "标题「跨轮冒烟」。不要问我问题，把整条链一次跑完并直接渲染出片。"
                        f"调用 select_BGM 时把 query 参数显式填成"
                        f"「{TRACK_A.rsplit('.', 1)[0]}」。")
                frames1: list[dict] = []
                run1, row1 = await drive(c, token, ws, msg1, frames1)
                runs.append(run1)
                if not check(row1.get("status") == "completed",
                             f"① 第一次执行跑完（run={run1}）：{row1.get('status')}"):
                    return 1
                cp1 = await st.checkpoints.load(run1) or {}
                sid = str((cp1.get("scope") or {}).get("storyline_session") or "")
                aid1 = str((cp1.get("scope") or {}).get("artifact_id") or "")
                if not check(bool(sid), f"① 取到剪辑作用域键：{sid}"):
                    return 1
                bgm1 = await node_payload(st, sid, aid1, "select_BGM")
                if not check(bool(bgm1) and TRACK_A in json.dumps(bgm1, ensure_ascii=False),
                             f"① 第一版选到 A：{(bgm1 or {}).get('filename')}"):
                    return 1
                r1 = await render_done(st, sid, aid1)
                key1 = str((r1 or {}).get("video_object_key") or "")
                if not check((r1 or {}).get("status") == "done" and bool(key1),
                             f"① 第一版渲染终态 done：{key1[:60] or '（没有）'}"):
                    return 1

                # ---------- ② 第二轮：提示词里不给 run_id，模型得自己把跨轮回溯做出来
                msg2 = (f"上一版的配乐换掉：背景音乐改用「{TRACK_B.rsplit('.', 1)[0]}」，"
                        f"调用 select_BGM 时 query 显式填这个完整歌名，"
                        "然后重做时间线并直接出片。"
                        f"不要重头剪、不要再传附件：第一步就调用 rerun_from 回到选乐那一步"
                        f"（node 填 select_BGM，instruction 原样填这次的新要求）。"
                        "如果 rerun_from 回绝你说本轮回不到那一步，就照它列出的历史执行，"
                        "把其中那条 run_id 填进参数再调用一次 rerun_from，然后按新要求继续。"
                        "只在文字里说「接下来调用」而不出工具调用，视为本轮失败。")
                frames2: list[dict] = []
                run2, row2 = await drive(c, token, ws, msg2, frames2)
                runs.append(run2)
                calls2 = [str(f.get("tool") or "").replace("storyline_", "")
                          for f in frames2 if f.get("type") == "tool_call"]
                n_rr = len([t for t in calls2 if t.endswith("rerun_from")])
                print(f"  [② run] {run2} → {row2.get('status')}；[工具序] {calls2}", flush=True)
                if not check(n_rr >= 1, f"② 模型确实调了 rerun_from（{n_rr} 次）"):
                    ans = [str(f.get("answer"))[:300] for f in frames2
                           if f.get("type") == "answer"]
                    print(f"  [末次回答] {ans[-1] if ans else '（无）'}", flush=True)
                    return 1

                # 子 run 的 forked_from 是**上一轮那条**，不是本轮发起那条
                tip2 = await descendant_of(c, token, {run1, run2})
                for r in await runs_of(c, token):
                    print(f"  [② 清单] {str(r.get('run_id'))[:12]} "
                          f"{str(r.get('status')):<11} 作用域={_scope(r.get('artifact_id')):<14} "
                          f"分叉自={str(r.get('forked_from') or '')[:12] or '—'}", flush=True)
                if not check(bool(tip2) and tip2.get("status") == "completed",
                             f"② 跨轮分出的 run 跑到完成："
                             f"{str(tip2.get('run_id'))[:12] if tip2 else '（没分出来）'} "
                             f"{tip2.get('status') if tip2 else '—'}"):
                    return 1
                runs.append(str(tip2["run_id"]))

                rr = await _collect(st, [run2], "rerun_from")
                refused = [x for x in rr if "_raw" in x]
                if refused:
                    txt = str(refused[0]["_raw"])
                    check(("跨轮回溯" in txt or "本会话没有以" in txt)
                          and (run1[:8] in txt or "历史执行" in txt),
                          f"② 第一次调用被如实回绝，回绝文本就是模型的索引：{txt[:76]}")
                else:
                    # 不是缺陷也不写成通过：这条路径本轮没被走到，如实标注
                    print("  [记录] ② 模型一次就给了正确 run_id，"
                          "本轮没走到「回绝文本当索引」那一步", flush=True)

                # 成功那次的返回值**不落任何链**：分叉后上下文回退到一致点，那对
                # rerun_from 的调用/结果消息被设计性丢弃（子链从回退点重写）。所以判据取两处：
                # WS 帧里那句 cross_run（前半段，截断也还在），以及库里的谱系行。
                raws = [str(f.get("result") or "") for f in frames2
                        if f.get("type") == "tool_result"
                        and str(f.get("tool") or "").endswith("rerun_from")]
                check(any('"cross_run": true' in t for t in raws),
                      f"② 成功那次的返回值自报了这是跨轮（cross_run: true；"
                      f"工具返回 {len(raws)} 条）")
                check(str(tip2.get("forked_from")) == run1 and tip2.get("run_id") != run2,
                      f"② 库里谱系证实真的**跨到上一轮那条**："
                      f"forked_from={str(tip2.get('forked_from'))[:12]} ≠ 本轮 {run2[:12]}")

                aid2 = str(((await st.checkpoints.load(
                    str(tip2["run_id"])) or {}).get("scope") or {}).get("artifact_id") or "")
                check(bool(aid2) and aid2 != aid1, f"② 跨轮回溯后换新产物集：{aid1} → {aid2}")
                bgm2 = await node_payload(st, sid, aid2, "select_BGM")
                check(bool(bgm2) and TRACK_B in json.dumps(bgm2, ensure_ascii=False),
                      f"② 这一版真换上 B：{(bgm2 or {}).get('filename') or '（没选出歌）'}")
                plan2 = await timeline_with(st, sid, aid2, b["material_id"])
                check(bool(plan2), f"② 时间线带着 B（下游确实连带重做）：{plan2 or '（没有）'}")
                up1 = await node_payload(st, sid, aid1, "split_shots")
                up2 = await node_payload(st, sid, aid2, "split_shots")
                check(bool(up1) and up1 == up2,
                      "② 跨轮同样逐字节复用上游 split_shots（这正是它相对重剪省下的）")
                back1 = await node_payload(st, sid, aid1, "select_BGM")
                check(bool(back1) and TRACK_A in json.dumps(back1, ensure_ascii=False),
                      "② 上一轮那一版的产物没被改花（仍是 A）")

                pr1 = await st.checkpoints.load(run1)
                pr2 = await st.checkpoints.load(run2)
                check((pr1 or {}).get("status") == STATUS_SUPERSEDED,
                      f"② 被回溯的那条让位：{(pr1 or {}).get('status')}")
                check((pr2 or {}).get("status") == STATUS_SUPERSEDED,
                      f"② 本轮那条被弃用的也让位（否则它停在 running，崩溃恢复会重播它）："
                      f"{(pr2 or {}).get('status')}")

                r2 = await render_done(st, sid, aid2)
                key2 = str((r2 or {}).get("video_object_key") or "")
                check((r2 or {}).get("status") == "done" and bool(key2) and key2 != key1,
                      f"② 跨轮回溯重渲染出新片：{key2[:60] or '（没有）'}")
                if key2:
                    h2 = await st.objects.head(key2)
                    inf = mediaops.probe(await st.objects.localize(key2, tmp / "v2"))
                    check(bool(h2) and h2.bytes > 10_000 and inf["duration"] > 0,
                          f"② 新片字节可取且 ffprobe 可读：{h2.bytes if h2 else 0}B / "
                          f"{inf['duration']}s")
                check(any(f.get("type") == "media" for f in frames2),
                      f"② WS 里出现新成片卡（本轮 {len(frames2)} 帧）")
        return 0 if not FAILS else 1
    finally:
        main_svc.kill()
        story.kill()
        residue = await cleanup(st, user, runs, bgm_ids, mat_ids, CONV)
        left_bgm = 0
        for m in [x for x in bgm_ids if x]:
            if await st.db.get_by_pk("materials", {"id": m}):
                left_bgm += 1
        await st.close()
        print("\n" + ("SMOKE PASSED" if not FAILS else f"FAILED（{len(FAILS)} 项）"), flush=True)
        for f in FAILS:
            print("  - " + f, flush=True)
        print(f"自建数据残留复核：{residue} / 曲库行 {left_bgm}"
              f"（共享身份 {BGM_USER} 的会话行不属于本冒烟，未触碰）", flush=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
