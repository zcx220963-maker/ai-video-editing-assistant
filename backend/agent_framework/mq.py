"""消息队列抽象层（对应架构图「Message Queue → 会话路由」）。

职责：把 Web 层收到的请求投递给 Agent 消费端，并用 key=session_id 承载分区语义。

**分区与并发是两件事**（设计原文 §3/§4）：
  * Partition 是**路由**单位：决定消息由哪个 reader 读、offset 怎么提交；
  * Session 是**并发**单位：同一 session 串行、不同 session 并行。

所以两种实现都不再「在 reader 里 await handler」，而是把消息交给
`dispatch.SessionDispatcher`（per-session 串行 actor + 全局并发上限），
读循环只负责读和 ack。这样同分区里的两个 session 也能并行——旧实现里
它们会互相排队，一个 20 分钟的渲染能把整条分区堵死。

两种实现，接口一致，装配时按 backend 切换：
  - KafkaMessageQueue：真实 Kafka（aiokafka），手动提交 + 分区水位 + 死信。
  - InMemoryMessageQueue：进程内替身，保留同样的 per-key 有序 / 跨 key 并行语义。

消息载荷约定为 dict：{"user_id","conversation_id","message","run_id"}。
"""

from __future__ import annotations

import asyncio
import json
import logging
import zlib
from abc import ABC, abstractmethod
from functools import partial
from typing import Any, Awaitable, Callable

from .dispatch import QueueFull, SessionDispatcher, session_of

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
    """进程内 MQ：复刻「很多 session → 按 session_id hash → 有限 Partition」模型。

    分区仍是路由单位（同 key 必同分区 → 同 key 有序）；但**分区内的消息不再串行
    执行**，而是投给 `SessionDispatcher`：同 session 串行、不同 session 并行。
    """

    def __init__(
        self,
        num_partitions: int = 4,
        *,
        max_concurrent_sessions: int = 32,
        queue_max: int = 32,
    ) -> None:
        self.num_partitions = num_partitions
        self.max_concurrent_sessions = max_concurrent_sessions
        self.queue_max = queue_max
        self._subs: dict[tuple[str, str], Handler] = {}
        self._dispatchers: dict[tuple[str, str], SessionDispatcher] = {}
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

    def _done(self) -> None:
        """一条消息真正跑完（含被丢弃）：drain 只认这个回调。"""
        async def _dec() -> None:
            async with self._lock:
                self._inflight -= 1
                if self._inflight <= 0:
                    self._idle.set()

        # 回调来自 actor 协程内部，直接建任务即可；异常只影响计数，不该抛回 actor。
        asyncio.create_task(_dec())

    async def _partition_worker(self, sub: tuple[str, str], p: int) -> None:
        dispatcher = self._dispatchers[sub]
        q = self._queues[(sub, p)]
        while True:
            payload = await q.get()
            try:
                await dispatcher.submit(payload, on_done=self._done)
            except QueueFull:
                # 单个 session 积压到上限：丢这条并**算它跑完**，否则 drain 永远等不到。
                log.error("session 队列已满，丢弃一条消息：run_id=%s",
                          payload.get("run_id"))
                self._done()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  单条失败不应中断该分区
                log.exception("投递失败（topic=%s group=%s partition=%s）：run_id=%s",
                              sub[0], sub[1], p, payload.get("run_id"))
                self._done()

    async def start(self) -> None:
        self._running = True
        for sub, handler in self._subs.items():
            dispatcher = SessionDispatcher(
                handler,
                max_concurrent=self.max_concurrent_sessions,
                queue_max=self.queue_max,
            )
            await dispatcher.start()
            self._dispatchers[sub] = dispatcher
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
        for d in self._dispatchers.values():
            await d.stop()
        self._dispatchers.clear()

    async def drain(self, timeout: float = 2.0) -> None:
        """等待当前所有已投递消息处理完毕（测试 / 同步语义用）。"""
        await asyncio.wait_for(self._idle.wait(), timeout)


class _Watermark:
    """一个分区的 offset 水位。

    reader 不再 await handler，offset 就不能「读到就提交」——那样进程一崩，
    正在跑的那条消息就永远丢了。做法是记住「哪些 offset 还没跑完」，
    只提交到**最小的未完成 offset**；死信与拒收也必须 ack，否则水位会永久钉在那里。
    """

    __slots__ = ("pending", "next_offset", "dirty")

    def __init__(self) -> None:
        self.pending: set[int] = set()
        self.next_offset = 0
        self.dirty = False

    def track(self, offset: int) -> None:
        self.pending.add(offset)
        self.next_offset = max(self.next_offset, offset + 1)
        self.dirty = True

    def ack(self, offset: int) -> None:
        self.pending.discard(offset)
        self.dirty = True

    def commit_offset(self) -> int:
        return min(self.pending) if self.pending else self.next_offset


