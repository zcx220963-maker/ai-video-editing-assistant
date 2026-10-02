"""计划卡「当轮落库」的 contextvar 通道（与 ``media_replay`` 同一条形状）。

``PlanCardHook.after_execute_tools`` 把本轮通过四重校验的候选计划登记在这里，
``MessagesRepo.append(role=assistant)`` 落库时 drain 进 ``qa.parts``
（新增 ``{"type": "plan", ...}`` 片段），于是刷新后 /convs/{id}/messages 能把
**还没确认**的那张计划卡原样重放出来——确认入口不能只活在那一帧 WS 里。

与成片卡同样刻意只放 contextvar 与纯函数：既不 import storage 也不 import hooks，
供两侧各自**单向**依赖（``plan_gate`` 从这里 re-export，调用方无需知道本模块存在）。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Mapping, Sequence

_pending: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "pending_plan_cards", default=None
)
_pending_warnings: ContextVar[list[str] | None] = ContextVar(
    "pending_plan_warnings", default=None
)


def record_plan_card(plans: Sequence[Mapping[str, Any]],
                     warnings: Sequence[str] | None = None) -> None:
    """本轮待落库的候选计划（已经过服务端校验的那一份）与校验警告。

    警告是**一轮**级别的（哪一步被降级为不可跳之类），卡片是按版本一条条记的，
    所以另开一条缓冲，落库时由 ``drain_plan_round`` 一起取走挂成片段级字段。
    """
    buf = _pending.get()
    if buf is None:
        buf = []
        _pending.set(buf)
    buf.extend(dict(p) for p in plans)
    if warnings:
        got = list(_pending_warnings.get() or [])
        got.extend(str(w) for w in warnings)
        _pending_warnings.set(got)


def drain_plan_round() -> tuple[list[dict[str, Any]], list[str]]:
    """取出并清空本轮的（候选计划, 校验警告）——落库方要用这个，别只取一半。"""
    cards, warnings = _pending.get(), _pending_warnings.get()
    _pending.set(None)
    _pending_warnings.set(None)
    return cards or [], list(warnings or [])


def drain_plan_cards() -> list[dict[str, Any]]:
    """取出并清空：一次规划产出的卡片只归一条 assistant 消息。"""
    return drain_plan_round()[0]


def reset_plan_cards() -> None:
    """新一轮开始（落 user 行）时清空，兜住上一轮异常未 drain 的残留。"""
    _pending.set(None)
    _pending_warnings.set(None)
