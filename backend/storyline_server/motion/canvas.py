# -*- coding: utf-8 -*-
"""图示类版式的公共画布原语（单源）。

为什么单独一层：``diagrams.py`` 的七个内置图示和 ``graphics.py`` 的四个绘制 op 族
要在同一张 viewBox 上画图、共用同一套取字段/转义/题注的口径。两边各抄一遍的后果是
「同一张卡片在两条路上字号不一样」，而那种差异只有抽出成片帧才看得见。

这一层不放注册表，也不认识卡型——它只提供「在 1000×560 的坐标里画点东西」的零件。
"""

from __future__ import annotations

import random
import re
from html import escape
from typing import Any

INK = "#23211c"
VERMILION = "#a8352c"
BLUE = "#2f4f8f"
MUTED = "#6c6455"
PAPER = "#f7f2e5"
HAIR = "#b9ad93"

VB = "0 0 1000 560"
VB_W, VB_H = 1000.0, 560.0
START_MS = 500          # 卡片入场（420ms）之后再开始画图
STEP_MS = 260           # 相邻数据项的入场间隔
DUR_MS = 560

MAX_ITEMS = 8
MAX_NOTE_CHARS = 26

#: 颜色只收这一种写法：它们会被直接拼进 SVG 的 fill/stroke 属性。
#: 放行函数式颜色（``url()``、``calc()``）等于让一个「改个配色」的指针拿到引用外部资源的能力。
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_RE = re.compile(r"^rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}"
                     r"(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\)$")


def esc(value: Any) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def field(item: Any, key: str, default: str = "") -> str:
    """从一项里取字段：既接受 ``{"label": "…"}``，也接受裸字符串当 label。"""
    if isinstance(item, dict):
        for k, v in item.items():
            if str(k).strip().lower() == key:
                return str(v if v is not None else default)
        return default
    return str(item) if key == "label" else default


def num(item: Any, key: str, default: float) -> float:
    if isinstance(item, dict):
        for k, v in item.items():
            if str(k).strip().lower() == key:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return default
    return default


def clip_chars(text: Any, cap: int = MAX_NOTE_CHARS) -> str:
    return esc(str(text or "")[:cap])


def paint(value: Any, default: str = "") -> tuple[str | None, str]:
    """→ (可用的颜色字面量, 拒收理由)。

    理由必须说清收到的是什么：这一栏是模型/用户手打的，回一句「格式不对」等于让人猜。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return default, ""
    if _HEX_RE.match(text) or _RGB_RE.match(text):
        return text, ""
    return None, f"颜色 {text[:32]!r} 不是 #hex 或 rgb()/rgba() 写法"


def colors(raw: Any, defaults: list[str], name: str) -> tuple[list[str], list[str]]:
    """一组颜色 → (可用清单, 拒收理由)。空值按 defaults 回落。"""
    items = as_list(raw)
    if not items:
        return list(defaults), []
    out: list[str] = []
    errors: list[str] = []
    for i, item in enumerate(items, 1):
        ok, why = paint(item, "")
        if ok is None:
            errors.append(f"{name} 第 {i} 个{why}")
        else:
            out.append(ok)
    return (out or list(defaults)), errors


def svg(cls: str, body: str) -> str:
    return (f'<svg class="dg {cls}" xmlns="http://www.w3.org/2000/svg" viewBox="{VB}"'
            f' preserveAspectRatio="xMidYMid meet">{body}</svg>')


def head(v: dict[str, Any]) -> str:
    """图题：一行中文 + 一行小注（与内置图示同一套字号，两类画面混排时字级才一致）。"""
    title, note = esc(v.get("title") or ""), esc(v.get("note") or "")
    out = ""
    if title:
        out += f'<text class="h" x="500" y="66" text-anchor="middle">{title}</text>'
    if note:
        out += f'<text class="s" x="500" y="100" text-anchor="middle">{note}</text>'
    return out


def caption_row(v: dict[str, Any], y: float = 524, extra: str = "") -> str:
    cap = esc(v.get("caption") or "")
    if extra:
        cap = f"{cap}　·　{esc(extra)}" if cap else esc(extra)
    if not cap:
        return ""
    return f'<text class="s" x="500" y="{y:.0f}" text-anchor="middle">{cap}</text>'


def rng(seed: Any) -> random.Random:
    """固定种子的随机源。

    必须是**整数**种子：``random.Random("abc")`` 在 CPython 里按字符串哈希展开，
    跨版本不保证同结果，而这条链的按镜缓存靠「同一份输入必然同一张图」才立得住。
    """
    try:
        return random.Random(int(seed))
    except (TypeError, ValueError):
        return random.Random(7)


def at_ms_of(v: dict[str, Any], key: str, default: float) -> float:
    return num(v, key, default)
