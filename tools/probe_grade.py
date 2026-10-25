# -*- coding: utf-8 -*-
"""决定性验证：同一镜渲两版（有/无提亮层），量 YAVG 差。

为什么不能拿两条不同片子比：主题、画面、光源都不同，比出来的是"这两条片子谁亮"，
不是"提亮层有没有用"。要判提亮层，必须只改这一个变量。

跑法：python tools/probe_grade.py <film_id>
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

from storyline_server.simple import kernel, system  # noqa: E402
from storyline_server.simple.film import SCENE_HEADER  # noqa: E402

GRADE_LINE = ("      <AbsoluteFill style={{ backgroundColor: '#ffffff',"
              " opacity: 0.05 }} />\n")


def yavg(jpg: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "lavfi", "-i",
         f"movie={jpg},signalstats", "-show_entries",
         "frame_tags=lavfi.signalstats.YAVG", "-of", "csv=p=0"],
        capture_output=True, text=True).stdout.strip()
    try:
        return float(out.splitlines()[0].split(",")[0])
    except (IndexError, ValueError):
        return -1.0


async def one(body: str, tag: str, seconds: float, aspect: str) -> tuple[float, Path]:
    code = SCENE_HEADER + "\n" + system.SYSTEM_TSX + "\n" + body + "\n\nexport default Scene;\n"
    pairs = await kernel.stills_pairs(code, [12], seconds=seconds, aspect=aspect, scale=0.5)
    raw = base64.b64decode(pairs[0]["jpeg_b64"])
    dst = Path(f"/tmp/grade_{tag}.jpg")
    dst.write_bytes(raw)
    return yavg(dst), dst


async def main() -> int:
    fid = sys.argv[1]
    doc = json.loads((Path("/app/.runtime/simple/films") / fid / "film.json").read_text("utf-8"))
    bodies = doc.get("bodies") or []
    secs = [float(s) for s in doc.get("secs") or []]
    aspect = doc.get("aspect") or "landscape"
    print(f"工程 {fid}　{len(bodies)} 镜　aspect={aspect}")
    print(f"{'镜':<5}{'有提亮层':>10}{'无提亮层':>10}{'差':>8}  说明")
    print("-" * 60)
    deltas = []
    for i, b in enumerate(bodies):
        has = GRADE_LINE in b
        if not has:
            print(f"s{i + 1:02d}   （这一镜没有提亮层）")
            continue
        stripped = b.replace(GRADE_LINE, "")
        a, _ = await one(b, f"on{i}", secs[i], aspect)
        c, _ = await one(stripped, f"off{i}", secs[i], aspect)
        deltas.append(a - c)
        print(f"s{i + 1:02d}  {a:>10.1f}{c:>10.1f}{a - c:>+8.1f}")
    print("-" * 60)
    if deltas:
        print(f"平均提亮 {sum(deltas) / len(deltas):+.1f} 级（YAVG 0~255）")
        print("判据：≥6 说明提亮层真的在起作用；≈0 说明它被别的层盖掉了或没渲染")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
