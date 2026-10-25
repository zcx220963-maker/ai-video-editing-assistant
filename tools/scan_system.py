# -*- coding: utf-8 -*-
"""扫 demos/ 里的组件，生成设计系统清单草案（只读，不改任何现有代码）。

产出两份：
  remotion/src/system/manifest.draft.json   给机器（写手提示词、覆盖率计算）
  docs/清单-组件库草案-2026-10-24.md         给人（分组表格 + 挑选建议）
"""
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\xu'zhi'cheng\Desktop\智能创作助手")
DEMOS = ROOT / "remotion" / "src" / "demos"
OUT_JSON = ROOT / "remotion" / "src" / "system" / "manifest.draft.json"
OUT_MD = ROOT / "docs" / "清单-组件库草案-2026-10-24.md"

SKIP = ("_legacy", "_fixtures", "_textures")

#: 顶层目录 → 中文类目（对齐 Vibe 那种七层结构）
CATEGORY_CN = {
    "opening": "开场", "outro": "收尾", "transition": "转场",
    "typography": "字幕与字", "data": "数据与图表", "science": "科普图解",
    "effects": "光效与质感", "camera": "镜头运动", "ui-entrance": "界面件",
    "interaction": "交互演示", "rhythm": "节奏与切点",
}


def first_doc(text: str) -> str:
    """取文件头那段块注释/行注释的第一句。"""
    m = re.search(r"/\*\*(.*?)\*/", text, re.S)
    if m:
        body = m.group(1)
    else:
        lines = []
        for ln in text.splitlines()[:40]:
            s = ln.strip()
            if s.startswith("//"):
                lines.append(s.lstrip("/ "))
            elif s.startswith("import") or s.startswith("'use"):
                break
            elif lines:
                break
        body = " ".join(lines)
    body = re.sub(r"^\s*\*\s?", "", body, flags=re.M)
    body = re.sub(r"\s+", " ", body).strip()
    for sep in ("。", "；", ". "):
        if sep in body:
            body = body.split(sep)[0]
    return body[:90]


def props_of(text: str) -> list[str]:
    """从 `interface XxxProps {` / `type XxxProps = {` 里抠字段名。"""
    m = re.search(r"(?:interface|type)\s+\w*Props\s*(?:=|)\s*\{(.*?)\n\}", text, re.S)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        mm = re.match(r"\s*(\w+)\??\s*:", ln)
        if mm:
            out.append(mm.group(1))
    return out[:12]


def main() -> None:
    rows = []
    for p in sorted(DEMOS.rglob("*.tsx")):
        rel = p.relative_to(DEMOS)
        if any(s in rel.parts for s in SKIP):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        cat = rel.parts[0]
        name = p.stem
        imports = sorted({m for m in re.findall(r'from\s+["\'](@dg/[^"\']+)["\']', text)})
        rows.append({
            "name": name,
            "category": cat,
            "category_cn": CATEGORY_CN.get(cat, cat),
            "file": str(rel).replace("\\", "/"),
            "summary": first_doc(text),
            "props": props_of(text),
            "imports": imports,
            "lines": len(text.splitlines()),
            "uses": {
                "three": "@remotion/three" in text or bool(
                    re.search(r'from\s+["\']three["\']', text)),
                "glsl": "ShaderMaterial" in text or "glsl" in text.lower(),
                "canvas": "<canvas" in text,
                "svg": "<svg" in text,
                "katex": "katex" in text,
                "rough": "roughjs" in text or "rough-notation" in text,
            },
        })

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({
        "note": "草案：由 tools/scan_system.py 从 demos/ 扫出，未经人工筛选。"
                "最终清单应收到 27~40 个，覆盖七类。",
        "count": len(rows),
        "items": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[(r["category"], r["category_cn"])].append(r)

    lines = [
        "# 组件库清单草案（demos/ 全量扫描）",
        "",
        f"> 2026-10-24 · 由 `tools/scan_system.py` 扫出，共 **{len(rows)} 个组件**，未经人工筛选",
        "> 机器版：`remotion/src/system/manifest.draft.json`",
        "> 用途：这是 P0「组件编目」的第一步——先看清手里有什么，再挑 27~40 个收进 `system/`",
        "",
        "## 按类目分布",
        "",
        "| 类目 | 数量 | 平均行数 | 用到 3D | 用到 canvas |",
        "|---|---|---|---|---|",
    ]
    for (cat, cn), items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        avg = sum(i["lines"] for i in items) // max(1, len(items))
        n3d = sum(1 for i in items if i["uses"]["three"])
        ncv = sum(1 for i in items if i["uses"]["canvas"])
        lines.append(f"| {cn}（`{cat}`） | {len(items)} | {avg} | {n3d} | {ncv} |")

    n_props = sum(1 for r in rows if r["props"])
    lines += [
        "",
        f"**有 props 声明的只有 {n_props}/{len(rows)} 个** —— 其余是「自包含的样片」，"
        f"不是「可复用的零件」。",
        f"这是编目真正要干的活：**给挑出来的那 27~40 个补 props 契约**，不是搬文件。",
        "",
        "## 全部组件",
        "",
    ]
    for (cat, cn), items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {cn}（`{cat}`，{len(items)} 个）")
        lines.append("")
        lines.append("| 组件 | 一句话 | props | 行 |")
        lines.append("|---|---|---|---|")
        for i in sorted(items, key=lambda x: x["name"]):
            props = "、".join(i["props"][:5]) or "—"
            summary = (i["summary"] or "—").replace("|", "/")[:60]
            lines.append(f"| `{i['name']}` | {summary} | {props} | {i['lines']} |")
        lines.append("")

    lines += [
        "## 挑选建议（收到 27~40 个）",
        "",
        "对照 Vibe 那种片子的七层结构，每类留 2~4 个够用：",
        "",
        "| 层 | 建议留几个 | 从哪类挑 |",
        "|---|---|---|",
        "| 逐词字幕 | 2~3 | `typography`（KeywordColorCaption / OutlinedCaption / SplitTextStagger） |",
        "| 科技卡 / 数据 | 3~4 | `data`（ChartLive / GaugeReadout / TapeScroll） |",
        "| 转场 | 3~4 | `transition`（Wipe / CircleIris / WhipBrake / LightLeak） |",
        "| 镜头运动 | 2~3 | `camera`（DepthLayerMoves / OverheadTabletop / CrashZoom） |",
        "| 粒子 / 光效 | 3~4 | `effects`（GlowOrb / SpotlightSweep / Scanline） |",
        "| 纹理 / 噪点 | 2 | `effects`（AuroraBloom / RisoMisregistration） |",
        "| 开场 / 收尾 | 3~4 | `opening` + `outro`（TextAsMask / OrbitRingTitle / LogoShrink） |",
        "| 科普图解 | 4~6 | `science`（CrossSection / ExplodedAssembly / MetaphorBridge） |",
        "",
        "**判据**：同一层里选**props 最少、行数最少、不依赖 3D**的那个——",
        "覆盖率要的是「用得上的复用」，不是「最炫的那个」。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"扫到 {len(rows)} 个组件")
    for (cat, cn), items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print(f"  {cn:6s} {len(items):3d}")
    print(f"\n写入:\n  {OUT_JSON}\n  {OUT_MD}")


main()
