# -*- coding: utf-8 -*-
"""`simple/` 的单元测试：只测**现在还活着**的那些件。

2026-10-26 重写：原来这里一半的断言测的是 explainer 那条路
（`shot_count` / `split_seconds` / `ensure_vivid` / `safe_scene` / `_inject_ribbon`），
那条路随「LLM 当导演」一起删了。测已删代码的断言不该留着——
它会让人以为那些行为还在。新设计的断言在 `test_showrunner.py`。

这里守的是**拼装层**：转场表、`assemble` 的改名与夹值、设计系统的比例兜底、
兜底场景的完整性、旁白定镜长。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.simple import film, kernel, system  # noqa: E402
from storyline_server.simple import voice as vo  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _FAILS += 1


def main() -> int:
    print("\n① 转场表：词 → TSX 表达式，缺一档就是静默硬切")
    kinds = film.transitions_for([{}, {}, {}])
    check(len(kinds) == 2, f"3 镜 → 2 处接缝（{len(kinds)}）")
    check(all(k in film.TRANS_TSX or k == "cut" for k in film.TRANS_CYCLE),
          f"循环表里每一档都实现过（{film.TRANS_CYCLE}）")
    kept = film.transitions_for([{"transition": "WIPE"}, {"transition": "cut"}, {}])
    check(kept[0] == "wipe", f"大小写归一（{kept[0]}）")
    check(kept[1] == "cut", f"cut 是合法值，不是「没写」（{kept[1]}）")

    print("\n② 拼装：TransitionSeries + MOTIF_AT 改名 + 每镜一个组件")
    body = ("const MOTIF_AT = { x: 0.5, y: 0.5, r: 0.05 };\n"
            "const Scene = ({ dur }: { dur: number }) => {\n"
            "  return <svg viewBox=\"0 0 1080 1920\"><circle cx={10} cy={10} r={5} /></svg>;\n"
            "};")
    code, tl = film.assemble([body, body], [3.0, 3.0], ["cut"], fps=30)
    check("TransitionSeries" in code, "用 TransitionSeries（Series 放不下 Transition）")
    check("const S01" in code and "const S02" in code, "每镜各自改名（不撞 no-redeclare）")
    check("MOTIF_AT_01" in code and "MOTIF_AT_02" in code, "MOTIF_AT 也按镜号改名")
    check(len(tl["starts"]) == 2 and tl["cuts"], f"时间表有两镜与切点（{len(tl['starts'])}）")

    print("\n③ 负尺寸兜底：SVG 负值会静默不渲染，必须在拼装时夹住")
    bad = '<rect x={0} y={0} width={width} height={height * (1 - p * 2)} />'
    ok = film._fix_rects(bad)
    check("Math.max(0" in ok, f"可能为负的尺寸被夹住（{ok}）")
    already = '<rect width={Math.max(0, w)} height={Math.max(0, h)} />'
    check(film._fix_rects(already) == already, "已经夹过的不重复夹")

    print("\n④ 兜底场景：结构完整、组件齐全")
    fb = film.safe_story_scene({"text": "一句字幕"}, index=0)
    check(film._looks_complete(fb), "兜底场景是完整组件（括号配平、以 }; 收尾）")
    used = system.used_components(fb)
    check(len(used) >= 3, f"用到的组件 ≥3 件（{used}）")
    check("const Scene" in fb, "导出的是约定的 `const Scene`（assemble 靠它改名）")

    print("\n⑤ 设计系统的比例兜底：收「比例」的组件必须过 R()/N()")
    tsx = system.SYSTEM_TSX
    check("const R = " in tsx and "const N = " in tsx,
          "设计系统里有 R()（比例）与 N()（计数）两个兜底函数")
    blocks: dict[str, str] = {}
    for part in tsx.split("\nconst ")[1:]:
        name = part.split("=")[0].strip().split(":")[0].strip()
        blocks[name] = part
    ratio_users = ["Backdrop", "Caption", "GlowOrb", "Ribbon", "HudFrame"]
    for name in ratio_users:
        body = blocks.get(name, "")
        check(name in blocks and ("R(" in body or "N(" in body),
              f"{name} 收了比例参数并且过 R()/N()")

    print("\n⑥ 旁白定镜长：留白、上下限都夹住")
    check(vo.fit_secs([2.0])[0] >= 2.0, f"2.0 秒语音至少留 2.0（{vo.fit_secs([2.0])}）")
    check(vo.fit_secs([0.5])[0] == vo.MIN_SHOT_SEC,
          f"太短的语音抬到下限 {vo.MIN_SHOT_SEC}（{vo.fit_secs([0.5])}）")
    check(vo.fit_secs([99.0])[0] == vo.MAX_SHOT_SEC,
          f"太长的语音夹到上限 {vo.MAX_SHOT_SEC}（{vo.fit_secs([99.0])}）")

    print("\n⑦ 物理上限：总长超过就明确报错，不静默摊薄")
    check(kernel.MAX_SECONDS >= 180, f"上限够长片（{kernel.MAX_SECONDS}）")

    print("\n⑧ 画法自动判：make_film 的 mode 缺省是 auto")
    import inspect
    check(inspect.signature(film.make_film).parameters["mode"].default == "auto",
          "make_film 的 mode 缺省是 auto（用户不该看到 explainer/story）")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
