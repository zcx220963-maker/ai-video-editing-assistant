# -*- coding: utf-8 -*-
"""内置图示卡：七个数据驱动的 SVG 版式组件。

为什么是 SVG 而不是 HTML 绝对定位：**一套 viewBox 通吃两种画幅**。竖屏的画面带是
1080×1920，横屏是 1760×658，`preserveAspectRatio="xMidYMid meet"` 让同一份图形坐标
在两条边里都自动居中缩放到刚好装下——不需要为横屏再画一版（档案系卡片没有这层便利，
它们在 ``templates.py`` 里靠横屏那套定位规则落进同一条画面带）。
代价是「装得下」不等于「铺得满」：横屏带是 2.67:1，1000×560 的图按带高 658 缩放，
实得 1175×658，两侧各留 292 纸。要把横屏带宽吃满，得自画宽幅 viewBox（``card=custom``），
内置这七种不为横屏重画一版。

三条与模板的约定：

1. 动效一律写 ``data-anim``，由 ``templates._JS`` 的 ``apply(t)`` 按时刻算；带动效的
   元素**自身不写 ``transform`` 属性**——CSS transform 会盖掉属性里的定位，需要位移
   就外面套一层 ``<g>``；
2. 时值在这里定死（``START``/``STEP_MS``），模型只填数据。这样 ``settle_ms`` 算出的
   「动画停止时刻」与画面真正同步，不必让模型承诺它看不见的毫秒；
3. 形状数量由数据条数决定，每条都有上限：超出直接截断并在 spec 校验里退回，
   免得一张图把每帧排版时间顶上去。
"""

from __future__ import annotations

import math
from typing import Any

INK = "#23211c"
VERMILION = "#a8352c"
BLUE = "#2f4f8f"
MUTED = "#6c6455"
PAPER = "#f7f2e5"
HAIR = "#b9ad93"

VB = "0 0 1000 560"
START_MS = 500          # 卡片入场（420ms）之后再开始画图
STEP_MS = 260           # 相邻数据项的入场间隔
DUR_MS = 560

MAX_ITEMS = 8
MAX_POINTS = 30
MAX_NOTE_CHARS = 26


def _esc(value: Any) -> str:
    from html import escape
    return escape(str(value if value is not None else ""), quote=True)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _field(item: Any, key: str, default: str = "") -> str:
    if isinstance(item, dict):
        for k, v in item.items():
            if str(k).strip().lower() == key:
                return str(v if v is not None else default)
        return default
    return str(item) if key == "label" else default


def _num(item: Any, key: str, default: float) -> float:
    if isinstance(item, dict):
        for k, v in item.items():
            if str(k).strip().lower() == key:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return default
    return default


def _clip(items: list[Any], cap: int) -> list[Any]:
    return items[:cap]


#: 最大/最小跨过这个倍数，「按最大值等比」就把小值画成了轴线上的一个点——换对数轴。
LOG_TRIGGER_RATIO = 100.0
#: 对数轴上最小的那条也留出看得见的长度（轴底已经退了一格，这里是兜 0 和显式 log）
LOG_MIN_FRAC = 0.05

#: chart 头顶留给最高那根读数的净空：读数基线在柱顶再上 14，单位行在 top_y-18。
BAR_HEAD_ROOM = 46.0

#: 大数在画面上的单位。1e12 原样排是 13 个等宽字符，levels 那列数字起笔 880，
#: 直接捅出 viewBox 右边缘；chart 头顶的读数会把相邻两根糊在一起。
_CN_UNITS = ((1e12, "万亿"), (1e8, "亿"), (1e4, "万"))


