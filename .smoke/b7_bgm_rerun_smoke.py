# -*- coding: utf-8 -*-
"""B7 真机冒烟：整链「换 BGM → 分叉 → 在真 Storyline 上重渲染出片」。

这条链此前只分别验过零件（契约的下游闭包、HTTP 分叉的 run 语义、一次真出片），
从没在同一条片子上验过「换一首背景音乐，上游产物全部复用、只有它和它的下游重做」。
本冒烟三段全打真的：真 Storyline（当前代码、临时端口、临时工作区）+ 真主服务
（真 MQ/WS）+ 真 PG + 真 MinIO + 真 DeepSeek + 真 ffmpeg/MoviePy 渲染。

① 出第一版：曲库里两条测试音轨 → 一句话驱动 → 选到 A → 渲染出片可播；
② HTTP 分叉：回 select_BGM 之前那个一致点，只重做 select_BGM（连带下游作废），
   换 B → 子 run 出第二版：作用域换新、上游 split_shots 逐字节复用、父那一版不动；
③ 模型侧 rerun_from：模型在同一轮里先出一版再自己回到选乐那一步换回来，
   验 handover 真的接手了（父 run 让位、子 run 完成、交接后继续往下调工具）。

自己起自己的端口与工作区，不动开发者常驻的 :8000/:8001；token 与密钥只报存在/长度；
跑完把自建的 run/产物/渲染任务/曲库行/会话/身份全收回并复核零残留。

跑法（需 docker compose up -d，.env 五项已填，且库里有任一身份配过模型密钥）：
    PYTHONPATH=. python -u .smoke/b7_bgm_rerun_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".smoke"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
import websockets

from agent_framework.checkpoint import rebuild  # noqa: E402
from agent_framework.ingest import ingest_bytes  # noqa: E402
from agent_framework.secrets import API_KEY_NAME  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from storyline_server import mediaops  # noqa: E402

from b6_fork_smoke import Service, bearer, borrow_key, free_port  # noqa: E402

STAMP = int(time.time())
CONV = f"c_b7_{STAMP}"
TRACK_A = f"冒烟A轻快_{STAMP}.wav"
TRACK_B = f"冒烟B沉重_{STAMP}.wav"
BGM_USER, BGM_CONV = "u-bgm-library", "c-bgm-library"
RUN_WAIT = 1800.0          # 一次真渲染分钟级；本脚本要跑四次出片，各给足
FAILS: list[str] = []


def check(cond, label) -> bool:
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)
    if not cond:
        FAILS.append(label)
    return bool(cond)


# --------------------------------------------------------------- 造素材与曲库

def make_clip(dst: Path, speech: Path, sec: float) -> Path:
    """两段纯色测试画面 + 循环人声：够切镜、够 ASR、够渲染。"""
    mediaops.ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=480x270:rate=25:duration={sec}",
                    "-f", "lavfi", "-i", f"color=c=teal:size=480x270:rate=25:duration={sec}",
                    "-stream_loop", "-1", "-i", str(speech),
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,scale=480:270[v]",
                    "-map", "[v]", "-map", "2:a", "-t", f"{sec}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(dst))
    return dst


async def put_bgm(st, path: Path) -> dict:
    """音轨进共享曲库：select_BGM 检索的就是 origin='bgm' 这一池（跨用户可见）。"""
    await st.users.provision(BGM_USER)

    async def chunks():
        yield path.read_bytes()

    return await ingest_bytes(st, chunks(), filename=path.name, user_id=BGM_USER,
                              conversation_id=BGM_CONV, origin="bgm")


# ------------------------------------------------------------------- PG 侧读

def _scope(aid: str) -> str:
    """首轮 run 的 artifact_id 是空串，仓库按 ``_default`` 那一集落库——查询侧同规则归一。"""
    return str(aid or "_default")


async def node_payload(st, sid: str, aid: str, node: str) -> dict | None:
    """某个产物作用域里某节点的落库结果。"""
    if not sid:
        return None
    rows = await st.db.select("artifacts", where={"session_id": sid,
                                                 "artifact_id": _scope(aid),
                                                 "node": node})
    return rows[0]["payload"] if rows else None


async def render_of(st, sid: str, aid: str) -> dict | None:
    rows = await st.db.select("render_jobs", where={"session_id": sid,
                                                   "artifact_id": _scope(aid)})
    return max(rows, key=lambda r: str(r.get("updated_at") or "")) if rows else None


async def render_done(st, sid: str, aid: str, deadline_sec: float = 420) -> dict | None:
    """等这一版渲染落终态再取证据。

    渲染改成提交+轮询后，「run 回答完了」与「片子渲完了」是两件事：后台任务可能仍在跑，
    这时读到 queued/running 不是没出片，只是还没出完。
    """
    end = time.time() + deadline_sec
    row = await render_of(st, sid, aid)
    while time.time() < end and (row or {}).get("status") not in ("done", "failed"):
        await asyncio.sleep(2)
        row = await render_of(st, sid, aid)
    return row


PLAN_NODES = ("plan_timeline", "plan_timeline_pro", "plan_timeline_ai_transition")


async def timeline_with(st, sid: str, aid: str, material_id: str) -> str:
    """时间线产物里存的是配乐的本地化路径（文件名取自对象键＝素材 id）。

    三个规划变体模型按需要挑一个，所以判据是「哪个变体带着这一首的素材 id」。
    """
    for n in PLAN_NODES:
        p = await node_payload(st, sid, aid, n)
        if p and material_id in json.dumps(p, ensure_ascii=False):
            return n
    return ""


async def wait_run(c, tok, run_id, timeout=RUN_WAIT) -> dict | None:
    """按 /runs/{id} 轮询到终态（completed / failed / superseded）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await c.get(f"/runs/{run_id}", headers=bearer(tok))
        if r.status_code == 200:
            row = (r.json() or {}).get("run") or {}
            if row.get("status") in ("completed", "failed", "superseded"):
                return row
        await asyncio.sleep(3)
    return None


