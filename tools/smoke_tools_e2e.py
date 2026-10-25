"""端到端冒烟：**通过工具注册表**走完整链路（一句话 → 出片 → 改一镜 → 重渲）。

    docker cp tools/smoke_tools_e2e.py creation-app:/app/tools_smoke_tools_e2e.py
    docker exec creation-app python -u /app/tools_smoke_tools_e2e.py [user_id]

用 12 秒（3 镜）而不是 60 秒：链路是否通与片长无关，而 3 镜只要 5~6 次调用、
两次渲染各约 2 分钟。真机上跑 60 秒请用 `tools/smoke_film.py`。

会花模型钱（约 6 次调用）与两次渲染，不进自动测试套件。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from agent_framework.identity import use_identity
from agent_framework.tool import ToolRegistry
from agent_framework.tools.short_video import register_short_video_tools
from agent_framework.storage import build_storage
from storyline_server.simple import kernel

USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 12
CONV = "smoke-tools"
PROMPT = sys.argv[3] if len(sys.argv) > 3 else "为什么傍晚的天空会变红"


def show(tag: str, raw: str) -> dict:
    try:
        got = json.loads(raw)
    except ValueError:
        print(f"{tag}: 非 JSON → {raw[:200]}")
        return {}
    keep = {k: v for k, v in got.items() if k not in ("hint",)}
    print(f"{tag}: {json.dumps(keep, ensure_ascii=False)[:400]}")
    return got


async def main() -> int:
    st = build_storage("pg_minio")
    await st.start()
    await kernel.ensure_storage()

    reg = ToolRegistry()
    register_short_video_tools(reg, storage=st)

    with use_identity(USER, CONV):
        print("== ① 一句话出片（12 秒 / 3 镜，含渲染）==")
        got = show("make", await reg.execute(
            "make_short_video",
            {"prompt": PROMPT, "seconds": SECONDS, "aspect": "portrait", "render": True}))
        if not got.get("ok"):
            print("XX 出片失败")
            return 1
        fid = got["film_id"]
        print(f"   film_id={fid} 镜数={got['shots']} 调用={got['calls']} "
              f"质检={got['audit']['ratio']} 成片={bool(got['mp4_url'])}")

        print("\n== ② 只改第 2 镜（1 次调用，出静帧）==")
        got2 = show("reshoot", await reg.execute(
            "reshoot_shot",
            {"film_id": fid, "shot": 2, "note": "第二个画面太暗了，加一块亮的饱和色"}))
        if not got2.get("ok"):
            print("XX 改镜失败")
            return 1
        print(f"   判定={got2['verdict']} 静帧 {len(got2['stills'])} 张")

        print("\n== ③ 按当前工程重渲整片（不再调模型）==")
        got3 = show("render", await reg.execute("render_short_film", {"film_id": fid}))
        if not got3.get("ok"):
            print("XX 重渲失败")
            return 1

        print("\n== ④ 列最近工程 ==")
        got4 = show("list", await reg.execute("list_short_films", {}))
        hit = [r for r in got4.get("films") or [] if r["id"] == fid]
        print(f"   找到本条：{bool(hit)}")

    print("\nOK")
    return 0


raise SystemExit(asyncio.run(main()))
