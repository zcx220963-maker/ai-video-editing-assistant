"""Session 级并发调度：把「分区」和「并发单元」拆开。

设计原文 §3/§4 的模型是：**很多 session 按 hash 落到有限 Partition，但同一 session
串行、不同 session 并行**。这两件事不是一回事——

  * Partition 是**路由**单位（决定消息由哪个 reader 读、offset 怎么提交）；
  * Session 是**并发**单位（决定谁跟谁排队）。

旧实现把两者绑死了：每个分区一个 worker、分区内 `await handler`。后果是同分区里
A1 在跑 20 分钟的渲染，同分区的 C1 只能干等——设计图里 A1 与 C1 恰恰都在
Partition 0，却要求它们并行。

本模块只做一件事：每个 session 一条串行队列 + 一个 actor 协程，全局并发数用
semaphore 封顶。Partition reader 读到消息**不 await**，`submit()` 进去就返回，
完成时回调 `on_done`——offset 水位就靠这个回调推进（见 `mq.KafkaMessageQueue`）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("mq")

Handler = Callable[[dict[str, Any]], "Awaitable[None] | None"]
DoneCallback = Callable[[], None]


def session_of(payload: dict[str, Any]) -> str:
    """从载荷取会话键：优先 session_id，否则用 user_id:conversation_id 拼。

    与 `identity.session_key` 同一个形状；这里不 import 它，是为了让 mq 层
    不依赖 identity（离线测试可以只测 MQ，不用起身份层）。
    """
    sid = payload.get("session_id")
    if sid:
        return str(sid)
    return f"{payload.get('user_id', '')}:{payload.get('conversation_id', '')}"


class QueueFull(Exception):
    """该 session 的待处理队列已满——调用方应落死信并 ack，而不是阻塞分区。"""


class SessionDispatcher:
    """per-session 串行 actor + 全局并发上限。

    参数口径：
      * ``max_concurrent``：同时活跃的 session 数上限（默认 32）。这不是「线程数」，
        而是「最多几个 session 在同时调模型」——再高只会把上游打到限流。
      * ``queue_max``：单个 session 的待处理消息上限（默认 32）。超界抛
        `QueueFull`，由调用方落死信并 ack，否则整个分区会被一条坏消息卡死。
      * ``idle_timeout``：actor 空闲多久回收（默认 60s）。不回收的话，一天下来
        会攒出几十万个空协程。
    """

    def __init__(
        self,
        handler: Handler,
        *,
        max_concurrent: int = 32,
        queue_max: int = 32,
        idle_timeout: float = 60.0,
    ) -> None:
        self._handler = handler
        self._max_concurrent = max(1, int(max_concurrent))
        self._queue_max = max(1, int(queue_max))
        self._idle_timeout = float(idle_timeout)
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._queues: dict[str, asyncio.Queue] = {}
        self._actors: dict[str, asyncio.Task] = {}
        self._running = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for t in list(self._actors.values()):
            t.cancel()
        for t in list(self._actors.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._actors.clear()
        self._queues.clear()

    async def drain(self, timeout: float = 5.0) -> None:
        """等所有已提交消息跑完（测试与 `--drain` 用）。"""
        await asyncio.wait_for(self._idle.wait(), timeout)

    # ---- 提交 ----

    async def submit(
        self, payload: dict[str, Any], on_done: DoneCallback | None = None,
    ) -> None:
        """把一条消息塞进它所属 session 的队列；满了抛 `QueueFull`。

        注意本方法**不等待 handler 跑完**——这正是分区 reader 不被长任务阻塞的前提。
        """
        sid = session_of(payload)
        q = self._queues.get(sid)
        if q is None:
            q = asyncio.Queue()
            self._queues[sid] = q
            self._actors[sid] = asyncio.create_task(self._actor(sid, q))
        if q.qsize() >= self._queue_max:
            raise QueueFull(sid)
        async with self._lock:
            self._inflight += 1
            self._idle.clear()
        await q.put((payload, on_done))

    async def _actor(self, sid: str, q: asyncio.Queue) -> None:
        """单个 session 的串行消费者：一条跑完才取下一条。"""
        try:
            while self._running:
                try:
                    payload, on_done = await asyncio.wait_for(
                        q.get(), timeout=self._idle_timeout)
                except asyncio.TimeoutError:
                    return              # 空闲回收：队列空且长时间没消息
                try:
                    async with self._sem:
                        result = self._handler(payload)
                        if asyncio.iscoroutine(result):
                            await result
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  单条失败不拖垮整个 session
                    log.exception(
                        "handler 失败（session=%s）：run_id=%s", sid, payload.get("run_id"))
                finally:
                    if on_done is not None:
                        try:
                            on_done()
                        except Exception:  # noqa: BLE001  ack 回调本身不该抛
                            log.exception("on_done 回调失败（session=%s）", sid)
                    async with self._lock:
                        self._inflight -= 1
                        if self._inflight <= 0:
                            self._idle.set()
        finally:
            self._queues.pop(sid, None)
            self._actors.pop(sid, None)

    # ---- 观测 ----

    @property
    def active_sessions(self) -> int:
        return len(self._actors)

    @property
    def pending(self) -> int:
        return sum(q.qsize() for q in self._queues.values())
