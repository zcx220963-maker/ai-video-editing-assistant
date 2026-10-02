"""成片卡「当轮落库」的最小持久链接（README §6 已知缺口的修复面）。

背景：MediaCardHook 只在渲染完成的**当轮**把 ``media_url`` 经 MQ OutBound 回投前端播放
（见 hooks.py）。这条链接是**临时的 presigned 直链**，历史上没有任何一行把「某条
assistant 消息」与「那一次成功渲染的成片」对上的持久键——刷新后重放历史，附件能回填，
成片播放卡回不来。

本模块只提供一条 **contextvar 通道**，把「当轮渲染出的成片指针」带到该轮 assistant 行
落库时并入 ``qa.parts``（新增 ``{"type": "media", ...}`` 片段）：
    MediaCardHook.after_execute_tools  →  record_rendered_media(...)
    MessagesRepo.append(role=assistant) →  drain_rendered_media()  并入 qa
    MessagesRepo.append(role=user)      →  reset_rendered_media()  兜住上一轮残留

这里刻意只放 contextvar 与纯函数，既不 import storage、也不 import hooks，供两侧各自
**单向**依赖，避免 repositories↔hooks 的反向引用。持久链接只存**对象键**（稳定），
presigned 直链在读取时现签（有效期到下一轮也不失效），与 /upload 附件的展示回投一致。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# 当前任务内「本轮待落库的成片链接」缓冲。asyncio 每个 Task 进入时复制 context，
# 消费者每条消息各起一个 Task，天然按消息隔离；同轮内 record→append 是同一条 await 链，
# 值随协程向前传播（与 identity.py 用 contextvar 传身份的既有约定同构）。
_pending: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "pending_render_media", default=None
)


def record_rendered_media(card: dict[str, Any]) -> None:
    """渲染成功当轮登记一条成片持久链接（对象键 + 元信息），供本轮 assistant 行 drain。"""
    buf = _pending.get()
    if buf is None:
        buf = []
        _pending.set(buf)
    buf.append(dict(card))


def drain_rendered_media() -> list[dict[str, Any]]:
    """取出并清空当前挂起的成片链接。

    落 assistant 行时调用：drain 语义保证一次渲染只归一条消息，取完即空。
    """
    buf = _pending.get()
    _pending.set(None)
    return buf or []


def reset_rendered_media() -> None:
    """新一轮开始（落 user 行）时清空，兜住上一轮异常未 drain 的残留。"""
    _pending.set(None)
