# -*- coding: utf-8 -*-
"""出 N 条候选，只渲挑中的那一条。

为什么不是"渲 N 条成片让人挑"：渲一镜 20~40 秒、几毛钱；静帧是它的 1/100。
3 条候选各渲 3 张静帧 ≈ 1.3 条成片的成本，但用户拿到的选择权是 3 倍。

这就是「网上那些 demo 好看」的真实机制——他们也是先看几帧再渲，只是没告诉你。
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import kernel
from .kernel import KernelError

#: 候选条数上限。再多就是在让用户做选择题而不是做片子。
MAX_TAKES = 3


async def draft(prompt: str, *, seconds: float = kernel.DEFAULT_SECONDS,
                aspect: str = kernel.DEFAULT_ASPECT, fps: int = kernel.DEFAULT_FPS,
                takes: int = 1, llm: Any = None, user_id: str = "",
                conversation_id: str = "") -> list[dict[str, Any]]:
    """并行写 N 段代码，各渲 3 张静帧。返回 ``[{index, code, stills, error, calls}]``。

    **不抛**：某一条失败时那一条带 `error`，其余照常返回——三条里坏一条不该全废。
    """
    n = max(1, min(MAX_TAKES, int(takes or 1)))

    async def one(i: int) -> dict[str, Any]:
        try:
            code, calls = await kernel.write_code(
                prompt, seconds=seconds, aspect=aspect, fps=fps, llm=llm,
                user_id=user_id, conversation_id=conversation_id)
        except KernelError as exc:
            return {"index": i, "code": "", "stills": [], "calls": 0, **exc.as_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"index": i, "code": "", "stills": [], "calls": 0,
                    "stage": "write", "error": f"{type(exc).__name__}: {exc}"}
        try:
            shots = await kernel.stills(code, seconds=seconds, aspect=aspect, fps=fps)
        except KernelError as exc:
            # 静帧失败不算失败：代码是好的，用户仍可以选它去渲。
            return {"index": i, "code": code, "stills": [], "calls": calls,
                    "error": f"静帧渲不出来（{exc}），但这段代码可以渲"}
        return {"index": i, "code": code, "stills": shots, "calls": calls, "error": ""}

    return list(await asyncio.gather(*[one(i) for i in range(n)]))


def pick(items: list[dict[str, Any]], index: int) -> dict[str, Any]:
    """从候选里取一条。取不到就抛——这里抛是调用方的错，不是用户的错。"""
    for item in items:
        if int(item.get("index", -1)) == int(index):
            if not item.get("code"):
                raise KernelError("pick", f"第 {index} 条没有可用代码（{item.get('error') or '写码失败'}）")
            return item
    raise KernelError("pick", f"没有第 {index} 条候选")