def _axis(v: dict[str, Any], values: list[float]) -> dict[str, Any]:
    """条长/线高的取尺：默认按最大值等比，跨量级时自动换 log10 轴。

    「1e12 和 1、8 同框」在线性轴上后两根只有 7e-13 的带宽——数据在，画面没传达。
    对数轴留住「谁比谁大一个量级」，读数仍由 ``count`` 按原值报出，所以不骗人；
    代价是条长不再等比，于是题注里必须写明（见 ``_axis_note``）。
    模型可用 ``scale: "linear"`` / ``"log"`` 覆盖这条判断。
    """
    requested = str(v.get("scale") or "").strip().lower()
    vmax = max((abs(x) for x in values), default=0.0) or 1.0
    positive = [abs(x) for x in values if abs(x) > 0]
    vmin = min(positive, default=0.0)
    use_log = requested == "log" or (
        requested != "linear" and bool(positive)
        and vmax / max(vmin, 1e-300) >= LOG_TRIGGER_RATIO)
    if not use_log:
        return {"log": False, "frac": lambda x: abs(x) / vmax}
    # 轴底退一格：最小那条正好落在 1/总格数 上，不会出现零长度条
    floor = vmin / 10.0 if vmin > 0 else 1.0
    lo = math.log10(floor)
    hi = math.log10(max(vmax, floor * 10.0))
    span = hi - lo

    def frac(x: float) -> float:
        a = abs(x)
        if a <= 0:
            return 0.0
        return min(1.0, max(LOG_MIN_FRAC, (math.log10(a) - lo) / span))

    return {"log": True, "frac": frac, "decades": span}


def _axis_note(axis: dict[str, Any]) -> str:
    return "对数刻度：每格 10 倍" if axis.get("log") else ""


def _num_label(num: float) -> tuple[float, int, str]:
    """把数值拆成（滚动目标值, 小数位, 中文单位）——只挪小数点，不改大小。"""
    a = abs(num)
    for base, unit in _CN_UNITS:
        if a >= base:
            scaled = num / base
            dp = 0 if abs(scaled) >= 100 else (1 if abs(scaled) >= 10 else 2)
            return scaled, dp, unit
    return num, (0 if float(num).is_integer() else 1), ""


def _count_attrs(num: float) -> str:
    scaled, dp, unit = _num_label(num)
    return (f'data-count-to="{scaled:g}" data-dp="{dp}"'
            + (f' data-num-unit="{_esc(unit)}"' if unit else ""))


def _svg(cls: str, body: str) -> str:
    return (f'<svg class="dg {cls}" xmlns="http://www.w3.org/2000/svg" viewBox="{VB}"'
            f' preserveAspectRatio="xMidYMid meet">{body}</svg>')


def _head(v: dict[str, Any]) -> str:
    """图题：一行中文 + 一行小注。"""
    title = _esc(v.get("title") or "")
    note = _esc(v.get("note") or "")
    out = ""
    if title:
        out += f'<text class="h" x="500" y="66" text-anchor="middle">{title}</text>'
    if note:
        out += f'<text class="s" x="500" y="100" text-anchor="middle">{note}</text>'
    return out


def _caption_row(v: dict[str, Any], y: float = 524, extra: str = "") -> str:
    cap = _esc(v.get("caption") or "")
    if extra:
        cap = f"{cap}　·　{extra}" if cap else _esc(extra)
    if not cap:
        return ""
    return f'<text class="s" x="500" y="{y:.0f}" text-anchor="middle">{cap}</text>'


# ---------------------------------------------------------------------------
# 1. flow：一步步推进的流程链
# ---------------------------------------------------------------------------

