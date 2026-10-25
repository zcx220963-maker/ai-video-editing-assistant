"""分析一条参考片：参数、切点节奏、亮度/饱和度分布、抽帧。

    docker cp tools/analyze_reference.py creation-app:/app/tools_analyze_reference.py
    docker exec creation-app python /app/tools_analyze_reference.py /tmp/ref.mp4 [抽帧数]

为什么用脚本而不是 ffmpeg 一行行敲：`select='gt(scene,0.28)'` 这种带引号的滤镜
在 PowerShell → docker exec → sh 三层转义里必炸（已经炸过两次）。subprocess 传
list 参数就没有这个问题。

四个读数分别回答：
· 切点 → 节奏（几镜、平均镜长、最长镜）
· YAVG → 亮度（暗场？高调？）
· SATAVG → 饱和度（黑白片 <5、高级暗调 8~12、鲜艳 25+）
· 抽帧 → 构图与元素密度（这个只能看）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path("/tmp/ref-frames")


def run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    return (p.stdout or "") + (p.stderr or "")


def probe(path: str) -> dict:
    raw = run(["ffprobe", "-v", "error", "-show_entries",
               "format=duration,bit_rate:stream=codec_name,codec_type,width,height,"
               "r_frame_rate,nb_frames", "-of", "json", path])
    doc = json.loads(raw[raw.index("{"):])
    v = next((s for s in doc.get("streams") or [] if s.get("codec_type") == "video"), {})
    a = next((s for s in doc.get("streams") or [] if s.get("codec_type") == "audio"), {})
    fps = v.get("r_frame_rate") or "0/1"
    num, _, den = fps.partition("/")
    return {
        "duration": round(float(doc["format"]["duration"]), 2),
        "mbps": round(int(doc["format"].get("bit_rate") or 0) / 1e6, 2),
        "size": f"{v.get('width')}x{v.get('height')}",
        "fps": round(float(num) / float(den or 1), 2) if num else 0,
        "frames": int(v.get("nb_frames") or 0),
        "vcodec": v.get("codec_name"),
        "acodec": a.get("codec_name") or "（无音轨）",
    }


def cuts(path: str, thr: float = 0.28) -> list[float]:
    out = run(["ffmpeg", "-v", "info", "-i", path, "-vf",
               f"select='gt(scene,{thr})',showinfo", "-an", "-f", "null", "/dev/null"])
    return [round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", out)]


def stats(path: str) -> dict:
    """亮度 / 饱和度：抽每 15 帧算一次 signalstats，取分位数。"""
    out = run(["ffmpeg", "-v", "error", "-i", path, "-vf",
               "select='not(mod(n,15))',signalstats,metadata=print:file=-",
               "-an", "-f", "null", "/dev/null"])
    ys = [float(x) for x in re.findall(r"lavfi\.signalstats\.YAVG=([0-9.]+)", out)]
    ss = [float(x) for x in re.findall(r"lavfi\.signalstats\.SATAVG=([0-9.]+)", out)]
    if not ys:
        return {}

    def q(v: list[float], p: float) -> float:
        v = sorted(v)
        return round(v[min(len(v) - 1, int(len(v) * p))], 1)

    return {"YAVG p10/p50/p90": [q(ys, .1), q(ys, .5), q(ys, .9)],
            "SATAVG p10/p50/p90": [q(ss, .1), q(ss, .5), q(ss, .9)],
            "samples": len(ys)}


def frames(path: str, n: int, duration: float) -> list[str]:
    OUT.mkdir(parents=True, exist_ok=True)
    got = []
    step = duration / max(1, n)
    for i in range(n):
        t = round(i * step + step * 0.4, 2)
        dst = OUT / f"f{i:02d}.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", path,
             "-frames:v", "1", "-vf", "scale=480:-1", str(dst)])
        if dst.exists():
            got.append(f"{dst}  @{t}s")
    return got


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ref.mp4"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    # 第三个参数是抽帧目录。**必须能分开**：两条片子都往 /tmp/ref-frames 写，
    # 后跑的那条会把前一条的帧覆盖掉（做对比图时踩过，结果左右两栏都是自己）。
    if len(sys.argv) > 3:
        OUT = Path(sys.argv[3])
    info = probe(src)
    print("== 参数 ==")
    for k, v in info.items():
        print(f"  {k:<9} {v}")

    print("\n== 节奏 ==")
    cs = cuts(src)
    print(f"  切点 {len(cs)} 处（阈值 scene>0.28）")
    if cs:
        bounds = [0.0, *cs, info["duration"]]
        lens = [round(b - a, 2) for a, b in zip(bounds, bounds[1:])]
        print(f"  镜数 ≈ {len(lens)}  平均 {round(sum(lens) / len(lens), 2)}s  "
              f"最长 {max(lens)}s  最短 {min(lens)}s")
        print(f"  前 12 段时长 {lens[:12]}")
        print(f"  切点时间 {cs[:14]}")

    print("\n== 明暗与颜色 ==")
    for k, v in stats(src).items():
        print(f"  {k:<18} {v}")

    print("\n== 抽帧 ==")
    for row in frames(src, n, info["duration"]):
        print("  " + row)
