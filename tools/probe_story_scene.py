"""单镜诊断：只跑一次 `write_scene_story_code`，把失败原因原样打出来。

    docker cp tools/probe_story_scene.py creation-app:/app/tools_probe_story_scene.py
    docker exec creation-app python /app/tools_probe_story_scene.py

为什么要它：整片跑一遍 25 分钟，而"15 镜全走兜底"这种失败会被兜底盖住——
日志上只看到质检 1.0，看不出模型写的代码到底错在哪。
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from agent_framework.identity import use_identity
from agent_framework.storage import build_storage
from storyline_server.simple import film, kernel, story

USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"
SCENE = {"text": "那天，屏幕上跳出一段信号。", "hi": "一段信号",
         "plate": "一个男人站在巨大的绿色荧光屏前，屏幕上起伏着波形，"
                  "他的侧脸被屏幕照亮，背景是全黑的机房，屏幕下方有一排外星文字",
         "chapter": "第 183 号文明", "time": "地球时间 1975 年"}


async def main() -> int:
    st = build_storage("pg_minio")
    await st.start()
    await kernel.ensure_storage()

    with use_identity(USER, "probe-scene"):
        try:
            body = await story.write_scene_story_code(
                SCENE, index=0, total=1, seconds=7.0, aspect="landscape",
                user_id=USER, conversation_id="probe-scene")
        except Exception as exc:  # noqa: BLE001
            print("XX 失败：", type(exc).__name__, str(exc)[:600])
            return 1

    print("== 模型交的场景体 ==")
    print(body[:1500])
    print("\n== 自检 ==")
    print("_looks_complete:", film._looks_complete(body))
    wrapped = film.wrap_story_scene(body, SCENE, index=0)
    code, _tl = film.assemble([wrapped], [7.0], [])
    from storyline_server.motion import remotion_client as rc
    lint = rc.lint(code)
    print("lint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:8]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:140]}")
    return 0


raise SystemExit(asyncio.run(main()))
