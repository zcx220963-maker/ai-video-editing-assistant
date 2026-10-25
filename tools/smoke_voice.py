"""音轨冒烟：TTS → 定镜长 → 铺时间轴 → 与画面合流（不花模型钱）。

    docker cp tools/smoke_voice.py creation-app:/app/tools_smoke_voice.py
    docker exec creation-app python -u /app/tools_smoke_voice.py

用现成的 12 秒片子当画面（`/app/.runtime/simple/cdd8f54b0ca3c3f1.mp4` 或任意 mp4），
只验"声音那条流水线"能不能跑通、时长对不对。
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from storyline_server.simple import voice as vo

OUT = Path("/tmp/voice-probe")
TEXTS = ["为什么傍晚的天空会变红", "蓝光被散射得最厉害", "剩下红橙穿过大气"]


def dur(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(p)],
                       capture_output=True, text=True)
    try:
        return round(float(r.stdout.strip()), 3)
    except ValueError:
        return 0.0


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    print("① 合成")
    narr = await vo.narrate(TEXTS, OUT / "mp3")
    print("   语音秒数", narr["secs"], "失败", narr["failed"])
    assert not narr["failed"], "有镜合成失败"

    print("\n② 定镜长")
    secs = vo.fit_secs(narr["secs"])
    print("   镜长", secs, "合计", round(sum(secs), 2))
    assert all(vo.MIN_SHOT_SEC - 1e-6 <= s <= vo.MAX_SHOT_SEC + 1e-6 for s in secs)
    print("   超长镜（会被切尾）", vo.over_long(narr["secs"]))

    print("\n③ 铺时间轴")
    track = vo.build_track(narr["files"], secs, OUT / "track.m4a", work=OUT / "parts")
    got = dur(track)
    print(f"   音轨 {track.name} {got}s（应为 {round(sum(secs), 2)}s）")
    assert abs(got - sum(secs)) < 0.15, f"音轨长度 {got} 与镜长合计 {sum(secs)} 不一致"

    print("\n④ 合流")
    vids = sorted(Path("/app/.runtime/simple").glob("*.mp4"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not vids:
        print("   （没有现成 mp4，跳过合流）")
        return 0
    src = vids[0]
    print(f"   画面 {src.name} {dur(src)}s")
    av = vo.mux(src, track, OUT / "av.mp4")
    print(f"   成片 {av.name} {dur(av)}s（-shortest 取较短的一边）")
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_name,channels",
                        "-of", "default=nw=1", str(av)], capture_output=True, text=True)
    print("   音轨流：", " ".join(r.stdout.split()))
    assert "codec_name=aac" in r.stdout, "成片里没有 aac 音轨"

    print("\nOK")
    return 0


raise SystemExit(asyncio.run(main()))
