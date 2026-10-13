# -*- coding: utf-8 -*-
"""T54：`scale_safe` 的**实测**——「这件素材缩到片子里的最小尺寸时，最细笔画还有几像素」。

**为什么要量而不是让人填。** 部件档（icon/part）在画面里的最小占地是短边的 0.18
（``code_spec.SUBJECT_MIN_BBOX``：主体铺开不少于这一档，否则 ⑲-a 就判「太小了」）。
在 1080×1920 的画布上这一档是 194 px。一件 24×24 viewBox 里画着 1.5 单位细线的图标，
放到 194 px 时那根线是 ``1.5 × 194/24 ≈ 12 px``——没问题；但同一集合里 512 viewBox、
线宽 2 的图标，落到 194 px 只剩 ``0.75 px``，画出来是一道灰雾。**「看起来挺清楚」是在
图标自己的尺寸下看的**，缩到片子里的尺寸才是它要活的尺寸，而这两者差一个比例因子。

**为什么不用人声明的值。** 一个手填的 `scale_safe: true` 与真实笔画没有任何机制联系：
它会在上一格通过、下一格（真正缩小以后）失效，而失效的样子是「部件档糊了」——
一条闸拦不住它要拦的东西，等于没有。所以这一栏只有两种取值来源：**量出来的真值**，
或者 **量不出来（未判）**；**没有任何入口接受人工写值**（Q114 的同一句话：
不把闸的成本转移成人工标注作业）。

**量法（几何，不开栅格化器）。** SVG 的「最细笔画」在几何上是可读的：
① 显式 `stroke-width`；② 用了 `stroke` 却没写宽度 → SVG 默认 1 用户单位；
③ `<rect>`/`<line>` 的短边（一条 0.5 单位的横杠就是 0.5）。
取这些量的**最小值**，乘上「目标像素 / viewBox 长边」这个比例因子，得最小笔画像素数。
判据：**≥ 2 px**（``MIN_STROKE_PX``）。这个 2 不是拍的：片子最终可能被平台二次压缩，
一像素的线在任何重采样下都会变成半透明的灰边，两像素是「压一刀之后还能看见」的下限。

**量不出来的那一半如实记未判。** 纯填充路径（`<path fill>` 无描边）的「最细处」是
路径形状的宽度，几何上要解曲线的局部宽度——本卷不做。这种件返回 `None`（未判），
而不是返回 `True`（D-未判不绿：没量到 ≠ 合格）。栅格图（PNG/JPEG）同理：没有重采样器
就量不出「缩小后还剩几像素」，记未判。**未判的件不会被拦**（它只是没被这一格证明），
但会在巡检回执里单列一个数，`asset_role='part'` 的件里未判占比就是要补的那张清单。
"""

from __future__ import annotations

import re
from typing import Any

#: 部件档在画面**短边**上的最小占地（与 `code_spec.SUBJECT_MIN_BBOX` 同值）。
#: 两处同值是刻意的：这一格量的是「⑲-a 允许的最小那件」，把量法锚在那个数上，
#: 才谈得上「缩到片子里最小尺寸时」——锚在别处量出来的数就没法回答这个问题。
PART_BAND_SHARE = 0.18

#: 画布短边的取值：portrait 1080×1920、square 1080×1080、landscape 1920×1080。
#: 三种画幅短边都是 1080，所以这里一个常量就够，不必把画幅传进来。
CANVAS_SHORT_SIDE = 1080

#: 最小笔画像素数（判据）。**≥ 2 px**：片子可能被平台二次压缩，1 px 的线压一刀
#: 就成灰雾，2 px 是「压一刀之后还看得见」的下限。
MIN_STROKE_PX = 2.0

