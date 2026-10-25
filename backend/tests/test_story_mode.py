# -*- coding: utf-8 -*-
"""插画叙事模式（`mode="story"`）的测试。

    python tests/test_story_mode.py

不调模型、不渲染——`tools/smoke_story_code.py` 才是真机那条。

**这个文件同时是"反屎山"的守门人**：它钉住三件事——
① 画面写手的提示词里**没有"可选零件清单"**（清单本身就是模板）；
② 叙事模式**没有出图通道**（`plate.py` 已删，那是误判时写的废代码）；
③ 每镜的画法指纹能分开（`variety` 读数不是恒等于 1 的假指标）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storyline_server.simple import film, morph, qa, scene, story, system  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    if cond:
        print(f"  ok   {label}")
    else:
        _FAILS += 1
        print(f"  XX   {label}")


SCENE = {"text": "那天，屏幕上跳出一段信号。", "hi": "一段信号",
         "chapter": "第 183 号文明", "time": "地球时间 1975 年"}


def main() -> int:
    print("\n① 两种片子两套参数：镜少而长 vs 镜多而短")
    check(film.MODES == ("explainer", "story"), f"两种模式（{film.MODES}）")
    check(film.shot_count_story(135) == 15,
          f"135 秒 → 15 镜（9 秒一镜；{film.shot_count_story(135)}）")
    check(film.shot_count(135) == 34,
          f"同一时长，动效模式要 34 镜（{film.shot_count(135)}）")
    check(film.STORY_MAX_SHOT_SEC == 12.0 and film.MAX_SHOT_SEC == 5.2,
          f"叙事镜长上限 {film.STORY_MAX_SHOT_SEC}s，动效 {film.MAX_SHOT_SEC}s")
    check(film.shot_count_story(600) == film.STORY_MAX_SHOTS,
          f"超长也压在 {film.STORY_MAX_SHOTS} 镜（镜少而长，不靠堆镜数）")

    print("\n② 画面写手的提示词：**不给零件清单**（这是「别做成模板」的那一条）")
    sysmsg = film._STORY_SCENE_SYSTEM
    # 判据是「有没有把件名一个个列出来」，不是「有没有出现某个词」——
    # 提示词里那句「没有"可选零件清单"」本身就在讲这件事，用词面判断会误伤。
    listed = [n for n in ("Sky", "Dish", "Figure", "Planet", "Ruin", "Fleet",
                          "StarField", "Horizon", "Sun", "Beam")
              if n in sysmsg]
    check(listed == [], f"提示词里没列出任何电影感件名（实得 {listed}）")
    check("%%STORY_MANIFEST%%" not in sysmsg and "STORY_MANIFEST" not in sysmsg,
          "也没有把清单留成占位符（占位符迟早会被填回去）")
    check("画面全部由你画" in sysmsg, "明确说了「画面全部由你画」")
    check("<path>" in sysmsg and "<polygon>" in sysmsg,
          "给的是画法（path/polygon/渐变），不是件名")
    check("≥ 38/255" in sysmsg and "≥ 40" in sysmsg,
          "给了可核对的亮度数字（模型对「亮多少」没有直觉，必须给数）")
    check("ChapterMark" in sysmsg and "不要写" in sysmsg,
          "明说文字层不要自己画（否则成片上两个章节号）")

    print("\n③ 外层包装：Camera 运镜 + 三件文字层，由外层统一加")
    inner = ('const Scene = ({ dur }: { dur: number }) => { const t = 0;\n'
             '  return (<><svg viewBox="0 0 1 1"><path d="M0 0 L1 1" /></svg></>);\n};')
    wrapped = film.wrap_story_scene(inner, SCENE, index=2)
    check("const Inner03" in wrapped, "模型画的场景改名成 Inner03（不撞外层 Scene）")
    check(wrapped.count("const Scene") == 1, "整段只有一个 `const Scene`")
    check("<Camera " in wrapped and "<Inner03 dur={dur} />" in wrapped, "外层套运镜")
    check(film._looks_complete(wrapped), "包完仍是完整组件")
    check({"Camera", "RichCaption", "ChapterMark", "TimeMark"}
          <= set(system.used_components(wrapped)), "文字层三件 + 运镜都在外层")
    check("bottom={0.07}" in wrapped, "字幕按底部锚定（长句不会溢出画面下沿）")
    # 提亮层：提示词写"亮度 ≥ 38/255"，模型稳定做到 27——它对绝对亮度没有直觉。
    # 所以在最后一道确定性代码里补，而不是继续往提示词里加数字。
    check(f"opacity: {scene.GRADE_OPACITY}" in wrapped,
          f"有提亮层（{scene.GRADE_OPACITY}，等效 eq=brightness=0.05）")
    check(wrapped.index("</Camera>") < wrapped.index(f"opacity: {scene.GRADE_OPACITY}")
          < wrapped.index("<RichCaption"),
          "提亮层在画面之上、文字之下（文字不该被一起洗白）")

    print("\n④ 高亮词必须是正文子串")
    bad = film.wrap_story_scene(inner, {"text": "换你守在 1379 号站", "hi": "警告"}, index=0)
    check('hi=""' in bad, "hi 不在 text 里 → 置空")
    good = film.wrap_story_scene(inner, {"text": "上报，还是警告？", "hi": "警告"}, index=0)
    check('hi="警告"' in good, "是子串 → 保留")

    print("\n⑤ 转场：叙事片只 cut / fade")
    kinds = film.transitions_for_story([{}] * 6)
    check(all(k in ("cut", "fade") for k in kinds), f"只有 cut/fade（{kinds}）")
    check(film.transitions_for_story([{"transition": "wipe"}] * 3) == ["fade", "cut"],
          "模型写了 wipe 也不采纳（叙事片不用擦除）")

    print("\n⑥ 两套判据：同一条暗调低饱和的画面，动效判 flat，叙事判 ok")
    dark_low_sat = {"content": 0.55, "vivid": 0.02, "mean_lum": 42.0,
                    "p95_lum": 180.0, "edge": 1.4, "bg_lum": 30}
    check(qa.verdict(dark_low_sat) == "flat",
          "动效模式：判寡淡（参考片那种 SATAVG 6.3 会在这里被打回）")
    check(qa.verdict(dark_low_sat, story=True) == "ok",
          "叙事模式：放行——暗调低饱和正是它要的样子")
    check(qa.verdict({"content": 0.02, "vivid": 0.0, "mean_lum": 20.0,
                      "p95_lum": 30.0, "edge": 0.2, "bg_lum": 18},
                     story=True) == "empty", "叙事模式仍判空镜")
    check(qa.verdict({"content": 0.4, "vivid": 0.0, "mean_lum": 3.0,
                      "p95_lum": 6.0, "edge": 0.1, "bg_lum": 2},
                     story=True) == "black", "叙事模式仍判全黑")

    print("\n⑦ 兜底也必须是叙事片的兜底（不能混进几何动效）")
    fb = film.safe_story_scene(SCENE, index=0)
    used = set(system.used_components(fb))
    check("Ribbon" not in used and "ScanGrid" not in used,
          f"兜底里没有信息图零件（{sorted(used)}）")
    check({"Sky", "Horizon"} <= used, "兜底用的是天空/地平线这一套")
    check(film._looks_complete(fb), "兜底本身是完整组件")

    print("\n⑧ 画法指纹：能分开不同画法（variety 不是恒等于 1 的假指标）")
    a = film._scene_fingerprint('<path d="M0 0"/><path d="M1 1"/><circle r={2}/>')
    b = film._scene_fingerprint('<rect x={0}/><rect x={1}/><rect x={2}/>')
    c = film._scene_fingerprint('<path d="M0 0"/><path d="M1 1"/><circle r={2}/>')
    check(a != b, f"两份不同画法的指纹不同（{a} vs {b}）")
    check(a == c, "同一份画法的指纹相同")
    check(film._scene_fingerprint("") == (0, 0, 0, 0, 0, 0, 0, 0),
          "空组件指纹全 0（不会因为没画东西而「看起来不同」）")

    print("\n⑨ 反屎山：出图通道已删，别再加回来")
    simple_dir = Path(__file__).resolve().parents[1] / "storyline_server" / "simple"
    check(not (simple_dir / "plate.py").exists(),
          "`simple/plate.py` 不存在（那是误判「参考片是 AI 插画」时写的废代码）")
    check("Plate" not in system.SYSTEM_TSX,
          "设计系统里也没有 `Plate` 组件了（留着就是一条没人走的路）")
    check(not hasattr(film, "write_scene_story"),
          "`write_scene_story`（Plate 版）已删")
    names = {m["name"] for m in system.MANIFEST}
    check(len(names) == len(system.MANIFEST), f"清单无重名（{len(names)} 件）")

    print("\n⑩ 拆文件后的自检：write_scene_story_code 真的能跑通")
    # 2026-10-25 拆 story.py 时，`SCENE_HEADER` 没跟着搬，于是每一镜都在函数里
    # NameError，然后被兜底静默盖住（整片 15 镜全成兜底，日志上只看到「质检 1.0」）。
    # 这一条就是为了让那种错**在测试里就红**：用假的模型回话走一遍真实函数。
    import asyncio  # noqa: PLC0415
    canned = "```tsx\n" + scene._STORY_SAFE_BODIES[0] + "\n```"
    orig = film.kernel._complete

    async def fake(messages, **kw):  # noqa: ANN001, ANN003
        return canned

    film.kernel._complete = fake
    try:
        got = asyncio.run(story.write_scene_story_code(
            SCENE, index=0, total=1, seconds=7.0, aspect="landscape"))
        check("const Scene" in got,
              "能拿到场景体（说明 SCENE_HEADER / _looks_complete 这些全局名都在）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"write_scene_story_code 抛了：{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        film.kernel._complete = orig

    print("\n⑪ 跨镜共享元素：锚点解析 + 只在两边都有锚点时接力")
    at = morph.parse_motif_at('const MOTIF_AT = { x: 0.62, y: 0.45, r: 0.05 };\n'
                              'const Scene = () => null;')
    check(at == (0.62, 0.45, 0.05), f"能读出锚点（{at}）")
    check(morph.parse_motif_at("const Scene = () => null;") is None,
          "没写就返回 None（不是 (0,0,0)，那会接到左上角）")
    check(morph.parse_motif_at("const MOTIF_AT = { x: 1.8, y: 0.2, r: 0.05 };") is None,
          "越界的 x 不认（模型会写像素值）")
    check(morph.parse_motif_at("const MOTIF_AT = null;") is None, "显式 null 也当没有")

    tl = film.timeline([6.0] * 4, ["cut", "fade", "cut"])
    # 前 3 镜都有锚点、第 4 镜没有 → 应该只有 2 跳（第 3→4 处不该接）
    hops = film.shared_hops(
        tl, [(0.2, 0.3, 0.05), (0.7, 0.6, 0.09), (0.4, 0.4, 0.06), None], 4)
    check(len(hops) == 2, f"4 镜里只有 2 处两边都有锚点（{len(hops)}）")
    check(hops[0]["from"] == (0.2, 0.3, 0.05) and hops[0]["to"] == (0.7, 0.6, 0.09),
          "接力从上一镜的位置到下一镜的位置")
    check(hops[0]["at"] == tl["cuts"][0],
          f"接在切点上（{hops[0]['at']} == cuts[0]={tl['cuts'][0]}）")
    check(hops[1]["at"] == tl["cuts"][1], "第二跳接在第二处切点")
    check(film.shared_hops(tl, [None] * 4, 4) == [], "全都没锚点 → 一次都不接")
    check(film.shared_hops(tl, None, 4) == [], "没给 motifs → 空（老行为不变）")

    code2, _tl2 = film.assemble([inner, inner, inner], [4.0] * 3, ["cut", "cut"],
                                motifs=[(0.2, 0.3, 0.05), (0.6, 0.5, 0.08), None])
    check("<SharedMorph hops={" in code2, "拼装时把接力层写进了模块")
    check(code2.count("<SharedMorph") == 1, "只有一层接力（不是每镜一个）")
    check("const SharedMorph" in system.SYSTEM_TSX, "组件实现在设计系统里")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
