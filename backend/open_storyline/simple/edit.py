# -*- coding: utf-8 -*-
"""改一镜：只重写这一镜，重新拼装，只复核这一镜。

为什么改一镜值得单独一个文件：它是「Claude + Remotion 那种工作流」的实质——
「看静帧 → 说哪一镜不对 → 只改那一镜 → 再渲」。整片重跑是 24 次调用 + 15 分钟，
而且重跑出来的第 7 镜还是另一个样子；用户说的「把那个太阳改小一点」，
只有在能只改一镜的时候才成立。

**2026-10-26 改成统一路径**：原来这里按 `mode == "story"` 分两支，
叙事片走 `write_scene_story_code`、动效片走 `write_scene`。取消片种之后
新片的 `mode` 是 `"auto"`，那个判断恒为假 → 叙事片会被**动效写手**重画，
出来是一帧几何色块混在剪影叙事里（这条注释原本就在警告这个失败）。
现在只有一条路：与出片时同一个 `showrunner.write_shot`。

**方向是单向的**：本文件 import film，film 不 import 本文件——所以没有循环。
调用方（`agent_framework/tools/short_video.py`）直接 import 这里。
"""

from __future__ import annotations

from typing import Any

from .film import KernelError, assemble
from .kernel import DEFAULT_ASPECT, DEFAULT_FPS, norm_aspect
from .morph import parse_motif_at
from .scene import safe_story_scene, wrap_story_scene
from .showrunner import physical, shot_evidence, write_shot


def _audit_of(rows: list[dict[str, Any]], focus: int) -> dict[str, Any]:
    """证据 → 与旧 `qa.audit` 同形的账（`reshoot_shot` 与前端都读这个形状）。"""
    out: list[dict[str, Any]] = []
    for e in rows:
        r = e.get("readings") or {}
        out.append({
            "index": int(e["index"]),
            "content": r.get("content"), "vivid": r.get("vivid"),
            "mean_lum": r.get("mean_lum"), "edge": r.get("edge"),
            "readings": r, "verdicts": [physical(e)], "worst": physical(e),
        })
    out.sort(key=lambda x: (x["index"] != focus, x["index"]))
    bad = [x["index"] for x in out if x["worst"] != "ok"]
    return {"ok": bool(out), "shots": out, "bad": bad,
            "ratio": round((len(out) - len(bad)) / max(1, len(out)), 3),
            "frames": sum(int(e.get("frames") or 0) for e in rows)}


async def reshoot(proj: dict[str, Any], shot_no: int, *, note: str = "",
                  llm: Any = None, user_id: str = "",
                  conversation_id: str = "") -> dict[str, Any]:
    """只重写一镜，重新拼装，并**只复核这一镜**。

    代价：1 次模型调用 + 1 张静帧。整片重跑是 24 次调用 + 15 分钟，
    而且重跑出来的第 7 镜还是另一个样子。

    `note` 是用户的原话（"太阳太大、挡住字了"）。它会被原样放进提示词的
    "上一版的问题"那一栏——**不要翻译成术语**，模型对用户原话的理解比对转述准。
    """
    bodies = list(proj.get("bodies") or [])
    plan = proj.get("plan") or {}
    scenes = list(plan.get("scenes") or [])
    n = len(bodies)
    if not 1 <= int(shot_no) <= n:
        raise KernelError("reshoot", f"镜号要在 1~{n} 之间（收到 {shot_no}）")

    i = int(shot_no) - 1
    secs = [float(s) for s in proj.get("secs") or []]
    kinds = list(proj.get("kinds") or [])
    aspect = norm_aspect(proj.get("aspect") or DEFAULT_ASPECT)
    fps = int(proj.get("fps") or DEFAULT_FPS)
    scene = scenes[i] if i < len(scenes) else {}
    tone = str(plan.get("tone") or "")
    motif = str(plan.get("motif") or "")

    hint = (note or "").strip() or (
        "上一版这一镜不够好，重画一遍：换一个构图，主体再大一些、层次再多一层。")
    body = await write_shot(scene, index=i, total=n, tone=tone, shots=scenes,
                            aspect=aspect, llm=llm, user_id=user_id,
                            conversation_id=conversation_id, extra=hint,
                            motif=motif)
    bodies[i] = wrap_story_scene(body, scene, index=i)

    def rebuild() -> tuple[str, dict[str, Any]]:
        """重新拼装。要**重读锚点**——重写过的镜位置变了，沿用旧锚点会接空。

        `fx` 也要跟着工程走（2026-10-27）：不传就落到 `DEFAULT_FX`，
        于是"改一镜"会顺手把这条片子的扫光/白闪换成颗粒+暗角——改一镜不该改整片观感。
        """
        return assemble(bodies, secs, kinds, fps=fps,
                        motifs=[parse_motif_at(b) for b in bodies],
                        fx=plan.get("fx"))

    async def check() -> tuple[str, dict[str, Any], dict[str, Any]]:
        code, tl = rebuild()
        ev = await shot_evidence(code, secs, aspect=aspect, fps=fps)
        return code, tl, _audit_of(ev, int(shot_no))

    code, tl, audit = await check()

    # 重写后仍是"没渲出来"（全黑/全白）：换成一定画得出来的那三套剪影，
    # 不把问题交回给用户。注意这里**只兜物理失败**——"不够好看"归评审管，
    # 一次重画之后就交给用户判断，不无限重试。
    if audit["bad"]:
        bodies[i] = safe_story_scene(scene, index=i)
        code, tl, audit = await check()

    return {"code": code, "bodies": bodies, "timeline": tl, "audit": audit,
            "shot": int(shot_no), "secs": secs, "kinds": kinds}
