# -*- coding: utf-8 -*-
"""一镜的**包装**与**兜底**：模型只画场景本身，外面这层由这里统一套。

从 `story.py` 拆出来的（2026-10-25）：那边 364 行、已经超过 350 的硬顶。
拆的边界是"谁写内容"：

    story.py   画法：提示词 + 写手（`write_scene_story_code`）+ 分镜（`plan_story`）
    scene.py   包装：运镜/文字层/字幕排版 + 兜底三套 + 画法指纹

为什么包装不能交给模型逐镜决定：十几镜的字幕位置、章节标记、推拉方向必须**一致**
——那是"一部片子"而不是"十几张图"的感觉来源。所以这一层是确定性的代码，
不调模型。
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import caption

#: 运镜四档轮换。推/拉/横移/微推——同一档连用两镜会看出"卡住"。
_STORY_ZOOM = ((1.0, 1.10), (1.12, 1.0), (1.0, 1.06), (1.08, 1.02))
_STORY_PAN = ((0.10, 0.0), (-0.08, 0.0), (0.0, 0.06), (0.0, -0.05))

#: 整片的**提亮层**（白，盖在画面上、文字下）。
#:
#: **2026-10-26 置 0**：曝光改由 `showrunner` 的按镜闭环接管（`brightness(gain)`，
#: 一镜一个倍数）。盖白是**加性**的——会把黑压成灰、把饱和色洗淡，实测成片 SATAVG
#: 只有 6.7（接近黑白）；`brightness()` 是**乘性**的，提亮同时保住色度，所以这一层
#: 不再需要。留着常量是为了能一行回退。
GRADE_OPACITY = 0.0


def _scene_fingerprint(body: str) -> tuple[int, ...]:
    """一镜的**画法指纹**：各类 SVG 元素各几个、用了几种渐变。

    为什么不用"用了哪些组件"当多样性读数：把零件清单从提示词里拿掉之后，
    每镜都会是 `<svg>` + 自己的 path，组件集合恒等于"Camera + 文字层"，
    那个读数会变成 1，反而看不出多样性。**数画法才看得出"每镜是不是各画各的"。**
    """
    def n(pat: str) -> int:
        return len(re.findall(pat, body))

    return (n(r"<path\b"), n(r"<polygon\b"), n(r"<rect\b"), n(r"<circle\b"),
            n(r"<ellipse\b"), n(r"<line\b"),
            n(r"linearGradient"), n(r"radialGradient"))


def _caption_fit(text: str) -> float:
    """按字数给字号（占**短边**的比例）。实现在 `caption.py`。

    为什么搬走：字号、位置、"这一镜要不要外层字幕"是同一个关注点，
    散在两个文件里就会出现"改了字号忘了位置"。这里留一层薄壳，
    是因为 `film.py` 与测试都按这个名字取。
    """
    return caption.fit(text)


def wrap_story_scene(body: str, scene: dict[str, Any], *, index: int,
                     gain: float = 1.0, bg: str = "") -> str:
    """把模型画的场景包成完整一镜：`Camera` 运镜 + 章节号 + 时间标记 + 字幕。

    为什么运镜与章节标记由外层统一加，而不是让模型写：
    十几镜的推拉方向、章节标记位置必须**一致**——那是"一部片子"的感觉来源。

    **但字幕不是**（2026-10-25 改）。字幕分三层：
      可读性下限（字号/描边/安全区）由 `RichCaption` 保；
      版式意图（要不要、放哪、多大）由分镜的 `caption` 字段给；
      `mode="drawn"` 时外层**一个字不加**，字幕由画面写手自己画进画面。
    判据只有一条：分镜说 drawn 就不加，没说就兜底（见 `caption.py`）。
    """
    inner = body.replace("const Scene", f"const Inner{index + 1:02d}", 1)
    z0, z1 = _STORY_ZOOM[index % len(_STORY_ZOOM)]
    px, py = _STORY_PAN[index % len(_STORY_PAN)]
    text = str(scene.get("text") or "").strip()
    hi = str(scene.get("hi") or "").strip()
    if hi and text and hi not in text:
        hi = ""

    def q(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    # **模型自己写了画框（`<AbsoluteFill`）→ 运镜由它定，外层只留提亮层。**
    # 这是"不把画法写死"的落点：原来 23 镜全被套上同一个 Camera 缓推，
    # 收据上"组件覆盖率 100%"就是这么来的——那 100% 是外层注入的，不是它画的。
    own = "<AbsoluteFill" in body
    tag = f"      <Inner{index + 1:02d} dur={{dur}} />"
    if gain > 1.02:
        # 曝光闭环：实测偏暗就按倍数提亮。**只包场景、不包字幕**（字幕本来就白）。
        # 用 CSS filter 而不是全片盖白：盖白会把黑压成灰、把饱和色洗淡。
        tag = (f"      <div style={{{{ filter: 'brightness({gain:.2f})' }}}}>\n"
               f"        <Inner{index + 1:02d} dur={{dur}} />\n"
               f"      </div>")
    cam = [] if own else [
        f"      <Camera z0={{{z0}}} z1={{{z1}}} panX={{{px}}} panY={{{py}}}>",
        tag,
        "      </Camera>",
    ]
    # **兜底底色跟着 tone 走**（原来写死 `#05070c`）。这一层是模型没铺满整帧时露出来的
    # 东西——写死近黑，等于给所有片子垫了一块暗底；亮色调的片子会被它压暗。
    # 字幕/章节号的颜色按底色明暗反着来：白字压在明黄底上等于没有字。
    bgc = bg or "#05070c"
    fg = "#F2F5F8" if sum(int(bgc[i:i + 2], 16) for i in (1, 3, 5)) < 420 else "#12161F"
    lines = [
        "const Scene = ({ dur }: { dur: number }) => {",
        "  const frame = useCurrentFrame();",
        "  const t = dur > 0 ? frame / dur : 0;",
        "  return (",
        f"    <AbsoluteFill style={{{{ backgroundColor: '{bgc}' }}}}>",
    ] + (cam or [tag]) + [
        # 提亮层：在画面之上、文字之下（文字不该被一起洗白）。见 GRADE_OPACITY。
        f"      <AbsoluteFill style={{{{ backgroundColor: '#ffffff',"
        f" opacity: {GRADE_OPACITY} }}}} />",
    ]
    cap = caption.plan(scene)
    if text and cap["mode"] == "overlay" and not own:
        lines.append(f"      <RichCaption text={q(text)} hi={q(hi)} t={{t}} color={q(fg)}"
                     f" bottom={{{cap['bottom']}}} size={{{cap['size']}}}"
                     f" align={q(cap['align'])} />")
    lines += ["    </AbsoluteFill>", "  );", "};"]
    return inner + "\n" + "\n".join(lines)


def _ensure_motif(body: str) -> str:
    """模型**用了** `MOTIF_AT` 却没**声明**它时，补一个安全声明。

    真机踩过：它写 `const cx = width * MOTIF_AT.x;` 却不写那行声明——lint 不报
    （`no-undef` 没开），渲染到那一帧直接炸，整镜黑。补一个居中的默认值，
    位置不准但不会崩；同时这条会在报告里体现（锚点读出来是 (0.5,0.5,0.04)）。
    """
    if "MOTIF_AT" not in body:
        return body
    if re.search(r"const\s+MOTIF_AT\s*=", body):
        return body
    return ("const MOTIF_AT = { x: 0.5, y: 0.5, r: 0.04 };   // 补：模型用了没声明\n"
            + body)


#: 兜底镜的色相轮换。**只保证"这一镜有颜色、看得见"，不决定风格。**
_FALLBACK_HUES = ("#FF6B2C", "#22D3EE", "#FFC93C", "#FF3D7F")


def _fallback_body(index: int) -> str:
    """兜底一镜：**按镜号推**出来的最小场景，不是现成画面。

    原来这里是三套手写插画（射电望远镜/行星/城市废墟）——那是模板。现在只留"一定看得见"
    的几何：三段底色 + 一处径向光 + 一个落在 `MOTIF_AT` 的主体 + 一条地平线，按镜号轮换。
    """
    hue = _FALLBACK_HUES[index % len(_FALLBACK_HUES)]
    x = 0.32 if index % 2 == 0 else 0.68
    y = (0.42, 0.56, 0.68)[index % 3]
    gid = f"fb{index + 1:02d}"
    return f"""const MOTIF_AT = {{ x: {x}, y: {y}, r: 0.06 }};
