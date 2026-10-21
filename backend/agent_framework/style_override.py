# -*- coding: utf-8 -*-
"""这一轮/这个会话里被对话改写过的风格包名。

为什么不能只靠身份里的 contextvar：工具调用走 ``asyncio.gather``，每个协程在
**自己的上下文副本**里跑；工具里 ``set`` 的 contextvar 出了那个任务就没了，而
`plan_motion` 是下一轮才发出的 MCP 调用，读不到。这个进程内的表按会话键记，
MCP 包装层在注入 ``style_skill`` 时优先读它。

生命周期：进程内、按 ``{user}:{conv}`` 键；最多 512 个会话，超出丢最早的一条。
进程重启后为空——前端会把「当前风格」随下一条消息带回来（它收到过 style 事件），
所以不需要落库，也不会在重启后把风格悄悄换掉。
"""
from __future__ import annotations

from typing import Any

_STYLE: dict[str, str] = {}
_MAX_SESSIONS = 512


def set_style_override(session_id: str, style_skill: str) -> None:
    sid = str(session_id or "")
    if not sid:
        return
    if sid not in _STYLE and len(_STYLE) >= _MAX_SESSIONS:
        # 最早插入的那条先丢：dict 保持插入顺序，不额外引依赖。
        _STYLE.pop(next(iter(_STYLE)), None)
    _STYLE[sid] = str(style_skill or "")


def get_style_override(session_id: str) -> str | None:
    """没有改写记录回 None（区别于「用户明确说用回默认」的空串）。"""
    return _STYLE.get(str(session_id or ""))


def clear_style_override(session_id: str) -> None:
    """忘掉这个会话的改写记录：回到「按本轮请求带的值」的口径。"""
    _STYLE.pop(str(session_id or ""), None)


def effective_style_skill(session_id: str, from_identity: Any) -> str:
    """MCP 注入用的最终值：对话里改过的优先，否则用本轮请求带的值。"""
    got = get_style_override(session_id)
    return str(from_identity or "") if got is None else got