async def runs_of(c, tok) -> list[dict]:
    r = await c.get(f"/convs/{CONV}/runs", headers=bearer(tok))
    return (r.json() or {}).get("runs") or []


async def wait_turn_idle(c, tok, timeout=RUN_WAIT) -> bool:
    """等本会话再没有 running 的执行：模型中途分叉时，发起那条会立刻让位而子 run 还在跑。"""
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


async def settle_fork(c, tok, parent: str, timeout=RUN_WAIT) -> dict | None:
    """顺 forked_from 链追到最深的那条后代，等它到终态。

    模型一轮里可能连分两次（第二次分叉会把第一条子 run 也置 superseded），
    所以只认链尾，不认第一个落定的子 run。
    """
    deadline = time.time() + timeout
    tip = parent
    seen: dict[str, dict] = {}
    while time.time() < deadline:
        rows = await runs_of(c, tok)
        seen = {r["run_id"]: r for r in rows}
        kids = [r for r in rows if r.get("forked_from") == tip]
        if kids:
            tip = max(kids, key=lambda r: int(r.get("created_at_ms") or 0))["run_id"]
            continue
        row = seen.get(tip) or {}
        if tip != parent and row.get("status") in ("completed", "failed", "superseded"):
            return row
        await asyncio.sleep(3)
    return None


async def wait_final(c, tok, run_id) -> dict:
    """等这条 run 交出结果；它若被中途再分叉顶掉，就顺着链取最深的那条后代。"""
    row = (await wait_run(c, tok, run_id)) or {}
    await wait_turn_idle(c, tok)
    rows = {r["run_id"]: r for r in await runs_of(c, tok)}
    row = rows.get(run_id) or row
    if row.get("status") == "superseded":
        row = (await settle_fork(c, tok, run_id)) or row
    return row


