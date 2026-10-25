# -*- coding: utf-8 -*-
"""看一条工程落盘的镜长分布（确认 `fill_to` 真的补了留白）。"""
import json
import sys
from pathlib import Path

fid = sys.argv[1]
doc = json.loads((Path("/app/.runtime/simple/films") / fid / "film.json").read_text("utf-8"))
secs = [float(s) for s in doc.get("secs") or []]
print(f"工程 {fid}　mode={doc.get('mode')}　目标 {doc.get('seconds')}s")
print(f"镜数 {len(secs)}　总长 {sum(secs):.2f}s　"
      f"最短 {min(secs):.2f}　最长 {max(secs):.2f}")
print("每镜：" + " ".join(f"{s:g}" for s in secs))
print(f"title: {doc.get('title')}")
print(f"audio: {doc.get('audio')}　calls: {doc.get('calls')}")
