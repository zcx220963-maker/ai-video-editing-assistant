# -*- coding: utf-8 -*-
"""渲一帧看排版：字幕与 TimeMark 有没有撞、字号对不对、黑条有没有了。

为什么不重渲整片：位置类改动用静帧看就够，一条 60 秒片子要 16 分钟。
静帧几秒，改错立刻知道。

跑法：python tools/probe_layout.py <film_id> [镜号]
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

from storyline_server.simple import kernel, system  # noqa: E402
from storyline_server.simple.film import SCENE_HEADER  # noqa: E402


async def main() -> int:
    fid = sys.argv[1]
    want = [int(x) for x in sys.argv[2:]] or None
    doc = json.loads((Path("/app/.runtime/simple/films") / fid / "film.json").read_text("utf-8"))
    bodies = doc.get("bodies") or []
    secs = [float(s) for s in doc.get("secs") or []]
    aspect = doc.get("aspect") or "landscape"
    out = Path("/tmp/layout")
    out.mkdir(exist_ok=True)
    for f in out.glob("*.jpg"):
        f.unlink()

    picks = [i for i in range(len(bodies)) if not want or (i + 1) in want]
    for i in picks:
        # **`dur` 得自己接上**：`kernel.props_of()` 只给 `durationInFrames`，
        # 没有 `dur` 这个 prop，于是 `dur > 0` 为假、`t` 恒为 0——
        # 字幕与时间标记的 `opacity` 停在淡入起点（0），静帧里一个字都看不见。
        # 整片渲染没这问题（`assemble()` 会传 `dur={...}`），只有单镜静帧要补。
        body = bodies[i].replace(
            "const Scene = ({ dur }: { dur: number }) => {",
            "const Scene = () => { const dur = useVideoConfig().durationInFrames;", 1)
        code = (SCENE_HEADER + "\n" + system.SYSTEM_TSX + "\n" + body
                + "\n\nexport default Scene;\n")
        # 取 0.55 处：字幕已经入场（start=0.04 后 0.3 走完），TimeMark 也在了
        frame = max(2, int(secs[i] * 0.55 * 30))
        pairs = await kernel.stills_pairs(code, [frame], seconds=secs[i],
                                         aspect=aspect, scale=0.75)
        dst = out / f"s{i + 1:02d}.jpg"
        dst.write_bytes(base64.b64decode(pairs[0]["jpeg_b64"]))
        print(f"s{i + 1:02d} frame={frame} → {dst}")
    print(f"\n共 {len(picks)} 张，在 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
