# -*- coding: utf-8 -*-
"""逐帧质检：**渲完抽帧，量"这一镜到底有没有东西、有没有颜色"**。

为什么必须有这一环：组件覆盖率只证明"代码里用了组件"，不证明"画面上有东西"。
实测踩过——第 15 镜覆盖率 100%、lint 全绿、动效 ok，抽帧出来是一片黑。

三条判据，**全部是量出来的**（阈值见各常量上的注释）：

1. **内容占比** `content`：与画面主色（亮度众数）差 >20 的像素占多少。低于 0.22 判空镜。
2. **平均亮度** `mean_lum`：低于 10/255 判"太黑"，低于 3 判"全黑"。
3. **饱和色块占比** `vivid`：**亮且有色**的像素（`max-min ≥ 70` 且 `max ≥ 110`）。
   低于 0.11 判"寡淡"——它抓的是"深蓝渐变 + 一行白字"这种帧：content 有 0.15~0.21，
   看着就是一页排版。这一维 2026-10-25 加，标定见 `tools/measure_probe.py`。

为什么 `vivid` 不用 HSV 的 `S=(max-min)/max`：深蓝底 `#0a0c12` 的 S=0.44，
按那个口径整片都"很鲜艳"（实测 8 帧全部 130~160，零区分力）。要用**绝对色差**
再加一个亮度下限，才分得开"有色块"和"暗底自带的那点蓝"。

一次调用渲完所有帧（`render_stills` 收一个帧数组），成本约整片的 1/100。
"""

from __future__ import annotations

import base64
import io
from typing import Any

from . import kernel
from .kernel import KernelError

#: 与主色亮度差多少才算"画面内容"。20 是实测值：暗场渐变的噪声在 10 以内。
CONTENT_TOL = 20
#: 内容占比低于这个数判空镜。
#:
#: **0.22 是量出来的，不是拍的**：2026-10-25 拿成片抽帧量了三类——
#: 有主体的帧 0.40~0.51、只有渐变+一行字的帧 0.13~0.21、纯黑 0.00~0.02。
#: 原来定 0.12 时，那几帧"渐变+一行字"全部混过去了（看着就是一页排版）。
EMPTY_RATIO = 0.22
#: 平均亮度低于这个数判"太黑"。
DARK_LUM = 10.0
#: "亮且有色"的像素口径：色差 ≥70（看得出是什么颜色）且最亮通道 ≥110（够亮才看得见）。
VIVID_CHROMA, VIVID_LUM = 70.0, 110.0
#: 饱和色块占比低于这个数判"寡淡"。
#:
#: **0.11 也是量出来的**：同一批 8 帧，寡淡的 0.000/0.019/0.068/0.069，
#: 有饱和色块的 0.155/0.226/0.413/0.424——空档在 0.07~0.15 之间，取中间。
#: 注意它比"目标 25%"低：25% 是**追求值**，0.11 是**打回线**，
#: 把打回线定在目标值上，会让每一条片子都重写一半镜头。
FLAT_VIVID = 0.11
#: 每镜抽几帧（占这一镜的比例）。
#:
#: 2026-10-25 从两点（0.35/0.75）加到三点：原来镜内**前 8% 是盲区**，
#: 实测定版片子的第 1330 帧（硬切后 6 帧）content 0.18 / vivid 0.04，
#: 质检完全看不见——那一瞬间画面几乎空着。加 0.15 那一点把它盖住。
#: 判定规则不变（**一帧合格就算这一镜合格**），所以不会因为"进场动画还没画完"
#: 误伤：三点里只要有一点画满了就放行。
SAMPLE_AT = (0.15, 0.5, 0.85)
#: `/render-stills` 一次最多渲这么多帧（服务端上限，实测 6 张只回 4 张）。
BATCH = 4

#: 每镜至少几个视觉元素（SVG 图元 + 设计系统组件实例，由 `system.element_count` 数）。
#:
#: 为什么要有这一条：像素判据只分得清"空 / 不空"与"有没有颜色"，
#: **分不清"一层"与"三层"**。一镜只有一个圆 + 一行字时，content 与 vivid 都可能合格，
#: 看着却还是像一页排版。元素数把"层次不够"变成可数的东西。
MIN_ELEMENTS = 8

#: 不合格的严重程度。数字小的先重写——预算有限时先救"全黑"，最后才救"层次少"。
STORY_DARK_LUM = 6.0      # 比 3 高一点：插画片的暗部也要看得见东西
STORY_EMPTY_RATIO = 0.10  # 一张插画铺满整帧，内容占比天然高；低于 10% 说明图没加载



