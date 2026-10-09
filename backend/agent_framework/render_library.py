"""成片入库：渲染到终态就把这一版片子登记进素材库（``materials``）。

为什么是渲染节点自己登记，而不是让前端去拉一遍：``materials`` 是这套系统里唯一
「不随对话消失」的持久层（``owner_user_id`` 是用户级，``conv_id`` 在对话删除时置 NULL），
而「这一版字节确实烧出来了」的确切时刻只有渲染终态知道。登记晚一步，
删对话就可能赶在它前面，把这版片子当会话垃圾清掉（回收口径见 ``session_gc``）。

入库失败**不改判这次渲染**：片子已经发布到对象存储、终态行已经写了 done，
素材库少一行只是少一个「以后还能在库里找到它」的入口——所以只回一句说明，
调用方把它并进 ``notes`` 对用户明说，不许静默吞掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .identity import parse_storyline_session

ORIGIN = "render"


def _filename_of(title: str, object_key: str) -> str:
    """库里展示的文件名：优先用这次的标题，标题里的路径分隔符换掉（它只是展示名）。"""
    base = (title or "").strip() or Path(object_key).stem
    for ch in '/\\:*?"<>|\n\r\t':
        base = base.replace(ch, "_")
    return f"{base[:80] or 'render'}.mp4"


async def register_render(storage: Any, session_id: str, object_key: str, *,
                          title: str = "",
                          duration_sec: float | None = None) -> tuple[str, str]:
    """登记成片，回 ``(material_id, 说明)``——说明非空表示这次没入库（不改判渲染成败）。"""
    user_id, conv_id = parse_storyline_session(session_id)
    if not user_id:
        # 离线直调的会话键（sess-*）没有归属人，外键会直接炸——如实说明，不硬造一个 owner
        return "", f"会话键 {session_id!r} 拆不出用户，这版成片没入素材库"
    try:
        info = await storage.objects.head(object_key)
        if info is None:
            return "", f"成片对象不在库里（{object_key}），素材库这一行没登记"
        row = await storage.materials.db.select(
            storage.materials.table, where={"object_key": object_key}, limit=1)
        values = {"filename": _filename_of(title, object_key),
                  "bytes": info.bytes, "sha256": info.sha256 or "",
                  "duration_sec": duration_sec}
        if row:
            # 同一个对象键再烧一次（整片重渲、局部改后重发）：改那一行，不插第二条——
            # object_key 上有唯一约束，插第二条必炸 IntegrityConflict。
            await storage.materials.db.update(
                storage.materials.table, values, where={"id": row[0]["id"]})
            return str(row[0]["id"]), ""
        got = await storage.materials.register(
            user_id, conv_id, object_key, values["filename"], "video",
            bytes_=values["bytes"], sha256=values["sha256"], mime="video/mp4",
            duration_sec=duration_sec, has_audio=True, origin=ORIGIN)
        return str(got["id"]), ""
    except Exception as exc:                        # noqa: BLE001 - 入库失败不牵连出片
        return "", f"成片入素材库失败（{type(exc).__name__}: {str(exc)[:120]}）"
