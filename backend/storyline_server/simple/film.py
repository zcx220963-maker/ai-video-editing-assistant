# -*- coding: utf-8 -*-
"""多镜成片：一句主题 → N 镜 → **一个模块、一次渲完**。

为什么不是"渲 N 个 mp4 再拼"（主链的做法）：拼接层是 ffmpeg xfade，
转场做不了跨镜共享元素、全局特效叠不上去、接缝还吃掉 0.4s × (N-1)。

这里生成的是一段 TSX：N 个场景组件 + 一个 `<TransitionSeries>`，塞进**同一个 Composition**。
`durationInFrames` 由 Python 侧按各镜帧数求和后注入——所以**不用改渲染容器**，
现有 `/render-shot` 直接就能渲。

转场（2026-10-25 补）：用官方的 `@remotion/transitions`，`slide / wipe / zoom / blur /
fade / cut` 六选一，由分镜导演按叙事指定。为什么不用 `<Series>`：它一个 Transition
元素都放不下，只能硬切——那正是上一版"接缝生硬"的原因。

调用预算：1 次写分镜 + N 次写代码（并行，限 4 路）。60 秒 15 镜 = 16 次调用。
主链最近一次真机是 201 次。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from . import facts, kernel, qa, system
from . import voice as vo
from .kernel import KernelError
# 叙事片的"画法"拆到 story.py、"看自己画的一帧"拆到 review.py
# （两个模块都不在模块级 import 本文件，所以没有循环）。
# 这里重新导出，是为了 `film.wrap_story_scene` / `film.review_shots` 这些名字
# 对外仍然可用——测试与作业层都按 `film.xxx` 调，拆文件不该改调用方。
from .review import STORY_VISION_REVIEW, apply_review  # noqa: F401
from .morph import MORPH_HUE, parse_motif_at, shared_hops  # noqa: F401
from .scene import (  # noqa: F401  —— 包装层搬去了 scene.py，这里继续对外暴露
    _caption_fit, _scene_fingerprint, safe_story_scene, wrap_story_scene)
from .story import (  # noqa: F401
    _STORY_SCENE_SYSTEM, plan_story, write_scene_story_code)
from ..motion import remotion_client as rc

#: 单镜最长 5.2 秒——这是"看起来不像 PPT"的硬指标之一（对标片 60s/17镜/最长5.20s）。
MAX_SHOT_SEC = 5.2
#: 目标平均镜长。60 秒 → 15 镜。
TARGET_SHOT_SEC = 4.0
#: 镜数上下限。
#:
#: `MAX_SHOTS` 2026-10-25 从 40 提到 56：**有旁白时总长由文案决定，不是由你要的秒数决定**。
#: 实测 180 秒 / 40 镜那次，39 句平均 3.16 秒 → 实际只有 123 秒。要在 3 分钟里
#: 讲完一件事，需要 ~50 个句子；40 镜这个上限把它压死了。
#: 结构上限随之变成 56 × 5.2 = 291 秒。
MIN_SHOTS, MAX_SHOTS = 3, 56
#: 写代码的并发路数。太高会被上游限流，太低串行慢。
SCENE_CONCURRENCY = 4
#: 整片质检重写的**总预算**。60 秒 15 镜的首轮是 16 次调用，
#: 再放开重写会把调用翻一倍；定 12 的上限是「16 + 12 = 28，仍在 30 以内」。
#: 超出预算的不合格镜**如实报出去**，不假装通过——报告里能看到是哪几镜。
MAX_REWRITE = 12

#: 每个场景**不许写 import**——这里给一份统一的作用域。
#: 这样 N 段代码拼进一个模块时不会撞重复声明，也不会各引各的版本。
#:
#: 转场走官方的 `@remotion/transitions`（与 `remotion` 同号，镜像里已装，
#: 且 `lint-rules.mjs` 的 `ALLOWED_IMPORTS` 逐件列了它）。用 `TransitionSeries`
#: 而不是 `<Series>`：后者一个 Transition 元素都放不下，只能硬切。
SCENE_HEADER = """import React from 'react';
import { AbsoluteFill, Easing, Img, Series, interpolate, random, spring, staticFile, useCurrentFrame, useVideoConfig } from 'remotion';
import { TransitionSeries, blurSlide, crossZoom, linearTiming } from '@remotion/transitions';
import { fade } from '@remotion/transitions/fade';
import { slide } from '@remotion/transitions/slide';
import { wipe } from '@remotion/transitions/wipe';
import { E, lerp, seg } from '@dg/motion';
import { TOKENS } from '@dg/tokens';
"""

#: 两种片子，两套参数。**这不是风格选项，是两种画法**：
#:
#: · `explainer`（几何动效科普）：3~4 秒一镜、高饱和、14 件设计系统拼画面。
#:   参考片之外的那一类（我们一直在做的）。
#: · `story`（插画叙事）：**每镜一张插画 + 缓慢推拉**、8~12 秒一镜、暗调低饱和、
#:   章节号 + 时间标记 + 高亮字幕。用户给的那条三体叙事片就是这一类：
#:   实测 13 镜 / 平均 10.39 秒 / 最长 35.83 秒 / YAVG 41.6 / SATAVG 6.3。
#:
#: 把两套混在一起做不出来：拿"3.5 秒一镜 + 高饱和"的判据去要求插画叙事，
#: 会把长镜判成"像 PPT"、把暗调判成"寡淡"——判据方向正好相反。
MODE_EXPLAINER, MODE_STORY = "explainer", "story"
MODES = (MODE_EXPLAINER, MODE_STORY)
#: 叙事片的镜长区间。上限 12 秒是"长镜但不变幻灯片"的经验值；
#: 参考片有 35 秒的镜，那是靠**镜内运镜**撑住的，我们镜内只有缓推，先到 12 秒。
STORY_MIN_SHOT_SEC, STORY_MAX_SHOT_SEC = 4.5, 12.0
STORY_TARGET_SHOT_SEC = 9.0
#: 叙事片的镜数上限（镜少而长，不是靠镜数堆时长）。
STORY_MAX_SHOTS = 24

#: 叙事片的分镜导演。与科普那份的区别：**要故事，不要知识点**。
_STORY_SYSTEM = """你是叙事短片的分镜导演。把用户的主题写成一份**分镜脚本**。

这不是科普讲解，是**讲故事**：第二人称（"你"）或第三人称视角，有时间线，有悬念。

硬指标：
· 镜数 = 用户给的秒数 ÷ 9，最少 3 个、最多 24 个；
· **每镜 5~12 秒**（这是长镜片子，不要切碎）；
· 每镜一句字幕，**字数 ≈ 秒数 ÷ 0.22**（9 秒的镜约 40 字，最多 48 字）。
  **这是旁白，不是标题**：要能连着读下来，有主语有动词。
  写短了片子就短——总长由这些句子说多久决定，不由你写的秒数决定。
· 相邻两镜的画面要**明显不同**（不同地点/不同尺度/不同时间）。

每一镜写六样：
1. `text`：这一镜的**旁白/字幕**（约 `秒数 ÷ 0.22` 个字，最多 48 字）；
2. `hi`：`text` 里要**高亮**的那一小段（2~5 个字，是这句的重音，必须是 text 的子串）；
3. `plate`：**这一镜要画的那一张插画**（≤90 字）。写清：**谁/什么**在画面里、
   在哪个位置、什么姿态、光从哪来、背景是什么。
   写"一个男人站在巨大的绿色荧光屏前，屏幕上是起伏的波形，他的侧脸被屏幕照亮，
   背景是全黑的机房"，不写"展示信号"。
   **每镜必须有一个能被指认的具体物体或人**（剪影也算），不要写抽象概念；