def flow(v: dict[str, Any]) -> str:
    steps = _clip(_as_list(v.get("steps") or v.get("items")), MAX_ITEMS)
    n = len(steps)
    if n < 2:
        return _svg("flow", '<text class="h" x="500" y="280" text-anchor="middle">'
                            '（flow 需要至少两步）</text>')
    gap = 44.0
    box_w = min(280.0, (1000.0 - 70.0 * 2 - gap * (n - 1)) / n)
    box_h = 128.0
    top = 216.0
    body = [_head(v)]
    for i, item in enumerate(steps):
        x = 70.0 + i * (box_w + gap)
        at = START_MS + i * STEP_MS
        label = _esc(_field(item, "label"))
        note = _clip_chars(_field(item, "note"))
        body.append(
            f'<g data-anim="rise" data-at="{at}" data-dur="{DUR_MS}">'
            f'<rect x="{x:.0f}" y="{top:.0f}" width="{box_w:.0f}" height="{box_h:.0f}" '
            f'rx="6" fill="{PAPER}" stroke="{INK}" stroke-width="3"/>'
            f'<text class="t" x="{x + box_w / 2:.0f}" y="{top + (58 if note else 76):.0f}" '
            f'text-anchor="middle">{label}</text>'
            + (f'<text class="s" x="{x + box_w / 2:.0f}" y="{top + 92:.0f}" '
               f'text-anchor="middle">{note}</text>' if note else "")
            + "</g>")
        if i:
            ax1, ax2 = x - gap + 4, x - 8
            mid = top + box_h / 2
            body.append(
                f'<g data-anim="fade" data-at="{at - STEP_MS / 2:.0f}" data-dur="320">'
                f'<line x1="{ax1:.0f}" y1="{mid:.0f}" x2="{ax2:.0f}" y2="{mid:.0f}" '
                f'stroke="{VERMILION}" stroke-width="4"/>'
                f'<polygon points="{ax2:.0f},{mid - 9:.0f} {ax2 + 12:.0f},{mid:.0f} '
                f'{ax2:.0f},{mid + 9:.0f}" fill="{VERMILION}"/></g>')
    body.append(_caption_row(v))
    return _svg("flow", "".join(body))


def _clip_chars(text: str) -> str:
    text = str(text or "")
    return _esc(text[:MAX_NOTE_CHARS])


# ---------------------------------------------------------------------------
# 2. compare：左右对照
# ---------------------------------------------------------------------------

def _compare_side(v: dict[str, Any], key: str) -> dict[str, Any]:
    side = v.get(key)
    if isinstance(side, dict):
        return side
    return {"title": "", "points": _as_list(side)}


#: 每边要点的封顶数：面板 150~510、行距 46，第 5 条（y=516）就画出边框了。
#: 契约里"每边 ≤8 条"是照抄 MAX_ITEMS 的口头承诺，画面装不下——按能装下的算。
COMPARE_MAX_POINTS = 4


def compare(v: dict[str, Any]) -> str:
    left, right = _compare_side(v, "left"), _compare_side(v, "right")
    vs = _esc(v.get("vs") or "VS")
    # 题注基线 524：面板底留在 480 才不骑在边框上（真机横屏帧量出来的碰撞）
    panel_h = 330.0 if str(v.get("caption") or "").strip() else 360.0
    cols = [(40.0, left, "slide-x", -70.0), (520.0, right, "slide-x", 70.0)]
    body = [_head(v)]
    for x, side, _kind, dist in cols:
        items = _clip(_as_list(side.get("points")), COMPARE_MAX_POINTS)
        at = START_MS + (0 if x < 500 else 180)
        body.append(
            f'<g data-anim="slide-x" data-at="{at}" data-dur="620" data-dist="{dist:.0f}">'
            f'<rect x="{x:.0f}" y="150" width="440" height="{panel_h:.0f}" rx="8" '
            f'fill="{PAPER}" stroke="{INK}" stroke-width="3"/>'
            f'<text class="h" x="{x + 220:.0f}" y="212" text-anchor="middle">'
            f'{_esc(side.get("title"))}</text>'
            f'<line x1="{x + 30:.0f}" y1="236" x2="{x + 410:.0f}" y2="236" '
            f'stroke="{HAIR}" stroke-width="2"/></g>')
        for i, point in enumerate(items):
            py = 286 + i * 46
            body.append(
                f'<g data-anim="fade" data-at="{at + 320 + i * 150}" data-dur="360">'
                f'<circle cx="{x + 46:.0f}" cy="{py - 9}" r="5" fill="{VERMILION}"/>'
                f'<text class="t" x="{x + 66:.0f}" y="{py}">{_clip_chars(point)}</text></g>')
    body.append(f'<g data-anim="pop" data-at="{START_MS + 300}" data-dur="420">'
                f'<circle cx="500" cy="330" r="46" fill="{INK}"/>'
                f'<text class="vs" x="500" y="344" text-anchor="middle">{vs}</text></g>')
    body.append(_caption_row(v))
    return _svg("compare", "".join(body))