_VIEWBOX_RE = re.compile(r'viewBox\s*=\s*["\']([^"\']+)["\']', re.I)
_W_RE = re.compile(r'width\s*=\s*["\']\s*([0-9.]+)', re.I)
_H_RE = re.compile(r'height\s*=\s*["\']\s*([0-9.]+)', re.I)
_STROKE_W_ATTR_RE = re.compile(r'stroke-width\s*=\s*["\']\s*([0-9.]+)', re.I)
_STROKE_W_CSS_RE = re.compile(r'stroke-width\s*:\s*([0-9.]+)', re.I)
_RECT_RE = re.compile(r'<(rect|line)\b([^>]*)>', re.I)
_ATTR_RE = re.compile(r'([A-Za-z-]+)\s*=\s*["\']([^"\']*)["\']')
#: 认得出来的颜色写法（与 code_spec 的取色口径同一类：十六进制为主，`none` 特判）。
_HEX_RE = re.compile(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b')


def _num(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def svg_viewbox_long_side(svg: str) -> float:
    """viewBox 的长边（用户单位）。没有 viewBox 就用 width/height，都没有 → 0（量不了）。"""
    m = _VIEWBOX_RE.search(svg)
    if m:
        parts = [p for p in re.split(r"[\s,]+", m.group(1).strip()) if p]
        if len(parts) == 4:
            w, h = abs(_num(parts[2])), abs(_num(parts[3]))
            if w > 0 and h > 0:
                return max(w, h)
    wm, hm = _W_RE.search(svg), _H_RE.search(svg)
    if wm and hm:
        w, h = _num(wm.group(1)), _num(hm.group(1))
        if w > 0 and h > 0:
            return max(w, h)
    return 0.0


def _features(svg: str) -> tuple[list[tuple[float, str]], bool]:
    """→ ([(用户单位厚度, 出处)], 有没有出现没写宽度的 stroke)。

    「有没有出现没写宽度的 stroke」单独回一位，是因为它对应 SVG 的**默认**笔宽 1——
    这个 1 是规范给的值，不是我们猜的，所以它照样算实测；但要把出处写清楚，
    回执上才看得见「这一条是默认值撑着的」。
    """
    out: list[tuple[float, str]] = []
    for m in _STROKE_W_ATTR_RE.finditer(svg):
        out.append((_num(m.group(1)), "stroke-width 属性"))
    for m in _STROKE_W_CSS_RE.finditer(svg):
        out.append((_num(m.group(1)), "stroke-width 样式"))
    for tag, attrs in _RECT_RE.findall(svg):
        a = {k.lower(): v for k, v in _ATTR_RE.findall(attrs)}
        filled = bool(a.get("fill")) and a.get("fill") != "none"
        if tag.lower() == "line":
            # 线段的粗细只由描边给（长度不是「笔画」），不给就落到默认 1 那一档
            continue
        w, h = _num(a.get("width")), _num(a.get("height"))
        if w > 0 and h > 0:
            # 实心块也能量：它的「笔画厚度」就是它自己的短边（一条 22×22 的实心方块，
            # 缩到目标尺寸之后那条边还是 22 单位的长度）。这一档必须认——不认的话
            # 「实心剪影」这类件会落进「没有可量的笔画」＝未判，而它是**过**的
            # （T54 的红样里就点了这一个：「造一个实心剪影 → true」）。
            why = f"实心 <rect> 的短边（{w}×{h}）" if filled and "stroke" not in a \
                else f"<rect> 的短边（{w}×{h}）"
            out.append((min(w, h), why))
    # 用了 stroke 却没写宽度的元素：SVG 默认 stroke-width=1
    bare_stroke = bool(re.search(r'stroke\s*=\s*["\'](?!none)[^"\']+["\']', svg, re.I)) \
        and not _STROKE_W_ATTR_RE.search(svg) and not _STROKE_W_CSS_RE.search(svg)
    if bare_stroke:
        out.append((1.0, "SVG 默认描边宽度 1（写了 stroke 没写 stroke-width）"))
    return out, bare_stroke


def ink_colors(svg: str) -> list[str]:
    """SVG 里出现过的**墨色**（fill/stroke 的非 none 十六进制值，去重保序）。

    这一栏是给「部件压在底上读不读得出」那一半用的：判据本身在片子那侧
    （``code_spec.contrast_ratio`` / ``CONTRAST_MIN``），这里只负责把墨色读出来——
    两处各写一遍对比度公式，就会出现「素材侧说 4.6、镜头侧说 2.8」这种两份真相。
    """
    out: list[str] = []
    for m in _ATTR_RE.finditer(svg):
        key = m.group(1).lower()
        if key not in ("fill", "stroke"):
            continue
        val = m.group(2).strip()
        if val.lower() == "none":
            continue
        for hexv in _HEX_RE.findall(val):
            low = hexv.lower()
            if low not in out:
                out.append(low)
    return out


def measure_svg(svg: str, *, target_px: int | None = None) -> dict[str, Any]:
    """SVG → `scale_safe` 实测回执（**不写库**，跟着这一行的 `scale_facts` 走）。

    → ``{"target_px":…, "viewbox_long_side":…, "min_feature_units":…,
    "min_stroke_px":…, "verdict": True/False/None, "reason":…, "raws":[…]}``
    `verdict=None` 是**未判**（量不出来），不是过也不是不过。
    """
    target = int(target_px or round(PART_BAND_SHARE * CANVAS_SHORT_SIDE))
    long_side = svg_viewbox_long_side(svg)
    feats, bare = _features(svg)
    raws = [{"units": round(u, 3), "why": why} for u, why in feats]
    facts: dict[str, Any] = {
        "target_px": target, "viewbox_long_side": long_side,
        "min_feature_units": None, "min_stroke_px": None,
        "verdict": None, "raws": raws, "ink": ink_colors(svg),
    }
    if long_side <= 0:
        facts["reason"] = "SVG 里没有 viewBox 也没有 width/height：缩放因子算不出来，量不了"
        return facts
    if not feats:
        facts["reason"] = ("SVG 里没有可量的笔画（没有 stroke-width、没有 <rect>/<line> 短边）"
                           "——纯填充路径的『最细处』要解曲线局部宽度，本卷不做，记未判")
        return facts
    min_u = min(u for u, _ in feats)
    if min_u <= 0:
        facts["reason"] = f"读到的最细笔画是 {min_u}（0 或负）——声明本身是坏的，量不了"
        return facts
    px = min_u * (target / long_side)
    facts["min_feature_units"] = round(min_u, 4)
    facts["min_stroke_px"] = round(px, 3)
    # 同一个数的第二个键名：验收口径（T54）点名要 `measured_min_stroke_px` 这一栏，
    # 而 `min_stroke_px` 是它在本模块里的短名。**两个名字指同一个数**，不重算一次——
    # 分开算就会出现「实测 1.9 而 measured 写 2.1」这种两份真相（同 `scale_verdict` 那条）。
    facts["measured_min_stroke_px"] = facts["min_stroke_px"]
    facts["verdict"] = bool(px >= MIN_STROKE_PX)
    facts["reason"] = (
        f"最细笔画 {min_u:g} 用户单位 × {target}/{long_side:g} = {px:.2f} px"
        f"（判据 ≥ {MIN_STROKE_PX:g} px，画布短边 {CANVAS_SHORT_SIDE} × "
        f"部件档 {PART_BAND_SHARE:g}）" + ("；其中一部分来自 SVG 默认笔宽 1" if bare else ""))
    return facts


def measure_asset(data: bytes | None, *, mime: str = "", ext: str = "",
                  target_px: int | None = None) -> dict[str, Any]:
    """一件素材的 `scale_safe` 实测回执。**认不出形态就记未判**，绝不返回 True。

    判据只对矢量（SVG）成立：栅格图的「最细笔画」要在缩小之后量，那需要重采样器，
    本卷没有——把栅格件记成 `True` 就是「没量过却说合格」，正是这一格要堵的形状。
    """
    is_svg = (mime or "").strip().lower() == "image/svg+xml" \
        or (ext or "").strip().lower().lstrip(".") == "svg"
    if data is None:
        return {"target_px": int(target_px or round(PART_BAND_SHARE * CANVAS_SHORT_SIDE)),
                "verdict": None, "reason": "拿不到字节（活库里只有行、对象没取到），量不了"}
    if not is_svg:
        return {"target_px": int(target_px or round(PART_BAND_SHARE * CANVAS_SHORT_SIDE)),
                "verdict": None,
                "reason": f"不是矢量件（mime={mime or '-'} ext={ext or '-'}）：栅格图的"
                          "最细笔画要在缩小之后量，本卷没有重采样器，记未判"}
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        return {"verdict": None, "reason": "SVG 字节解不出文本，量不了"}
    return measure_svg(text, target_px=target_px)


def scale_verdict(row: dict[str, Any]) -> str:
    """一行库行的这一格是**过 / 不过 / 未判**（三态，不把未判并进任何一边）。

    落库的 `scale_safe` 是布尔可空列：`True` 过、`False` 不过、`NULL` 未判。
    `scale_facts` 里带着量法原话，回执与巡检都读那一份，不重算。
    """
    v = row.get("scale_safe", None)
    facts = row.get("scale_facts") or {}
    if isinstance(facts, dict) and facts.get("verdict") is not None and v is None:
        v = facts.get("verdict")
    if v is None:
        return "undetermined"
    return "safe" if v else "unsafe"


def verdict_of(facts: dict[str, Any] | None) -> bool | None:
    """`scale_facts` → 落库那一格布尔值的**唯一投影**（T54）。

    写库的两条路（`AssetLibraryRepo.add` / `set_facts`）都调它，而且**都不收**
    `scale_safe` 入参——于是「人工写一个 true」在代码里没有入口，这一格只可能是
    实测函数（:func:`measure_asset`）的输出。三态：`True` 过 / `False` 不过 /
    `None` 未判（拿不到字节、栅格件、纯填充路径）。
    """
    if not isinstance(facts, dict):
        return None
    v = facts.get("verdict")
    return None if v is None else bool(v)


def check_asset_readable(facts: dict[str, Any], *, background: str | None = None
                         ) -> tuple[bool | None, str]:
    """部件压在底上**读不读得出**的那一半的**输入准备**：返回 (有没有墨色, 说明)。

    真正的比值由片子那侧算（``code_spec.contrast_ratio`` + ``CONTRAST_MIN``）——
    这一格与那一格共用同一条公式，本模块**不重写**它。`background` 为空时只回
    「墨色有没有读到」，比值那一半留未判（底是什么要等它落进某一镜才知道）。
    """
    inks = [str(x) for x in (facts.get("ink") or [])]
    if not inks:
        return None, "这一件没有可读的墨色（fill/stroke 全是 none 或没写）——对比度那一半未判"
    if not background:
        return None, (f"读到墨色 {'、'.join(inks)}，但这一件还没有落进任何一镜，"
                      "底是什么未知——对比度那一半由镜头侧判（闸⑩ 的同一套公式）")
    return True, f"墨色 {'、'.join(inks)} 与底 {background} 的比值由镜头侧判（闸⑩ 同一公式）"


def scale_facts_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一批库行的 `scale_safe` 账：过几件 / 不过几件 / 未判几件。

    未判**单列**（不并进任何一边）：它是「今天没量到」，既不是「合格」也不是「不合格」，
    并进哪一边都会把一个待办说成一件已完成的事。
    """
    safe = unsafe = undet = 0
    unsafe_sample: list[str] = []
    for r in rows:
        v = scale_verdict(r)
        if v == "safe":
            safe += 1
        elif v == "unsafe":
            unsafe += 1
            if len(unsafe_sample) < 10:
                unsafe_sample.append(str(r.get("src_id") or r.get("id") or ""))
        else:
            undet += 1
    return {"safe": safe, "unsafe": unsafe, "undetermined": undet,
            "total": len(rows), "unsafe_sample": unsafe_sample,
            "min_stroke_px": MIN_STROKE_PX, "target_px": round(PART_BAND_SHARE * CANVAS_SHORT_SIDE)}
