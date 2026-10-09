# -*- coding: utf-8 -*-
"""受控的绘制 op 族：把「程序化画一片东西」写成参数，由这里展开成 SVG。

为什么不是放开 JS（见 art.py 的三条口径）：这条链的可治理性全押在「画面是 spec 的纯函数」
上——分镜校验闸能在几秒内退回报错、命中表能按 ``/shots/…/visual/…`` 指到具体那一格、
按镜缓存敢把像素钉住、局部改只重烧受影响的那一镜。放开任意脚本就能一次拿到全部画风，
代价是这四条同时失效。所以这里走另一条路：**扩的是词汇表，不是执行权**。

四个族各自解决一类「卡片+文字」撑不住的观感：

* ``pixel``——小画布整数倍放大的像素画面，配 ``poses`` 就是逐帧角色动画；
* ``burst``——粒子群（蓄力、迸发、飘散）；
* ``net``——节点加连线（注意力图、关系网、神经网络那一类）；
* ``brush``——抖动的重叠笔触（彩铅、蜡笔、手绘示意图）。

三条与模板的约定和内置图示完全一致：动效只写 ``data-anim``、时值在这里定死、
形状数量由参数决定且每一项都有上限。展开完仍然是 SVG 结构树，命中表与校验闸照常工作。
"""

from __future__ import annotations

import math
from typing import Any, Callable

from . import canvas as _c

# ---------------------------------------------------------------------------
# 上限：逐帧截图按「元素数 × 活帧数」计费，这几个数是经验值不是审美判断
# ---------------------------------------------------------------------------

PIXEL_MAX_ROWS = 48
PIXEL_MAX_COLS = 96
PIXEL_MAX_POSES = 24
PIXEL_MAX_RECTS = 1200
#: 画图用的纵带：图题占 66/100 两行，题注住在 524，中间这段才归画面主体。
PIXEL_TOP, PIXEL_BOTTOM = 120.0, 492.0

BURST_MAX = 100
BURST_DEFAULT = 40

NET_MAX_NODES = 24
NET_MAX_EDGES = 80

BRUSH_MAX_STROKES = 12
BRUSH_MAX_PASSES = 3
BRUSH_MAX_POINTS = 40

#: pixel 里当作「不画」的字符（透明底）。
TRANSPARENT_CHARS = frozenset({".", " ", "-", "_"})

#: 默认色板：pixel 卡没写 palette 时的回落，只有这张表里有的字符才允许裸用。
DEFAULT_PALETTE = {"1": _c.INK, "2": _c.VERMILION, "3": _c.BLUE, "4": _c.MUTED,
                   "5": _c.PAPER, "6": "#e8c34a", "7": "#3f7d5a"}

HEAD_KEYS = ("title", "note", "caption")


def _err(out: list[str], msg: str) -> None:
    out.append(msg)


# ---------------------------------------------------------------------------
# 1. pixel：像素网格 / 逐帧角色动画
# ---------------------------------------------------------------------------

