"""探针：容器里 edge-tts 能不能用（能不能出音频、能不能拿到词级时间轴）。

    docker cp tools/probe_tts.py creation-app:/app/tools_probe_tts.py
    docker exec creation-app python /app/tools_probe_tts.py

为什么要先探：音轨这条线全靠它。edge-tts 是**在线服务**（微软 Edge 的朗读接口），
容器没外网就整个音轨做不了——而这个失败在代码里表现为一句「TTS 失败」，
看不出是网络还是音色还是账号。
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")

OUT = Path("/tmp/tts-probe")


async def main() -> int:
    try:
        import edge_tts
    except Exception as exc:  # noqa: BLE001
        print("XX edge_tts 没装：", exc)
        return 1
    print("edge_tts", getattr(edge_tts, "__version__", "?"))
    OUT.mkdir(parents=True, exist_ok=True)

    text = "为什么傍晚的天空会变红"
    for voice in ("zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural"):
        dst = OUT / f"{voice}.mp3"
        words = []
        try:
            com = edge_tts.Communicate(text, voice, rate="+8%",
                                       boundary="WordBoundary")
            with dst.open("wb") as fh:
                async for chunk in com.stream():
                    if chunk.get("type") == "audio":
                        fh.write(chunk["data"])
                    elif chunk.get("type") == "WordBoundary":
                        words.append(chunk.get("text"))
        except Exception as exc:  # noqa: BLE001
            print(f"XX {voice} 失败：{type(exc).__name__}: {exc}")
            continue
        size = dst.stat().st_size if dst.exists() else 0
        dur = ""
        if size > 512:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(dst)],
                capture_output=True, text=True)
            dur = r.stdout.strip()
        print(f"ok {voice}: {size} 字节, {dur}s, 词级 {len(words)} 个 {words[:4]}")
    return 0


raise SystemExit(asyncio.run(main()))