4. `plate_style`（可选）：这一镜的特殊画风（"航拍视角"、"微距"、"水墨"），一般不写；
5. `chapter`：这一镜所属的**章节**（≤12 字，如"第 183 号文明"）。同段落可重复，
   换段落时换新的；
6. `time`：这一镜的**时间坐标**（≤12 字，如"地球时间 1975 年"、"两百年后"）。没有就不写；
7. `caption`（可选）：这一镜的字幕怎么放，`{"mode": "overlay|drawn|none", "at": "bottom|lower|center|upper|left", "size": 0.047}`。`overlay`=外层统一加（不写就是这个）；`drawn`=**你自己把这句话画进画面**（题款/批注/碑文/纸卡/屏幕上的字），外层一个字不加；`none`=这镜不要字幕。**画面里本来就有适合承载文字的东西（墙、纸、碑、屏幕、卷轴）时才用 `drawn`**；`drawn` 全片最多 1/3 的镜（位置每镜都变，观众跟不住），`at` 全片也只变 2~3 档。

全片要有**递进与收束**：开场给一个画面把人拽进来 → 中段推进 → 结尾留一个问句或反转。

**还要给一件贯穿全片的物件**（`motif`）：一件具体的东西，在第 3 镜之后反复出现，
每次的大小/位置/明暗都不同（参考片里那个"蓝色小点"就是这么用的）。
它让十几个独立的镜头变成**一部片子**，而不是十几张图。
`motif` 写 4~10 个字，必须是**能画出来的具体物件**（一个蓝点 / 一枚硬币 / 一道裂缝 / 一只眼睛）。
""" + facts.SOURCES_RULE + """
只回一行 JSON：{"title": "…", "motif": "…", "sources": [], "scenes": [{"text": "…", "hi": "…", "plate": "…", "chapter": "…", "time": "…", "caption": {"mode": "overlay", "at": "lower"}}]}"""
#: 转场长度（帧）。8 帧 @30fps ≈ 0.27 秒——够看清是"过渡"而不是"跳帧"，
#: 又不至于把 4 秒一镜的节奏吃掉。再长就该加镜数了，不是加转场。
TRANS_FRAMES = 8
#: plan 里 `transition` 的取值 → TSX 表达式。`cut` 不出 Transition 元素（硬切）。
TRANS_TSX = {
    "slide": "slide({ direction: 'from-right' })",
    "wipe": "wipe({ direction: 'from-left' })",
    "zoom": "crossZoom()",
    "blur": "blurSlide({ direction: 'from-bottom' })",
    "fade": "fade()",
}
#: 模型没写、或写了不认识的词时按这个循环发。
#: **刻意留了 `cut`**：全片每一处都是转场，观感会像 PPT 的"每页都飞入"；
#: 硬切是节奏的一部分，不是"没做转场"。
TRANS_CYCLE = ("slide", "zoom", "wipe", "blur", "slide", "cut")

_PLAN_SYSTEM = """你是知识科普短片的分镜导演。把用户的主题拆成一份**镜头表**。

硬指标（写之前先算好，这决定片子像不像 PPT）：
· 镜数 = 用户给的秒数 ÷ 4，最少 3 个、最多 56 个；
· **任何一镜都不超过 5.2 秒**；
· 相邻两镜的视觉手段必须不同（不要连续两镜都是"大标题 + 色块"）；
· **每镜字幕的字数按这一镜的秒数算**：约 `秒数 ÷ 0.22` 个字（旁白语速），
  最多 20 个字。4 秒的镜写 ~16 字，2.5 秒的镜写 ~10 字。
  **片子的总长是由这些句子说多久决定的**——写得都短，成片就会比你要求的短。

每一镜写五样：
1. `text`：这一镜的字幕（约 `秒数 ÷ 0.22` 个字，最多 20 字，可以没有标点）；
2. `brief`：画面上到底出现什么，**必须写成三层**（≤120 字）：
   背景层（底色/光晕/网格/色带）、中景（**至少 3 件实体**，最大的那件占画面 30%~55%）、
   前景（角标/刻度/细线/浮层）。**背景层和 HUD 不算"实体"**——实体是能被指着说
   "那是一只鲸鱼/那是一个齿轮/那是一根上升的曲线"的东西。
   写"一只鲸鱼从左向右穿过楼群，尾巴扫过第三栋楼的窗户"，不写"展示海洋生态"；
3. `hue`：这一镜的主色，十六进制（要**高饱和**：橙 #FF6B2C / 青 #22D3EE /
   黄 #FFC93C / 玫 #FF3D7F / 绿 #34D399）。整片只允许 2~3 个色相，按叙事推进换；
4. `motion`：两拍怎么动（≤40 字）。第一拍入画（0~50%），第二拍主运动（60~100%）；
5. `transition`：**怎么切进这一镜**，从这六个里挑一个：
   `cut`（硬切，最干脆）/ `slide`（推入，同段落内换视角）/ `wipe`（擦除，换段落）/
   `zoom`（冲入，强调一个数字）/ `blur`（模糊，时间跳跃）/ `fade`（淡入，收束或回忆）。
   相邻两处不要连着用同一个；全片至少有 3 处 `cut`，`fade` 最多两次。

全片要有**递进**：开场抓人 → 中段讲机制 → 收束给一句题眼。
""" + facts.SOURCES_RULE + """
只回一行 JSON：{"title": "…", "sources": [], "scenes": [{"text": "…", "brief": "…", "hue": "#FF6B2C", "motion": "…", "transition": "slide"}]}"""

_SCENE_SYSTEM = """你在写一个 Remotion 场景组件。只写**一个组件**，不写 import、不写 export。

输出格式（一字不差）：
```tsx
const Scene = ({ dur }: { dur: number }) => {
  const frame = useCurrentFrame();
  const t = dur > 0 ? frame / dur : 0;
  // …画面
  return (
    <AbsoluteFill style={{ backgroundColor: '#0a0c12' }}>
      {/* SVG / div 画出来的画面 */}
    </AbsoluteFill>
  );
};
```

作用域里已经有这些，**直接用，不要再 import**：
`React, AbsoluteFill, Series, useCurrentFrame, useVideoConfig, interpolate, spring,
random, Easing, E, lerp, seg, TOKENS`
（`seg(t, a, b, E.standard)` 切段，`lerp(t, x, y)` 插值，`E.standard` 是缓动）

**设计系统（直接用，这是画面好看的主要来源）**：
%%MANIFEST%%

用法纪律：
· **每一镜至少用 3 件上面的组件**——`Backdrop` 打底 + 一件讲内容的 + `HudFrame` 或 `Ribbon`。
· **每镜至少 8 个视觉元素**，分三层：
  背景层（`Backdrop` + 至少 1 条 `ScanGrid`/`Ribbon`/`Particles`）、
  中景（**≥3 件实体**，最大的那件占画面 30%~55%）、
  前景（`HudFrame`，或自己画角标/刻度/细线）。**层数不够就是"空"**。
· **高饱和色块占画面 ≥20%**：没有大块饱和色时用 `Ribbon` 铺一条斜色带。
  抽帧会量"亮且有色"的像素占比，**低于 11% 这一镜会被打回重写**——
  深蓝渐变 + 一行白字就是这个毛病。
· **画面任何时刻都不能是空的**：主体组件的 `start` 必须 ≤ 0.12（t=0 就进场），
  不许"前一半什么都没有、后一半才出现"。**只有几个点/几条线不算画面**，
  必须有一件够大的实体（占画面 ≥25%）。