def _pixel_palette(v: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    raw = v.get("palette")
    table = dict(DEFAULT_PALETTE)
    errors: list[str] = []
    if raw is None:
        return table, errors
    if not isinstance(raw, dict):
        _err(errors, "palette 必须是 {字符: 颜色} 的对象")
        return table, errors
    for ch, color in raw.items():
        key = str(ch)
        if len(key) != 1:
            _err(errors, f"palette 的键 {key!r} 不是一个字符——它是行文字母表，一键一色")
            continue
        ok, why = _c.paint(color, "")
        if ok is None:
            _err(errors, f"palette 里 {key!r}{why}")
        else:
            table[key] = ok
    return table, errors


def _pixel_poses(v: dict[str, Any], table: dict[str, str],
                 errors: list[str]) -> list[dict[str, Any]]:
    """poses / rows → [{rows, at, hold}]，顺手把「画不出来」的都记进 errors。"""
    raw = v.get("poses")
    items: list[Any] = []
    if raw is not None:
        if not isinstance(raw, (list, tuple)):
            _err(errors, "poses 必须是数组（每个元素一个姿势：{rows, at_ms, hold_ms}）")
        else:
            items = list(raw)
    elif v.get("rows") is not None:
        items = [v.get("rows")]
    if len(items) > PIXEL_MAX_POSES:
        _err(errors, f"姿势 {len(items)} 个 > {PIXEL_MAX_POSES}——逐帧角色动画每格都要"
                     "排一遍矩形，一个角色换两三套姿势足够，再多就只是拖慢出片")
        items = items[:PIXEL_MAX_POSES]
    out: list[dict[str, Any]] = []
    cursor = _c.START_MS
    for n, item in enumerate(items, 1):
        rows_raw = item.get("rows") if isinstance(item, dict) else item
        at = _c.num(item, "at_ms", cursor) if isinstance(item, dict) else cursor
        hold = _c.num(item, "hold_ms", 0.0) if isinstance(item, dict) else 0.0
        rows = [str(r) for r in _c.as_list(rows_raw)]
        if not rows:
            _err(errors, f"第 {n} 个姿势没有 rows——这一格画出来是空的")
        width = max((len(r) for r in rows), default=0)
        if width > PIXEL_MAX_COLS:
            _err(errors, f"第 {n} 个姿势最宽 {width} 格 > {PIXEL_MAX_COLS}，"
                         f"像素风靠的是小网格，不是把每格缩到看不见")
        if len(rows) > PIXEL_MAX_ROWS:
            _err(errors, f"第 {n} 个姿势 {len(rows)} 行 > {PIXEL_MAX_ROWS}")
        if any(len(r) != width for r in rows):
            _err(errors, f"第 {n} 个姿势各行字数不一致（{sorted({len(r) for r in rows})}）"
                         "——像素网格按列对齐，长短不齐会画成斜的")
        miss = sorted({ch for r in rows for ch in r} - set(table) - set(TRANSPARENT_CHARS))
        if miss:
            _err(errors, f"第 {n} 个姿势用了色板里没有的字符 {''.join(miss)!r}——"
                         "palette 里给它配个颜色，或改成透明占位符 . / 空格")
        if hold <= 0 and n < len(items):
            _err(errors, f"第 {n} 个姿势没写 hold_ms：轮播必须有停留时长，"
                         "只有最后一个姿势可以省（它播完就停住）")
            hold = 400.0
        out.append({"rows": rows, "at": max(0.0, at), "hold": max(0.0, hold)})
        cursor = max(0.0, at) + (max(0.0, hold) or 400.0)
    return out


def _pixel_runs(rows: list[str]) -> list[tuple[int, int, int, str]]:
    """行文字 → [(行, 起始列, 连续格数, 字符)]：同色横条合成一个矩形。

    这一步不是为了好看。一个 64×48 的角色逐格写是 3072 个 ``<rect>``，逐帧排版就得多花
    一次浏览器启动的时间；按横条合并后通常剩 60~140 个，而像素完全不变。
    """
    runs: list[tuple[int, int, int, str]] = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            ch = row[x]
            if ch in TRANSPARENT_CHARS:
                x += 1
                continue
            n = x
            while n < len(row) and row[n] == ch:
                n += 1
            runs.append((y, x, n - x, ch))
            x = n
    return runs


def pixel(v: dict[str, Any]) -> str:
    table, _ = _pixel_palette(v)
    poses = _pixel_poses(v, table, [])
    rows_all = [p["rows"] for p in poses if p["rows"]]
    if not rows_all:
        return _c.svg("pixel", '<text class="h" x="500" y="300" text-anchor="middle">'
                               '（pixel 需要 visual.rows 或 visual.poses）</text>')
    cols = max(len(r) for rows in rows_all for r in rows)
    line_h = max(len(r) for r in rows_all)
    box_h = PIXEL_BOTTOM - PIXEL_TOP
    # cell 是「每格几像素」：写了就固定它（像素风的一大半观感在这个数——同一张字符画，
    # 12 格是细腻、32 格是块状），没写才按画面带自动铺满。
    auto = math.floor(min(900.0 / cols, box_h / line_h))
    cell = max(3.0, min(40.0, _c.num(v, "cell", float(auto)) or float(auto)))
    sprite_w, sprite_h = cols * cell, line_h * cell
    origin_x = (1000.0 - sprite_w) / 2.0
    origin_y = PIXEL_TOP + (box_h - sprite_h) / 2.0
    entry = 200.0
    body = [_c.head(v)]
    for i, pose in enumerate(poses):
        runs = _pixel_runs(pose["rows"])
        hold = pose["hold"]
        rects = "".join(
            f'<rect x="{origin_x + x * cell:.2f}" y="{origin_y + y * cell:.2f}"'
            f' width="{w * cell:.2f}" height="{cell:.2f}"'
            f' fill="{table.get(ch, _c.INK)}"/>'
            for (y, x, w, ch) in runs)
        tail = "" if not hold else f' data-hold="{hold:.0f}"'
        body.append(
            f'<g data-anim="pose" data-at="{pose["at"]:.0f}" data-dur="{entry:.0f}"{tail}>'
            f'<g data-anim="pop" data-at="{pose["at"]:.0f}" data-dur="{entry:.0f}">'
            f'{rects}</g></g>')
    body.append(_c.caption_row(v))
    return _c.svg("pixel", "".join(body))


def check_pixel(v: dict[str, Any]) -> list[str]:
    table, errors = _pixel_palette(v)
    poses = _pixel_poses(v, table, errors)
    if not any(p["rows"] for p in poses) and v.get("rows") is None and v.get("poses") is None:
        _err(errors, "visual.rows / visual.poses 一个都没填——画面上是一张空纸")
    rects = sum(len(_pixel_runs(p["rows"])) for p in poses)
    if rects > PIXEL_MAX_RECTS:
        _err(errors, f"同色横条合并之后仍有 {rects} 个矩形，超过上限 {PIXEL_MAX_RECTS}："
                     f"缩网格或把细节拆成几个镜头")
    return errors


# ---------------------------------------------------------------------------
# 2. burst：粒子群（迸发 / 汇聚 / 飘散）
# ---------------------------------------------------------------------------

def _burst_params(v: dict[str, Any]) -> dict[str, Any]:
    count = int(_c.num(v, "count", BURST_DEFAULT))
    count = max(1, min(BURST_MAX, count if count > 0 else BURST_DEFAULT))
    dist = _c.num(v, "dist", 180.0)
    dist_max = _c.num(v, "dist-max", dist)
    return {
        "count": count,
        "x": _c.num(v, "x", 500.0), "y": _c.num(v, "y", 260.0),
        "dir": _c.num(v, "dir", -90.0), "spread": _c.num(v, "spread", 360.0),
        "dist": max(1.0, min(460.0, dist)),
        "dist_max": max(1.0, min(460.0, dist_max if dist_max > 0 else dist)),
        "size": max(1.0, min(40.0, _c.num(v, "size", 9.0))),
        "shape": str(v.get("shape") or "circle").strip().lower(),
        "colors": _c.colors(v.get("colors"), [_c.VERMILION, _c.BLUE, _c.INK], "colors"),
        "seed": v.get("seed"),
        "at": max(0.0, _c.num(v, "at_ms", _c.START_MS)),
        "dur": max(40.0, _c.num(v, "dur_ms", 900.0)),
        "stagger": max(0.0, _c.num(v, "stagger_ms", 40.0)),
        "twinkle": bool(v.get("twinkle")),
    }


def burst(v: dict[str, Any]) -> str:
    p = _burst_params(v)
    colors, errors = p["colors"]
    rand = _c.rng(p["seed"])
    n = p["count"]
    full = p["spread"] >= 360.0
    body = [_c.head(v)]
    for i in range(n):
        if full:
            ang = rand.uniform(0.0, 360.0)
        else:
            step = p["spread"] / n if n > 1 else 0.0
            ang = p["dir"] - p["spread"] / 2.0 + step * i + rand.uniform(-step / 2, step / 2)
        radius = rand.uniform(p["dist"], max(p["dist"], p["dist_max"]))
        size = max(1.0, p["size"] * rand.uniform(0.6, 1.2))
        color = colors[i % len(colors)]
        at = p["at"] + i * p["stagger"]
        # 落点写在元素上（cx=radius），动效把它从原点推过去：
        # slide-x 的终态是「不偏移」，所以静止位置必须就是目的地，否则粒子会缩回中心。
        if p["shape"] == "rect":
            inner = (f'<rect x="{radius - size:.1f}" y="{-size:.1f}"'
                     f' width="{size * 2:.1f}" height="{size * 2:.1f}" fill="{color}"/>')
        else:
            inner = f'<circle cx="{radius:.1f}" cy="0" r="{size:.1f}" fill="{color}"/>'
        twinkle = f' data-anim="pulse" data-at="{at:.0f}" data-period="1600"' \
            if p["twinkle"] and i % 3 == 0 else ""
        if twinkle:
            inner = inner.replace(' fill="', f'{twinkle} fill="', 1)
        body.append(
            f'<g transform="translate({p["x"]:.1f},{p["y"]:.1f}) rotate({ang:.1f})">'
            f'<g data-anim="slide-x" data-at="{at:.0f}" data-dur="{p["dur"]:.0f}"'
            f' data-dist="{radius:.1f}">{inner}</g></g>')
    body.append(_c.caption_row(v, 524, "粒子持续闪动，本镜每帧都要截" if p["twinkle"] else ""))
    return _c.svg("burst", "".join(body))


def check_burst(v: dict[str, Any]) -> list[str]:
    errors = _burst_params(v)["colors"][1]
    shape = str(v.get("shape") or "circle").strip().lower()
    if shape not in ("circle", "rect"):
        _err(errors, f"shape={shape!r} 画不出来（circle / rect）")
    for key in ("count", "dist", "size"):
        if key in v:
            try:
                float(v[key])      # type: ignore[index]
            except (TypeError, ValueError):
                _err(errors, f"{key}={v[key]!r} 不是数字——它决定粒子怎么铺，不接受文字")
    return errors


# ---------------------------------------------------------------------------
# 3. net：节点 + 连线（关系网 / 注意力图）
# ---------------------------------------------------------------------------

NET_MODES = ("ring", "layers", "mesh", "star")


def _net_nodes(v: dict[str, Any], errors: list[str]) -> list[dict[str, Any]]:
    raw = v.get("nodes")
    nodes: list[dict[str, Any]] = []
    if raw is not None:
        if not isinstance(raw, (list, tuple)):
            _err(errors, "nodes 必须是数组（每项 {label, x?, y?}）")
            return nodes
        for item in raw:
            entry = item if isinstance(item, dict) else {"label": item}
            nodes.append({
                "label": str(_c.field(entry, "label") or ""),
                "x": _c.num(entry, "x", -1.0), "y": _c.num(entry, "y", -1.0),
            })
    else:
        count = int(_c.num(v, "count", 8))
        for _ in range(max(0, count)):
            nodes.append({"label": "", "x": -1.0, "y": -1.0})
    if len(nodes) > NET_MAX_NODES:
        _err(errors, f"节点 {len(nodes)} 个 > {NET_MAX_NODES}——连线会糊成一团，"
                     "要讲层次请拆镜")
        del nodes[NET_MAX_NODES:]
    return nodes


def _net_layout(nodes: list[dict[str, Any]], mode: str, seed: Any) -> list[tuple[float, float]]:
    """节点坐标：显式给的就用显式的，其余按模式铺在 1000×560 的带里。"""
    n = len(nodes)
    pts: list[tuple[float, float]] = []
    cx, cy, radius = 500.0, 288.0, 178.0
    for i, node in enumerate(nodes):
        if node["x"] >= 0 and node["y"] >= 0:
            pts.append((node["x"], node["y"]))
            continue
        if mode == "layers":
            cols = max(1, min(4, n))
            rows = max(1, math.ceil(n / cols))
            col, row = i % cols, i // cols
            x = 500.0 if cols == 1 else 170.0 + col * (660.0 / (cols - 1))
            y = 288.0 if rows == 1 else 160.0 + row * (256.0 / (rows - 1))
            pts.append((x, y))
        elif mode == "mesh":
            rand = _c.rng(seed if seed is not None else 7)
            pts.append((rand.uniform(140.0, 860.0), rand.uniform(140.0, 440.0)))
        elif mode == "star":
            ang = (i - 1) * 2 * math.pi / max(1, n - 1) - math.pi / 2
            pts.append((cx, cy) if i == 0 else
                       (cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
        else:   # ring
            ang = i * 2 * math.pi / max(1, n) - math.pi / 2
            pts.append((cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
    return pts


def _net_edges(nodes: list[dict[str, Any]], pts: list[tuple[float, float]],
               mode: str, v: dict[str, Any], errors: list[str]) -> list[tuple[int, int]]:
    raw = v.get("edges")
    if raw is not None:
        pairs: list[tuple[int, int]] = []
        if not isinstance(raw, (list, tuple)):
            _err(errors, "edges 必须是数组（每项 [i, j]，指 nodes 的下标）")
            return pairs
        for k, item in enumerate(raw, 1):
            pair = _c.as_list(item)
            if len(pair) < 2:
                _err(errors, f"edges 第 {k} 项不是 [i, j] 两个下标")
                continue
            try:
                a, b = int(pair[0]), int(pair[1])      # type: ignore[arg-type]
            except (TypeError, ValueError):
                _err(errors, f"edges 第 {k} 项的下标不是整数：{item!r}")
                continue
            if not (0 <= a < len(nodes) and 0 <= b < len(nodes)) or a == b:
                _err(errors, f"edges 第 {k} 项 [{a}, {b}] 超出节点范围"
                             f"（0~{len(nodes) - 1}，且不能连自己）")
                continue
            pairs.append((a, b))
        if len(pairs) > NET_MAX_EDGES:
            _err(errors, f"连线 {len(pairs)} 条 > {NET_MAX_EDGES}，画面会糊")
            pairs = pairs[:NET_MAX_EDGES]
        return pairs
    n = len(nodes)
    if n < 2:
        return []
    if mode == "star":
        return [(0, i) for i in range(1, n)]
    if mode == "ring":
        pairs = [(i, i + 1) for i in range(n - 1)]
        if bool(v.get("loop")):
            pairs.append((n - 1, 0))
        return pairs
    # layers / mesh：每个节点连到最近的两个（不含自己），确定性、不重复
    seen: set[tuple[int, int]] = set()
    for i in range(n):
        order = sorted((j for j in range(n) if j != i),
                       key=lambda j: ((pts[j][0] - pts[i][0]) ** 2
                                      + (pts[j][1] - pts[i][1]) ** 2))
        for j in order[:2]:
            seen.add((min(i, j), max(i, j)))
    return sorted(seen)[:NET_MAX_EDGES]


def net(v: dict[str, Any]) -> str:
    errors: list[str] = []
    mode = str(v.get("mode") or "ring").strip().lower()
    if mode not in NET_MODES:
        mode = "ring"
    nodes = _net_nodes(v, errors)
    pts = _net_layout(nodes, mode, v.get("seed"))
    edges = _net_edges(nodes, pts, mode, v, errors)
    line = _c.paint(v.get("line"), _c.HAIR)[0] or _c.HAIR
    dot = _c.paint(v.get("color"), _c.VERMILION)[0] or _c.VERMILION
    r = max(4.0, min(30.0, _c.num(v, "r", 13.0)))
    at0 = max(0.0, _c.num(v, "at_ms", _c.START_MS))
    draw_dur = max(80.0, _c.num(v, "dur_ms", 620.0))
    body = [_c.head(v)]
    for k, (a, b) in enumerate(edges):
        ax, ay = pts[a]
        bx, by = pts[b]
        body.append(
            f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}"'
            f' stroke="{line}" stroke-width="2.5" data-anim="draw"'
            f' data-at="{at0 + k * 40:.0f}" data-dur="{draw_dur:.0f}"/>')
    for i, node in enumerate(nodes):
        x, y = pts[i]
        label = _c.clip_chars(node["label"], 14)
        at = at0 + 260.0 + i * _c.STEP_MS * 0.6
        body.append(
            f'<g data-anim="pop" data-at="{at:.0f}" data-dur="360">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{dot}"/>'
            + (f'<text class="s" x="{x:.1f}" y="{y + r + 26:.1f}" text-anchor="middle">'
               f'{label}</text>' if label else "")
            + "</g>")
    body.append(_c.caption_row(v, 524))
    return _c.svg("net", "".join(body))


def check_net(v: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    mode = str(v.get("mode") or "ring").strip().lower()
    if mode not in NET_MODES:
        _err(errors, f"mode={mode!r} 不是排法之一（{', '.join(NET_MODES)}）："
                     "ring 环形 / layers 分层 / mesh 网 / star 中心辐射")
    nodes = _net_nodes(v, errors)
    if len(nodes) < 2:
        _err(errors, "net 卡至少要有 2 个节点（visual.nodes 或 visual.count）——"
                    "一个节点连不出网")
    for key in ("color", "line"):
        if key in v and _c.paint(v[key], "")[0] is None:
            _err(errors, f"{key}{_c.paint(v[key], '')[1]}")
    _net_edges(nodes, _net_layout(nodes, mode, v.get("seed")), mode, v, errors)
    return errors


# ---------------------------------------------------------------------------
# 4. brush：抖动的重叠笔触（彩铅 / 蜡笔 / 手绘）
# ---------------------------------------------------------------------------

BRUSH_SHAPES = ("line", "circle", "rect", "poly")


def _brush_samples(stroke: dict[str, Any]) -> list[tuple[float, float]]:
    shape = str(stroke.get("shape") or "line").strip().lower()
    if shape == "circle":
        cx, cy = _c.num(stroke, "x", 500.0), _c.num(stroke, "y", 280.0)
        radius = max(4.0, _c.num(stroke, "r", 90.0))
        steps = max(16, min(BRUSH_MAX_POINTS * 2, int(2 * math.pi * radius / 18)))
        return [(cx + radius * math.cos(k * 2 * math.pi / steps),
                 cy + radius * math.sin(k * 2 * math.pi / steps)) for k in range(steps + 1)]
    if shape == "rect":
        x, y = _c.num(stroke, "x", 260.0), _c.num(stroke, "y", 150.0)
        w, h = _c.num(stroke, "w", 480.0), _c.num(stroke, "h", 260.0)
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
        out: list[tuple[float, float]] = []
        for (ax, ay), (bx, by) in zip(corners, corners[1:]):
            steps = max(2, int(math.hypot(bx - ax, by - ay) / 24))
            out += [(ax + (bx - ax) * k / steps, ay + (by - ay) * k / steps)
                    for k in range(steps + 1)]
        return out
    if shape == "poly":
        raw = _c.as_list(stroke.get("points"))
        pts: list[tuple[float, float]] = []
        for item in raw:
            pair = _c.as_list(item)
            if len(pair) >= 2:
                try:
                    pts.append((float(pair[0]), float(pair[1])))     # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        # 采样点数就是 path 里 L 段的数量：它直接进每帧排版耗时，所以按上限截
        return pts[:BRUSH_MAX_POINTS]
    x1, y1 = _c.num(stroke, "x", 200.0), _c.num(stroke, "y", 380.0)
    x2, y2 = _c.num(stroke, "x2", 800.0), _c.num(stroke, "y2", 200.0)
    steps = max(2, int(math.hypot(x2 - x1, y2 - y1) / 24))
    return [(x1 + (x2 - x1) * k / steps, y1 + (y2 - y1) * k / steps)
            for k in range(steps + 1)]


def _brush_path(pts: list[tuple[float, float]], jitter: float, rand: Any) -> str:
    """折线 → 带抖动的 path。抖的是**中间点**，端点钉住，否则一笔一根会越画越歪。"""
    if len(pts) < 2:
        return ""
    parts = [f'M{pts[0][0]:.1f} {pts[0][1]:.1f}']
    for (x, y) in pts[1:-1]:
        parts.append(f'L{x + rand.uniform(-jitter, jitter):.1f} '
                     f'{y + rand.uniform(-jitter, jitter):.1f}')
    parts.append(f'L{pts[-1][0]:.1f} {pts[-1][1]:.1f}')
    return " ".join(parts)


def brush(v: dict[str, Any]) -> str:
    strokes = _c.as_list(v.get("strokes"))[:BRUSH_MAX_STROKES]
    body = [_c.head(v)]
    for i, stroke in enumerate(strokes):
        item = stroke if isinstance(stroke, dict) else {"shape": stroke}
        color = _c.paint(item.get("color"), _c.INK)[0] or _c.INK
        width = max(1.0, min(40.0, _c.num(item, "width", 6.0)))
        passes = max(1, min(BRUSH_MAX_PASSES, int(_c.num(item, "passes", 2))))
        jitter = max(0.0, min(40.0, _c.num(item, "jitter", 5.0)))
        at = max(0.0, _c.num(item, "at_ms", _c.START_MS + i * _c.STEP_MS))
        dur = max(120.0, _c.num(item, "dur_ms", 900.0))
        pts = _brush_samples(item)
        if len(pts) < 2:
            continue
        for k in range(passes):
            rand = _c.rng(_c.num(item, "seed", 7) + i * 31 + k * 7)
            d = _brush_path(pts, jitter, rand)
            if not d:
                continue
            body.append(
                f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width:.1f}"'
                f' stroke-linecap="round" stroke-linejoin="round"'
                f' opacity="{(0.55 + 0.45 / (k + 1)):.2f}"'
                f' data-anim="draw" data-at="{(at + k * 90):.0f}" data-dur="{dur:.0f}"/>')
    body.append(_c.caption_row(v, 524))
    return _c.svg("brush", "".join(body))


def check_brush(v: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    strokes = _c.as_list(v.get("strokes"))
    if not strokes:
        _err(errors, "visual.strokes 是空的——笔触卡至少要有一笔")
        return errors
    if len(strokes) > BRUSH_MAX_STROKES:
        _err(errors, f"笔触 {len(strokes)} 笔 > {BRUSH_MAX_STROKES}，一镜画不下这么多")
    for i, stroke in enumerate(strokes, 1):
        item = stroke if isinstance(stroke, dict) else {"shape": stroke}
        shape = str(item.get("shape") or "line").strip().lower()
        if shape not in BRUSH_SHAPES:
            _err(errors, f"第 {i} 笔的 shape={shape!r} 画不出来"
                         f"（{', '.join(BRUSH_SHAPES)}）")
            continue
        if _c.paint(item.get("color"), "")[0] is None:
            _err(errors, f"第 {i} 笔{_c.paint(item.get('color'), '')[1]}")
        if shape == "poly" and len(_brush_samples(item)) < 2:
            _err(errors, f"第 {i} 笔是 poly 但 points 不足两点")
        for key in ("x", "y", "x2", "y2", "r", "w", "h", "width", "jitter", "passes"):
            if key in item:
                try:
                    float(item[key])       # type: ignore[arg-type,index]
                except (TypeError, ValueError):
                    _err(errors, f"第 {i} 笔的 {key}={item[key]!r} 不是数字"     # type: ignore[arg-type]
                                 "——形状坐标只收数字")
    return errors


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "pixel": pixel,
    "burst": burst,
    "net": net,
    "brush": brush,
}

#: 每个族的 visual 顶层键（写在这张表外的键会被 spec 的死键闸退回）。
VISUAL_KEYS: dict[str, tuple[str, ...]] = {
    "pixel": ("rows", "poses", "palette", "cell") + HEAD_KEYS,
    "burst": ("count", "x", "y", "dir", "spread", "dist", "dist-max", "size",
              "shape", "colors", "seed", "at_ms", "dur_ms", "stagger_ms",
              "twinkle") + HEAD_KEYS,
    "net": ("nodes", "count", "edges", "mode", "loop", "color", "line", "r",
            "seed", "at_ms", "dur_ms") + HEAD_KEYS,
    "brush": ("strokes",) + HEAD_KEYS,
}

#: 「这张卡画得出来吗」的那一关（spec._check_visual_content 调它）。
CHECKS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "pixel": check_pixel,
    "burst": check_burst,
    "net": check_net,
    "brush": check_brush,
}

#: 每个族的中文一句话（进 schema 与技能文档，单源）。
LABELS = {
    "pixel": "像素网格（rows 逐行字符画 + palette 配色；poses 就是逐帧角色动画）",
    "burst": "粒子群（从一个原点迸发或铺散，参数定数量/方向/射程/配色）",
    "net": "节点连线网（关系图、注意力图、神经网络那一类）",
    "brush": "手绘笔触（抖动重描的线/圆/方框/折线，彩铅蜡笔的观感）",
}
