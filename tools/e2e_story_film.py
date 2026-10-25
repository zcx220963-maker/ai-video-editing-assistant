# -*- coding: utf-8 -*-
"""端到端：真跑一条**叙事片**，验证「曲库配乐 + 旁白 + 渲染」整条路。

为什么必须真跑：这条链有四处只在真机才暴露的东西——分镜镜数、语音定镜长、
曲库取歌、混音后的音轨。任何一处静默失效，单测都不会红。

跑法（容器里，后台跑，20 分钟量级）：
    nohup python /app/tools/e2e_story_film.py <user_id> <seconds> > /tmp/e2e.log 2>&1 &
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

BASE = "http://localhost:8000"
PROMPT = ("人类第一次收到来自半人马座的外星信号。1975 年一台射电望远镜捕捉到一段"
          "重复的脉冲，四十年后人们才明白那是一次告别。")


def call(path: str, tok: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"authorization": f"Bearer {tok}",
                 "content-type": "application/json"},
        method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def probe(path: Path) -> dict:
    """量成片：时长、有没有音轨、亮度、饱和度。"""
    ffprobe = "ffprobe"
    out = {}
    d = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True).stdout.strip()
    out["duration"] = float(d) if d else 0.0
    st = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a",
                         "-show_entries", "stream=codec_name",
                         "-of", "default=nw=1:nk=1", str(path)],
                        capture_output=True, text=True).stdout.strip()
    out["audio"] = st or "(none)"
    vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    for line in vol.splitlines():
        if "mean_volume:" in line:
            out["mean_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
    sig = subprocess.run(["ffprobe", "-v", "error", "-f", "lavfi", "-i",
                          f"movie={path},signalstats", "-show_entries",
                          "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.SATAVG",
                          "-of", "csv=p=0"], capture_output=True, text=True).stdout
    ys, ss = [], []
    for row in sig.strip().splitlines():
        parts = row.split(",")
        if len(parts) >= 2:
            try:
                ys.append(float(parts[0]))
                ss.append(float(parts[1]))
            except ValueError:
                continue
    if ys:
        ys.sort()
        ss.sort()
        out["yavg_p50"] = round(ys[len(ys) // 2], 1)
        out["satavg_p50"] = round(ss[len(ss) // 2], 1)
    return out


async def main() -> int:
    uid = sys.argv[1] if len(sys.argv) > 1 else "u-6d962435c873"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0

    from agent_framework.auth import Authenticator
    from agent_framework.storage import build_storage

    st = build_storage("pg_minio")
    await st.start()
    tok = await Authenticator(st).issue_jwt(uid)
    print(f"用户 {uid}　目标 {seconds:g} 秒　画法 story", flush=True)

    sub = call("/simple/video", tok, {
        "prompt": PROMPT, "seconds": seconds, "aspect": "landscape",
        "mode": "story", "voice": True})
    jid = sub["job_id"]
    print(f"作业 {jid}　mode={sub.get('mode')} style={sub.get('style')}", flush=True)

    t0 = time.time()
    last = ""
    view: dict = {}
    while time.time() - t0 < 3600:
        await asyncio.sleep(20)
        try:
            view = call(f"/simple/job/{jid}", tok)
        except Exception as exc:  # noqa: BLE001
            print(f"  轮询失败：{exc}", flush=True)
            continue
        p = view.get("progress") or {}
        line = f"[{int(time.time() - t0):4d}s] {view.get('status')}/{view.get('stage')}"
        if p:
            line += f" {p.get('stage')} {p.get('done')}/{p.get('total')}"
        if line != last:
            print(line, flush=True)
            last = line
        if view.get("status") in ("done", "failed"):
            break

    print("-" * 60, flush=True)
    print(json.dumps({k: view.get(k) for k in
                      ("status", "error", "shots", "calls", "voice", "bgm",
                       "coverage", "receipt", "style")},
                     ensure_ascii=False, indent=1), flush=True)
    # 成片字段在 `_view` 里叫 `mp4`（不是 `mp4_url`）。
    url = view.get("mp4") or ""
    print(f"mp4: {url[:110]}", flush=True)
    if view.get("status") != "done":
        await st.close()
        return 1

    # presigned 链接的 host 是 MINIO_PUBLIC_ENDPOINT（127.0.0.1:9000），容器内连不上
    # ——本地文件还在，量它就行。
    dst = Path(f"/app/.runtime/simple/{jid}-av.mp4")
    if not dst.exists():
        print(f"XX 本地成片不在：{dst}", flush=True)
        await st.close()
        return 1
    print(f"成片 {dst}（{dst.stat().st_size / 1048576:.1f} MB）", flush=True)
    print(json.dumps(probe(dst), ensure_ascii=False), flush=True)
    await st.close()
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