def measure(jpeg_b64: str) -> dict[str, float]:
    """一张 JPEG → 四项读数。解码失败返回空表（**不抛**，也不当成通过）。"""
    try:
        import numpy as np
        from PIL import Image

        raw = base64.b64decode(jpeg_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        arr = np.asarray(img, dtype=np.float32)
        gray = np.asarray(img.convert("L"), dtype=np.float32)
    except Exception:  # noqa: BLE001 —— 量不了就说量不了，不当成"通过"
        return {}

    mx, mn = arr.max(axis=2), arr.min(axis=2)
    chroma = mx - mn
    vals, counts = np.unique(gray.astype(np.int16), return_counts=True)
    bg = int(vals[int(counts.argmax())])          # 亮度众数 = 背景
    edge = (float(np.abs(np.diff(gray, axis=1)).mean())
            + float(np.abs(np.diff(gray, axis=0)).mean())) / 2
    return {
        "content": round(float((np.abs(gray - bg) > CONTENT_TOL).mean()), 4),
        "vivid": round(float(((chroma >= VIVID_CHROMA) & (mx >= VIVID_LUM)).mean()), 4),
        "mean_lum": round(float(gray.mean()), 1),
        "p95_lum": round(float(np.percentile(gray, 95)), 1),
        "edge": round(edge, 2),
        "bg_lum": float(bg),
    }


def sample_frames(secs: list[float], *, fps: int) -> tuple[list[int], list[int]]:
    """每镜抽 2 帧，返回 ``(帧号数组, 每帧属于哪一镜)``。帧号是**整片绝对帧**。"""
    frames: list[int] = []
    owners: list[int] = []
    start = 0.0
    for i, sec in enumerate(secs):
        dur = max(1, kernel.frames_of(sec, fps))
        for r in SAMPLE_AT:
            frames.append(min(dur - 1, max(0, int(round(r * (dur - 1))))) + int(round(start * fps)))
            owners.append(i)
        start += sec
    return frames, owners


#: 「这帧根本没画东西」的线。**和 `EMPTY_RATIO` 是两件事**：
#: `EMPTY_RATIO=0.22` 是给几何动效科普片（大色块、高饱和）定的追求值；
#: 暗调剪影/细线片（近黑底 + 一处光晕）实测 0.03~0.20 就是正常画面。
#: 2026-10-26 真机：s03 content=0.061、s08=0.127 的画面都是合格的「观测仪器感」，
#: 却被 0.22 判不合格，23 镜里打回 21 镜，四轮重写全部白费。
BLANK_RATIO, BLANK_LUM = 0.02, 8.0


def is_blank(readings: dict[str, Any]) -> bool:
    """这一帧是不是**客观上空**：几乎没画东西 / 接近全黑 / 全白。

    这是唯一该由代码拦的东西。审美（有没有主体、留白克不克制、像不像片子）
    交给 `judge` 里那个看图说话的模型——它比任何比例阈值都可靠。
    2026-10-26 教训：拿 `content >= 0.22` 当闸，把 content=0.061 的
    「近黑底 + 白色剪影 + 光晕」合格画面判成不合格，23 镜打回 21 镜，四轮重写全白费。
    """
    if not readings:
        return False
    lum = float(readings.get("mean_lum") or 0.0)
    return (float(readings.get("content") or 0.0) < BLANK_RATIO
            or lum < BLANK_LUM or lum >= 250.0)


def readout_fix(r: dict[str, Any], *, warnings: list[str] | None = None) -> str:
    """把读数翻成**写手能执行的诊断**——给数字，不指定构图。

    2026-10-26：只说「几乎什么都没画，怎么画由你定」，模型重写三次还是全黑。
    它看不见自己的输出，需要的不是鼓励，是**一个无法反驳的数字**：
    整帧 95% 的像素亮度都在多少以下、背景是多少。加上对比度的物理事实
    （差不到 20/255 就等于看不见），它就知道该改明度而不是改构图。

    `warnings` 是渲染时浏览器控制台抓到的 ``console.error`` / ``console.warn``
    （由 `server.mjs` 的 `onBrowserLog` 带回，含 file:line）。如果这一镜渲出来是黑屏、
    却又带报错，那「几乎没画东西」根本不是构图问题，是**组件运行时挂了**——
    这条信息能让模型直接去修那一行，而不是又重写一遍整镜。
    """
    content = float(r.get("content") or 0.0)
    lum = float(r.get("mean_lum") or 0.0)
    p95 = float(r.get("p95_lum") or 0.0)
    bg = float(r.get("bg_lum") or 0.0)
    bad: list[str] = []
    if content < BLANK_RATIO:
        bad.append(f"与背景亮度差 >20 的像素只占 {content:.1%}")
    if lum < BLANK_LUM:
        bad.append(f"整帧平均亮度 {lum:.0f}/255")
    if p95 < 90 and (bad or content < 0.05):
        bad.append(f"**整帧最亮的 5% 像素也只有 {p95:.0f}/255**（背景 {bg:.0f}）"
                   f"——差不到 20 就等于看不见，屏幕上一片黑")
    if not bad:
        return ""
    msg = ("读数：" + "；".join(bad) + "。"
           "改法你定，但必须先让「看得见」成立：主体用实心亮色"
           "（#E8F0F5 或 #4FD1E0，明度 200+/255），背景保持 #0a0c14；"
           "描边细线也要 2px 以上、opacity 0.6 以上。")
    if warnings:
        msg += ("\n\n但更可能是**组件运行时出错**——下面这行报错才是根因，"
                "照着 file:line 去修那一处，别重写整镜：\n"
                + "\n".join(f"- {w}" for w in warnings[:8]))
    return msg


def verdict(m: dict[str, float], *, story: bool = False) -> str:
    """一帧的判定：ok / empty / dark / black / flat / unknown。

    `story=True` 换一套尺：只判"图加载出来没有 / 是不是全黑"，
    **不判饱和度、不判层次**——暗调低饱和正是叙事片要的样子。
    """
    if not m:
        return "unknown"
    if story:
        if m["mean_lum"] < STORY_DARK_LUM:
            return "black"
        if m["content"] < STORY_EMPTY_RATIO:
            return "empty"
        return "ok"
    if m["mean_lum"] < 3:
        return "black"
    if m["content"] < EMPTY_RATIO:
        return "empty"
    if m["mean_lum"] < DARK_LUM:
        return "dark"
    if m.get("vivid", 1.0) < FLAT_VIVID:
        return "flat"
    return "ok"