async def seq_before_bgm(st, run_id: str) -> tuple[int, str]:
    """分叉点：链上「select_BGM 还没执行过」的那个 seq（与 seq_before_tool 同一条规则）。

    远程工具名可能带 storyline_ 前缀；落在链首则前面没东西可留，那不是分叉而是重开。
    """
    entries = await st.checkpoints.load_entries(run_id)
    names = {str(m.get("name")) for e in entries for m in (e.get("payload") or [])
             if isinstance(m, dict) and m.get("role") == "tool"}
    tool = next((t for t in ("select_BGM", "storyline_select_BGM") if t in names), "")
    if not tool:
        raise AssertionError(f"链上没有 select_BGM 的执行记录，工具名实得 {sorted(names)}")
    for e in entries:
        if any(isinstance(m, dict) and m.get("role") == "tool" and m.get("name") == tool
               for m in (e.get("payload") or [])):
            if int(e["seq"]) == 0:
                raise AssertionError("select_BGM 的结果落在链首，之前没有可复用的一致点")
            return int(e["seq"]) - 1, tool
    raise AssertionError("unreachable")


def trace_of(frames: list[dict], *suffixes: str) -> list[str]:
    """带参数的工具调用序：模型驱动那一步要能回看它到底开口要了哪一首。"""
    out: list[str] = []
    for f in frames:
        if f.get("type") != "tool_call":
            continue
        t = str(f.get("tool") or "")
        if not any(t.endswith(s) for s in suffixes):
            continue
        args = f.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:  # noqa: BLE001 - 参数不是 JSON 就只留个工具名
                args = {}
        args = args if isinstance(args, dict) else {}
        what = str(args.get("query") or args.get("node") or "")
        out.append(f"{t.replace('storyline_', '')}<{what[:22]}>")
    return out


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
    await wait_run(c, tok, run_id)      # 先到终态：完成，或因中途分叉立刻让位
    await wait_turn_idle(c, tok)        # 让位之后子 run 可能还在跑
    await asyncio.sleep(1.0)            # 让尾部帧落地
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=15)
    except Exception:  # noqa: BLE001
        task.cancel()
    rows = {r["run_id"]: r for r in await runs_of(c, tok)}
    return run_id, rows.get(run_id, {})


async def drain(ws) -> int:
    """取空 WS 缓冲：上一段没人读的帧不该混进下一段的断言。"""
    n = 0
    while True:
        try:
            await asyncio.wait_for(ws.recv(), timeout=0.5)
            n += 1
        except asyncio.TimeoutError:
            return n
        except Exception:  # noqa: BLE001
            return n


# ------------------------------------------------------------------ 三段主流程

