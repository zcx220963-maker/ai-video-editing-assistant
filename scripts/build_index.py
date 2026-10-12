#!/A
"""R1: 重写 shots/INDEX.md，每行加 demo relpath + 字节数。"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "backend/examples/skills/motion_explainer/shots"
DEMOS = ROOT / "backend/examples/skills/motion_explainer/demos"
MANIFEST = DEMOS / "MANIFEST.json"

def main():
    manifest = {m["relpath"]: m["bytes"] for m in json.loads(MANIFEST.read_text("utf-8"))}
    lines = ["# shot 卡索引（124 张，按类别）\n"]
    lines.append("每行：**卡名** [能量] — 描述 | demo relpath | 字节数\n")
    total = 0
    for cat_dir in sorted(SHOTS.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith("."):
            continue
        cards = sorted(cat_dir.glob("*.md"))
        if not cards:
            continue
        lines.append(f"\n## {cat_dir.name}（{len(cards)} 张）\n")
        for card in cards:
            text = card.read_text("utf-8")
            name = card.stem
            m = re.search(r'\[(.+?)\]', text)
            energy = m.group(1) if m else "?"
            desc_m = re.search(r'^-\s*\*\*[^*]+\*\*\s*\[[^\]]+\]\s*—\s*(.+)', text, re.M)
            desc = desc_m.group(1).strip()[:60] if desc_m else ""
            demo_m = re.search(r'参考实现\s*\n(demos/[^\n]+)', text)
            demo_rel = demo_m.group(1).strip() if demo_m else ""
            demo_bytes = 0
            if demo_rel:
                demo_dir = demo_rel.rstrip("/")
                for rel, sz in manifest.items():
                    if rel.startswith(demo_dir + "/") or rel == demo_dir:
                        demo_bytes += sz
            total += demo_bytes
            lines.append(f"- **{name}** [{energy}] — {desc} | `{demo_rel}` | {demo_bytes}")
    lines.append(f"\n总计 demo 字节：{total}")
    out = SHOTS / "INDEX.md"
    out.write_text("\n".join(lines) + "\n", "utf-8")
    print(f"INDEX.md 重写完成：{out}")

if __name__ == "__main__":
    main()