const Scene = ({{ dur }}: {{ dur: number }}) => {{
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  const {{ width, height }} = useVideoConfig();
  const cx = width * {x}, cy = height * {y}, r = width * 0.15 * (1 + 0.06 * t);
  return (
    <svg width={{width}} height={{height}}>
      <defs>
        <linearGradient id="{gid}-bg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0a1020" />
          <stop offset="0.55" stopColor="#241d33" />
          <stop offset="1" stopColor="#4a2b26" />
        </linearGradient>
        <radialGradient id="{gid}-glow">
          <stop offset="0" stopColor="{hue}" stopOpacity="0.85" />
          <stop offset="1" stopColor="{hue}" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect x={{0}} y={{0}} width={{width}} height={{height}} fill="url(#{gid}-bg)" />
      <circle cx={{cx}} cy={{cy}} r={{r * 2.8}} fill="url(#{gid}-glow)" />
      <path d={{`M 0 ${{height * 0.78}} L ${{width}} ${{height * 0.74}} L ${{width}} ${{height}} L 0 ${{height}} Z`}}
            fill="#05070c" />
      <circle cx={{cx}} cy={{cy}} r={{r}} fill="{hue}" opacity={{0.92}} />
    </svg>
  );
}};"""


def safe_story_scene(scene: dict[str, Any], *, index: int, bg: str = "") -> str:
    """兜底一镜（见 `_fallback_body`）。报告里单独列 `fallback`，不假装是模型画的。"""
    return wrap_story_scene(_fallback_body(index), scene, index=index, bg=bg)