async def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="b7_smoke_"))
    port_main, port_story = free_port(), free_port()
    base = f"http://127.0.0.1:{port_main}"

    # ---- 临时 Storyline：换端口 + 把工作区/缓存挪进临时目录，不碰开发机的 .storyline
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
        check("已接入" in main_svc.log and "未连通" not in main_svc.log,
              "主服务接上了剪辑能力（Storyline 已接入，非「本服务无剪辑」降级）")

        async with httpx.AsyncClient(base_url=base, timeout=60) as c:
            reg = (await c.post("/register", json={"device_name": "b7"})).json()
            user, token = reg["user_id"], reg["token"]
            key = await borrow_key(st)
            if not check(bool(key), "借到一把模型密钥（值不打印）"):
                return 1
            sk = await c.post("/settings/api-key", headers=bearer(token), json={"api_key": key})
            check(sk.status_code == 200, "页面同款写入口配上密钥")

            # 两条只差音色与文件名的测试音轨进共享曲库
            a = await put_bgm(st, make_tone(tmp / TRACK_A, 392))
            b = await put_bgm(st, make_tone(tmp / TRACK_B, 98))
            bgm_ids += [a["material_id"], b["material_id"]]
            check(a["material_id"] != b["material_id"], "两条测试音轨进了曲库（跑完回删）")

            import edge_tts
            speech = tmp / "口播.mp3"
            await edge_tts.Communicate("第一句开场，第二句铺垫，最后一句收尾。",
                                       "zh-CN-XiaoxiaoNeural").save(str(speech))
            dur = float(mediaops.probe(speech)["duration"]) + 1.0
            clip = make_clip(tmp / "冒烟.mp4", speech, dur)
            blob = clip.read_bytes()
            up = (await c.post(f"/upload?conversation_id={CONV}&filename={clip.name}",
                               content=blob,
                               headers={**bearer(token), "content-type": "video/mp4"})).json()
            mid = up.get("material_id") or ""
            mat_ids.append(mid)
            if not check(bool(mid), f"素材上传入库：{mid}"):
                return 1

            ws_url = f"ws://127.0.0.1:{port_main}/ws/{CONV}?token={token}"
            async with websockets.connect(ws_url, max_size=None) as ws:
                await ws.recv()      # connected

                # ---------------- ① 第一版：一句话驱动，BGM 选到 A，出片可播
                msg1 = (f"用附件 {mid} 剪一条 {int(dur)} 秒左右的短片：保留原声、配字幕、"
                        f"背景音乐必须用曲库里的「{TRACK_A.rsplit('.', 1)[0]}」，"
                        f"标题「换乐冒烟」。不要问我问题，把整条链一次跑完并直接渲染出片。"
                        f"调用 select_BGM 时把 query 参数显式填成"
                        f"「{TRACK_A.rsplit('.', 1)[0]}」。")
                frames1: list[dict] = []
                run1, row1 = await drive(c, token, ws, msg1, frames1)
                runs.append(run1)
                if not check(row1.get("status") == "completed",
                             f"① 第一次执行跑完（run={run1}）：{row1.get('status')}"):
                    return 1
                scope1 = (await st.checkpoints.load(run1) or {}).get("scope") or {}
                sid = str(scope1.get("storyline_session") or "")
                aid1 = str(scope1.get("artifact_id") or "")
                if not check(bool(sid), f"① 取到剪辑作用域键：{sid}"):
                    return 1
                bgm1 = await node_payload(st, sid, aid1, "select_BGM")
                if not check(bool(bgm1) and TRACK_A in json.dumps(bgm1, ensure_ascii=False),
                             f"① select_BGM 真选到 A：{bgm1 and bgm1.get('filename')}"):
                    calls1 = [str(f.get("tool")) for f in frames1 if f.get("type") == "tool_call"]
                    errs = [str(f.get("error"))[:200] for f in frames1
                            if f.get("type") in ("error", "tool_result") and f.get("error")]
                    ans = [str(f.get("answer"))[:400] for f in frames1
                           if f.get("type") == "answer"]
                    print(f"  [本轮 run] {run1} → {row1.get('status')} "
                          f"iter={row1.get('iteration')} session={sid} artifact={aid1}")
                    print(f"  [工具调用序] {calls1}")
                    print(f"  [末次回答] {ans[-1] if ans else '（无 answer 帧）'}")
                    print(f"  [错误帧] {errs[:4] or '（无）'}")
                    nodes = sorted({str(r['node']) for r in await st.db.select(
                        "artifacts", where={"session_id": sid})})
                    print(f"  [该会话已落库节点] {nodes}")
                    return 1
                render1 = await render_done(st, sid, aid1)
                key1 = str((render1 or {}).get("video_object_key") or "")
                if not check((render1 or {}).get("status") == "done" and bool(key1),
                             f"① 渲染任务 done，成片对象键 {key1[:70]}"):
                    return 1
                h1 = await st.objects.head(key1)
                check(h1 is not None and h1.bytes > 10_000,
                      f"① 第一版字节在 MinIO：{h1.bytes if h1 else 0} B")
                check(any(f.get("type") == "media" for f in frames1),
                      f"① WS 里出现成片播放卡（本轮 {len(frames1)} 帧）")
                # 判别性证据：剪这条片的是本次临时起的这台 Storyline，不是开发机常驻那台
                # ——它写的是自己 config 里的 cache_root / workspace_root（都在 tmp 下）。
                mine = [p for d in ("cache", "ws") for p in (tmp / d).rglob("*")
                        if p.is_file()]
                check(bool(mine),
                      f"① 临时 Storyline 真在干活：自带缓存/工作区落了 {len(mine)} 个文件")
                at_seq, toolname = await seq_before_bgm(st, run1)
                check(at_seq > 0, f"① 链上取到分叉点 seq={at_seq}（工具名 {toolname}）")

                # ---------------- ② HTTP 分叉：回 select_BGM 之前，只重做它，换 B
                fk = await c.post(f"/runs/{run1}/fork", headers=bearer(token), json={
                    "at_seq": at_seq, "rerun_nodes": ["select_BGM"],
                    "message": f"背景音乐换成「{TRACK_B.rsplit('.', 1)[0]}」，"
                               f"调用 select_BGM 时把 query 参数显式填成它，"
                               "其余没点名的步骤一律复用已有产物，不要重头剪。"})
                body = fk.json() or {}
                run2 = body.get("run_id") or ""
                runs.append(run2)
                if not check(fk.status_code == 200 and bool(run2),
                             f"② HTTP 分叉入队：{fk.status_code} {body.get('status')}"):
                    return 1
                fin2 = await wait_final(c, token, run2)
                if not check(bool(fin2) and fin2.get("status") == "completed",
                             f"② 分叉出的 run 真重渲染到完成："
                             f"{fin2.get('run_id')} → {fin2.get('status')}"):
                    return 1
                runs.append(str(fin2.get("run_id") or ""))
                pr1 = await st.checkpoints.load(run1)
                check(pr1 is not None and pr1["status"] == "superseded", "② 父 run 已让位")
                aid2 = str(fin2.get("artifact_id") or "")
                check(bool(aid2) and aid2 != aid1, f"② 子 run 换了产物作用域：{aid1} → {aid2}")
                bgm2 = await node_payload(st, sid, aid2, "select_BGM")
                check(bool(bgm2) and TRACK_B in json.dumps(bgm2, ensure_ascii=False),
                      f"② 子这一版真换上 B：{bgm2 and bgm2.get('filename')}")
                plan2 = await timeline_with(st, sid, aid2, b["material_id"])
                if not check(bool(plan2),
                             f"② 下游时间线里带着 B 这一首（确实被连带重做了，"
                             f"不只是改了选择那一步）：{plan2 or '（三个变体都没有 B）'}"):
                    nodes2 = sorted({str(r["node"]) for r in await st.db.select(
                        "artifacts", where={"session_id": sid,
                                            "artifact_id": _scope(aid2)})})
                    print(f"  [这一版落库节点] {nodes2}")
                    print(f"  [B 素材 id] {b['material_id']} / 选乐产物={bgm2}")
                up1 = await node_payload(st, sid, aid1, "split_shots")
                up2 = await node_payload(st, sid, aid2, "split_shots")
                check(bool(up1) and up1 == up2,
                      "② 上游 split_shots 产物逐字节复用（分叉相对重跑省下的正是这些）")
                back1 = await node_payload(st, sid, aid1, "select_BGM")
                check(bool(back1) and TRACK_A in json.dumps(back1, ensure_ascii=False),
                      "② 父那一版的产物没被改花（仍是 A）")
                render2 = await render_done(st, sid, aid2)
                key2 = str((render2 or {}).get("video_object_key") or "")
                check(bool(key2) and key2 != key1, f"② 重渲染出了新对象：{key2[:70]}")
                h2 = await st.objects.head(key2) if key2 else None
                check(h2 is not None and h2.bytes > 10_000 and h1 is not None
                      and h2.bytes != h1.bytes,
                      f"② 第二版字节确实不同且可取：{h2.bytes if h2 else 0} B "
                      f"vs {h1.bytes if h1 else 0} B")
                if key2:
                    info = mediaops.probe(await st.objects.localize(key2, tmp / "v2"))
                    check(info["duration"] > 0 and info["width"] > 0,
                          f"② 第二版 ffprobe 可读：{info['duration']}s "
                          f"{info['width']}x{info['height']}")

                # ---------------- ③ 模型侧 rerun_from：模型自己在同一轮回退重跑
                # 模型的分叉入口只在「本轮执行内」有效：新起一轮的链上没有 select_BGM
                # 的结果，seq_before_tool 会如实回绝（跨轮复用是有意划下的边界）。
                await drain(ws)
                msg3 = (f"再用附件 {mid} 完整剪一条短片并直接出片：背景音乐用"
                        f"「{TRACK_B.rsplit('.', 1)[0]}」（query 显式填这个歌名）。"
                        "出片后本轮不要结束：调用一次 rerun_from 工具回到选乐那一步，"
                        f"node 填 select_BGM，并把 instruction 参数原样填成："
                        f"「背景音乐改用《{TRACK_A.rsplit('.', 1)[0]}》，"
                        f"select_BGM 的 query 就填「{TRACK_A.rsplit('.', 1)[0]}」，"
                        "然后只重做选乐之后的步骤（规划时间线、渲染出片），其余全部复用」。"
                        "分叉之后按那条 instruction 做，只分叉一次，出片即收尾。"
                        "只在文字里说「接下来调用」而不出工具调用，视为本轮失败。")
                frames3: list[dict] = []
                run3, row3 = await drive(c, token, ws, msg3, frames3)
                runs.append(run3)
                calls = [str(f.get("tool")) for f in frames3 if f.get("type") == "tool_call"]
                hit = [i for i, t in enumerate(calls) if t.endswith("rerun_from")]
                if not check(bool(hit),
                             f"③ 模型自己调了 rerun_from（{len(calls)} 次工具调用"
                             f"{calls[-6:]}；本轮 run={run3} → {row3.get('status')}）"):
                    ans3 = [str(f.get("answer"))[:300] for f in frames3
                            if f.get("type") == "answer"]
                    err3 = [str(f.get("error"))[:200] for f in frames3
                            if f.get("type") == "error" or f.get("error")]
                    print(f"  [末次回答] {ans3[-1] if ans3 else '（无）'}")
                    print(f"  [错误/回绝] {err3[:3] or '（无）'}")
                    return 1
                tip3 = await settle_fork(c, token, run3)
                print(f"  [③ 选乐/分叉调用序] "
                      f"{trace_of(frames3, 'select_BGM', 'rerun_from')}")
                for r in await runs_of(c, token):
                    print(f"  [③ run] {str(r.get('run_id'))[:12]} "
                          f"{r.get('status'):<10} 作用域={r.get('artifact_id') or '_default':<14} "
                          f"分叉自={str(r.get('forked_from') or '')[:12] or '—'}")
                if not check(bool(tip3) and tip3.get("status") == "completed",
                             f"③ 分叉出的 run 接手跑到完成："
                             f"{tip3.get('run_id') if tip3 else '（没分出来）'} → "
                             f"{tip3.get('status') if tip3 else '—'}"):
                    return 1
                runs.append(str(tip3.get("run_id") or ""))
                pr3 = await st.checkpoints.load(run3)
                check(pr3 is not None and pr3["status"] == "superseded",
                      "③ 发起分叉的那条已让位")
                aid3 = str(tip3.get("artifact_id") or "")
                parent3 = str(((pr3 or {}).get("scope") or {}).get("artifact_id") or "")
                check(bool(aid3) and aid3 != aid1, f"③ 交出这一版的是新产物集：{aid3}")

                msgs3 = rebuild(await st.checkpoints.load_entries(str(tip3["run_id"])))
                # 分叉必须把「这次要改什么」带进子执行：回退后的上下文末尾若还是父 run
                # 那句旧话，模型只会照旧话再演一遍（这一条早先真就这么反复选回同一首）。
                asks = [str(m.get("content") or "") for m in msgs3
                        if isinstance(m, dict) and m.get("role") == "user"
                        and TRACK_A.rsplit(".", 1)[0] in str(m.get("content") or "")]
                check(bool(asks),
                      f"③ 分叉把新诉求带进了子执行：{(asks[-1] if asks else '')[:64]}")

                bgm3 = await node_payload(st, sid, aid3, "select_BGM")
                bgm3b = await node_payload(st, sid, parent3, "select_BGM")
                raw3 = json.dumps(bgm3 or {}, ensure_ascii=False)
                picked = TRACK_A if TRACK_A in raw3 else (TRACK_B if TRACK_B in raw3 else "")
                check(bool(picked),
                      f"③ 交接后子 run 真的重新选了一次乐：{picked or '（这一版没选出歌）'}")
                prev = json.dumps(bgm3b or {}, ensure_ascii=False)
                check(bool(picked) and picked not in prev,
                      f"③ 换到了另一首：父那一版 "
                      f"{(bgm3b or {}).get('filename') or '（无）'} → 这一版 {picked or '（无）'}")
                print(f"  [模型点了哪首] 要的是 {TRACK_A}，这一版交出的是 {picked or '（无）'}")

                pid = (a if picked == TRACK_A else b)["material_id"]
                plan3 = await timeline_with(st, sid, aid3, pid)
                check(bool(plan3),
                      f"③ 这一版的时间线带着重选后的那一首（{picked}）：{plan3 or '（没有）'}")
                up0 = await node_payload(st, sid, parent3, "split_shots")
                up3 = await node_payload(st, sid, aid3, "split_shots")
                check(bool(up0) and up0 == up3,
                      "③ 模型驱动的分叉同样逐字节复用了上游 split_shots")

                r3 = await render_done(st, sid, aid3)
                key3 = str((r3 or {}).get("video_object_key") or "")
                if bool(key3) and key3 not in (key1, key2):
                    h3 = await st.objects.head(key3)
                    inf3 = (mediaops.probe(await st.objects.localize(key3, tmp / "v3"))
                            if h3 else None)
                    check(bool(h3) and h3.bytes > 10_000 and bool(inf3) and inf3["duration"] > 0,
                          f"③ 模型驱动的分叉又出一版新片：{key3[-42:]} "
                          f"{h3.bytes if h3 else 0}B / "
                          f"{inf3['duration'] if inf3 else 0}s")
                else:
                    check(False, f"③ 模型驱动的分叉又出一版新片：{key3 or '（这一版没交出片）'}")
                tail = [t for i, t in enumerate(calls) if i > hit[0]]
                redid = [t for t in tail if t.replace("storyline_", "")
                         in ("select_BGM", *PLAN_NODES, "render_video")]
                check(bool(redid), f"③ 交接后确实往下重跑了选乐/时间线/渲染：{redid[:6]}")
                # 交接痕迹：分叉工具那对消息按设计不留在子链里（上下文已回退到那一步之前），
                # 留在里面的是 _adopt_fork 补的那条系统标注。
                note = [str(m.get("content") or "") for m in msgs3
                        if isinstance(m, dict) and "已回到一致点" in str(m.get("content") or "")]
                check(bool(note),
                      f"③ 交出结果的 run 链里留着交接标注：{note[:1] or '（没有）'}")
        return 0 if not FAILS else 1
    finally:
        main_svc.kill()
        story.kill()
        residue = await cleanup(st, user, runs, bgm_ids, mat_ids, CONV)
        await st.close()
        print("\n" + ("SMOKE PASSED" if not FAILS else f"FAILED（{len(FAILS)} 项）"))
        for f in FAILS:
            print("  - " + f)
        print(f"自建数据残留复核：{residue}")
    return 1


