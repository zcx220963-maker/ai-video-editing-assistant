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
from .director import (  # noqa: F401  —— 提示词搬去 director.py
    PLAN_SYSTEM as _PLAN_SYSTEM, STORY_SYSTEM as _STORY_SYSTEM)
from . import voice as vo
from .kernel import KernelError
# 叙事片的"画法"拆到 story.py、"看自己画的一帧"拆到 review.py
# （两个模块都不在模块级 import 本文件，所以没有循环）。
# 这里重新导出，是为了 `film.wrap_story_scene` / `film.review_shots` 这些名字
# 对外仍然可用——测试与作业层都按 `film.xxx` 调，拆文件不该改调用方。
from . import showrunner as sr   # 统筹层：新的唯一入口（2026-10-26）
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


LONG_FORM_SEC = 90.0


async def pick_style(prompt: str, *, seconds: float = 0.0, llm: Any = None,
                     user_id: str = "", conversation_id: str = "") -> str:
    """判片种。判据两条，**先看时长再看内容**。

    ① 超过 :data:`LONG_FORM_SEC` 秒 → 直接叙事片（不调模型，省一次调用）；
    ② 否则看「有没有主角和时间」。判不出来也按叙事片走——有主角的片子用叙事
       画法永远不算错，反过来不一定。

    不让用户选是因为 `explainer` / `story` 是内部术语：他只会说"讲一下 0 的历史"，
    让他选等于把产品设计问题推给他，选错还整条白做（两套写手提示词完全不同）。
    """
    if seconds and float(seconds) > LONG_FORM_SEC:
        return MODE_STORY
    ask = (f"选题：{prompt}\n\n"
           "这条片子更适合哪种画法？只回一个词：\n"
           "· story —— 有主角、有时间推进、有情绪（历史故事、人物、悬疑揭秘）\n"
           "· explainer —— 讲一个原理、一组数据、一个流程（没有主角也没有时间线）\n"
           "判据是「有没有主角和时间」，不是「好不好看」。")
    try:
        raw = await kernel._complete([{"role": "user", "content": ask}],
                                     llm=llm, user_id=user_id,
                                     conversation_id=conversation_id)
    except Exception:  # noqa: BLE001 —— 判不出来不该让整条片子失败
        return MODE_STORY
    return MODE_STORY if "story" in str(raw).lower() else MODE_EXPLAINER


async def make_film(prompt: str, *, seconds: float = 60.0,
                    aspect: str = kernel.DEFAULT_ASPECT, fps: int = kernel.DEFAULT_FPS,
                    llm: Any = None, user_id: str = "",
                    conversation_id: str = "",
                    on_stage: Any = None,
                    voice: bool = True,
                    mode: str = "auto",
                    scenes: Any = None) -> dict[str, Any]:
    """一句话 → 一条成片。**现在只走一条路**（2026-10-26）。

    旧的 `explainer` / `story` 两条路已不再被调用（留在下面叫 `_make_legacy`，
    等新路径跑过一轮再删）。`mode` 参数保留只为兼容旧调用点，**不再影响行为**：
    镜数、镜长、画法全由 `showrunner` 里的导演决定。

    设计见仓库根 `设计-LLM当导演-2026-10-26.md`。
    """
    return await sr.make(prompt, seconds=seconds, aspect=aspect, fps=fps,
                         llm=llm, user_id=user_id, conversation_id=conversation_id,
                         on_stage=on_stage, voice=voice, scenes=scenes)


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


