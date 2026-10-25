# -*- coding: utf-8 -*-
"""量一条片子的「画法散不散」+「版式文字有没有真画」。

为什么不用 `/simple/job` 的 coverage：叙事片每镜的组件集合恒等于
「Camera + ChapterMark + RichCaption + TimeMark」（外层统一套的），
那个读数对多样性没有区分力。要数**画法**——各类 SVG 元素各几个。

三个读数：
  ① 画法指纹去重后剩几种（越接近镜数越好）
  ② 每镜 <text> 个数（版式文字：出处/注脚/对照表 靠它画）
  ③ 每镜 <line>/<polyline> 个数（虚线连线：地理分布图靠它画）

跑法：python tools/analyze_shots.py <film_id>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app" if Path("/app").is_dir() else str(Path(__file__).resolve().parents[1]))

from storyline_server.simple.scene import _scene_fingerprint  # noqa: E402


def count(pat: str, s: str) -> int:
    return len(re.findall(pat, s))


def main() -> int:
    fid = sys.argv[1]
    doc = json.loads((Path("/app/.runtime/simple/films") / fid / "film.json").read_text("utf-8"))
    bodies = doc.get("bodies") or []
    print(f"工程 {fid}　mode={doc.get('mode')}　{len(bodies)} 镜　"
          f"总长 {sum(float(s) for s in doc.get('secs') or []):.1f}s")
    print(f"title: {doc.get('title')}")
    scenes = ((doc.get("plan") or {}).get("scenes") or [])
    print("-" * 92)
    print(f"{'镜':<4}{'指纹(path/poly/rect/circ/ell/line/linG/radG)':<42}"
          f"{'cap模式':<9}{'外层字幕':<9}{'text':>5}{'line':>6}{'dash':>6}")
    fps = []
    for i, b in enumerate(bodies):
        fp = _scene_fingerprint(b)
        fps.append(fp)
        cap = (scenes[i] or {}).get("caption") if i < len(scenes) else None
        cap = cap if isinstance(cap, dict) else {}
        mode = str(cap.get("mode") or "overlay")
        has_rc = "有" if "<RichCaption" in b else "—"
        print(f"s{i + 1:02d}  {str(fp):<42}{mode:<9}{has_rc:<9}"
              f"{count(r'<text\b', b):>5}{count(r'<line\b', b):>6}"
              f"{count(r'strokeDasharray', b):>6}")
    uniq = len(set(fps))
    print("-" * 92)
    print(f"画法多样性：{uniq}/{len(bodies)} 种不同的指纹")
    drawn = [i + 1 for i, s in enumerate(scenes)
             if isinstance(s, dict) and isinstance(s.get("caption"), dict)
             and str(s["caption"].get("mode") or "") == "drawn"]
    rc = [i + 1 for i, b in enumerate(bodies) if "<RichCaption" in b]
    texts = [count(r'<text\b', b) for b in bodies]
    lines = [count(r'<line\b', b) for b in bodies]
    print(f"分镜标 drawn 的镜：{drawn or '（一镜都没标）'}")
    print(f"外层加了字幕的镜：{rc}（共 {len(rc)} 镜）")
    print(f"drawn 的镜里有没有真画字："
          + ("、".join(f"s{i:02d}={texts[i - 1]} 个 <text>" for i in drawn) if drawn else "—"))
    print(f"版式文字合计 {sum(texts)} 个 <text>；虚线连线合计 {sum(lines)} 条 <line>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
