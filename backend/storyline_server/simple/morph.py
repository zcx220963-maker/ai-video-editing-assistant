# -*- coding: utf-8 -*-
"""跨镜接力：让「贯穿物件」在切镜时**不断**。

为什么单独一个文件：它跨在"叙事片的画法"和"整片拼装"中间——
`story` 负责在场景里声明锚点，`film.assemble` 负责在切点处把两层接起来，
两边都不该独占它。

做法（参考片那个"小点一路变成地球"的同一个道理）：
    每镜声明 `const MOTIF_AT = { x, y, r }`（归一化坐标）
      → `parse_motif_at` 读出来
      → `shared_hops` 把相邻两镜的锚点变成"一跳"（只在**两边都有**锚点时接）
      → 外层 `SharedMorph` 在切点前后各 `dur/2` 帧里插值 + 淡入淡出

它画在 `TransitionSeries` **外面**，所以切镜时不会跟着上一镜一起卸载——
这就是"共享元素"和"每镜各画各的"的全部区别。
"""

from __future__ import annotations

import re
from typing import Any

#: 模型在场景体最上面声明的那一行（见 `_STORY_SCENE_SYSTEM`）。
_MOTIF_AT_RE = re.compile(
    r"const\s+MOTIF_AT\s*=\s*\{\s*x\s*:\s*(-?[\d.]+)\s*,\s*y\s*:\s*(-?[\d.]+)\s*,"
    r"\s*r\s*:\s*(-?[\d.]+)\s*\}")


def parse_motif_at(body: str) -> tuple[float, float, float] | None:
    """从场景体里读出「这一镜的贯穿物件在哪」。读不到就返回 None（不报错）。

    为什么用"声明 + 解析"而不是让模型返回 JSON：它已经在写 TSX 了，
    多一个字段就要多一段格式约束；而一行 `const MOTIF_AT = {…}` 是它顺手就能写的，
    解析也只需要一个正则——**这比再加一次模型调用便宜两个数量级**。
    """
    m = _MOTIF_AT_RE.search(body or "")
    if not m:
        return None
    try:
        x, y, r = (float(m.group(i)) for i in (1, 2, 3))
    except ValueError:
        return None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and 0.0 < r <= 0.5):
        return None
    return (round(x, 4), round(y, 4), round(r, 4))


#: 接力的颜色。用暖金而不是纯白：白会像镜头光斑，暖金像"那件东西本身在发光"。
MORPH_HUE = "#FFD08A"


def shared_hops(tl: dict[str, Any], motifs: list[tuple[float, float, float] | None] | None,
                n: int) -> list[dict[str, Any]]:
    """把"每镜物件的位置"变成"每次切点的一跳"。纯函数，可单独测。

    只在**两边都有位置**的切点上生成——一边没声明就说明那一镜里没有那件东西，
    硬接一个过去反而更假。这一条是"接力"和"乱飞"的分界。
    """
    if not motifs or len(motifs) < 2:
        return []
    from .film import TRANS_FRAMES      # 延迟 import：转场长度是 film 的常量
    out: list[dict[str, Any]] = []
    for i in range(1, min(n, len(motifs))):
        a, b = motifs[i - 1], motifs[i]
        if not a or not b:
            continue
        out.append({"at": int(tl["cuts"][i - 1]), "dur": TRANS_FRAMES * 2,
                    "from": a, "to": b, "hue": MORPH_HUE})
    return out