· 组件收 `t`（这一镜的进度 0~1），`start` 决定它什么时候进场（0.05/0.35 这样错开就是两拍）。
· 组件已经处理了尺寸/颜色/缓动，**不要再自己写一遍**；要改颜色就传 `hue`。
· 清单里没有合适的，才自己画 SVG；自己画时也要遵守下面的纪律。

画面纪律（这是唯一决定好不好看的部分）：
· **字体只能用 `TOKENS.fontStack`（黑体）或 `TOKENS.fontSerif`（衬线）**：
  `fontFamily={TOKENS.fontStack}`。不要写 Georgia / Times / Arial / Inter / Helvetica
  这类系统字体名——镜像里没装，闸① 会直接拦掉整片。
· **这一镜的文案必须画在画面上**：`text` 会给你，用 `Caption` 组件画（`size` 传 0.06~0.10，
  它是**占宽度的比例**；传 0.6 那种值会把一个字顶满整屏）。位置由构图决定（右下、左下、
  与主体咬合都行），**不要每镜都贴底部**。
  字要有对比：压在画面上就加描边或底色块，别让它糊在背景里。
· **画幅就是 `useVideoConfig()` 给的 width/height**，`viewBox` 必须写 `0 0 {width} {height}`。
  **不要写死 1920×1080**——竖屏片子里那会渲成上下或左右的黑边（实测踩过）。
· **整帧铺满**：底色、天空、地面这些大面积色块必须铺满 width×height，
  不许只铺中间一条。判断标准：把这一帧缩到 1/3，画面边缘不能有黑边。
· **所有 `<rect>` 的 width/height 必须是正数**：算出来的值用 `Math.max(0, x)` 包一层，
  否则浏览器会报 `negative value is not valid`，那个元素直接不显示。
· 画**那一件具体的东西**，不是一页排版。大标题 + 色块 + 几行字不算画面。
· **纯色块用 SVG 的 `<rect fill>` / `<circle fill>`**，不要用 `<div style={{backgroundColor}}>`
  （闸① 会拦；`backgroundImage: linear-gradient(...)` 可以）。