# ---------------------------------------------------------------------------
# 3. timeline：横轴时间线
# ---------------------------------------------------------------------------

def timeline(v: dict[str, Any]) -> str:
    marks = _clip(_as_list(v.get("marks") or v.get("events")), MAX_ITEMS)
    body = [_head(v)]
    axis_y = 300.0
    if len(marks) < 2:
        return _svg("timeline", "".join(body) +
                    '<text class="h" x="500" y="280" text-anchor="middle">'
                    '（timeline 需要至少两个节点）</text>')
    x0, x1 = 70.0, 930.0
    step = (x1 - x0) / (len(marks) - 1)
    span = 900.0
    body.append(f'<g data-anim="wipe" data-at="{START_MS - 300}" data-dur="{span:.0f}">'
                f'<line x1="{x0:.0f}" y1="{axis_y:.0f}" x2="{x1:.0f}" y2="{axis_y:.0f}" '
                f'stroke="{INK}" stroke-width="4"/></g>')
    for i, item in enumerate(marks):
        x = x0 + i * step
        at = START_MS + i * STEP_MS * 1.4
        s = 1 if i % 2 == 0 else -1        # +1 标签垂在轴下，-1 挑在轴上
        stem = 36 * s                      # 短线：再长就会从文字中间穿过去
        label = _clip_chars(_field(item, "label"))
        year = _clip_chars(_field(item, "year")) or _clip_chars(_field(item, "time"))
        body.append(
            f'<g data-anim="pop" data-at="{at:.0f}" data-dur="360">'
            f'<circle cx="{x:.0f}" cy="{axis_y:.0f}" r="9" fill="{VERMILION}"/>'
            f'<line x1="{x:.0f}" y1="{axis_y:.0f}" x2="{x:.0f}" y2="{axis_y + stem:.0f}" '
            f'stroke="{HAIR}" stroke-width="2"/></g>'
            f'<g data-anim="rise" data-at="{at + 160:.0f}" data-dur="420">'
            f'<text class="yr" x="{x:.0f}" y="{axis_y + s * 82:.0f}" '
            f'text-anchor="middle">{year}</text>'
            f'<text class="s" x="{x:.0f}" y="{axis_y + s * 118:.0f}" '
            f'text-anchor="middle">{label}</text></g>')
    body.append(_caption_row(v, 500))
    return _svg("timeline", "".join(body))


# ---------------------------------------------------------------------------
# 4. levels：一层层台阶（能级 / 阶梯 / 分级）
# ---------------------------------------------------------------------------

def levels(v: dict[str, Any]) -> str:
    rows = _clip(_as_list(v.get("levels") or v.get("rows")), MAX_ITEMS)
    body = [_head(v)]
    if not rows:
        return _svg("levels", "".join(body) +
                    '<text class="h" x="500" y="280" text-anchor="middle">'
                    '（levels 需要至少一层）</text>')
    values = [_num(r, "value", 0.0) for r in rows]
    axis = _axis(v, values)
    top, bottom = 150.0, 470.0
    gap = (bottom - top) / max(1, len(rows) - 1) if len(rows) > 1 else 0.0
    for i, item in enumerate(rows):
        y = bottom - i * gap
        # 最长一条停在 780，读数右对齐在 990：竖屏帧量过「880 起笔 + 全宽中文单位」
        # 会把 1.00万亿 的尾巴挤出 viewBox
        width = 100.0 + 380.0 * axis["frac"](values[i])
        at = START_MS + i * STEP_MS
        name = _clip_chars(_field(item, "name") or _field(item, "label"))
        num = _num(item, "value", 0.0)
        body.append(
            f'<g data-anim="wipe" data-at="{at}" data-dur="700" data-dist="100">'
            f'<line x1="300" y1="{y:.0f}" x2="{300 + width:.0f}" y2="{y:.0f}" '
            f'stroke="{BLUE}" stroke-width="6"/></g>'
            f'<text class="t" x="280" y="{y + 10:.0f}" text-anchor="end" '
            f'data-anim="fade" data-at="{at}" data-dur="420">{name}</text>'
            f'<text class="num" x="990" y="{y + 12:.0f}" text-anchor="end" '
            f'data-anim="count" data-at="{at + 120:.0f}" data-dur="760" '
            f'{_count_attrs(num)}>0</text>')
    body.append(_caption_row(v, 528, _axis_note(axis)))
    return _svg("levels", "".join(body))


