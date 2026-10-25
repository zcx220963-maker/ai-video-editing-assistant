"""看一条工程的全部读数：镜数、每镜秒数、旁白、元素数、成片有没有音轨。

    docker cp tools/inspect_film.py creation-app:/app/tools_inspect_film.py
    docker exec creation-app python /app/tools_inspect_film.py            # 最近一条
    docker exec creation-app python /app/tools_inspect_film.py <film_id>

为什么要这个脚本：工程的真相分散在 `film.json`（读数）、`film.tsx`（画面）、
`audio.m4a`（旁白）三处，而"成片有没有声音"还得 ffprobe 一遍。
一条命令看全，比每次现敲一堆引号可靠（PowerShell 里内联 python 的引号极容易炸）。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from storyline_server.simple import project  # noqa: E402


def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name",
         "-of", "json", str(path)], capture_output=True, text=True)
    try:
        doc = json.loads(r.stdout or "{}")
    except ValueError:
        return {}
    streams = doc.get("streams") or []
    return {
        "sec": round(float((doc.get("format") or {}).get("duration") or 0), 2),
        "video": next((s.get("codec_name") for s in streams
                       if s.get("codec_type") == "video"), ""),
        "audio": next((s.get("codec_name") for s in streams
                       if s.get("codec_type") == "audio"), ""),
    }


def main() -> int:
    fid = sys.argv[1] if len(sys.argv) > 1 else ""
    if not fid:
        rows = project.list_recent(1)
        if not rows:
            print("还没有工程")
            return 1
        fid = rows[0]["id"]
    doc = project.load(fid)
    d = project.dir_of(fid)

    print(f"工程 {fid}")
    print(f"  主题 {doc.get('prompt')}")
    print(f"  镜数 {len(doc.get('bodies') or [])}  目标 {doc.get('seconds')}s  "
          f"实际 {round(sum(doc.get('secs') or []), 2)}s")
    print(f"  每镜秒数 {doc.get('secs')}")
    print(f"  转场 {doc.get('kinds')}")
    print(f"  调用 {doc.get('calls')}  覆盖率 {doc.get('coverage')}")
    aud = doc.get("audio") or ""
    print(f"  音轨字段 {aud or '（无）'}  文件存在 "
          f"{(d / aud).exists() if aud else False}")
    for name in ("film.tsx", "audio.m4a"):
        p = d / name
        print(f"  {name:<10} {'%.1f KB' % (p.stat().st_size / 1024) if p.exists() else '缺'}")
    print(f"  成片链接 {'有' if doc.get('mp4_url') else '无'}")

    mp4s = sorted(Path("/app/.runtime/simple").glob("*.mp4"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if mp4s:
        info = probe(mp4s[0])
        print(f"  最近一次渲染 {mp4s[0].name}: {info.get('sec')}s "
              f"video={info.get('video')} audio={info.get('audio') or '（无音轨）'}")
    return 0


raise SystemExit(main())