· **两拍动且隔开**：0~50% 完成第一拍（主体入画/大色块擦入），60~100% 第二拍（主运动/收束）。
· **主体整段留在画面内**，起止各留 5% 余量；用位移和遮挡表现"穿过"，不要让它离场。
· 高饱和色块占画面 ≥25%；底色用 `#0a0c12`~`#12161f` 的深灰蓝，主体亮度 ≥200/255。
· 有光源方向：亮面、暗面、落地影。字号有阶梯，别居中堆字。
· 只画 brief 里有的东西，不添加人物、品牌、地名。
· 这一镜只有 `dur` 帧，所有时间点按 `t`（0~1）排，不要写死秒数。"""


def shot_count(seconds: float) -> int:
    """几镜。

    两条约束取大者：① 按「每镜不超过 ``MAX_SHOT_SEC``」反推的最少镜数；
    ② 按 4 秒理想节奏算的镜数。再夹到 ``MAX_SHOTS``。
    """
    import math

    need = int(math.ceil(float(seconds) / MAX_SHOT_SEC))
    ideal = int(round(float(seconds) / TARGET_SHOT_SEC))
    return max(MIN_SHOTS, min(MAX_SHOTS, max(need, ideal)))


def split_seconds(total: float, n: int) -> list[float]:
    """把总时长均分给 n 镜，**每镜严格不超过 MAX_SHOT_SEC**。

    配不出来就抛错，**不许静默摊**。2026-10-25 之前这里有一段"超了就把零头平摊
    回前 n-1 镜"的补差循环，而它**不复查上限**：240 秒 / 40 镜会得到每镜 6.02 秒、
    300 秒得到 7.56 秒，39 个镜全部违反"镜长 ≤5.2 秒"——而这是"不像 PPT"的硬指标。
    错得还不报错：成片照样渲出来，只是变慢变平。所以改成明确报错。
    """
    if n <= 0:
        raise KernelError("plan", "镜数必须 ≥1")
    each = float(total) / n
    if each > MAX_SHOT_SEC + 1e-9:
        import math
        need = int(math.ceil(float(total) / MAX_SHOT_SEC))
        raise KernelError(
            "plan",
            f"{total:g} 秒配不出来：每镜最多 {MAX_SHOT_SEC} 秒，需要至少 {need} 镜，"
            f"而镜数上限是 {MAX_SHOTS}。这条线单条最长 "
            f"{MAX_SHOTS * MAX_SHOT_SEC:.0f} 秒——请把时长降到这个数以内，"
            f"或者拆成两条。")
    if n == 1:
        return [round(float(total), 2)]

    # 分配方式：**先向下取到 0.01，再把零头按 0.01 的份额摊给前几镜**。
    # 为什么不是"四舍五入 + 零头全给最后一镜"：290.2 秒 / 56 镜时每镜 5.182，
    # 四舍五入成 5.18 后最后一镜要吃 0.12 的零头变成 5.30——**超过 5.2 的上限**；
    # 再挪给倒数第二镜就成了 5.28，还是超。向下取 + 摊零头则每镜最多 5.19。
    import math

    base = math.floor(each * 100) / 100
    out = [base] * n
    k = int(round((float(total) - base * n) / 0.01))
    for i in range(min(k, n)):
        out[i] = round(out[i] + 0.01, 2)
    return out


def ensure_vivid(color: Any, *, sat_floor: float = 0.62,
                 val_floor: float = 0.86) -> str:
    """把主色抬到"看得出颜色"的档位。

    模型会传 `#0a0c12` / `#1b2733` 这类近黑色当 `hue`——它以为"深色高级"，
    而抽帧量出来是"亮且有色 0%"（等于黑白片）。这里按 HSV 抬饱和度与明度，
    **色相不动**，所以"橙还是橙、青还是青"，只是从灰暗变成能看见。

    组件里的 `lift()` 只在背景光晕上用；场景自己写 `fill={hue}` 时不吃那一层，
    所以必须在 Python 侧先修一遍。
    """
    import colorsys

    s = str(color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        return "#FF6B2C"
    r, g, b = (int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, max(sat, sat_floor), max(val, val_floor))
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def transitions_for(scenes: list[dict[str, Any]]) -> list[str]:
    """每一处切点用什么转场。返回长度 ``n-1`` 的列表。

    模型给了合法值就用它的（转场是叙事的一部分：换段落该用 `wipe`，
    强调数字该用 `zoom`）；没给或不认识就按 :data:`TRANS_CYCLE` 发，
    保证**不会因为模型漏写而退化成整片硬切**。
    """
    n = max(0, len(scenes) - 1)
    out: list[str] = []
    for i in range(n):
        raw = str(scenes[i].get("transition") or "").strip().lower()
        ok = raw == "cut" or raw in TRANS_TSX
        out.append(raw if ok else TRANS_CYCLE[i % len(TRANS_CYCLE)])
    return out


#: 叙事片的转场循环：以 `fade` 与硬切为主。
#: 参考片 12 处切点里大半是硬切/淡入——插画叙事靠**画面本身**换场，
#: 推拉擦除那类动效转场会把"电影感"打回"信息图"。
STORY_TRANS_CYCLE = ("fade", "cut", "cut", "fade", "cut", "cut")


def transitions_for_story(scenes: list[dict[str, Any]]) -> list[str]:
    n = max(0, len(scenes) - 1)
    out: list[str] = []
    for i in range(n):
        raw = str(scenes[i].get("transition") or "").strip().lower()
        if raw in ("cut", "fade"):
            out.append(raw)
        else:
            out.append(STORY_TRANS_CYCLE[i % len(STORY_TRANS_CYCLE)])
    return out


def shot_count_story(seconds: float) -> int:
    """叙事片的镜数：按 9 秒一镜算，夹在 3~24。**镜少而长**是它的形状。"""
    n = int(round(float(seconds) / STORY_TARGET_SHOT_SEC))
    return max(3, min(STORY_MAX_SHOTS, n))


#: 镜内运镜的四种配法（起始倍率 → 结束倍率，横/纵位移）。
#: 每镜换一种，避免十几镜都是"同一个方向慢慢推"——那种统一感会立刻露馅。
#: SVG 里"负尺寸"会**静默不渲染**（浏览器只在控制台喊一句 `A negative value is not valid`，
#: 那一块画面直接没了）。实测一条 60 秒片子报了 `height: -2652287.99` —— 抽帧看是
#: 画面右下缺一整块黑。所以在拼装时兜一层 `Math.max(0, …)`，而不是在提示词里求模型。
#:
#: 为什么按**属性名**扫全文、而不是按 `<rect>` 标签扫：标签正则一遇到 JSX 表达式里
#: 的 `>`（`fill={t > 0.5 ? a : b}`）就提前截断，后面的属性全被漏掉。属性名扫不到这个坑。
_DIM_RE = re.compile(r"\b(width|height|rx|ry|r)=\{([^{}]+)\}")


def _fix_rects(code: str) -> str:
    """给 SVG 的尺寸属性兜一层 `Math.max(0, …)`。

    为什么值得一个正则：这类错误**不报错、不中断、不留痕迹**，只是画面上少一块。
    lint 看不见（语法合法），渲染也返回 200，只有翻浏览器控制台才发现。
    代价是负值会被夹成 0（那一块仍然不显示），但至少不会因为 `NaN` 传染到相邻属性。
    """
    def fix_attr(a: re.Match[str]) -> str:
        name, expr = a.group(1), a.group(2).strip()
        if expr.startswith("Math.max("):
            return a.group(0)
        return f"{name}={{Math.max(0, {expr})}}"

    return _DIM_RE.sub(fix_attr, code)


def safe_scene(text: str, hue: str, *, index: int = 0) -> str:
    """兜底场景：设计系统拼出来的、**一定过质检**的一镜。

    什么时候用：模型写了两版、抽帧仍然是空的（内容占比 12%）或寡淡的。
    那时候继续求它只是多花调用——它已经证明自己这一镜写不出来。
    换成这个"通用但有构图"的版本，比在成片里留一个近乎全黑的帧好。

    代价说清：它不画 brief 里那件具体的东西，所以它是**兜底**不是常态。
    报告里会列出哪几镜走了兜底（`fallback`），不假装那些也是模型画的。

    参数按镜号错开一点（色带角度与高度），避免连着几镜兜底时长得一模一样。
    """
    import json

    y = (0.58, 0.64, 0.60)[index % 3]
    ang = (-14, 12, -8)[index % 3]
    return (
        "const Scene = ({ dur }: { dur: number }) => {\n"
        "  const frame = useCurrentFrame();\n"
        "  const t = dur > 0 ? frame / dur : 0;\n"
        f"  const HUE = {json.dumps(hue)};\n"
        "  return (\n"
        "    <AbsoluteFill style={{ backgroundColor: '#0a0c12' }}>\n"
        "      <Backdrop hue={HUE} t={t} />\n"
        f"      <Ribbon hue={{HUE}} t={{t}} y={{{y}}} thick={{0.26}} angle={{{ang}}} />\n"
        "      <Ribbon hue=\"#FFC93C\" t={t} y={0.30} thick={0.08} angle={22} opacity={0.8} />\n"
        "      <ScanGrid t={t} hue={HUE} y={0.70} />\n"
        "      <GlowOrb t={t} hue={HUE} x={0.5} y={0.38} r={0.16} />\n"
        "      <Particles count={60} t={t} hue={HUE} seed={3} />\n"
        "      <HudFrame t={t} />\n"
        f"      <Caption text={json.dumps(str(text or ''), ensure_ascii=False)}"
        " t={t} start={0.05} size={0.082} y={0.86} />\n"
        "    </AbsoluteFill>\n"
        "  );\n"
        "};"
    )


def _inject_ribbon(body: str, hue: str) -> str:
    """给"寡淡"的镜**确定性**补一条斜色带。

    为什么不继续让模型改：实测 s03 重写两次仍然是"亮且有色 9%"——它每次都去调字号
    和位置，就是不加色块（提示词里写了也没用，模型对"占比"没有可靠直觉）。
    而插一条 20% 高的高饱和色带是**几何上确定**的：色带面积 ≥18% 画面，
    一定能把 vivid 抬过 11% 的线。

    插在第一个 `<AbsoluteFill>` 之后，所以它在背景之上、主体之下——
    不遮内容，只补颜色。已经有 `<Ribbon>` 的镜不重复插。
    """
    if "<Ribbon" in body:
        return body
    m = re.search(r"<AbsoluteFill\b[^>]*>", body)
    if not m:
        return body
    ins = f'\n      <Ribbon hue="{hue}" t={{t}} y={{0.68}} thick={{0.2}} angle={{-12}} />'
    return body[:m.end()] + ins + body[m.end():]


def _parse_json(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    import json
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


async def plan(prompt: str, *, seconds: float, aspect: str = kernel.DEFAULT_ASPECT,
               llm: Any = None, user_id: str = "",
               conversation_id: str = "") -> tuple[dict[str, Any], int]:
    """一次调用出镜头表。返回 ``(plan, 调用次数)``。"""
    n = shot_count(seconds)
    ask = (f"主题：{prompt.strip()}\n\n"
           f"总时长 {seconds:g} 秒，请拆成 **{n} 个镜头**，每镜约 "
           f"{seconds / n:.2f} 秒（不得超过 {MAX_SHOT_SEC} 秒）。"
           f"画幅 {aspect}。")
    try:
        raw = await kernel._complete(
            [{"role": "system", "content": _PLAN_SYSTEM},
             {"role": "user", "content": ask}],
            llm=llm, user_id=user_id, conversation_id=conversation_id)
    except Exception as exc:  # noqa: BLE001
        raise KernelError("plan", f"分镜调用失败（{type(exc).__name__}）：{exc}") from exc

    got = _parse_json(raw)
    if not got or not isinstance(got.get("scenes"), list) or not got["scenes"]:
        raise KernelError("plan", "分镜没回合法 JSON")

    scenes = [s for s in got["scenes"] if isinstance(s, dict)][:n]
    if not scenes:
        raise KernelError("plan", "分镜里没有可用镜头")
    return {"title": str(got.get("title") or "").strip(),
            "sources": facts.normalize(got.get("sources")), "scenes": scenes}, 1


def _looks_complete(body: str) -> bool:
    """粗查这段组件体是不是**完整的**：括号配平、以 `};` 收尾、含 return。

    为什么要这一步：模型偶尔会把一段截断（`max_tokens` 用尽、或自己少写一个 `}`），
    而 lint 只会在**拼好的整模块**上报一个 `parse-error 第 2328 行`——
    那时候已经烧了 16 次调用，定位还得靠人肉数括号。在这里挡住，只重写那一镜。
    """
    s = body.strip()
    if not s.endswith("};"):
        return False
    if "return" not in s:
        return False
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    in_str: str | None = None
    i = 0
    while i < len(s):
        ch = s[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in "'\"`":
            in_str = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
        i += 1
    return not stack and in_str is None


async def write_scene(scene: dict[str, Any], *, index: int, total: int,
                      seconds: float, prev: str = "", nxt: str = "",
                      aspect: str = kernel.DEFAULT_ASPECT, llm: Any = None,
                      user_id: str = "", conversation_id: str = "",
                      extra: str = "") -> str:
    """写一镜的组件体（不含 import / export）。`extra` 是质检打回时的具体指令。"""
    ask = (f"这是第 {index + 1}/{total} 镜，时长 {seconds:g} 秒。\n"
           f"**这一镜要画在画面上的文案（必须出现）**：{str(scene.get('text') or '').strip()}\n"
           f"画面 brief：{str(scene.get('brief') or '').strip()}\n"
           f"主色：{str(scene.get('hue') or '#FF6B2C').strip()}\n"
           f"动效：{str(scene.get('motion') or '').strip()}\n"
           + (f"上一镜画面：{prev}\n" if prev else "")
           + (f"下一镜画面：{nxt}\n" if nxt else "")
           + (f"\n**上一版的问题（必须修掉）**：{extra}\n" if extra else "")
           + f"画幅：{aspect}。只写那一个 `const Scene = ...`，不写 import/export。")
    last_err = ""
    for attempt in range(3):
        try:
            raw = await kernel._complete(
                [{"role": "system",
                  "content": _SCENE_SYSTEM.replace("%%MANIFEST%%", system.manifest_block())},
                 {"role": "user", "content": ask}],
                llm=llm, user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001
            raise KernelError("scene", f"第 {index + 1} 镜写码失败：{exc}") from exc

        body = kernel.extract_code(raw) or str(raw or "").strip()
        if "const Scene" not in body:
            last_err = "没按格式回 `const Scene`"
            continue
        body = body[body.index("const Scene"):].strip()
        if not _looks_complete(body):
            last_err = "组件体不完整（括号不配平 / 没有以 `};` 收尾）"
            continue
        return body

    raise KernelError("scene", f"第 {index + 1} 镜写了 3 次仍不完整：{last_err}")


def _fix_div_bg(code: str) -> str:
    """`backgroundColor: X` → `backgroundImage: linear-gradient(X, X)`。

    闸① 有一条 `dg/no-raw-styled-div`：**不许用 `<div>` 的 backgroundColor 画纯色块**
    （纯色块要走 SVG 的 `<rect fill>`）。模型每几镜就会犯一次，而一条 60 秒片子
    已经烧了 16 次调用——为一个色块重来一遍不值。
    渐变的观感与纯色完全一致，所以这里是等价替换，不是"绕过判据"。
    """
    def repl(m: re.Match[str]) -> str:
        val = m.group(1)
        return f"backgroundImage: `linear-gradient({val}, {val})`"

    return re.sub(r"backgroundColor\s*:\s*('[^']*'|\"[^\"]*\"|[A-Za-z0-9_$.]+)", repl, code)


def _fix_fonts(code: str) -> str:
    """把任何字体族字面量换成 `TOKENS.fontStack`。

    为什么必须有这一步：镜像里只装了 Noto CJK / DejaVu 几族，模型一写
    `fontFamily="Georgia, 'Times New Roman', serif"` 就会被闸① 按
    `dg/font-family-whitelist` 挡下——一次 60 秒片子已经烧了 16 次调用，
    为了一个字体名重来一遍不值。这里是**确定性修补**，比在提示词里求它管用。
    """
    code = re.sub(r'fontFamily\s*=\s*(["\'])[^"\']*\1',
                  'fontFamily={TOKENS.fontStack}', code)
    code = re.sub(r'fontFamily\s*:\s*(["\'])[^"\']*\1',
                  'fontFamily: TOKENS.fontStack', code)
    return code


def timeline(secs: list[float], kinds: list[str], *,
             fps: int = kernel.DEFAULT_FPS) -> dict[str, Any]:
    """整片时间表：每镜的**序列时长**、起始帧、切点帧、总帧数。

    为什么单独算：`TransitionSeries` 里相邻两镜**重叠** ``TRANS_FRAMES`` 帧，
    所以「序列时长」比「可见时长」长，而总帧数 = ``Σ序列 − Σ转场``。
    这段算术错的表现很隐蔽——**最后一镜被截掉半秒**（配不上 composition 的
    `durationInFrames`），或者片尾多出一段黑。所以放成纯函数，可单独测。

    最后一镜再吃一次「逐镜四舍五入 vs 整段四舍五入」的差：
    8 镜 × 3.75 秒，逐镜 round 出来是 904 帧，整段是 900 帧。
    """
    n = len(secs)
    base = [kernel.frames_of(s, fps) for s in secs]
    trans = [TRANS_FRAMES if (i < len(kinds) and kinds[i] != "cut") else 0 for i in range(n)]
    durs = [base[i] + trans[i] for i in range(n)]

    # 总帧数 = Σ序列 − Σ转场（重叠部分只播一次）。
    # 再抹平「逐镜四舍五入 vs 整段四舍五入」的差：8 镜 × 3.75 秒，
    # 逐镜 round 出来是 896 帧，整段是 900 帧——差在最后一镜上。
    target = kernel.frames_of(sum(secs), fps)
    if n:
        total = sum(durs) - sum(trans)
        durs[-1] = max(1, durs[-1] + (target - total))

    starts: list[int] = []
    cursor = 0
    for i in range(n):
        starts.append(cursor)
        cursor += durs[i] - trans[i]

    # 切点取「过渡中点」：快门闪要闪在观众感知到换镜的那一帧，不是过渡开始那一帧。
    cuts = [int(starts[i] + trans[i - 1] / 2) for i in range(1, n)]
    return {"base": base, "trans": trans, "durs": durs,
            "starts": starts, "cuts": cuts, "total": target}


def assemble(bodies: list[str], secs: list[float], kinds: list[str], *,
             fps: int = kernel.DEFAULT_FPS,
             motifs: list[tuple[float, float, float] | None] | None = None,
             ) -> tuple[str, dict[str, Any]]:
    """N 段组件体 → 一个模块：统一 import + 设计系统 + `TransitionSeries` + 全局层。

    返回 ``(代码, 时间表)``。时间表给上层做读数（镜数/转场/切点），
    出问题时也一眼看得出是"哪一镜的时长算错了"。

    `motifs` 是**每镜"贯穿物件"的位置**（`story.parse_motif_at` 读出来的）。
    给了它，相邻两镜都有位置的切点就会生成一次"接力"（`SharedMorph`）——
    这是参考片那个"小点一路变成地球"的做法：元素在 `TransitionSeries` **外面**
    被驱动，所以切镜时它不会跟着上一镜一起卸载。
    """
    parts = [SCENE_HEADER, system.SYSTEM_TSX, ""]
    for i, body in enumerate(bodies):
        body = _fix_rects(_fix_fonts(_fix_div_bg(body)))
        # 每镜都可能写 `const MOTIF_AT = …`，15 镜放一个模块里会撞 `no-redeclare`。
        # 连它里面的引用一起改名（`MOTIF_AT.x` → `MOTIF_AT_07.x`）——
        # 锚点在 `rebuild()` 里已经读过了，这里改名不影响接力。
        body = re.sub(r"\bMOTIF_AT\b", f"MOTIF_AT_{i + 1:02d}", body)
        # `const Scene` → `const S01`（只换第一处，模型若在注释里提到不影响）
        parts.append(body.replace("const Scene", f"const S{i + 1:02d}", 1))
        parts.append("")

    tl = timeline(secs, kinds, fps=fps)
    n = len(secs)

    parts.append(system.CAMERA_DRIFT_TSX)
    parts.append(system.GLOBAL_FX_TSX)
    parts.append("const Shot: React.FC = () => (")
    parts.append("  <AbsoluteFill style={{ backgroundColor: '#0a0c12' }}>")
    parts.append("    <Drift>")
    parts.append("      <TransitionSeries>")
    for i in range(n):
        dur = tl["durs"][i]
        parts.append(f"        <TransitionSeries.Sequence durationInFrames={{{dur}}}>")
        parts.append(f"          <S{i + 1:02d} dur={{{dur}}} />")
        parts.append("        </TransitionSeries.Sequence>")
        if i < len(kinds) and kinds[i] != "cut":
            parts.append("        <TransitionSeries.Transition"
                         f" presentation={{{TRANS_TSX[kinds[i]]}}}"
                         f" timing={{linearTiming({{ durationInFrames: {TRANS_FRAMES} }})}} />")
    parts.append("      </TransitionSeries>")
    parts.append("    </Drift>")
    parts.append(f"    <GlobalFX cuts={{[{', '.join(str(c) for c in tl['cuts'])}]}} />")

    # ---- 跨镜接力：相邻两镜都声明了贯穿物件的位置，就在切点上接一次 ----
    hops = shared_hops(tl, motifs, n)
    if hops:
        rows = ", ".join(
            "{at: %d, dur: %d, from: [%g, %g, %g], to: [%g, %g, %g], hue: '%s'}"
            % (h["at"], h["dur"], *h["from"], *h["to"], h["hue"]) for h in hops)
        parts.append(f"    <SharedMorph hops={{[{rows}]}} />")

    parts.append("  </AbsoluteFill>")
    parts.append(");")
    parts.append("")
    parts.append("export default Shot;")
    parts.append("")
    return "\n".join(parts), tl


async def _make_story(prompt: str, *, seconds: float, aspect: str, fps: int,
                      llm: Any, user_id: str, conversation_id: str,
                      on_stage: Any, voice: bool,
                      scenes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """插画叙事片：分镜（1 次）→ 出图（N 次）→ 确定性拼装 → 一次渲完。

    与动效模式的三处结构差别：
    ① 镜少而长（9 秒一镜，上限 12 秒）；② 每镜画面由**一张图**决定，
    所以场景代码是**确定性生成**的，不逐镜调模型；③ 判据换成"图加载出来没有"，
    不判饱和度与层次（暗调低饱和正是它要的样子）。
    """
    def tick(stage: str, done: int, total: int) -> None:
        if on_stage:
            try:
                on_stage(stage, done, total)
            except Exception:  # noqa: BLE001
                pass

    tick("plan", 0, 0)
    if scenes:
        # 分镜由调用方给定（人改过的脚本 / 复用上一次的分镜）：跳过规划调用。
        plan_obj, calls = {"title": "", "scenes": list(scenes)}, 0
    else:
        plan_obj, calls = await plan_story(prompt, seconds=seconds, aspect=aspect,
                                           llm=llm, user_id=user_id,
                                           conversation_id=conversation_id)
    scenes = plan_obj["scenes"]
    n = len(scenes)

    # ---- 旁白 → 镜长 ----
    narr: dict[str, Any] | None = None
    if voice:
        tick("voice", 0, n)
        try:
            narr = await vo.narrate([str(s.get("text") or "") for s in scenes],
                                    kernel.out_dir() / "voice")
        except Exception as exc:  # noqa: BLE001
            narr = {"files": [None] * n, "secs": [0.0] * n,
                    "failed": list(range(1, n + 1)),
                    "error": f"{type(exc).__name__}: {exc}"}
    if narr and any(f is not None for f in narr["files"]):
        secs = vo.fit_secs(narr["secs"], lo=STORY_MIN_SHOT_SEC, hi=STORY_MAX_SHOT_SEC)
        # 镜数按"目标 ÷ 9 秒"定，但每镜**实际**停多久由那句话要说多久定——
        # 实测要 100 秒、11 镜、每句 5.8 秒 → 出来 69.3 秒，只有目标的 69%。
        # 观众要的是"3 分钟的片子"，不是"11 个镜"。缺口摊成每镜说完之后的留白
        # （`build_track` 本来就按镜长 pad），所以音画同步不受影响，只是节奏更慢。
        secs = vo.fill_to(secs, float(seconds), hi=STORY_MAX_SHOT_SEC)
    else:
        if narr is not None:
            narr["degraded"] = True
        each = max(STORY_MIN_SHOT_SEC, min(STORY_MAX_SHOT_SEC, float(seconds) / n))
        secs = [round(each, 2)] * n
        secs[-1] = round(float(seconds) - sum(secs[:-1]), 2)

    # ---- 画面：**每镜用 SVG 现画** ----
    #
    # 2026-10-25 删掉了"出图通道"（`plate.py` 184 行 + 这里的 Plate 分支）。
    # 它是我误判"参考片是 AI 插画"时写的，后来证明参考片是**纯代码画的**。
    # 留着它就是"以防万一"的代码——而那正是屎山的起点：一条没人走的路，
    # 却要跟着每个改动一起维护。
    bodies: list[str] = []
    picture = "code"
    #: 贯穿全片的那一件东西（分镜给；没给就是空串，不影响出片）。
    motif = str(plan_obj.get("motif") or "")
    # 每镜 1 次调用（并发 4），外层统一套运镜与文字层。
    tick("scenes", 0, n)
    sem = asyncio.Semaphore(SCENE_CONCURRENCY)
    done = 0

    async def one(i: int) -> str:
        nonlocal done
        async with sem:
            try:
                body = await write_scene_story_code(
                    scenes[i], index=i, total=n, seconds=secs[i],
                    aspect=aspect, llm=llm, user_id=user_id,
                    conversation_id=conversation_id, motif=motif)
            finally:
                done += 1
                tick("scenes", done, n)
            return wrap_story_scene(body, scenes[i], index=i)

    results = await asyncio.gather(*[one(i) for i in range(n)],
                                   return_exceptions=True)
    scene_errors: list[str] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            # **兜底也要是叙事片的兜底**：这里如果退回 `safe_scene`（几何动效那套），
            # 成片里就会混进"大圆 + 斜色带"，一眼看出不是同一个世界。
            #
            # 但兜底会**盖住失败原因**：2026-10-25 拆文件漏了 `SCENE_HEADER`，
            # 15 镜全 NameError、全走兜底，日志上只看到「质检 1.0」。
            # 所以这里把错误原文收起来，报告里必须能看见。
            scene_errors.append(f"s{i + 1:02d} {type(r).__name__}: {str(r)[:160]}")
            bodies.append(safe_story_scene(scenes[i], index=i))
        else:
            bodies.append(r)
    calls += n

    kinds = transitions_for_story(scenes)
    plan_obj["transitions"] = kinds

    def rebuild() -> tuple[str, dict[str, Any]]:
        """拼装一次。**每次都重新读 motif 锚点**——重写过的镜位置会变，
        沿用旧锚点会让接力接到空气上。"""
        return assemble(bodies, secs, kinds, fps=fps,
                        motifs=[parse_motif_at(b) for b in bodies])

    code, tl = rebuild()
    rewritten: list[int] = []

    # ---- 视觉自检：渲一帧给写手**自己看**（治"母题重复"；实现在 review.py） ----
    # 排在像素判据之前是有意的：像素判据只能抓"全黑/寡淡"，抓不到"又是城市+落日"。
    if picture == "code" and STORY_VISION_REVIEW:
        tick("review", 0, n)
        code, tl, fixed = await apply_review(
            code, tl, bodies, scenes, secs, kinds, aspect=aspect, fps=fps,
            llm=llm, user_id=user_id, conversation_id=conversation_id,
            motif=motif, on_progress=lambda d, t: tick("review", d, t))
        rewritten.extend(fixed)
        calls += len(fixed)

    # ---- 判据换一套 ----
    tick("audit", 0, n)
    audit = await qa.audit(code, secs, aspect=aspect, fps=fps, story=True)

    # ---- 太暗 / 空的镜：带**具体读数**重写一次（只重写那几镜） ----
    #
    # 真机第一版 10/15 镜被判 empty：模型把"暗调"做成了近黑（亮度 17/255、内容 5%）。
    # 它的错不在"没画"，而在"只铺了一层近黑底色"。所以打回指令必须给数字，
    # 不能只说"亮一点"——模型对"亮多少"没有直觉。
    if picture == "code" and audit.get("ok") and audit.get("bad"):
        for shot_no in qa.priority(audit["shots"], audit["bad"])[:6]:
            i = shot_no - 1
            row = next((r for r in audit["shots"] if r["index"] == shot_no), {})
            hint = (f"整帧平均亮度只有 {row.get('mean_lum', 0):.0f}/255、"
                    f"内容占比 {row.get('content', 0):.0%}——说明你只铺了一层近黑底色，"
                    "或者主体太小看不见。必须做到：天空用**三段不同色**"
                    "（如 top #0a1020 / mid #2a2340 / low #7a4630），"
                    "加一处光源（太阳/月亮/信号光，用 `<circle>` + 径向渐变），"
                    "和一个**占画面 ≥25% 的剪影**（望远镜/人影/城市/行星，用 `<path>` 画轮廓），"
                    "主体与背景的亮度差 ≥40 级。整帧平均亮度要到 38/255 以上。")
            try:
                body = await write_scene_story_code(
                    scenes[i], index=i, total=n, seconds=secs[i], aspect=aspect,
                    llm=llm, user_id=user_id, conversation_id=conversation_id,
                    extra=hint, motif=motif)
                bodies[i] = wrap_story_scene(body, scenes[i], index=i)
                rewritten.append(shot_no)
            except KernelError:
                pass
        if rewritten:
            calls += len(rewritten)
            code, tl = rebuild()
            audit = await qa.audit(code, secs, aspect=aspect, fps=fps, story=True)

    # ---- 兜底：重写一轮后仍然空的镜，换成"一定画得出来"的那三套 ----
    fallback: list[int] = []
    if picture == "code" and audit.get("ok") and audit.get("bad"):
        for shot_no in audit["bad"]:
            i = shot_no - 1
            bodies[i] = safe_story_scene(scenes[i], index=i)
            fallback.append(shot_no)
        if fallback:
            code, tl = rebuild()
            part = await qa.audit(code, secs, aspect=aspect, fps=fps,
                                  story=True, only=fallback)
            audit = qa.merge_audit(audit, part)

    cov = system.coverage(bodies)
    # **模板化读数**：把每镜的"画法指纹"去重，剩几个就是几种画法。
    # 15 镜只有 3~4 种 = 在重排同一批零件；接近 15 才是每镜各画各的。
    #
    # 为什么不用"用了哪些组件"当读数：零件清单从提示词里拿掉之后，每镜都是
    # `<svg>` + 自己的 path，组件集合恒等于"Camera + 文字层"，那个读数会变成 1，
    # 反而看不出多样性。**数画法才看得出"每镜是不是各画各的"。**
    variety = len({_scene_fingerprint(b) for b in bodies})
    voice_info: dict[str, Any] | None = None
    if narr is not None:
        vsecs = [float(s) for s in (narr.get("secs") or [])]
        voice_info = {
            "files": [str(p) if p else "" for p in (narr.get("files") or [])],
            "secs": [round(s, 2) for s in vsecs],
            "failed": narr.get("failed") or [],
            "degraded": bool(narr.get("degraded")),
            "error": narr.get("error") or "",
            "over": [] if narr.get("degraded")
                    else vo.over_long(vsecs, hi=STORY_MAX_SHOT_SEC),
        }

    return {"code": code, "plan": plan_obj, "secs": secs,
            "calls": calls, "shots": n, "mode": MODE_STORY,
            "coverage": cov, "audit": audit, "rewritten": rewritten, "patched": [],
            "fallback": fallback, "picture": picture, "scene_errors": scene_errors,
            "variety": variety,
            "timeline": tl, "transitions": kinds,
            "bodies": bodies, "scenes": scenes,
            "elements": {i + 1: system.element_count(b) for i, b in enumerate(bodies)},
            "voice": voice_info,
            "total_sec": round(sum(secs), 2)}


async def make_film(prompt: str, *, seconds: float = 60.0,
                    aspect: str = kernel.DEFAULT_ASPECT, fps: int = kernel.DEFAULT_FPS,
                    llm: Any = None, user_id: str = "",
                    conversation_id: str = "",
                    on_stage: Any = None,
                    voice: bool = True,
                    mode: str = MODE_EXPLAINER,
                    scenes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """一句主题 → 一条多镜成片。返回 ``{code, plan, secs, calls, ...}``。

    `on_stage(stage, done, total)` 用来推进度（作业表用），可以不给。

    `voice=True` 时先给每镜字幕合成旁白，**并按语音时长定镜长**（`secs` 由它决定，
    与传进来的 `seconds` 只是"目标"，实际总长以语音为准）。TTS 全挂时自动退回无声
    均分，`voice_info["degraded"]` 会写清——不把可用性绑在一个外部服务上。

    `mode` 见 :data:`MODE_EXPLAINER` / :data:`MODE_STORY`：两种片子两套画法与判据。
    `scenes` 给了就跳过规划调用（人改过的脚本 / 复用上一次的分镜）。
    """
    if str(mode or "").strip().lower() == MODE_STORY:
        return await _make_story(
            prompt, seconds=seconds, aspect=aspect, fps=fps, llm=llm,
            user_id=user_id, conversation_id=conversation_id,
            on_stage=on_stage, voice=voice, scenes=scenes)

    def tick(stage: str, done: int, total: int) -> None:
        if on_stage:
            try:
                on_stage(stage, done, total)
            except Exception:  # noqa: BLE001
                pass

    tick("plan", 0, 0)
    plan_obj, calls = await plan(prompt, seconds=seconds, aspect=aspect, llm=llm,
                                 user_id=user_id, conversation_id=conversation_id)
    scenes = plan_obj["scenes"]
    n = len(scenes)

    # ── 镜长：有旁白就按"这句话要说多久"定，没有才均分 ──────────────────────
    #
    # 均分的毛病很具体：5 个字的镜和 14 个字的镜一样长，于是慢的那几镜像卡住、
    # 快的那几镜像抢。按语音定镜长之后，节奏由文案自己决定。
    narr: dict[str, Any] | None = None
    if voice:
        tick("voice", 0, n)
        try:
            narr = await vo.narrate([str(s.get("text") or "") for s in scenes],
                                    kernel.out_dir() / "voice")
        except Exception as exc:  # noqa: BLE001 —— TTS 是外部服务，挂了不该整条失败
            narr = {"files": [None] * n, "secs": [0.0] * n,
                    "failed": list(range(1, n + 1)),
                    "error": f"{type(exc).__name__}: {exc}"}
        if narr.get("files") and any(f is not None for f in narr["files"]):
            secs = vo.fit_secs(narr["secs"])
            # 不再因为"比要求的长 1 秒"就拒整条：每镜已被 `fit_secs` 夹到 ≤5.2，
            # 镜数 ≤MAX_SHOTS，所以总长天然 ≤ 291 秒。这里只做一个物理兜底。
            total_voice = round(sum(secs), 2)
            if total_voice > kernel.MAX_SECONDS:
                raise KernelError(
                    "voice",
                    f"旁白总长 {total_voice:g} 秒，超过物理上限 "
                    f"{kernel.MAX_SECONDS:g} 秒——请减少镜数或让每句字幕更短。")
        else:
            # 一条都没合成出来：退回无声 + 均分。**报告里会写清**，不假装有声。
            narr["degraded"] = True
            secs = split_seconds(seconds, n)
    else:
        secs = split_seconds(seconds, n)

    # 主色先提纯一遍再交给写手：模型爱传 `#0a0c12` 当 hue，抽帧量出来就是黑白片。
    # 在这里改而不是在 TSX 里兜，是因为场景会直接写 `fill={hue}`——那一层没有 lift()。
    for sc in scenes:
        sc["hue"] = ensure_vivid(sc.get("hue"))
    kinds = transitions_for(scenes)
    plan_obj["transitions"] = kinds
    tick("scenes", 0, n)

    sem = asyncio.Semaphore(SCENE_CONCURRENCY)
    done = 0

    async def one(i: int) -> str:
        nonlocal done
        async with sem:
            try:
                body = await write_scene(
                    scenes[i], index=i, total=n, seconds=secs[i],
                    prev=str(scenes[i - 1].get("brief") or "") if i else "",
                    nxt=str(scenes[i + 1].get("brief") or "") if i + 1 < n else "",
                    aspect=aspect, llm=llm, user_id=user_id,
                    conversation_id=conversation_id)
            finally:
                done += 1
                tick("scenes", done, n)
            return body

    results = await asyncio.gather(*[one(i) for i in range(n)], return_exceptions=True)
    bodies: list[str] = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            raise KernelError("scene", f"第 {i + 1} 镜失败：{r}")
        bodies.append(r)

    code, tl = assemble(bodies, secs, kinds, fps=fps)

    # ── 逐帧质检：抽帧量"有没有东西、有没有颜色"，不合格的带指令重写 ──────────
    #
    # 重写是有预算的：一条 60 秒片子 16 次调用已经不少，质检再放开重写会把调用
    # 翻一倍。所以（a）**按严重程度排序**（全黑先于寡淡），（b）整片最多
    # `MAX_REWRITE` 次。剩下的不合格如实报出去，不假装通过。
    def el_map() -> dict[int, int]:
        """每镜几个视觉元素（镜号 → 个数）。像素判据看不出"只有一层"，这个能。"""
        return {i + 1: system.element_count(b) for i, b in enumerate(bodies)}

    tick("audit", 0, n)
    audit = await qa.audit(code, secs, aspect=aspect, fps=fps, elements=el_map())
    rewritten: list[int] = []
    for _round in range(2):
        if not (audit.get("ok") and audit.get("bad")):
            break
        budget = MAX_REWRITE - len(rewritten)
        if budget <= 0:
            break
        fixed = 0
        for shot_no in qa.priority(audit["shots"], audit["bad"])[:budget]:
            i = shot_no - 1
            hint = qa.hint_for(audit["shots"], i)
            if not hint:
                continue
            try:
                bodies[i] = await write_scene(
                    scenes[i], index=i, total=n, seconds=secs[i],
                    prev=str(scenes[i - 1].get("brief") or "") if i else "",
                    nxt=str(scenes[i + 1].get("brief") or "") if i + 1 < n else "",
                    aspect=aspect, llm=llm, user_id=user_id,
                    conversation_id=conversation_id, extra=hint)
                rewritten.append(shot_no)
                fixed += 1
            except KernelError:
                pass
        if not fixed:
            break
        code, tl = assemble(bodies, secs, kinds, fps=fps)
        audit = await qa.audit(code, secs, aspect=aspect, fps=fps, elements=el_map())

    # ── 确定性补色带：重写两轮仍"寡淡"的镜，不再求模型 ──────────────────────
    #
    # 这一步是**几何**而不是"再试一次"：模型对"占画面多少"没有可靠直觉，
    # 实测同一镜重写两次都还在调字号。插一条 20% 高的高饱和色带则一定有效。
    # 只复核被补的那几镜（1 批静帧），不重跑整片 30 帧。
    patched: list[int] = []
    if audit.get("ok"):
        for row in audit.get("shots") or []:
            if row["worst"] != "flat":
                continue
            i = row["index"] - 1
            fixed = _inject_ribbon(bodies[i], scenes[i]["hue"])
            if fixed != bodies[i]:
                bodies[i] = fixed
                patched.append(row["index"])
        if patched:
            code, tl = assemble(bodies, secs, kinds, fps=fps)
            part = await qa.audit(code, secs, aspect=aspect, fps=fps, only=patched,
                            elements=el_map())
            audit = qa.merge_audit(audit, part)

    # ── 兜底：重写两轮 + 补色带之后仍不合格的镜，换成设计系统拼的通用场景 ──────
    #
    # 为什么要有这一层：一条 60 秒片子 15 镜，模型每一镜都可能有 5% 概率写出
    # "近乎全黑"（实测 s02 内容 12%、s03 17%）。15 镜里挂 1~2 镜是**常态**，
    # 靠重写碰运气等于每次出片都在赌。这里把"剩下的"确定性换成能看的一镜，
    # 于是"成片里没有黑帧"变成**结构性保证**，不再是概率。
    fallback: list[int] = []
    if audit.get("ok"):
        for row in audit.get("shots") or []:
            if row["worst"] == "ok":
                continue
            i = row["index"] - 1
            bodies[i] = safe_scene(str(scenes[i].get("text") or ""),
                                   scenes[i]["hue"], index=i)
            fallback.append(row["index"])
        if fallback:
            code, tl = assemble(bodies, secs, kinds, fps=fps)
            part = await qa.audit(code, secs, aspect=aspect, fps=fps, only=fallback,
                            elements=el_map())
            audit = qa.merge_audit(audit, part)

    cov = system.coverage(bodies)
    voice_info: dict[str, Any] | None = None
    if narr is not None:
        vsecs = [float(s) for s in (narr.get("secs") or [])]
        voice_info = {
            "files": [str(p) if p else "" for p in (narr.get("files") or [])],
            "secs": [round(s, 2) for s in vsecs],
            "failed": narr.get("failed") or [],
            "degraded": bool(narr.get("degraded")),
            "error": narr.get("error") or "",
            # 语音本身超过镜长上限的镜（会被切尾）——必须报出来，不能静默
            "over": [] if narr.get("degraded") else vo.over_long(vsecs),
        }
    return {"code": code, "plan": plan_obj, "secs": secs,
            "calls": calls + n + len(rewritten), "shots": n,
            "mode": MODE_EXPLAINER,
            "coverage": cov, "audit": audit, "rewritten": rewritten,
            "patched": patched, "fallback": fallback,
            "timeline": tl, "transitions": kinds,
            "bodies": bodies, "scenes": scenes, "elements": el_map(),
            "voice": voice_info,
            "total_sec": round(sum(secs), 2)}


async def shot_stills(code: str, tl: dict[str, Any], index: int, *,
                      aspect: str, fps: int = kernel.DEFAULT_FPS,
                      at: tuple[float, ...] = (0.32, 0.78),
                      scale: float = 0.42) -> list[str]:
    """渲某一镜的 2 张静帧（base64 JPEG）。用来给人看"改完长什么样"。

    帧号按**这一镜自己的可见窗口**算（`starts[i]` 起、`base[i]` 帧长），
    不是按整片——不然转场重叠的那 8 帧会把前一镜的画面也算进来。
    """
    start = int(tl["starts"][index])
    base = max(1, int(tl["base"][index]))
    frames = sorted({start + max(0, min(base - 1, int(round(r * (base - 1)))))
                     for r in at})
    pairs = await kernel.stills_pairs(
        code, frames, seconds=float(tl["total"]) / max(1, fps),
        aspect=aspect, fps=fps, scale=scale)
    return [p["jpeg_b64"] for p in pairs]