class KafkaMessageQueue(MessageQueue):
    """真实 Kafka 实现（aiokafka）。

    key=session_id 决定分区；消费端**手动提交**（`enable_auto_commit=False`），
    提交口径是「分区最小未完成 offset」的水位（见 `_Watermark`）。
    消息处理交给 `SessionDispatcher`，因此读循环不会被长任务阻塞。

    aiokafka 延迟导入：未安装或未连 broker 时，import 本模块不报错，只有真正
    start()/publish() 才需要 broker。
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str = "localhost:9092",
        client_id: str = "agent-framework",
        num_partitions: int = 4,
        instance_id: str = "",
        max_concurrent_sessions: int = 32,
        queue_max: int = 32,
        dead_letter_suffix: str = ".dead",
        pause_at_pending: int = 64,
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.client_id = client_id
        self.num_partitions = num_partitions
        self.instance_id = instance_id
        self.max_concurrent_sessions = max_concurrent_sessions
        self.queue_max = queue_max
        self.dead_letter_suffix = dead_letter_suffix
        self.pause_at_pending = max(1, int(pause_at_pending))
        self._subs: list[tuple[str, str, Handler]] = []
        self._producer: Any | None = None
        self._consumers: list[Any] = []
        self._tasks: list[asyncio.Task] = []
        self._dispatchers: dict[tuple[str, str], SessionDispatcher] = {}
        self._watermarks: dict[tuple[str, int], _Watermark] = {}
        self._running = False

    def subscribe(self, topic: str, group: str, handler: Handler) -> None:
        self._subs.append((topic, group, handler))

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers)
        await self._producer.start()
        self._running = True
        for topic, group, handler in self._subs:
            self._tasks.append(asyncio.create_task(self._consume(topic, group, handler)))

    async def _consume(self, topic: str, group: str, handler: Handler) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            group_id=group,
            bootstrap_servers=self.bootstrap_servers,
            client_id=self.client_id or None,
            enable_auto_commit=False,      # 水位自己算，见 _Watermark
            auto_offset_reset="earliest",
        )
        await consumer.start()
        self._consumers.append(consumer)
        dispatcher = SessionDispatcher(
            handler,
            max_concurrent=self.max_concurrent_sessions,
            queue_max=self.queue_max,
        )
        await dispatcher.start()
        self._dispatchers[(topic, group)] = dispatcher
        try:
            while self._running:
                batches = await consumer.getmany(timeout_ms=200, max_records=64)
                for tp, msgs in batches.items():
                    wm = self._watermarks.setdefault((tp.topic, tp.partition), _Watermark())
                    for msg in msgs:
                        try:
                            payload = json.loads(msg.value)
                        except Exception:  # noqa: BLE001  坏消息不能卡住分区
                            await self._dead_letter(tp.topic, msg, "bad json")
                            wm.track(msg.offset)
                            wm.ack(msg.offset)
                            continue
                        wm.track(msg.offset)
                        try:
                            await dispatcher.submit(
                                payload, on_done=partial(self._ack, tp, msg.offset))
                        except QueueFull:
                            # 拒收也要 ack：否则水位永远停在它这里，整条分区不动。
                            await self._dead_letter(tp.topic, msg, "session queue full")
                            wm.ack(msg.offset)
                    await self._backpressure(consumer, tp, wm)
                await self._commit(consumer)
        finally:
            await dispatcher.stop()
            await consumer.stop()

    def _ack(self, tp: Any, offset: int) -> None:
        wm = self._watermarks.get((tp.topic, tp.partition))
        if wm is not None:
            wm.ack(offset)

    async def _commit(self, consumer: Any) -> None:
        """按分区水位提交；没有变化就不提交（省一次 round-trip）。"""
        from aiokafka import OffsetAndMetadata, TopicPartition

        offsets = {}
        for (topic, partition), wm in self._watermarks.items():
            if not wm.dirty:
                continue
            offsets[TopicPartition(topic, partition)] = OffsetAndMetadata(
                wm.commit_offset(), "")
            wm.dirty = False
        if offsets:
            try:
                await consumer.commit(offsets)
            except Exception:  # noqa: BLE001  rebalance 期间提交失败是常态
                log.warning("offset 提交失败（下一轮重试）", exc_info=True)

    async def _backpressure(self, consumer: Any, tp: Any, wm: _Watermark) -> None:
        """未完成数超阈值就 pause 这个分区，降到一半再 resume。

        没有这一层，一次 rebalance 或一个慢 session 会把所有消息堆在内存里。
        """
        try:
            if len(wm.pending) >= self.pause_at_pending:
                consumer.pause(tp)
            elif len(wm.pending) <= self.pause_at_pending // 2:
                consumer.resume(tp)
        except Exception:  # noqa: BLE001  pause/resume 对已失效分区会抛
            pass

    async def _dead_letter(self, topic: str, msg: Any, reason: str) -> None:
        log.error("消息进死信（%s）：topic=%s partition=%s offset=%s",
                  reason, topic, msg.partition, msg.offset)
        if self._producer is None:
            return
        try:
            await self._producer.send_and_wait(
                topic + self.dead_letter_suffix,
                key=msg.key,
                value=json.dumps({"reason": reason,
                                  "offset": msg.offset,
                                  "raw": msg.value.decode("utf-8", "replace")},
                                 ensure_ascii=False).encode("utf-8"),
            )
        except Exception:  # noqa: BLE001  死信发不出去也不能卡住消费
            log.exception("死信投递失败")

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaMessageQueue 未 start()")
        await self._producer.send_and_wait(
            topic, key=key.encode("utf-8"), value=json.dumps(payload).encode("utf-8")
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for d in self._dispatchers.values():
            await d.stop()
        self._dispatchers.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def build_message_queue(backend: str = "memory", **kwargs: Any) -> MessageQueue:
    """按后端名构造 MQ：'memory' → InMemoryMessageQueue，'kafka' → KafkaMessageQueue。"""
    if backend == "memory":
        return InMemoryMessageQueue(
            num_partitions=int(kwargs.get("num_partitions", 4) or 4),
            max_concurrent_sessions=int(kwargs.get("max_concurrent_sessions", 32) or 32),
            queue_max=int(kwargs.get("queue_max", 32) or 32),
        )
    if backend == "kafka":
        return KafkaMessageQueue(**kwargs)
    raise ValueError(f"未知 MQ backend={backend!r}，仅支持 'memory' / 'kafka'")
