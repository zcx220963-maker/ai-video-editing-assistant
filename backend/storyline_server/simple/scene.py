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

#: 整片的**提亮层**（白，5% 不透明度，盖在画面上、文字下）。
#:
#: 为什么需要它：提示词里写着"整帧平均亮度 ≥ 38/255"，模型还是稳定做到 27。
#: 它不是不会画亮，是**对绝对亮度没有直觉**——和"对占画面多少没有直觉"是同一个毛病。
#: 与其继续在提示词里加数字（实测加了也没用），不如在最后一道确定性代码里补。
#:
#: 这个数不是拍的。实测在一条 100 秒叙事片上：
#:
#:     原片                 YAVG p50 = 27.0　SATAVG p50 = 6.2
#:     +eq brightness 0.05  YAVG p50 = 39.9　SATAVG p50 = 7.6
#:     参考片               YAVG p50 = 41.6　SATAVG p50 = 6.3
#:
#: 5% 白叠加与 `eq=brightness=0.05` 等效（+12.75/255），所以放在 Remotion 里做
#: **不用重编码**——`voice.mux` 的 `-c:v copy` 得以保留。
#: 加对比度（contrast 1.08/1.12）反而把中位压回 31~34，所以**只提亮、不加对比**。
GRADE_OPACITY = 0.05


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


def wrap_story_scene(body: str, scene: dict[str, Any], *, index: int) -> str:
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
    chapter = str(scene.get("chapter") or "").strip()
    tmark = str(scene.get("time") or "").strip()
    if hi and text and hi not in text:
        hi = ""

    def q(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    lines = [
        "const Scene = ({ dur }: { dur: number }) => {",
        "  const frame = useCurrentFrame();",
        "  const t = dur > 0 ? frame / dur : 0;",
        "  return (",
        "    <AbsoluteFill style={{ backgroundColor: '#05070c' }}>",
        f"      <Camera z0={{{z0}}} z1={{{z1}}} panX={{{px}}} panY={{{py}}}>",
        f"        <Inner{index + 1:02d} dur={{dur}} />",
        "      </Camera>",
        # 提亮层：在画面之上、文字之下（文字不该被一起洗白）。见 GRADE_OPACITY。
        f"      <AbsoluteFill style={{{{ backgroundColor: '#ffffff',"
        f" opacity: {GRADE_OPACITY} }}}} />",
    ]
    if chapter:
        lines.append(f"      <ChapterMark index={q(chapter)} t={{t}} />")
    if tmark:
        lines.append(f"      <TimeMark text={q(tmark)} t={{t}} />")
    cap = caption.plan(scene)
    if text and cap["mode"] == "overlay":
        lines.append(f"      <RichCaption text={q(text)} hi={q(hi)} t={{t}}"
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


#: 叙事片的**兜底场景**（三套轮换）。每套都是手写并**实测过**的：
#: 内容占比 ~47%、平均亮度 ~37/255、边缘 1.5+ —— 也就是"画面确实有东西"。
#:
#: 为什么要这一层：真机两轮下来，模型有 5/15 镜始终画成"一层近黑底色"
#: （内容 5%、亮度 22）。它的错不在"不会画"，而在**对亮度没有直觉**——
#: 打回两次给了具体数字，它还是把天空三段写成近乎同色。
#: 与其继续烧调用，不如把这几镜换成"一定看得见"的那一套：
#: 三层天空 + 一处光源 + 一个占画面 25% 的剪影。
_STORY_SAFE_BODIES = (
    # A 夜晚基地：射电望远镜 + 人影 + 星场
    """const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <>
      <Sky top="#0a1020" mid="#241d33" low="#7a4630" glowHue="#ffd9a0" glow={0.7} />
      <StarField count={150} seed={9} t={t} />
      <Horizon y={0.70} ground="#080b12" glowHue="#ffb066" ripple={0.6} t={t} />
      <Dish x={0.66} y={0.62} r={0.24} tilt={-16} rim="#7fe6cf" t={t} />
      <Figure x={0.16} y={0.98} h={0.42} rim="#7fe6cf" t={t} />
      <Beam x={0.66} y={0.42} angle={250} spread={9} length={0.7}
            hue="#8ff0d8" opacity={0.5} t={t} />
    </>
  );
};""",
    # B 太空：行星 + 舰队 + 星场
    """const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <>
      <Sky top="#05070f" mid="#0d1526" low="#1b2740" glowHue="#9fd4ff" glow={0.35} />
      <StarField count={220} seed={13} t={t} />
      <Planet x={0.68} y={0.34} r={0.22} hue="#c9a06a" ring t={t} />
      <Fleet y={0.80} count={300} seed={5} hue="#cfd8e3" spread={1.1} t={t} />
      <Beam x={0.2} y={0.1} angle={62} spread={6} length={0.9}
            hue="#9fd4ff" opacity={0.28} t={t} />
    </>
  );
};""",
    # C 城市废墟：天际线 + 火点 + 落日
    """const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  return (
    <>
      <Sky top="#0b0a12" mid="#3a1f22" low="#8a4a26" glowY={0.55}
           glowHue="#ffb066" glow={0.75} />
      <Sun x={0.5} y={0.55} r={0.05} hue="#ffcf8f" halo={0.7} />
      <Horizon y={0.74} ground="#05070a" glowHue="#ff7a2f" ripple={0.25} t={t} />
      <Ruin y={0.74} seed={4} count={22} hue="#ff7a2f" fires={7} t={t} />
      <Figure x={0.82} y={0.97} h={0.34} flip rim="#ff9d5c" t={t} />
    </>
  );
};""",
)


def safe_story_scene(scene: dict[str, Any], *, index: int) -> str:
    """叙事片的兜底一镜：**一定画得出来**的那三套轮换（见 `_STORY_SAFE_BODIES`）。

    代价说清：它不画 `plate` 里那件具体的东西，所以是兜底不是常态。
    报告里单独列 `fallback`，不假装那也是模型画的。
    """
    body = _STORY_SAFE_BODIES[index % len(_STORY_SAFE_BODIES)]
    return wrap_story_scene(body, scene, index=index)
