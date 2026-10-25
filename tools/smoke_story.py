"""叙事模式真机冒烟：**用参考片自己的帧当插画**，验"出图以外"的整条链路。

    docker cp tools/smoke_story.py creation-app:/app/tools_smoke_story.py
    docker exec creation-app nohup python -u /app/tools_smoke_story.py > /tmp/story.log 2>&1 &

验的是：长镜（12 秒）→ Plate 缓推 → 章节号/时间标记/高亮字幕 → 压暗 → fade/硬切
→ 一次渲完。**唯一没验的是"图从哪来"**，那一步由 `plate.py` 的后端接口负责。

`voice=False` 是有意的：这里要对齐的是**画面节奏**（参考片平均 10.39 秒一镜），
旁白会把镜长压到 4~5 秒，反而看不清长镜对不对。旁白链路在 30s/168s 那两条验过。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from storyline_server.simple import film, kernel

PLATES = "/tmp/plates-ref"
SECONDS = 108.0
OUT = "/app/.runtime/simple/story-ref.mp4"

#: 与 9 张插画对应的 9 个 beat（文案按三体那条片子的叙事走）。
SCENES = [
    {"text": "这已经是第 183 次。", "hi": "第 183 次",
     "chapter": "第 183 号文明", "time": "地球时间 1975 年"},
    {"text": "假如你是三体人。", "hi": "三体人",
     "chapter": "第 183 号文明", "time": ""},
    {"text": "直到那天，你们收到一封信——", "hi": "一封信",
     "chapter": "第一次接触", "time": "地球时间 1979 年"},
    {"text": "那天，屏幕上跳出一段信号。", "hi": "一段信号",
     "chapter": "第一次接触", "time": "地球时间 1979 年"},
    {"text": "不要回答。不要回答。不要回答。", "hi": "不要回答",
     "chapter": "第一次接触", "time": ""},
    {"text": "两百年后，两千艘战舰列阵。", "hi": "两千艘",
     "chapter": "末日战役", "time": "2205 年"},
    {"text": "水滴只用了一刻钟。", "hi": "一刻钟",
     "chapter": "末日战役", "time": "2205 年"},
    {"text": "你在 1379 号站，看见了这一切。", "hi": "1379 号站",
     "chapter": "监听员", "time": ""},
    {"text": "换你守在那里：上报，还是警告？", "hi": "警告",
     "chapter": "监听员", "time": "一切开始之前"},
]


async def main() -> int:
    await kernel.ensure_storage()

    def stage(name, done, total):
        print(f"  [{name}] {done}/{total}", flush=True)

    got = await film.make_film(
        "假如你是三体人", seconds=SECONDS, aspect="landscape",
        mode="story", plates_dir=PLATES, scenes=SCENES, voice=False,
        on_stage=stage)

    pl = got.get("plates") or {}
    print(f"\n镜数 {got['shots']}  实际 {got['total_sec']}s  调用 {got['calls']}  "
          f"插画 {len(pl.get('made') or [])} 张（{pl.get('mode')}）")
    print(f"每镜秒数 {got['secs']}")
    print(f"转场 {got['transitions']}")
    aud = got.get("audit") or {}
    print(f"质检 {aud.get('ratio')}  不合格 {aud.get('bad') or '无'}")
    for row in aud.get("shots") or []:
        if row.get("worst") != "ok":
            print(f"  s{row['index']:02d} {row['worst']} 内容{row['content']:.0%} "
                  f"亮度{row['mean_lum']:.0f} 有色{row.get('vivid', 0):.0%}")

    lint = await asyncio.to_thread(__import__(
        "storyline_server.motion.remotion_client", fromlist=["lint"]).lint, got["code"])
    print("lint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:6]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:110]}")
    if not lint.get("ok"):
        open("/tmp/story.tsx", "w", encoding="utf-8").write(got["code"])
        print("（模块已存 /tmp/story.tsx）")
        return 1

    path, res = await kernel.render(got["code"], seconds=got["total_sec"],
                                   aspect="landscape", out_path=OUT)
    print("\n成片", path, path.stat().st_size // 1024, "KB")
    print("回执", json.dumps(kernel.describe_receipt(res), ensure_ascii=False)[:220])
    return 0


raise SystemExit(asyncio.run(main()))
