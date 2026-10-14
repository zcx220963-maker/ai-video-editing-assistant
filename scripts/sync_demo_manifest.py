#!/usr/bin/env python
"""把新写的 demo 补进 demos/MANIFEST.json（不改已有条目）。

为什么单独一个脚本而不是手改 JSON：这份清单是 `shots/INDEX.md` 的 demo 字节来源，
也是 `tests/test_shot_index.py` 那道闸的读数。手改容易漏一条、也容易顺手改了别的字节
——而「已有条目的字节数变了」本身就是一条该报警的事（说明有人在生成物上做过手动编辑）。

判据：只**新增**盘上有、清单里没有的条目；已有条目一个字节都不许变。
"""
import json
import sys
from pathlib import Path

DEMOS = Path(__file__).resolve().parent.parent / "backend/examples/skills/motion_explainer/demos"
MANIFEST = DEMOS / "MANIFEST.json"

before = {m["relpath"]: m["bytes"] for m in json.loads(MANIFEST.read_text("utf-8"))}
on_disk = {
    str(p.relative_to(DEMOS)).replace("\\", "/"): p.stat().st_size
    for p in DEMOS.rglob("*")
    if p.is_file() and p.name != "MANIFEST.json"
}

added = []
drift = []
merged = dict(before)
for rel, size in sorted(on_disk.items()):
    if rel not in merged:
        merged[rel] = size
        added.append((rel, size))
    elif merged[rel] != size:
        drift.append((rel, merged[rel], size))
        merged[rel] = size

print(f"清单原有 {len(before)} 条，盘上 {len(on_disk)} 个文件")
print(f"新增 {len(added)} 条：")
for rel, size in added:
    print(f"  + {rel}  {size:,} B")
if drift:
    print(f"字节漂移 {len(drift)} 条（已按盘上值更新——这本来该由人手确认）：")
    for rel, old, new in drift:
        print(f"  ~ {rel}  {old:,} → {new:,}")
else:
    print("字节漂移 0 条：已有条目一个字节都没变")

out = [{"relpath": r, "bytes": merged[r]} for r in sorted(merged)]
MANIFEST.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", "utf-8")
print(f"写回 {len(out)} 条 → {MANIFEST}")
sys.exit(1 if drift else 0)