# ---------------------------------------------------------------------------
# 5. chart：柱状 / 折线
# ---------------------------------------------------------------------------

def chart(v: dict[str, Any]) -> str:
    series = _clip(_as_list(v.get("series") or v.get("bars") or v.get("data")), MAX_ITEMS)
    body = [_head(v)]
    if not series:
        return _svg("chart", "".join(body) +
                    '<text class="h" x="500" y="280" text-anchor="middle">'
                    '（chart 需要至少一个数据点）</text>')
    values = [_num(s, "value", 0.0) for s in series]
    axis = _axis(v, values)
    unit = _clip_chars(v.get("unit") or "")
    base, top_y = 452.0, 150.0
    # 读数住在柱子头顶（再减 14），单位小注住在 top_y-18：不留头顶，最高那根的读数
    # 会和单位行叠成一坨（真机横屏帧量到的「千克1.00万亿」）。
    span = base - top_y - BAR_HEAD_ROOM
    x0, x1 = 90.0, 950.0
    body.append(f'<g data-anim="wipe" data-at="{START_MS - 260}" data-dur="700">'
                f'<line x1="{x0:.0f}" y1="{base:.0f}" x2="{x1:.0f}" y2="{base:.0f}" '
                f'stroke="{INK}" stroke-width="3"/></g>')
    if str(v.get("mode") or "bar") == "line":
        step = (x1 - x0 - 60) / max(1, len(series) - 1)
        pts = [(x0 + 30 + i * step, base - span * axis["frac"](values[i]))
               for i in range(len(series))]
        path = "M" + " L".join(f"{px:.0f} {py:.0f}" for px, py in pts)
        body.append(f'<path d="{path}" fill="none" stroke="{BLUE}" stroke-width="5" '
                    f'data-anim="draw" data-at="{START_MS}" data-dur="1500"/>')
        for i, (px, py) in enumerate(pts):
            body.append(_point_dot(px, py, START_MS + 400 + i * STEP_MS))
    else:
        slot = (x1 - x0) / len(series)
        bar_w = min(110.0, slot * 0.62)
        for i, item in enumerate(series):
            cx = x0 + slot * (i + 0.5)
            h = span * axis["frac"](values[i])
            at = START_MS + i * 180
            body.append(
                f'<rect x="{cx - bar_w / 2:.0f}" y="{base - h:.0f}" width="{bar_w:.0f}" '
                f'height="{max(6.0, h):.0f}" fill="{BLUE if i % 2 == 0 else VERMILION}" '
                f'data-anim="grow-y" data-at="{at:.0f}" data-dur="700"/>'
                f'<text class="num" x="{cx:.0f}" y="{base - h - 14:.0f}" text-anchor="middle" '
                f'data-anim="count" data-at="{at + 100:.0f}" data-dur="760" '
                f'{_count_attrs(values[i])}>0</text>'
                f'<text class="s" x="{cx:.0f}" y="{base + 34:.0f}" text-anchor="middle">'
                f'{_clip_chars(_field(item, "label"))}</text>')
        if unit:
            body.append(f'<text class="s" x="{x0:.0f}" y="{top_y - 18:.0f}">{unit}</text>')
    body.append(_caption_row(v, 524, _axis_note(axis)))
    return _svg("chart", "".join(body))


def _point_dot(x: float, y: float, at: float) -> str:
    return (f'<circle cx="{x:.0f}" cy="{y:.0f}" r="9" fill="{PAPER}" stroke="{VERMILION}" '
            f'stroke-width="4" data-anim="pop" data-at="{at:.0f}" data-dur="340"/>')


