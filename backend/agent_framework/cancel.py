# -*- coding: utf-8 -*-
"""用户点「停止」：让正在跑的那一轮停在最近一致点。

**为什么不能走 MQ 投一帧 action**：同会话是**串行**的（`SessionDispatcher` 一条
session 一个 actor，一条跑完才取下一条）。一帧 `action.op="cancel"` 会排在正在跑的
那条 run **后面**，等它跑完才被读到——那时已经没有什么可停的了。所以停止必须是
**带外**的：HTTP 端点直接在本进程的内存里立一个 `asyncio.Event`，执行循环在几个
天然的检查点上自己看一眼。

**为什么是「停在一致点」而不是「立刻掐死」**：
* 一致点由 `checkpoint.save_progress` 定义（每次调模型之前落一次盘）。停在它上面，
  续跑就是「重放消息 + 跳过已完成的工具」，与崩溃恢复走同一条路——不需要第二套恢复逻辑。
* 在途的那一次工具调用没有回执，续跑时会被当成「上次没跑完」重放。这是对的：
  扔掉的只是那一次调用的**等待**，不是它前面已经落地的产物。

**内存登记表的代价**：多副本部署时，取消请求落在 A 实例、run 跑在 B 实例上就没用。
今天的部署是单应用进程（HTTP 与 worker 同一个容器），够用；真要多副本，这张表得换成
库里的一个标志位 + 执行循环轮询它。写在 docstring 里而不是假装没有。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Awaitable, TypeVar

T = TypeVar("T")

#: run_id → 停止请求。只有「已经被用户点过停止」的 run 才有条目。
_EVENTS: dict[str, asyncio.Event] = {}


class RunCancelled(Exception):
    """这一轮被用户要求停止。

    刻意**不是** `ToolError` 也不是 `asyncio.CancelledError`：前者会被当成「工具失败」
    回喂给模型继续跑，后者会被 `SessionDispatcher` 当成任务被撤销。它是一条独立的
    收尾路径——状态落 `cancelled`，终答如实说「已停下、可以从这里继续」。
    """

    def __init__(self, run_id: str = "") -> None:
        super().__init__(f"run {run_id or '?'} 已被用户停止")
        self.run_id = run_id


def _event(run_id: str) -> asyncio.Event | None:
    """取（没有就建）某个 run 的停止位；run_id 为空时回 None。

    **必须「没有就建」而不是「没有就跳过」**：点了停止之后才去登记，是常事——
    工具在 t=0 开始跑、用户在 t=30 秒点停止。若 guard 在 t=0 看到「表里没有」就退回
    普通 wait_for，那一次长调用会一直跑到自己结束，停止要等它回来才生效（实测 5 秒的
    夹具就是这么跑满的）。位子在开始等的时候就建好，停止请求随时都能点亮它。
    """
    rid = str(run_id or "")
    if not rid:
        return None
    ev = _EVENTS.get(rid)
    if ev is None:
        ev = asyncio.Event()
        _EVENTS[rid] = ev
    return ev


def request(run_id: str) -> bool:
    """立起停止请求；返回 True 表示这是第一次请求（之前没立过）。"""
    ev = _event(run_id)
    if ev is None:
        return False
    first = not ev.is_set()
    ev.set()
    return first


def clear(run_id: str) -> None:
    """撤掉登记（run 收尾时调）。不清的话，同一个 run_id 下次续跑一进循环就停。"""
    _EVENTS.pop(str(run_id or ""), None)


def is_cancelled(run_id: str) -> bool:
    ev = _EVENTS.get(str(run_id or ""))
    return ev is not None and ev.is_set()


def pending() -> list[str]:
    """当前立着停止请求的 run（观测/测试用）。"""
    return [rid for rid, ev in _EVENTS.items() if ev.is_set()]


async def guard(run_id: str, awaitable: Awaitable[T], *,
                timeout: float | None = None) -> T:
    """跑一件可能很久的事，同时盯着停止请求。

    语义与 `asyncio.wait_for` 相同（超时抛 `asyncio.TimeoutError`），只多一条：
    停止请求一到就撤销那件事并抛 `RunCancelled`。

    **工具「真跑完」优先于「刚好也被点了停止」**：两者同时到点时返回工具结果——
    它的副作用（提交出去的渲染、写下的文件）已经在世上，扔掉结果只会让续跑时
    重放一遍、再提交一次。
    """
    task = asyncio.ensure_future(awaitable)
    ev = _event(run_id)
    if ev is None:
        return await asyncio.wait_for(task, timeout=timeout)
    stop = asyncio.ensure_future(ev.wait())
    try:
        done, _ = await asyncio.wait(
            {task, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        # 停止请求先到（或超时）：把工具协程撤掉，别让它继续在后台跑。
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if stop in done:
            raise RunCancelled(str(run_id or ""))
        raise asyncio.TimeoutError()
    finally:
        stop.cancel()
        if not task.done():  # 兜底：wait 自己被取消时别把工具协程留成孤儿
            task.cancel()
            with contextlib.suppress(BaseException):
                await task


def _reset_for_tests() -> None:
    _EVENTS.clear()


__all__ = ["RunCancelled", "request", "clear", "is_cancelled", "pending", "guard"]
