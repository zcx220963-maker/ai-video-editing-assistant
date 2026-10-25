"""长片冒烟：**只走 make_short_video**（不 reshoot、不重渲），验 180 秒这条上限。

    docker cp tools/smoke_long.py creation-app:/app/tools_smoke_long.py
    docker exec creation-app nohup python -u /app/tools_smoke_long.py 180 > /tmp/long.log 2>&1 &

为什么单独一个：180 秒 = 40 镜 + 120 帧质检 + 5400 帧渲染，一次约 50 分钟。
走完整 e2e（再改一镜 + 再重渲一次）要翻倍，而那两步在 30 秒那条已经验过了。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from agent_framework.identity import use_identity
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry
from agent_framework.tools.short_video import register_short_video_tools
from storyline_server.simple import kernel

USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 180
PROMPT = sys.argv[3] if len(sys.argv) > 3 else "为什么天空是蓝色的：从瑞利散射讲到日落变红"


async def main() -> int:
    st = build_storage("pg_minio")
    await st.start()
    await kernel.ensure_storage()
    reg = ToolRegistry()
    register_short_video_tools(reg, storage=st)

    with use_identity(USER, "smoke-long"):
        print(f"== make_short_video {SECONDS:g}s ==", flush=True)
        raw = await reg.execute("make_short_video",
                                {"prompt": PROMPT, "seconds": SECONDS,
                                 "aspect": "landscape", "render": True})
    got = json.loads(raw)
    keep = {k: v for k, v in got.items() if k != "hint"}
    print(json.dumps(keep, ensure_ascii=False, indent=1)[:1600])
    return 0 if got.get("ok") else 1


raise SystemExit(asyncio.run(main()))