# ---------------------------------------------------------------------------
# 6. scatter：散点 / 分布
# ---------------------------------------------------------------------------

def scatter(v: dict[str, Any]) -> str:
    points = _clip(_as_list(v.get("points")), MAX_POINTS)
    body = [_head(v)]
    if not points:
        return _svg("scatter", "".join(body) +
                    '<text class="h" x="500" y="280" text-anchor="middle">'
                    '（scatter 需要至少一个点）</text>')
    xs = [_num(p, "x", 0.0) for p in points]
    ys = [_num(p, "y", 0.0) for p in points]
    x_max = abs(_num(v, "x-max", max(xs, default=1.0))) or 1.0
    y_max = abs(_num(v, "y-max", max(ys, default=1.0))) or 1.0
    x0, x1, y0, y1 = 120.0, 950.0, 460.0, 150.0
    body.append(
        f'<g data-anim="wipe" data-at="{START_MS - 260}" data-dur="760">'
        f'<line x1="{x0:.0f}" y1="{y0:.0f}" x2="{x1 + 20:.0f}" y2="{y0:.0f}" '
        f'stroke="{INK}" stroke-width="3"/></g>'
        f'<g data-anim="rise" data-at="{START_MS - 260}" data-dur="760" data-dist="{y0 - y1:.0f}">'
        f'<line x1="{x0:.0f}" y1="{y0:.0f}" x2="{x0:.0f}" y2="{y1 - 20:.0f}" '
        f'stroke="{INK}" stroke-width="3"/></g>'
        f'<text class="s" x="{x1 + 20:.0f}" y="{y0 + 40:.0f}" text-anchor="end">'
        f'{_clip_chars(v.get("x-label") or "")}</text>'
        f'<text class="s" x="{x0 - 8:.0f}" y="{y1 - 34:.0f}">'
        f'{_clip_chars(v.get("y-label") or "")}</text>')
    for i, item in enumerate(points):
        px = x0 + (x1 - x0) * min(1.0, max(0.0, xs[i] / x_max))
        py = y0 - (y0 - y1) * min(1.0, max(0.0, ys[i] / y_max))
        at = START_MS + i * 90
        label = _clip_chars(_field(item, "label"))
        body.append(_point_dot(px, py, at))
        if label:
            body.append(f'<text class="s" x="{px + 16:.0f}" y="{py - 10:.0f}" '
                        f'data-anim="fade" data-at="{at + 120:.0f}" data-dur="320">{label}</text>')
    body.append(_caption_row(v, 524))
    return _svg("scatter", "".join(body))


# ---------------------------------------------------------------------------
# 7. orbit：绕圈（唯一含连续运动的图示：整镜都要逐帧截）
# ---------------------------------------------------------------------------

def orbit(v: dict[str, Any]) -> str:
    parts = _clip(_as_list(v.get("parts") or v.get("planets")), 5)
    center = v.get("center") if isinstance(v.get("center"), dict) else {}
    body = [_head(v)]
    cx, cy = 500.0, 300.0
    if not parts:
        return _svg("orbit", "".join(body) +
                    '<text class="h" x="500" y="280" text-anchor="middle">'
                    '（orbit 需要至少一个环绕体）</text>')
    body.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="34" fill="{VERMILION}" '
                f'data-anim="pulse" data-at="{START_MS}" data-period="2600"/>')
    body.append(f'<text class="t" x="{cx:.0f}" y="{cy + 78:.0f}" text-anchor="middle">'
                f'{_clip_chars(center.get("label") or center.get("name"))}</text>')
    for i, item in enumerate(parts):
        # 圈半径封顶 165：中心在 300，再大就压到上面的图题或下面那行小字
        default_r = 90.0 + (75.0 * i / max(1, len(parts) - 1) if len(parts) > 1 else 0.0)
        radius = min(165.0, max(40.0, _num(item, "r", default_r)))
        period = _num(item, "period-ms", _num(item, "period", 4200.0 + i * 1600.0))
        at = START_MS + i * 220
        label = _clip_chars(_field(item, "label"))
        body.append(
            f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{radius:.0f}" fill="none" '
            f'stroke="{HAIR}" stroke-width="2" stroke-dasharray="6 8" '
            f'data-anim="fade" data-at="{at - 160:.0f}" data-dur="420"/>'
            f'<g transform="translate({cx:.0f},{cy:.0f})">'
            f'<g data-anim="orbit" data-at="{at:.0f}" data-dist="{radius:.0f}" '
            f'data-period="{period:.0f}">'
            f'<circle cx="0" cy="0" r="13" fill="{BLUE}"/>'
            + (f'<text class="s" x="20" y="8">{label}</text>' if label else "")
            + "</g></g>")
    body.append(_caption_row(v, 528))
    return _svg("orbit", "".join(body))