def make_tone(dst: Path, freq: int, sec: float = 12.0) -> Path:
    """一条纯正弦音轨：文件名与音高都不同，好让 select_BGM 按语义选对那一首。"""
    mediaops.tone_wav(dst, sec, freq)
    return dst


async def cleanup(st, user: str, runs: list[str], bgm_ids: list[str],
                  mat_ids: list[str], conv: str) -> str:
    """把冒烟自己造的东西收干净，并逐项复核零残留。"""
    for r in [x for x in runs if x]:
        try:
            await st.checkpoints.drop(r)
        except Exception:  # noqa: BLE001
            pass
    sid = f"u:{user}:c:{conv}" if user else ""
    for m in [x for x in mat_ids + bgm_ids if x]:
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
    await st.conversations.drop(user, conv)
    await st.secrets.drop(user, API_KEY_NAME)
    if user:
        await st.db.delete("users", where={"id": user})
    left_runs = 0
    for r in [x for x in runs if x]:
        if await st.checkpoints.load(r) is not None:
            left_runs += 1
    left_mat = [m for m in mat_ids + bgm_ids
                if m and await st.db.get_by_pk("materials", {"id": m})]
    return (f"run {left_runs} / 产物 {await st.db.count('artifacts', where={'session_id': sid})
            if sid else 0} / 渲染任务 "
            f"{await st.db.count('render_jobs', where={'session_id': sid}) if sid else 0} / "
            f"素材 {len(left_mat)} / 身份 "
            f"{'在' if user and await st.db.get_by_pk('users', {'id': user}) else '无'}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
