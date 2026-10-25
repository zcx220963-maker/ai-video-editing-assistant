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
SEVERITY = {"unknown": 0, "black": 1, "empty": 2, "dark": 3, "flat": 4, "sparse": 5}

#: ---- 叙事片（`mode="story"`）的另一套阈值 ----
#:
#: 两套阈值的差别不是"松紧"，是**方向**：几何动效要"亮 + 高饱和 + 层次多"，
#: 插画叙事要"暗 + 低饱和 + 一张完整的图"。用前者的尺去量后者，
#: 会把参考片那种 YAVG 41.6 / SATAVG 6.3 的画面**第一帧就判成寡淡打回**。
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


async def audit(code: str, secs: list[float], *, aspect: str,
                fps: int = kernel.DEFAULT_FPS, base: str | None = None,
                scale: float = 0.28,
                only: list[int] | None = None,
                elements: dict[int, int] | None = None,
                story: bool = False) -> dict[str, Any]:
    """整片抽帧质检。返回 ``{shots:[{index, verdicts, worst, content}], bad:[…]}``。

    `bad` 是"两帧都不合格"的镜号（1 起）——一帧合格就算它及格，
    因为进场/收束时画面本来就可能短暂偏空。

    `only` 只判指定镜号（1 起）。用在"确定性补色带之后只复核被补的那几镜"——
    重跑整片 30 帧要两三分钟，而补过的那一两镜只需要渲 2~4 帧。

    `elements` 是"每镜几个视觉元素"（镜号 → 个数，由 `system.element_count` 数出来）。
    给了它，像素合格但元素不够的镜会判 ``sparse``——这是**代码层**的判据，
    与抽帧那两条（内容 / 饱和度）互补：那两条看不出"只有一层"。
    """
    frames, owners = sample_frames(secs, fps=fps)
    total = sum(secs)
    if only:
        keep = {n - 1 for n in only}
        pairs_ko = [(f, o) for f, o in zip(frames, owners) if o in keep]
        frames = [f for f, _ in pairs_ko]
        owners = [o for _, o in pairs_ko]

    # 服务端一次最多 4 帧，分批渲；按**返回的 frame 号**对号入座，
    # 不按数组次序猜（少一张时按次序对会把第 5 镜的图记到第 4 镜上）。
    got: dict[int, dict[str, float]] = {}
    errors: list[str] = []
    for i in range(0, len(frames), BATCH):
        chunk = frames[i:i + BATCH]
        try:
            pairs = await kernel.stills_pairs(
                code, chunk, seconds=total, aspect=aspect, fps=fps,
                scale=scale, base=base)
        except KernelError as exc:
            errors.append(str(exc))
            continue
        for p in pairs:
            got[p["frame"]] = measure(p["jpeg_b64"])

    if not got:
        return {"ok": False, "error": errors[0] if errors else "一帧都没渲出来",
                "shots": [], "bad": []}

    by_shot: dict[int, list[dict[str, float]]] = {}
    for frame, owner in zip(frames, owners):
        m = got.get(frame)
        if m is not None:
            by_shot.setdefault(owner, []).append(m)

    wanted = sorted({n - 1 for n in only}) if only else list(range(len(secs)))
    shots = []
    bad: list[int] = []
    for i in wanted:
        ms = by_shot.get(i, [])
        vs = [verdict(m, story=story) for m in ms]
        good = [v for v in vs if v == "ok"]
        worst = "ok" if good else (vs[0] if vs else "unknown")
        n_el = (elements or {}).get(i + 1)
        # 像素合格但元素不够 → sparse。放在"像素判据之后"是有意的：
        # 全黑 / 空镜比"层次少"更该先修，严重程度表里 sparse 排在最后。
        if (not story) and worst == "ok" and n_el is not None and n_el < MIN_ELEMENTS:
            worst = "sparse"
        if worst != "ok":
            bad.append(i + 1)
        shots.append({
            "index": i + 1,
            "verdicts": vs,
            "worst": worst,
            "elements": n_el,
            "content": round(max([m.get("content", 0) for m in ms] or [0]), 3),
            "vivid": round(max([m.get("vivid", 0) for m in ms] or [0]), 3),
            "edge": round(max([m.get("edge", 0) for m in ms] or [0]), 2),
            "mean_lum": round(max([m.get("mean_lum", 0) for m in ms] or [0]), 1),
        })

    return {
        "ok": True,
        "shots": shots,
        "bad": bad,
        "ratio": round((len(shots) - len(bad)) / max(1, len(shots)), 3),
        "frames": len(got),
        "errors": errors[:2],
    }


