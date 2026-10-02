# -*- coding: utf-8 -*-
"""B3 渲染层直连冒烟：绕开 Agent 迭代环，按 DAG 顺序逐节点驱动 :8001，取 B3 链路硬证据。

跑法（容器在、:8000 / :8001 已起来）：
    PYTHONPATH=. python .smoke/b3_direct_render_smoke.py

覆盖：MCP 身份注入 → 会话作用域稳定 → localize 取素材 → MoviePy/ffmpeg 出片 →
publish 进 MinIO → render_jobs done/100 → presigned 直链区间读 → 工作区无媒体泄漏。
其中「部分调用带 user_request、部分不带」正是上一轮真机断链的形状，用来钉住作用域修复。
渲染已改成「提交 + 轮询」，所以 render_video 之后由本脚本调 render_status 追到终态。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx

from agent_framework.storage import build_storage
from agent_framework.tools.mcp import MCPServerConfig, connect_server, extract_text
from storyline_server import mediaops
from storyline_server.mediaops import MEDIA_EXTS

BASE = "http://127.0.0.1:8000"
MCP_URL = "http://127.0.0.1:8001/mcp"
USER, CONV = "", f"c_b3d_{int(__import__('time').time())}"  # USER 由 /register 现签


def sid() -> str:                    # 服务端由注入身份推出的会话作用域
    return f"u:{USER}:c:{CONV}"


def safe_sid() -> str:               # 对象键 / 工作区目录里的净化形式
    return sid().replace(":", "_")


FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}", flush=True)
    if not cond:
        FAILS += 1


async def poll_render(client, base_args: dict, payload: dict, deadline_sec: float = 900):
    """渲染提交后的轮询口：内联 grace 只兜住短片，长片要靠 render_status 追终态。

    走 MCP 工具而不是直连 HTTP，是因为这条路径正是模型侧用的那一条，冒烟要钉的是它。
    """
    render = payload.get("render") or {}
    if render.get("status") in ("done", "failed"):
        return payload
    art = render.get("artifact_id") or payload.get("artifact_id") or ""
    print(f"  [提交回句柄] status={render.get('status')} artifact={art}", flush=True)
    end = time.time() + deadline_sec
    while time.time() < end:
        await asyncio.sleep(3)
        text = extract_text(await client.call_tool(
            "render_status", dict(base_args, artifact_id=art), 120))
        if text.startswith("Error"):
            check(False, f"render_status 返回错误：{text[:500]}")
            return payload
        payload = json.loads(text)
        render = payload.get("render") or {}
        if render.get("status") in ("done", "failed"):
            print(f"  [轮询到终态] {render.get('status')} "
                  f"{render.get('percent')}% / {int(end - time.time())}s 余量", flush=True)
            return payload
    check(False, f"轮询 {deadline_sec}s 仍未落终态：{render}")
    return payload


def inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


WS_ROOT = Path(".storyline/workspace")


def media_outside_workspace() -> set[str]:
    """仓库里工作区之外的媒体文件清单——跑前后各取一次，只认本次新增的泄漏。"""
    return {str(p) for p in Path(".storyline").rglob("*.mp4") if not inside(p, WS_ROOT)} | \
           {str(p) for p in Path(".").glob("*.mp4")}


async def main() -> None:
    before = media_outside_workspace()
    tmp = Path(tempfile.mkdtemp(prefix="b3_direct_"))
    # 纯音素材会让 faster-whisper 的 VAD 把整段判为非语音并卡死转写，故先合成一段真人语音
    speech = tmp / "口播.mp3"
    import edge_tts
    await edge_tts.Communicate(
        "大家好，这是一段用来验证渲染管线的口播。第一句讲开场，第二句讲中段，最后一句收尾。",
        "zh-CN-XiaoxiaoNeural").save(str(speech))
    check(speech.stat().st_size > 5_000, f"口播素材 {speech.stat().st_size} 字节")
    dur = mediaops.probe(speech)["duration"] + 0.5
    src = tmp / "直连冒烟.mp4"
    mediaops.ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={dur}",
                    "-f", "lavfi", "-i", f"smptebars=size=640x360:rate=25:duration={dur}",
                    "-f", "lavfi", "-i", f"color=c=navy:size=640x360:rate=25:duration={dur}",
                    "-stream_loop", "-1", "-i", str(speech),
                    "-filter_complex",
                    f"[0:v][1:v][2:v]concat=n=3:v=1:a=0,scale=640:360,trim=duration={dur}[v]",
                    "-map", "[v]", "-map", "3:a", "-t", f"{dur}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src))
    info = mediaops.probe(src)
    check(info["has_audio"] and info["duration"] > 3, f"合成素材 {info['duration']}s 含音轨")
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        global USER
        reg = (await c.post("/register", json={"device_name": "b3d-smoke"})).json()
        USER = reg["user_id"]        # 身份由服务端签发（token 值不打印）
        r = await c.post(f"/upload?conversation_id={CONV}&filename={src.name}",
                         content=src.read_bytes(),
                         headers={"content-type": "video/mp4",
                                  "Authorization": f"Bearer {reg['token']}"})
        up = r.json()
        check(r.status_code == 200 and up.get("material_id"), f"POST /upload → {up.get('material_id')}")
    mid = up["material_id"]

    client = await connect_server(MCPServerConfig(
        name="storyline", type="streamableHttp", url=MCP_URL, tool_timeout=900))
    await client.initialize()

    # 与主 Agent 的 MCP 包装层同构：每个调用都带注入身份；user_request 只在部分调用出现
    def args(**kw):
        base = {"user_id": USER, "conversation_id": CONV}
        base.update(kw)
        return base

    steps: list[tuple[str, dict]] = [
        ("load_media", args(material_ids=[mid])),
        ("split_shots", args()),
        ("understand_clips", args()),
        ("group_clips", args(user_request="剪一条 6 秒内短片，标题「直连冒烟」，配字幕")),
        ("script_template_rec", args()),
        ("generate_script", args(user_request="片名《直连冒烟》，6 秒内，一句一条短字幕")),
        ("asr", args()),
        ("speech_rough_cut", args()),
        ("generate_voiceover", args()),
        ("select_BGM", args()),
        ("text_rec", args()),
        ("transition_rec", args()),
        ("plan_timeline", args()),
        ("render_video", args()),
    ]
    out: dict[str, dict] = {}
    for name, a in steps:
        if name == "render_video":
            sess_dir = Path(".storyline/workspace") / f"u:{USER}:c:{CONV}".replace(":", "_")
            print(f"  [工作区 {sess_dir}] "
                  f"{[str(p.relative_to(sess_dir)) + ':' + str(p.stat().st_size) for p in sess_dir.rglob('*') if p.is_file()] if sess_dir.exists() else '不存在'}",
                  flush=True)
        text = extract_text(await client.call_tool(name, a, 900))
        if text.startswith("Error"):
            check(False, f"{name} 返回错误：{text[:2000]}")
            break
        payload = json.loads(text)
        if name == "render_video":
            payload = await poll_render(client, a, payload)
        out[name] = payload.get("output") or {}
        check(bool(payload.get("output")), f"{name} → {sorted(payload.get('output') or {})[:6]}")

    rendered = out.get("render_video") or {}
    key = rendered.get("video", "")
    url = rendered.get("media_url", "")
    check(key.startswith(f"renders/{safe_sid()}/"), f"成片对象键：{key}")
    check(url.startswith("http://") or url.startswith("https://"),
          f"presigned 直链：{url[:100]}")

    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
        rng = await c.get(url, headers={"range": "bytes=0-1023"})
        check(rng.status_code == 206 and len(rng.content) == 1024,
              f"区间读 206/{len(rng.content)}B（可拖动）")
        full = await c.get(url)
        played = tmp / "played.mp4"
        played.write_bytes(full.content)
        info = mediaops.probe(played)
        check(full.status_code == 200 and len(full.content) > 10_000
              and info["duration"] > 0 and info["width"] == 640,
              f"整读 {len(full.content)}B → ffprobe {info['duration']}s "
              f"{info['width']}x{info['height']}")

    # ---------- 提交 + 轮询主路径：短片在 grace 内就 done，等于没验到轮询 ----------
    # 同一作用域再提交一次，wait_sec=0 让服务端不复位等待、纯靠 render_status 追终态。
    t0 = time.time()
    text = extract_text(await client.call_tool("render_video", args(wait_sec=0), 120))
    submit_ms = int((time.time() - t0) * 1000)
    submitted = json.loads(text)
    rs = submitted.get("render") or {}
    check(submit_ms < 5000 and rs.get("status") in ("queued", "running"),
          f"wait_sec=0 提交即回句柄（{submit_ms}ms → {rs.get('status')} "
          f"{rs.get('percent')}%）")
    check(bool(submitted.get("hint")) and not submitted.get("output"),
          f"未终态时回 hint 且不编造成片：{str(submitted.get('hint'))[:80]}")
    polled = await poll_render(client, args(), submitted)
    pr = polled.get("render") or {}
    purl = str((polled.get("output") or {}).get("media_url") or "")
    check(pr.get("status") == "done" and purl.startswith("http"),
          f"render_status 轮询到 done 才交出片与现签直链：{pr.get('status')}")

    # ---------- 工作区泄漏面：媒体字节只允许在 workspace / 本脚本临时目录里 ----------
    # 只认本次新增：跑前已存在的旧遗留文件不是本次渲染的泄漏，但也不能被忽略掉真泄漏
    strays = sorted(media_outside_workspace() - before)
    check(not strays, f"渲染未在工作区/对象存储之外留下新的成片字节（strays={strays[:5]}）")

    await client.close()

    storage = build_storage("pg_minio")
    await storage.start()
    try:
        jobs = [r for r in await storage.db.select("render_jobs") if r["session_id"] == sid()]
        check(len(jobs) == 1, f"render_jobs 恰 1 行（同产物复跑复位而非追加）：{len(jobs)}")
        job = jobs[0] if jobs else {}
        check(job.get("status") == "done" and job.get("percent") == 100
              and job.get("stage") == "done" and job.get("video_object_key") == key
              and float(job.get("duration_sec") or 0) > 0,
              f"任务终态 done/100 + 对象键一致：{ {k: job.get(k) for k in ('artifact_id', 'status', 'stage', 'percent', 'duration_sec')} }")
        arts = await storage.db.select("artifacts", where={"session_id": sid()})
        nodes = sorted({a["node"] for a in arts})
        check({"load_media", "plan_timeline", "render_video"} <= set(nodes) and len(nodes) >= 12,
              f"整链 {len(nodes)} 个节点产物在同一会话作用域：{nodes}")
        head = await storage.objects.head(key)
        check(head is not None and head.bytes == len(full.content),
              f"MinIO 字节数与回放取回一致：{head.bytes if head else 0}")
        local = await storage.objects.localize(key, tmp / "again")
        check(local.stat().st_size == head.bytes, "localize 再取一次不回源失败")
        art_dir = WS_ROOT / safe_sid()
        media_in_ws = [p for p in art_dir.rglob("*")
                       if p.is_file() and p.suffix.lower() in MEDIA_EXTS] if art_dir.exists() else []
        check(art_dir.exists() and bool(media_in_ws),
              f"工作区保留成功目录供同产物重渲：{[p.name for p in media_in_ws][:5]}")
    finally:
        await storage.close()

    print()
    print("FAILED" if FAILS else "DIRECT SMOKE PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
