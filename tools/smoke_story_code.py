"""叙事模式真机：**画面全由代码画**（不用插画、不调出图 API）。

    docker cp tools/smoke_story_code.py creation-app:/app/tools_smoke_story_code.py
    docker exec creation-app nohup python -u /app/tools_smoke_story_code.py > /tmp/sc.log 2>&1 &

参考片（三体叙事 135 秒）放大看是纯代码画的，所以这条要验的就是"我们也能画"：
分镜 1 次 → 每镜用电影感零件画场景 N 次 → 外层套 Camera + 章节号/时间/高亮字幕
→ 暗调判据 → 一次渲完。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from agent_framework.identity import use_identity
from agent_framework.storage import build_storage
from storyline_server.motion import remotion_client as rc
from storyline_server.simple import film, kernel

SECONDS = 135.0
OUT = "/app/.runtime/simple/story-code.mp4"
USER = sys.argv[1] if len(sys.argv) > 1 else "u-ebbfe7487e9a"
PROMPT = ("假如你是三体人：第 183 号文明收到地球的信号，做出回答；"
          "两百年后末日战役，两千艘战舰列阵；最后换你守在 1379 号站，上报还是警告")


async def main() -> int:
    # 必须自己绑存储 + 身份：模型密钥按 user_id 从 app_secrets 热读，
    # 不绑的话 `_complete` 只会看到环境变量，然后报「未配置模型密钥」。
    st = build_storage("pg_minio")
    await st.start()
    await kernel.ensure_storage()

    def stage(name, done, total):
        print(f"  [{name}] {done}/{total}", flush=True)

    with use_identity(USER, "smoke-story"):
        got = await film.make_film(PROMPT, seconds=SECONDS, aspect="landscape",
                                   mode="story", user_id=USER,
                                   conversation_id="smoke-story", on_stage=stage)

    print(f"\n镜数 {got['shots']}  实际 {got['total_sec']}s  调用 {got['calls']}  "
          f"画面={got.get('picture')}")
    print(f"每镜秒数 {got['secs']}")
    print(f"转场 {got['transitions']}")
    aud = got.get("audit") or {}
    print(f"质检 {aud.get('ratio')}  不合格 {aud.get('bad') or '无'}")
    for row in aud.get("shots") or []:
        if row.get("worst") != "ok":
            print(f"  s{row['index']:02d} {row['worst']} 内容{row['content']:.0%} "
                  f"亮度{row['mean_lum']:.0f} 有色{row.get('vivid', 0):.0%}")
    cov = got.get("coverage") or {}
    per = cov.get("per_shot") or []
    # **两把尺子**：`variety` 数"几种画法"（结构指纹去重）；`per_shot` 列每镜用了哪些件。
    # 只有前者接近镜数、后者基本不含件名，才说明"每镜各画各的"，不是在重排零件。
    errs = got.get("scene_errors") or []
    print(f"场景写失败 {len(errs)} 镜" + (f"：{errs[:2]}" if errs else ""))
    print(f"模板化读数：{got.get('variety')} 种画法 / {len(per)} 镜")
    for i, x in enumerate(per[:6], 1):
        print(f"  s{i:02d} {x}")
    v = got.get("voice") or {}
    print(f"旁白 {v.get('degraded') and '退回无声' or 'ok'}  失败镜 {v.get('failed') or '无'}")

    lint = await asyncio.to_thread(rc.lint, got["code"])
    print("lint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:8]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:120]}")
    if not lint.get("ok"):
        open("/tmp/story-code.tsx", "w", encoding="utf-8").write(got["code"])
        print("（模块已存 /tmp/story-code.tsx）")
        return 1

    path, res = await kernel.render(got["code"], seconds=got["total_sec"],
                                    aspect="landscape", out_path=OUT)
    print("\n成片", path, path.stat().st_size // 1024, "KB")
    print("回执", json.dumps(kernel.describe_receipt(res), ensure_ascii=False)[:200])
    return 0


raise SystemExit(asyncio.run(main()))