def merge_audit(full: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """把"只复核了某几镜"的结果并回整片读数里。

    不复核就报"已修好"是自欺；重跑整片又太贵。所以只换掉被复核的那几行，
    其余原样保留——报告里因此能同时看到"补过之后这一镜好了"和"别的镜没动过"。
    """
    if not (full.get("ok") and part.get("ok")):
        return full
    rows = {s["index"]: s for s in full.get("shots") or []}
    for s in part.get("shots") or []:
        rows[s["index"]] = s
    shots = [rows[k] for k in sorted(rows)]
    bad = [s["index"] for s in shots if s["worst"] != "ok"]
    return {**full, "shots": shots, "bad": bad,
            "ratio": round((len(shots) - len(bad)) / max(1, len(shots)), 3)}


def priority(shots: list[dict[str, Any]], bad: list[int]) -> list[int]:
    """把不合格的镜按严重程度排序（全黑先于寡淡）。预算有限时先救重的。"""
    rank = {s["index"]: SEVERITY.get(s["worst"], 9) for s in shots}
    return sorted(bad, key=lambda n: (rank.get(n, 9), n))


def hint_for(shots: list[dict[str, Any]], index: int) -> str:
    """给重写用的具体指令——不说"重写"，说"上一版错在哪"。"""
    row = next((s for s in shots if s["index"] == index + 1), None)
    if not row:
        return ""
    v = row["worst"]
    if v == "unknown":
        # 静帧都渲不出来 = 这一镜在**运行时**就炸了（数组 undefined / NaN 属性最常见）。
        # 这种镜必须简化，不能只是"加点东西"。
        return ("上一版这一镜**渲不出来**（运行时报错，常见原因是数组是 undefined、"
                "或某个属性算成了 NaN）。请大幅简化：只用 `Backdrop` + 一个内容组件"
                "（`StatCard` / `BarChart` / `OrbitDiagram` / `GlowOrb` 任选其一），"
                "所有数组都给字面量默认值，所有数字用 `Math.max(0, …)` 兜一层。")
    if v == "black":
        return ("上一版这一镜抽帧是**全黑**的。必须先用 `Backdrop` 铺满整帧，"
                "再放一件占画面 ≥30% 的实体；不要只有几个点或一行字。")
    if v in ("empty", "dark"):
        return (f"上一版这一镜**只有渐变背景和一行字**（内容占比 {row['content']:.0%}，"
                f"平均亮度 {row['mean_lum']:.0f}/255）。这是一页排版，不是画面。"
                "必须加一件**占画面 ≥30% 的实体**：具体的物件/人物/图表/几何体，"
                "有外轮廓、有受光面与背光面、有落地影；再加一个高饱和色块（≥20% 画面）。"
                "**`Backdrop` 的 hue 要传高饱和色（#22D3EE / #FF6B2C / #FFC93C），"
                "不要传 #0a0c12 这类近黑色**——那会让整帧发黑。")
    if v == "flat":
        return (f"上一版这一镜**没有颜色**（亮且有色 的像素只占 {row['vivid']:.0%}，"
                "目标是 ≥20%）。整帧是一层深蓝渐变加白字，看着像黑白片。"
                "必须加**至少两块高饱和色**：用 `Ribbon` 铺一条斜色带（它自带 20% 覆盖），"
                "或者把主体换成实心的高饱和色（#FF6B2C / #22D3EE / #FFC93C / #FF3D7F），"
                "再用 `HudFrame` 加前景四角标记，把画面撑满。"
                "**不要**只把字改大——那是排版，不是画面。")
    if v == "sparse":
        return (f"上一版这一镜只有 **{row.get('elements') or 0} 个视觉元素**（目标 ≥8），"
                "层次不够——像素上不空，但看着还是「一个主体 + 一行字」。"
                "必须补成三层：背景层（`Backdrop` + `ScanGrid` 或 `Ribbon` 或 `Particles`）、"
                "中景（**≥3 件实体**，最大那件占画面 30%~55%）、前景（`HudFrame`，"
                "或自己画角标/刻度/细线）。")
    return ""
