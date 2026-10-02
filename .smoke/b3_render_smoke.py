# -*- coding: utf-8 -*-
"""B3 真机冒烟：上传素材 → 一句话驱动渲染 → WS 播放卡片 → presigned 直链可播 → PG 核对。

跑法（需 docker compose up -d + .env 已填密钥，且 :8000 / :8001 已用新代码起来）：
    PYTHONPATH=. python .smoke/b3_render_smoke.py
身份按 B5 现签：开头 POST /register 拿一次性 token，HTTP 带 Authorization: Bearer、
WS 带 ?token=（token 值全程不打印）。
不发浏览器：WS 帧、MinIO HTTP 区间读、PG 行三处各钉一遍。全程不打印任何密钥。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import httpx
import websockets

from agent_framework.storage import build_storage
from storyline_server import mediaops

BASE = "http://127.0.0.1:8000"
CONV = f"c_b3_{int(__import__('time').time())}"     # 每次跑换新会话，不复用旧产物路径

FAILS = 0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="b3_smoke_"))
    # 纯音素材会让 faster-whisper 的 VAD 判为整段非语音并卡死转写，所以先合成人声口播
    import edge_tts
    speech = tmp / "口播.mp3"
    await edge_tts.Communicate(
        "大家好，这是一段用来验证上线链路的口播。第一句开场，第二句铺垫，最后一句收尾。",
        "zh-CN-XiaoxiaoNeural").save(str(speech))
    dur = mediaops.probe(speech)["duration"] + 0.5
    src = tmp / "管线冒烟.mp4"
    mediaops.ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25:duration={dur}",
                    "-f", "lavfi", "-i", f"smptebars=size=640x360:rate=25:duration={dur}",
                    "-f", "lavfi", "-i", f"color=c=blue:size=640x360:rate=25:duration={dur}",
                    "-stream_loop", "-1", "-i", str(speech),
                    "-filter_complex",
                    f"[0:v][1:v][2:v]concat=n=3:v=1:a=0,scale=640:360,trim=duration={dur}[v]",
                    "-map", "[v]", "-map", "3:a", "-t", f"{dur}",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src))
    blob = src.read_bytes()
    print(f"合成素材 {src.name}：{len(blob)} 字节 / {mediaops.probe(src)['duration']}s 含人声",
          flush=True)

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        reg = (await c.post("/register", json={"device_name": "b3-smoke"})).json()
        user, token = reg["user_id"], reg["token"]
        print(f"身份现签：user_id={user} token 长度={len(token)}（值不打印）", flush=True)
        auth = {"Authorization": f"Bearer {token}"}
        ws_url = f"ws://127.0.0.1:8000/ws/{CONV}?token={token}"

        r = await c.post(f"/upload?conversation_id={CONV}&filename={src.name}",
                         content=blob, headers={**auth, "content-type": "video/mp4"})
        up = r.json()
        check(r.status_code == 200 and up.get("material_id"), f"POST /upload → {up}")
        mid = up["material_id"]
        check("://" in str(up.get("url", "")), "上传回放 URL 是 presigned 直链")

        async with websockets.connect(ws_url, max_size=None) as w:
            hello = json.loads(await w.recv())
            check(hello.get("type") == "connected"
                  and hello.get("session_id") == f"{user}:{CONV}",
                  f"WS 已连接（回投键的 user 段由 token 反查）：{hello}")
            q = await c.post("/chat", json={
                "conversation_id": CONV,
                "message": f"用附件 {mid} 剪一条短片，保留原声，配字幕，标题就叫「管线冒烟」",
                "attachments": [mid]}, headers=auth)
            check(q.status_code == 200, f"POST /chat 入队：{q.json()}")

            media_frame = None
            seen: list[str] = []
            answers: list[str] = []
            loop = asyncio.get_running_loop()
            until = loop.time() + 900
            while loop.time() < until and media_frame is None:
                try:
                    fr = json.loads(await asyncio.wait_for(w.recv(), timeout=30))
                except asyncio.TimeoutError:
                    print(f"… 等待回投中（已收 {seen[-6:]}）", flush=True)
                    continue
                seen.append(fr.get("type", "?"))
                if fr.get("type") == "media":
                    media_frame = fr
                elif fr.get("type") == "answer":
                    answers.append(str(fr.get("answer") or ""))
                    print(f"  [answer] {answers[-1][:200]}", flush=True)
                elif fr.get("type") == "error":
                    check(False, f"链路回投了 error：{fr}")
                    break
            if media_frame is None:
                print(f"  [帧序] {seen}\n  [末次回答] {answers[-1] if answers else '（无）'}",
                      flush=True)
            check(media_frame is not None,
                  f"WS 收到 type=media 播放卡片帧（帧序尾段 {seen[-12:]}）")
            url = (media_frame or {}).get("media_url", "")
            check(url.startswith("http://") or url.startswith("https://"),
                  f"卡片链接是 presigned http(s) 直链（不是 /media/ 也不是本地路径）：{url[:110]}")

    if url:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            rng = await c.get(url, headers={"range": "bytes=0-1023"})
            check(rng.status_code == 206 and len(rng.content) == 1024
                  and str(rng.headers.get("content-range", "")).startswith("bytes 0-1023/"),
                  f"区间读 206 可用（视频拖动）：rc={rng.status_code} "
                  f"cr={rng.headers.get('content-range')}")
            full = await c.get(url)
            check(full.status_code == 200 and len(full.content) > 10_000
                  and "video" in str(full.headers.get("content-type", "")),
                  f"整读 200：{len(full.content)} 字节 "
                  f"{full.headers.get('content-type')}")
            played = tmp / "played.mp4"
            played.write_bytes(full.content)
            info = mediaops.probe(played)
            check(info["duration"] > 0 and info["width"] > 0,
                  f"取回的字节 ffprobe 可读：{info['duration']}s {info['width']}x{info['height']}")

    # ---------- PG 侧核对：三处真相各自的形状 ----------
    storage = build_storage("pg_minio")
    await storage.start()
    try:
        jobs = await storage.db.select("render_jobs")
        check(bool(jobs), f"render_jobs 有 {len(jobs)} 行")
        mine = [r for r in jobs if r["session_id"] == f"u:{user}:c:{CONV}"]
        check(bool(mine), f"整链共用一个会话作用域：session_id=u:{user}:c:{CONV} 有 {len(mine)} 行")
        latest = max(mine or [{}], key=lambda r: str(r.get("updated_at")))
        check(latest.get("status") == "done" and latest.get("percent") == 100
              and latest.get("stage") == "done" and latest.get("video_object_key"),
              f"本次渲染任务 done/100：{ {k: latest.get(k) for k in ('session_id', 'artifact_id', 'status', 'stage', 'percent', 'duration_sec', 'error')} }")
        key = latest.get("video_object_key")
        if not key:
            raise SystemExit(f"渲染未出片，PG 核对终止（latest={latest}）")
        head = await storage.objects.head(key)
        check(head is not None and head.bytes > 10_000,
              f"成片字节在 MinIO：{key}（{head.bytes if head else 0} 字节）")
        arts = await storage.db.select("artifacts", where={"session_id": latest["session_id"],
                                                           "artifact_id": latest["artifact_id"]})
        nodes = sorted({a["node"] for a in arts})
        check({"load_media", "split_shots", "plan_timeline", "render_video"} <= set(nodes)
              and len(nodes) >= 8, f"整链产物在 PG artifacts 表：{nodes}")
        local = await storage.objects.localize(key, tmp / "from_pg")
        check(local.stat().st_size == head.bytes, "PG 记录的对象键与本地取回字节一致（无死链）")
        mats = await storage.db.select("materials", where={"id": mid})
        check(len(mats) == 1 and mats[0]["object_key"],
              f"素材行仍在 materials（object_key={mats[0]['object_key'] if mats else '—'}）")
    finally:
        await storage.close()

    print()
    print("FAILED" if FAILS else "SMOKE PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
