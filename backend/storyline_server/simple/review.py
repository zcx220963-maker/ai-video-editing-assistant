# -*- coding: utf-8 -*-
"""视觉自检：把这一帧渲出来**给写手自己看**。

为什么这一步是关键：模型看不到自己画过什么，所以第 3 镜又画了一个落日——
实测 v12 指纹 15/15 各不相同，但抽帧是「城市剪影 + 大落日」反复出现。
它缺的不是画法，是**回头看自己画了什么**。这一轮就是那个回头。

从 `story.py` 拆出来（2026-10-25）：story 只该管"怎么画一镜"，
"怎么看自己画的"是另一个关注点。
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import kernel
from .kernel import KernelError

# ---------------------------------------------------------------------------
# 视觉自检：把这一帧渲出来**给写手自己看**
# ---------------------------------------------------------------------------
#
# 为什么这一步是关键：模型看不到自己画过什么，所以第 3 镜又画了一个落日——
# 实测 v12 指纹 15/15 各不相同，但抽帧是「城市剪影 + 大落日」反复出现。
# 它缺的不是画法，是**回头看自己画了什么**。这一轮就是那个回头。
#
# 代价：每镜 +1 次带图调用。15 镜多 15 次、静帧约 30 秒。

#: 关掉它就能退回"不看只判像素"的老行为（排查用）。
STORY_VISION_REVIEW = True

_FRAME_REVIEW_SYSTEM = """你看的是自己刚画的一帧。只回一句话：要不要改、改什么。

按重要性判：
1. 有没有**一件能被指着说的东西**（望远镜/人影/城市/行星/飞船/控制台）？
   只有渐变和色块 → 要改。
2. 整帧是不是**几乎全黑**、看不清主体轮廓 → 要改。
3. 和「前面几镜」是不是**同一个母题**（又是城市+落日）→ 要改：换地点、换尺度、换时间。
4. 字幕压的地方读得清吗？白字压在亮部 → 要改。

要改就回：`改：<一句具体改法，不超过 30 字>`
不用改就回：`过`
不要解释、不要客套、不要分点。"""


async def review_frame(jpeg_b64: str, *, prev: list[str], llm: Any = None,
                       user_id: str = "", conversation_id: str = "") -> str:
    """让写手看自己画的一帧。返回 ``"改：…"`` 或 ``""``（通过）。

    走的是**带图的一次调用**（`image_url` + base64）——这条通道本仓验过
    （`agent_framework/model_probe.py` 的 `probe_vision`），不是猜的。
    """
    if not jpeg_b64:
        return ""
    seen = "；".join(x for x in prev[-3:] if x) or "（这是第一镜）"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _FRAME_REVIEW_SYSTEM},
        {"role": "user", "content": [
            {"type": "text",
             "text": f"前面几镜画的是：{seen}\n这一帧（就是刚写的那一镜）："},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
        ]},
    ]
    try:
        raw = await kernel._complete(messages, llm=llm, user_id=user_id,
                                     conversation_id=conversation_id)
    except Exception:  # noqa: BLE001 —— 看不了就不看，别把整条拖垮
        return ""
    txt = str(raw or "").strip().replace("\n", " ")
    if not txt or txt.startswith("过"):
        return ""
    return txt[:120]


async def review_shots(code: str, scenes: list[dict[str, Any]], tl: dict[str, Any], *,
                       aspect: str, fps: int, llm: Any = None, user_id: str = "",
                       conversation_id: str = "",
                       on_progress: Any = None) -> dict[int, str]:
    """逐镜渲一帧、逐镜问"要不要改"。返回 ``{镜号: 改法}``（只含要改的）。

    并发 3：带图调用比纯文本慢，串行 15 镜要一分多钟。
    """
    from .film import shot_stills   # 延迟 import：避免模块级循环

    out: dict[int, str] = {}
    sem = asyncio.Semaphore(3)
    n = len(scenes)

    async def one(i: int) -> None:
        async with sem:
            try:
                frames = await shot_stills(code, tl, i, aspect=aspect, fps=fps,
                                           at=(0.55,), scale=0.35)
            except KernelError:
                return
            if not frames:
                return
            prev = [str(scenes[j].get("plate") or "") for j in range(max(0, i - 2), i)]
            hint = await review_frame(frames[0], prev=prev, llm=llm,
                                      user_id=user_id, conversation_id=conversation_id)
            if hint:
                out[i + 1] = hint
            if on_progress:
                try:
                    on_progress(len(out), n)
                except Exception:  # noqa: BLE001
                    pass

    await asyncio.gather(*[one(i) for i in range(n)])
    return out

async def apply_review(code: str, tl: dict[str, Any], bodies: list[str],
                       scenes: list[dict[str, Any]], secs: list[float],
                       kinds: list[str], *, aspect: str, fps: int,
                       llm: Any = None, user_id: str = "",
                       conversation_id: str = "",
                       on_progress: Any = None, motif: str = "",
                       max_fix: int = 8) -> tuple[str, dict[str, Any], list[int]]:
    """看完 → 改 → 重新拼装。返回 ``(新代码, 新时间表, 改过的镜号)``。

    只重写"自己说有问题"的那几镜，最多 8 镜（再多就该重做分镜了）。
    `assemble` 从 film 延迟 import——film 在模块级 import 本文件，这里不能反过来。
    """
    from .film import assemble                      # 延迟：避免模块级循环
    from .scene import wrap_story_scene
    from .story import write_scene_story_code

    try:
        notes = await review_shots(code, scenes, tl, aspect=aspect, fps=fps,
                                   llm=llm, user_id=user_id,
                                   conversation_id=conversation_id,
                                   on_progress=on_progress)
    except Exception:  # noqa: BLE001 —— 看不了就不看
        return code, tl, []
    if not notes:
        return code, tl, []

    fixed: list[int] = []
    for shot_no, hint in list(notes.items())[:max_fix]:
        i = shot_no - 1
        try:
            body = await write_scene_story_code(
                scenes[i], index=i, total=len(scenes), seconds=secs[i],
                aspect=aspect, llm=llm, user_id=user_id,
                conversation_id=conversation_id, motif=motif,
                extra=f"你自己看过这一帧，说：{hint}")
            bodies[i] = wrap_story_scene(body, scenes[i], index=i)
            fixed.append(shot_no)
        except KernelError:
            pass
    if not fixed:
        return code, tl, []
    code, tl = assemble(bodies, secs, kinds, fps=fps)
    return code, tl, fixed
