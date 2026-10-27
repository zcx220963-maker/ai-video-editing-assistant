# -*- coding: utf-8 -*-
"""对照表：分镜只给数据，排版由 `CompareTable` 组件算。

从 `caption.py` 拆出来的（2026-10-25）：那边 166 行顶破了 135 的硬顶，而"表格数据校验"
跟"字幕版式"不是一个关注点——一个管**多列数据**，一个管**一行台词**。

## 为什么表格必须由代码画，不能交给模型

表格是"数据 → 坐标"的映射。让模型每次重画，列宽不齐、行高不一、数值不对齐——
实测两次（工程 4a16e1b904fe / b150b70a8271）在提示词里明写「散落各国对照表」，
模型画出来的 `<text>` 是 **0 个**，它只会把数据念进字幕。

而表格恰恰是知识类视频信息密度最高的形态：参考片第 125 秒那两栏
「李白中古拟音 vs 你·今天的普通话」，一眼就懂；念一遍观众记不住。

所以分工是：**分镜给数据（`scene["table"]`），这里做校验与截断，组件负责排版**。
"""

from __future__ import annotations

from typing import Any

#: 对照表最多几行。再多字号就小到看不清了——那时候该拆成两镜，不是缩字。
MAX_ROWS = 6
#: 最多几列。两列是"名称 + 数值"的常规形态；三列留给"名称 + 两个对照值"。
MAX_COLS = 3


def plan(scene: dict[str, Any]) -> dict[str, Any] | None:
    """这一镜要不要画对照表。要就返回规整好的 ``{title, cols, rows, link}``，否则 None。

    只做**校验与截断**，不做任何排版决定：列 2~3、行 ≤6、每行补齐到列数、空值转空串。
    坏了就返回 None（这一镜不画表，但整片照出）——表格是加分项，不是前置条件。
    """
    raw = scene.get("table")
    if not isinstance(raw, dict):
        return None
    cols = raw.get("cols")
    rows = raw.get("rows")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return None
    cols = [str(c if c is not None else "").strip()[:12] for c in cols][:MAX_COLS]
    if len(cols) < 2:
        return None
    out: list[list[str]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        cells = [str(c if c is not None else "").strip()[:18] for c in row][:len(cols)]
        cells += [""] * (len(cols) - len(cells))
        out.append(cells)
        if len(out) >= MAX_ROWS:
            break
    if not out:
        return None
    return {"title": str(raw.get("title") or "").strip()[:12],
            "cols": cols, "rows": out,
            "link": str(raw.get("link") or "").strip()[:8]}
