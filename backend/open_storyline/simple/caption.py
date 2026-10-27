# -*- coding: utf-8 -*-
"""字幕怎么排：字号、位置、以及"这一镜到底要不要外层字幕"。

从 `scene.py` 拆出来的（2026-10-25）：那边只剩 8 行余量，而字幕这套东西
（按画幅算字号 + 五档位置 + 三种模式）塞进去必然超。

## 为什么字幕要独立成一个关注点

原来的字幕是**外层一个组件说了算**：字号按字数分四档、位置写死 `bottom: 0.07`、
底部铺一条 36% 高的黑渐变（`scrim`）保证可读。三个毛病：

1. **太大**：字号按**宽度**算，同一个 0.038 在 1920 宽是 73px、1280 宽是 48.6px、
   720 竖屏是 27px——观感差三倍。参考片 1280×720 实测约 34px（占短边 0.047）。
2. **割开画面**：那条 36% 高的黑渐变盖在画面下三分之一。参考片没有这东西，它靠描边 + 阴影。
3. **死板**：要不要字幕、放哪、多大，全由外层定，分镜插不上话。

## 三层职责（这是"灵活但不屎山"的关键）

    版式意图     分镜（每镜给）：要不要字幕、放哪、多大
    具体实现     画面写手：**默认**自己把字画进画面（题款/批注/碑文/纸卡/屏幕上的字）

**判据只有一条**：默认 `drawn`（模型自己画）；分镜点名 `overlay` 才由外层兜底。
不设闸——字幕可读性判不了像素，硬判就是旧链那种没有区分力的闸。
"""

from __future__ import annotations

from typing import Any

#: `caption.at` → 字幕底边距画面底部的比例（占**高度**）。
#:
#: 只有五档，不是任意坐标：位置每镜都变，观众跟不住。同一片子里变 2~3 档最舒服。
CAPTION_AT = {
    "bottom": 0.045,   # 贴底：短句、收束镜
    "lower": 0.080,    # 默认：略高于安全区
    "center": 0.400,   # 居中偏下：单句强调
    "upper": 0.620,    # 偏上：下面有大块内容要看
    "left": 0.080,     # 左对齐（配合长句、说明性字幕）
}

#: 默认档。分镜不写 `caption` 时走这一档，所以**老片子行为不变**。
DEFAULT_AT = "lower"

#: 三种模式。
#: - `drawn`：模型自己画进画面（**默认**），外层一个字不加
#: - `overlay`：外层画（只有分镜点名才用）
#: - `none`：这镜不要字幕（纯画面，靠旁白）
MODES = ("drawn", "overlay", "none")

#: 字号占**短边**的比例，按字数分档。
#:
#: 参考片实测：1280×720、字幕约 34px → 34/720 ≈ 0.047。
#: 按短边归一化之后，横屏与竖屏的字占画面**比例**一致，不再差三倍。
#:
#: 档位不是随手分的：可用宽度 0.88 × 1280 = 1126px，中文一字宽≈一个字号，
#: 所以 `size=0.047` 时一行放得下 1126 / (720×0.047) ≈ 33 字——**34 字就折行**，
#: 而且是"最后一个字单独一行"那种最难看的样子。所以 34 字这一档压到 0.045
#: （一行 34.7 字），刚好不折。
_SIZE_BY_LEN = ((16, 0.058), (24, 0.052), (34, 0.045), (10 ** 6, 0.040))

#: 外层兜底字号的下限与上限（占短边）。720 短边下 = 21.6px ~ 54px。
#: 下限是"再小就没人看得清"，上限是"再大就成标题了"。
SIZE_MIN, SIZE_MAX = 0.030, 0.075


def fit(text: str) -> float:
    """按字数给字号（占短边的比例）。"""
    n = len(str(text or ""))
    for limit, size in _SIZE_BY_LEN:
        if n <= limit:
            return size
    return _SIZE_BY_LEN[-1][1]


def plan(scene: dict[str, Any]) -> dict[str, Any]:
    """这一镜的字幕怎么放 → ``{mode, bottom, size, align}``。

    **默认 `drawn`**（2026-10-26 改）：文字交给画面写手画，外层不插手。原来默认
    `overlay`，等于每镜贴一条同款字幕——那是"像模板"的一半。点名 `overlay` 才兜底。
    """
    cap = scene.get("caption")
    cap = cap if isinstance(cap, dict) else {}
    mode = str(cap.get("mode") or "drawn").strip().lower()
    if mode not in MODES:
        mode = "drawn"
    at = str(cap.get("at") or DEFAULT_AT).strip().lower()
    bottom = CAPTION_AT.get(at, CAPTION_AT[DEFAULT_AT])

    size = cap.get("size")
    try:
        size = float(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    if size is None:
        size = fit(str(scene.get("text") or ""))
    size = max(SIZE_MIN, min(SIZE_MAX, size))

    return {"mode": mode, "bottom": bottom, "size": round(size, 4),
            "align": "left" if at == "left" else "center"}


def drawn_brief(scene: dict[str, Any]) -> str:
    """`drawn` 模式下给写手的那句话：字幕内容 + **全片统一规格**（字号/位置/字色/底衬）。

    规格来自分镜的 `caption`（导演给一次，`normalize` 下发到每一镜）——这是"23 镜一套字"
    的唯一来源。写手照它画，不许自己另定一套。
    """
    text = str(scene.get("text") or "").strip()
    hi = str(scene.get("hi") or "").strip()
    if not text:
        return ""
    tail = f"，重音是「{hi}」" if hi and hi in text else ""
    p = plan(scene)
    raw = scene.get("caption") if isinstance(scene.get("caption"), dict) else {}
    spec = (f"**全片统一规格（照它画，不许自己另定一套）**：字号 = 短边 × {p['size']}、"
            f"距底 {p['bottom']:.3f}（{raw.get('at') or 'lower'}，{p['align']} 对齐）、"
            f"字色 {raw.get('color') or '与画面反差最大'}、底衬 {raw.get('scrim') or 0}。\n")
    return (f"\n**这一镜的字幕要你自己画进画面**：内容是「{text}」{tail}。"
            + spec
            + "像题款、手写批注、碑文、纸卡、屏幕上的字那样画，让它成为画面的一部分"
            "——**外层这一镜不会加任何字幕**。\n"
            "硬要求（决定看不看得清，不是审美）：整句完整出现、不许截断；"
            "离画面边缘 ≥8%（外层可能套 1.1 倍缓推，会裁掉边缘）；与压住的底色差 ≥40 级亮度。\n")
