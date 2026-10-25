# -*- coding: utf-8 -*-
"""取回一条已完成作业的成片并量它（`_view` 里成片字段叫 `mp4`，不是 `mp4_url`）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, "/app")


def probe(path: Path) -> dict:
    out: dict = {}
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True).stdout.strip()
    out["duration"] = round(float(d), 2) if d else 0.0
    st = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                         "-show_entries", "stream=codec_name,sample_rate",
                         "-of", "default=nw=1", str(path)],
                        capture_output=True, text=True).stdout.strip()
    out["audio"] = st.replace("\n", " ") or "(none)"
    vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path),
                          "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    for line in vol.splitlines():
        if "mean_volume:" in line:
            out["mean_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
        if "max_volume:" in line:
            out["max_db"] = float(line.split("max_volume:")[1].split("dB")[0])
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
    jid = sys.argv[1]
    uid = sys.argv[2] if len(sys.argv) > 2 else "u-6d962435c873"
    from agent_framework.auth import Authenticator
    from agent_framework.storage import build_storage

    st = build_storage("pg_minio")
    await st.start()
    tok = await Authenticator(st).issue_jwt(uid)
    req = urllib.request.Request(f"http://localhost:8000/simple/job/{jid}",
                                 headers={"authorization": f"Bearer {tok}"})
    view = json.loads(urllib.request.urlopen(req, timeout=60).read())
    print(json.dumps({k: view.get(k) for k in
                      ("status", "style", "shots", "calls", "voice", "bgm",
                       "coverage", "seconds")}, ensure_ascii=False, indent=1))
    url = view.get("mp4") or ""
    print(f"mp4: {url[:120]}")
    # presigned 链接的 host 是 MINIO_PUBLIC_ENDPOINT（127.0.0.1:9000），容器内连不上
    # ——本地文件还在（`_run_film` 落在 out_dir 里），量它就行。
    dst = Path(f"/app/.runtime/simple/{jid}-av.mp4")
    if not dst.exists():
        print(f"XX 本地成片不在：{dst}")
        await st.close()
        return 1
    print(f"成片 {dst}（{dst.stat().st_size / 1048576:.1f} MB）")
    print(json.dumps(probe(dst), ensure_ascii=False))
    await st.close()
    return 0


raise SystemExit(asyncio.run(main()))
