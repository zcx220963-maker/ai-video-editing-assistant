"""探针：电影感零件到底画出来没有（不调模型）。

    docker cp tools/probe_cinema.py creation-app:/app/tools_probe_cinema.py
    docker exec creation-app python /app/tools_probe_cinema.py

为什么需要它：叙事片真机跑出来 10/15 镜被判 `empty`（内容占比 5%、亮度 17）。
两种可能——① 模型几乎没画东西；② 我们新写的零件本身不出像素。
这个探针用手写的场景（一定"画了东西"）单独渲一张静帧，把两种可能分开。
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from storyline_server.simple import film, kernel, qa, system

BODY = """const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <>
      <Sky top="#0a1020" mid="#241d33" low="#7a4630" glowHue="#ffd9a0" glow={0.7} />
      <StarField count={160} seed={9} t={t} />
      <Horizon y={0.70} ground="#080b12" glowHue="#ffb066" ripple={0.6} t={t} />
      <Dish x={0.66} y={0.62} r={0.24} tilt={-16} rim="#7fe6cf" t={t} />
      <Figure x={0.16} y={0.98} h={0.42} rim="#7fe6cf" t={t} />
      <Beam x={0.66} y={0.42} angle={250} spread={9} length={0.7} hue="#8ff0d8" opacity={0.5} t={t} />
    </>
  );
};"""


async def main() -> int:
    scene = {"text": "直到那天，你们收到一封信——", "hi": "一封信",
             "chapter": "第 183 号文明", "time": "地球时间 1975 年"}
    wrapped = film.wrap_story_scene(BODY, scene, index=0)
    code, tl = film.assemble([wrapped], [6.0], [])
    print("模块", len(code.splitlines()), "行")
    print("用到的零件:", system.used_components(wrapped))

    from storyline_server.motion import remotion_client as rc
    lint = rc.lint(code)
    print("lint:", "OK" if lint.get("ok") else "FAIL")
    for e in (lint.get("errors") or [])[:5]:
        print(f"  · 第 {e.get('line')} 行 [{e.get('rule')}] {str(e.get('msg'))[:100]}")

    pairs = await kernel.stills_pairs(code, [30, 90, 150], seconds=6.0,
                                      aspect="landscape", scale=0.5)
    print(f"\n静帧 {len(pairs)} 张")
    for p in pairs:
        m = qa.measure(p["jpeg_b64"])
        print(f"  frame {p['frame']}: 内容 {m.get('content')} 亮度 {m.get('mean_lum')} "
              f"有色 {m.get('vivid')} 边缘 {m.get('edge')}")
        open(f"/tmp/cinema-{p['frame']}.jpg", "wb").write(
            __import__("base64").b64decode(p["jpeg_b64"]))
    print("（帧已存 /tmp/cinema-*.jpg）")
    return 0


raise SystemExit(asyncio.run(main()))
