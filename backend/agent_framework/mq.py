"""消息队列抽象层（对应架构图「Message Queue → 会话路由」）。

职责：把 Web 层收到的请求投递给 Agent 消费端，并用 key=session_id 承载分区语义——
**同一 Session 有序（串行）、不同 Session 并行**，这正是生产环境 MQ 的
session_id → Partition 路由（见 agent.py 的注释）。

两种实现，接口一致，装配时按 backend 切换：
  - KafkaMessageQueue：真实 Kafka（aiokafka），producer 按 key 路由到分区，
    consumer group 消费。生产用。
  - InMemoryMessageQueue：进程内替身，保留同样的 per-key 有序 / 跨 key 并行语义，
    不需要 broker，供离线单测与本地最小闭环。

消息载荷约定为 dict：{"user_id","conversation_id","message","run_id"}。
"""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

# handler(payload) -> None | awaitable
Handler = Callable[[dict[str, Any]], "Awaitable[None] | None"]

log = logging.getLogger("mq")


class MessageQueue(ABC):
    """MQ 接口：publish 投递、subscribe 注册消费端、start/stop 生命周期。"""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, group: str, handler: Handler) -> None: ...


class InMemoryMessageQueue(MessageQueue):
    """进程内 MQ：复刻文档的「很多 session → 按 session_id hash → 有限 Partition」模型。

    每个 (topic, group) 有固定 num_partitions 条分区队列；publish 按 key(crc32) 取模
    落分区。**分区内严格 FIFO 串行**（同 key 必同分区 → 同 key 有序；同分区的不同
    session 也串行，与 Kafka 单分区单消费者语义一致），**分区之间并发**（异 session 并行）。
    """

    def __init__(self, num_partitions: int = 4) -> None:
        self.num_partitions = num_partitions
        self._subs: dict[tuple[str, str], Handler] = {}
        self._queues: dict[tuple[tuple[str, str], int], asyncio.Queue] = {}
        self._tasks: dict[tuple[tuple[str, str], int], asyncio.Task] = {}
        self._running = False
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._lock = asyncio.Lock()

    def partition_of(self, key: str) -> int:
        """稳定的 key→分区 映射（不用内置 hash：字符串 hash 每进程随机化）。"""
        return zlib.crc32(key.encode("utf-8")) % self.num_partitions

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        sub = (topic, group)
        self._subs[sub] = handler
        for p in range(self.num_partitions):
            self._queues.setdefault((sub, p), asyncio.Queue())

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        p = self.partition_of(key)
        for (t, g) in self._subs:
            if t == topic:
                async with self._lock:
                    self._inflight += 1
                    self._idle.clear()
                await self._queues[((t, g), p)].put(dict(payload))

    async def _partition_worker(self, sub: tuple[str, str], p: int) -> None:
        handler = self._subs[sub]
        q = self._queues[(sub, p)]
        while True:
            payload = await q.get()
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  单条失败不应中断该分区
                # 但必须留下痕迹：run 失败的真正原因只有这里知道，而 OutBound 的
                # error 帧只推给挂了 WS 的会话——没挂就整条形同消失。
                log.exception("handler 失败（topic=%s group=%s partition=%s）：run_id=%s",
                              sub[0], sub[1], p, payload.get("run_id"))
            finally:
                async with self._lock:
                    self._inflight -= 1
                    if self._inflight <= 0:
                        self._idle.set()

    async def start(self) -> None:
        self._running = True
        for sub in self._subs:
            for p in range(self.num_partitions):
                self._tasks[(sub, p)] = asyncio.create_task(self._partition_worker(sub, p))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks.values():
            t.cancel()
        for t in self._tasks.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    async def drain(self, timeout: float = 2.0) -> None:
        """等待当前所有已投递消息处理完毕（测试 / 同步语义用）。"""
        await asyncio.wait_for(self._idle.wait(), timeout)


class KafkaMessageQueue(MessageQueue):
    """真实 Kafka 实现（aiokafka）。key=session_id 决定分区，天然有序/并行。

    aiokafka 延迟导入：未安装或未连 broker 时，import 本模块不报错，只有真正
    start()/publish() 才需要 broker。
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str = "localhost:9092",
        client_id: str = "agent-framework",
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.client_id = client_id
        self._subs: list[tuple[str, str, Handler]] = []
        self._producer: Any | None = None
        self._consumers: list[Any] = []
        self._tasks: list[asyncio.Task] = []

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        self._subs.append((topic, group, handler))

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers)
        await self._producer.start()
        for topic, group, handler in self._subs:
            self._tasks.append(asyncio.create_task(self._consume(topic, group, handler)))

    async def _consume(self, topic: str, group: str, handler: Handler) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            group_id=group,
            bootstrap_servers=self.bootstrap_servers,
            enable_auto_commit=True,
        )
        await consumer.start()
        self._consumers.append(consumer)
        try:
            async for msg in consumer:
                payload = json.loads(msg.value)
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    await result
        finally:
            await consumer.stop()

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaMessageQueue 未 start()")
        await self._producer.send_and_wait(
            topic, key=key.encode("utf-8"), value=json.dumps(payload).encode("utf-8")
        )

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def build_message_queue(backend: str = "memory", **kwargs: Any) -> MessageQueue:
    """按后端名构造 MQ：'memory' → InMemoryMessageQueue，'kafka' → KafkaMessageQueue。"""
    if backend == "memory":
        return InMemoryMessageQueue()
    if backend == "kafka":
        return KafkaMessageQueue(**kwargs)
    raise ValueError(f"未知 MQ backend={backend!r}，仅支持 'memory' / 'kafka'")
