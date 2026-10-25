"""多镜成片冒烟：主题 -> N 镜 -> 一个模块 -> 一次渲完。

在容器里跑：

    docker cp tools/smoke_film.py creation-app:/app/tools_smoke_film.py
    docker exec creation-app python /app/tools_smoke_film.py "主题" 60 [user_id]

会花 N+1 次模型调用 + 一次渲染（60 秒约 4 分钟、几分钱），不进自动测试套件。
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")

from storyline_server.motion import remotion_client as rc
from storyline_server.simple import film, kernel

PROMPT = sys.argv[1] if len(sys.argv) > 1 else "为什么天空是蓝色的"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 60
USER = sys.argv[3] if len(sys.argv) > 3 else "u-ebbfe7487e9a"
OUT = f"/app/.runtime/simple/film-{int(SECONDS)}s.mp4"


async def main():
    await kernel.ensure_storage()

    def stage(name, done, total):
        print(f"  [{name}] {done}/{total}", flush=True)

    got = await film.make_film(PROMPT, seconds=SECONDS, user_id=USER, on_stage=stage)
    cov = got["coverage"]
    tl = got.get("timeline") or {}
    print(f"\n镜数 {got['shots']}，各镜 {got['secs'][:4]}…，合计 {got['total_sec']}s，"
          f"调用 {got['calls']} 次")
    print(f"时间表：总 {tl.get('total')} 帧，切点 {tl.get('cuts')}")
    print(f"转场：{got.get('transitions')}")
    print(f"组件覆盖率 {cov['ratio']:.0%}（{cov['used']}/{cov['total']}），"
          f"没用组件的镜：{cov['missing'] or '无'}")
    aud = got.get("audit") or {}
    if aud.get("ok"):
        bad = aud.get("bad") or []
        print(f"质检：抽 {aud.get('frames')} 帧，合格率 "
              f"{(len(aud['shots']) - len(bad)) / max(1, len(aud['shots'])):.0%}"
              f"，打回重写 {got.get('rewritten') or '无'}，"
              f"补色带 {got.get('patched') or '无'}，仍未合格 {bad or '无'}")
        for s in aud["shots"]:
            if s["worst"] != "ok":
                print(f"    s{s['index']:02d} {s['worst']} 内容{s['content']:.0%} "
                      f"有色{s.get('vivid', 0):.0%} 亮度{s['mean_lum']:.0f}")
    else:
        print("质检未判：", aud.get("error"))
    print("模块", len(got["code"].splitlines()), "行 /", len(got["code"]), "字符")
    open("/tmp/film.tsx", "w", encoding="utf-8").write(got["code"])

    lint = rc.lint(got["code"])
    print("\nlint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:8]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:100]}")
    if not lint.get("ok"):
        print("（模块已存 /tmp/film.tsx）")
        return

    path, res = await kernel.render(got["code"], seconds=got["total_sec"],
                                    out_path=OUT)
    print("\n成片:", path, path.stat().st_size // 1024, "KB")
    print("回执:", json.dumps(kernel.describe_receipt(res), ensure_ascii=False)[:300])


asyncio.run(main())
