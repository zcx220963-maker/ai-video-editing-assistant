"""Agent Worker：外层 `while True` 的那一层（设计原文 §8）。

设计把 Agent 分成两层循环：

    外层 Main Loop   while True   ← 本文件。不断取消息、分派、跑完再取下一条
    内层 Once Loop   有最大迭代  ← agent.AgentOnceRun。单次请求的 ReAct

旧代码里外层循环散在 `mq._partition_worker` 与 `consumer.SessionConsumer` 之间，
读者很难看出「主循环到底在哪」。这里把它收成一个有名字的对象：持有 MQ 订阅、
生命周期、以及「为什么停」的原因枚举。

它**不实现并发**——并发在 `dispatch.SessionDispatcher`（per-session 串行 actor）。
Worker 只负责「订阅 → 启动 → 等停 → 关掉」，这样两者各自可测。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, Awaitable, Callable

from .mq import MessageQueue

log = logging.getLogger("agent.worker")

Handler = Callable[[dict[str, Any]], "Awaitable[None] | None"]


class StopReason(str, Enum):
    """Worker 停下来的原因。写进日志与 `/status`，省得只看「进程没了」。"""

    SHUTDOWN = "shutdown"          # 正常收尾
    ERROR = "error"                # 主循环抛错
    BROKER_DOWN = "broker_down"    # MQ 起不来（Kafka 不可达等）
    CANCELLED = "cancelled"        # 被外部取消


class AgentWorker:
    """订阅 `chat`、跑外层循环、暴露 start/stop/drain。

    用法::

        worker = AgentWorker(mq, handler=consumer.handle)
        await worker.start()      # 非阻塞：订阅后立刻返回，循环在 MQ 里跑
        ...
        await worker.stop()
    """

    def __init__(
        self,
        mq: MessageQueue,
        *,
        handler: Handler,
        topic: str = "chat",
        group: str = "agent",
    ) -> None:
        self.mq = mq
        self.handler = handler
        self.topic = topic
        self.group = group
        self.stop_reason: StopReason | None = None
        self._started = False

    def subscribe(self) -> None:
        """注册消费端（幂等）。"""
        self.mq.subscribe(self.topic, self.group, self.handler)

    async def start(self) -> None:
        if self._started:
            return
        self.subscribe()
        try:
            await self.mq.start()
        except Exception as exc:  # noqa: BLE001  broker 不可达要给出可动作的错误
            self.stop_reason = StopReason.BROKER_DOWN
            raise RuntimeError(
                f"MQ 启动失败（topic={self.topic}）：{exc}。"
                "生产请检查 KAFKA_BOOTSTRAP_SERVERS / broker 健康；"
                "本地单进程请显式加 --mq memory。") from exc
        self._started = True

    async def drain(self, timeout: float = 5.0) -> None:
        drain = getattr(self.mq, "drain", None)
        if drain is not None:
            await drain(timeout)

    async def stop(self, reason: StopReason = StopReason.SHUTDOWN) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        if not self._started:
            return
        try:
            await self.mq.stop()
        except Exception:  # noqa: BLE001  关停失败不该掩盖真正原因
            log.exception("MQ 关停失败")
        finally:
            self._started = False

    async def run_forever(self) -> None:
        """给「想让 Worker 自己撑住进程」的调用方用：起完就等取消。"""
        await self.start()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.stop_reason = StopReason.CANCELLED
            raise
        finally:
            await self.stop()
