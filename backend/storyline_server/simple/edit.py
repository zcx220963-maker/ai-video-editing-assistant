# -*- coding: utf-8 -*-
"""改一镜：只重写这一镜，重新拼装，只复核这一镜。

从 `film.py` 拆出来的（2026-10-25）：那边加完"重写也要走这条片子自己的画法"之后
1,178 行，顶破了 1,165 的硬顶。拆的边界是**动词**：

    film.py    出片：分镜 → 并发写镜 → 拼装 → 质检环 → 分派
    edit.py    改片：读工程 → 只重写第 N 镜 → 重新拼装 → 只复核第 N 镜

为什么改一镜值得单独一个文件：它是"Claude + Remotion 那种工作流"的实质——
"看静帧 → 说哪一镜不对 → 只改那一镜 → 再渲"。整片重跑是 24 次调用 + 15 分钟，
而且重跑出来的第 7 镜还是另一个样子；用户说的"把那个太阳改小一点"，
只有在能只改一镜的时候才成立。

**方向是单向的**：本文件 import film，film 不 import 本文件——所以没有循环。
调用方（`agent_framework/tools/short_video.py`）直接 import 这里。
"""

from __future__ import annotations

from typing import Any

from . import qa, system
from .film import (MODE_STORY, KernelError, _inject_ribbon, assemble, safe_scene,
                   write_scene)
from .kernel import DEFAULT_ASPECT, DEFAULT_FPS, norm_aspect
from .morph import parse_motif_at
from .scene import safe_story_scene, wrap_story_scene
from .story import write_scene_story_code


async def reshoot(proj: dict[str, Any], shot_no: int, *, note: str = "",
                  llm: Any = None, user_id: str = "",
                  conversation_id: str = "") -> dict[str, Any]:
    """只重写一镜，重新拼装，并**只复核这一镜**。

    这是"改一镜"的全部代价：1 次模型调用 + 2 张静帧。整片重跑是 24 次调用 + 15 分钟，
    而且重跑出来的第 7 镜还是另一个样子——用户说的"把那个太阳改小一点"，
    只有在能只改一镜的时候才成立。

    `note` 是用户的原话（"太阳太大、挡住字了"）。它会被原样放进提示词的
    "上一版的问题"那一栏——**不要翻译成术语**，模型对用户原话的理解比对转述准。
    """
    bodies = list(proj.get("bodies") or [])
    scenes = list((proj.get("plan") or {}).get("scenes") or [])
    n = len(bodies)
    if not 1 <= int(shot_no) <= n:
        raise KernelError("reshoot", f"镜号要在 1~{n} 之间（收到 {shot_no}）")

    i = int(shot_no) - 1
    secs = [float(s) for s in proj.get("secs") or []]
    kinds = list(proj.get("kinds") or [])
    aspect = norm_aspect(proj.get("aspect") or DEFAULT_ASPECT)
    fps = int(proj.get("fps") or DEFAULT_FPS)
    scene = scenes[i] if i < len(scenes) else {}
    # **重写也必须走这条片子自己的画法**。叙事片用动效模式的写手重画一镜，
    # 出来的就是一帧"几何色块"混在剪影叙事里——一眼看出不是同一个世界。
    story = str(proj.get("mode") or "").strip().lower() == MODE_STORY
    motif = str((proj.get("plan") or {}).get("motif") or "")

    hint = (note or "").strip() or (
        "上一版这一镜不够好，重画一遍：换一个构图，主体再大一些、层次再多一层。")
    if story:
        body = await write_scene_story_code(
            scene, index=i, total=n, seconds=secs[i], aspect=aspect,
            llm=llm, user_id=user_id, conversation_id=conversation_id,
            extra=hint, motif=motif)
        bodies[i] = wrap_story_scene(body, scene, index=i)
    else:
        bodies[i] = await write_scene(
            scene, index=i, total=n, seconds=secs[i],
            prev=str(scenes[i - 1].get("brief") or "") if i and i - 1 < len(scenes) else "",
            nxt=str(scenes[i + 1].get("brief") or "") if i + 1 < len(scenes) else "",
            aspect=aspect, llm=llm, user_id=user_id,
            conversation_id=conversation_id, extra=hint)

    def rebuild() -> tuple[str, dict[str, Any]]:
        """重新拼装。叙事片要**重读锚点**——重写过的镜位置变了，沿用旧锚点会接空。"""
        return assemble(bodies, secs, kinds, fps=fps,
                        motifs=[parse_motif_at(b) for b in bodies] if story else None)

    async def check() -> tuple[str, dict[str, Any], dict[str, Any]]:
        code, tl = rebuild()
        got = await qa.audit(code, secs, aspect=aspect, fps=fps, only=[shot_no],
                             elements={int(shot_no): system.element_count(bodies[i])},
                             story=story)
        return code, tl, got

    code, tl, audit = await check()

    # 重写后仍不合格：走和整片同一条兜底（叙事片换那三套剪影，动效补色带/通用镜头），
    # 不把问题交回给用户。
    if audit.get("ok") and audit.get("bad"):
        if story:
            bodies[i] = safe_story_scene(scene, index=i)
        else:
            hue = str(scene.get("hue") or "#FF6B2C")
            bodies[i] = (_inject_ribbon(bodies[i], hue)
                         if audit["shots"][0]["worst"] == "flat"
                         else safe_scene(str(scene.get("text") or ""), hue, index=i))
        code, tl, audit = await check()

    return {"code": code, "bodies": bodies, "timeline": tl, "audit": audit,
            "shot": int(shot_no), "secs": secs, "kinds": kinds}