BUILDERS = {
    "flow": flow,
    "compare": compare,
    "timeline": timeline,
    "levels": levels,
    "chart": chart,
    "scatter": scatter,
    "orbit": orbit,
}

#: 每张图示卡**实际读**的 visual 顶层键——逐行照 builders 里的取值点核对出来的。
#: ``spec.validate_spec`` 用它拦「写了这张卡不认的键」：SVG 不会拒绝未知键，它照画
#: 不误，于是模型以为写了、画面却什么都没有。改 builder 的取值点就要改这里。
_HEAD_KEYS = ("title", "note", "caption")     # _head() + _caption_row() 共用
DIAGRAM_VISUAL_KEYS: dict[str, tuple[str, ...]] = {
    "flow": ("steps", "items") + _HEAD_KEYS,
    "compare": ("left", "right", "vs") + _HEAD_KEYS,
    "timeline": ("marks", "events") + _HEAD_KEYS,
    # scale 只有走 _axis 的两张卡认（scatter 用 x-max/y-max，不给它假承诺）
    "levels": ("levels", "rows", "scale") + _HEAD_KEYS,
    "chart": ("series", "bars", "data", "unit", "mode", "scale") + _HEAD_KEYS,
    "scatter": ("points", "x-max", "y-max", "x-label", "y-label") + _HEAD_KEYS,
    "orbit": ("parts", "planets", "center") + _HEAD_KEYS,
}

# 绘制 op 族（像素/粒子/连线/笔触）与内置图示在同一张画面带上画图，注册表也并到
# 这里合并出去：spec 的卡型清单、templates 的 fill 判定、空壳闸读的都得是同一份，
# 下游任何一处再抄一遍清单，就会出现「卡型能过校验却渲不出画面」的卡。
# 放在文件底部导入：graphics 复用本模块的画法口径，写在顶部就成了循环导入。
from . import graphics as _gfx  # noqa: E402

BUILDERS.update(_gfx.BUILDERS)
DIAGRAM_VISUAL_KEYS.update(_gfx.VISUAL_KEYS)

#: 「这张卡的 visual 填得够画吗」——图示卡里只有绘制 op 族带这道检查（七个内置
#: 图示的判断已经在 ``spec._LIST_FIELDS``/``_TEXT_FIELDS`` 那套表里写死了）。
VALIDATORS: dict[str, Any] = dict(_gfx.CHECKS)

#: 图示卡的坐标写死在 1000×560 的 viewBox 里，靠 preserveAspectRatio 适配任意画幅带。
DIAGRAM_CSS = """
.dg { width:100%; height:100%; }
.dg text { fill:#23211c; }
.dg .h { font-size:44px; font-weight:700; letter-spacing:.02em; }
.dg .t { font-size:30px; }
.dg .s { font-size:22px; fill:#6c6455; }
.dg .yr { font-size:30px; font-weight:700; font-family:monospace; }
.dg .num { font-size:32px; font-weight:700; font-family:monospace; fill:#2f4f8f; }
.dg .vs { font-size:30px; font-weight:700; fill:#f3ecd9; font-family:monospace; }
"""


def diagram_svg(kind: str, visual: dict[str, Any]) -> str | None:
    """图示卡 → SVG 串；kind 不是图示卡 → None（交给别的分支处理）。"""
    builder = BUILDERS.get(kind)
    if builder is None:
        return None
    return builder(visual or {})
