# -*- coding: utf-8 -*-
"""每日一条「演示版」提示词：写进去的每一个取值都得是契约认得的。

演示版多出来的一段效果编排，是本项目最容易被写出「提示词里写着、校验器不认」
的地方：转场名、入场动作、高亮方式、整片色板，错一个词整份计划就被打回，
而打回的是三十多镜、几分钟的写码。这一组用例只钉离线能验的那一半：

* 日常版不带效果编排，演示版带；
* 演示版点到的每个取值都在 ``code_spec`` 的枚举里（不是只在一段散文里）；
* 三条主线（转场 / 动效锚 / 画法清单）都真的进了提示词；
* 「不照做就打回」这类话不写进提示词正文。

运行：  python tests/test_daily_guide_showcase.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storyline_server.motion import code_spec as cs  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHECKS: list[tuple[str, bool]] = []


def ck(name: str, ok: bool) -> None:
    CHECKS.append((name, bool(ok)))


def _daily_guide():
    path = ROOT / "tools" / "daily_guide" / "daily_guide.py"
    spec = importlib.util.spec_from_file_location("daily_guide", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _entries() -> list[dict]:
    return [{
        "id": f"{i:03d}", "chapter": "01-不要早死.md", "num": i,
        "title": f"第 {i} 条", "tag": "", "cost": "不花钱", "plain": "说人话",
        "gain": "一个数字", "level": "A", "source": "", "note": "备注",
        "file": "01-不要早死.md",
    } for i in range(1, 11)]


def main() -> int:
    dg = _daily_guide()
    normal = dg.build_prompt(_entries(), 1, dg.DEFAULT_STYLE, {})
    demo = dg.build_prompt(_entries(), 1, dg.DEFAULT_STYLE, {}, showcase=True)

    ck("日常版不带效果编排", "演示版效果编排" not in normal)
    ck("演示版带效果编排", "演示版效果编排" in demo)
    ck("日常版高亮不强调", "highlight_style: plain" in normal)
    ck("演示版高亮也不强调", "highlight_style: plain" in demo)

    # 取值必须能过校验器：这些名字写错一个，整份 30 多镜的计划会被打回。
    for seam in ("dissolve", "circleopen", "circleclose", "zoomin", "hblur",
                 "fadeblack", "radial", "smoothleft"):
        ck(f"转场 {seam} 合法", seam in cs.TRANSITION_STYLES)
    for move in ("rise", "zoom"):
        ck(f"进出场 {move} 合法", move in cs.SHOT_MOTION_STYLES)
    ck("整片色板 midnight 合法", cs.norm_palette("midnight") == "midnight")
    ck("整片质感 film 合法", "film" in cs.TEXTURES)

    # 三条主线都要真写进去，而不只是有一个小标题。
    ck("写了段落转场", all(x in demo for x in
                           ("dissolve", "circleopen", "circleclose", "zoomin", "hblur")))
    ck("写了动效锚", "subtitle:<第几个词>" in demo)
    ck("写了逐镜两处动效", "每一镜至少两处看得见的动效" in demo)
    ck("写了节拍卡点", "beat_sync" in demo)

    # 教程里能落地的几类画法，各至少点名一次。
    for word in ("真三维", "手绘线稿", "遮罩揭示", "粒子", "手绘标注", "生长", "剪纸"):
        ck(f"画法清单含{word}", word in demo)
    ck("点名了至少一镜真三维", "真三维" in demo)
    ck("粒子有克制条件", "粒子亮度低于主体" in demo)
    ck("效果服务旁白", "效果和旁白对齐" in demo)
    ck("不贴外部素材", "不用外部素材" in demo)
    ck("数字镜交出可调字段", "可调字段" in demo)
    # 画面与字幕的关系（2026-10-21）：用户看到的第一版是「画面镶在中间、字幕站在
    # 画面外的黑带上」，原话是「字幕要和画面分开啊，很奇怪」。这条不写进提示词，
    # 下一份分镜还会照旧不申报满幅。
    ck("提示词要求满幅", "铺满整帧" in demo and "bleed: true" in demo)
    ck("提示词说字幕压在画面上", "压在画面上" in demo)
    ck("提示词收住颗粒", "不超过 0.12" in demo)
    # 中英双字幕（2026-10-21）：用户原话「字幕就用中英双字幕，黑边白字就行了，
    # 字体适中，且不需要 ⌈⌋ 这种符号」。不写进提示词，下一份分镜只会交中文那一行。
    ck("提示词要求英文字幕", "中英双字幕" in demo and "text_en" in demo)
    ck("日常版也要求英文字幕", "text_en" in normal)
    ck("提示词点名白字黑边", "白色字" in demo and "黑色描边" in demo)
    ck("提示词禁装饰括号", "⌈⌉⌊⌋" in demo and "装饰" in demo)
    # 写实（2026-10-21）：用户原话「画面还是不够高质量，要尽量的写实」。默认风格改成
    # 三维写实静物后，提示词里必须留下「哪套风格、什么质感、有没有接触阴影」这些
    # 可反查的锚；同时不能再让写手套纸片壳——那会把写实三维压成一张米色卡纸。
    ck("点名暗房微距", "暗房微距" in demo)
    ck("要求写实", "写实" in demo)
    ck("要求接触阴影", "接触阴影" in demo)
    ck("不再套纸片壳", "shell: paper-flat" not in demo and "纸底片壳" not in demo)
    ck("画面满幅落地", "铺满整帧" in demo and "#07080a" in demo)

    # 提示词是写给模型的要求，不是威胁；「不照做就打回」这类话不进正文。
    ck("没有打回威胁", "打回" not in demo)

    failed = [n for n, ok in CHECKS if not ok]
    for name, ok in CHECKS:
        print(("PASS " if ok else "FAIL ") + name)
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
