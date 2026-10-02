"""跨实例的 OutBound 广播：把「帧产生在哪个实例」与「WS 挂在哪个实例」解耦。

单副本时 OutBound 的一帧被本进程的 Connection Manager 消费到就直接推 WS。多副本
（起 N 个服务、前面轮询）就断了：MQ 的消费组语义保证一帧只被**一个**实例消费到
（见 mq.py 的 KafkaMessageQueue），而用户的 WS 只登记在它自己连上的那个实例的进程内
注册表里——帧被别的实例消费掉、本地查不到连接，这一路流就静默丢了，浏览器表现为
「发了消息、不出字」。

补法只加一跳，不改帧的形状：消费到该帧的实例把它原样 PUBLISH 到 Redis 频道，
**每个**实例都 SUBSCRIBE 这个频道，收到后先看本地有没有这条 session 的连接——
有就交给自己的 Connection Manager 按 session_id 找连接并写环形缓冲，没有就整条跳过
（不投递也不记账，否则每个副本都得为全量会话攒缓冲，内存随「会话数 × 副本数」涨）。
挂着连接的那个实例走的仍是 `ConnectionManager.route`，与单副本同一条代码路径。

代价是一条边界：浏览器被别的副本接走时，那个副本本地没攒过这条 session 的帧，
重连后只补不回**接管之前**的进度（接管之后的照常）。要跨副本补全得把环形缓冲也搬到
共享存储里，那是另一件事。

开启广播时 OutBound 的消费入口从 Connection Manager 换成本模块：同一
(topic, group) 只能有一个订阅者，两处都订阅会让一帧在 Kafka 消费组里被随机分给
其中一个（另一处再也看不到它），所以是「替换」而不是「叠加」。

broker 不可达时退回本地投递（`handle` 里的 except）：那正是广播之前的单副本口径——
本实例挂着连接的会话照常收到，丢的只是跨副本那半程，而不是整帧。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from .connection_manager import CONNECTION_GROUP, OUTBOUND_TOPIC, ConnectionManager
from .mq import MessageQueue

OUTBOUND_CHANNEL = "outbound:broadcast"


class OutboundBroadcaster:
    """OutBound 消费端 → Redis 频道 → 各实例本地 Connection Manager。"""

    def __init__(
        self,
        mq: MessageQueue,
        connections: ConnectionManager,
        *,
        url: str = "redis://localhost:6379/0",
        channel: str = OUTBOUND_CHANNEL,
        client_factory: Callable[[str], Any] | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.mq = mq
        self.connections = connections
        self.url = url
        self.channel = channel
        self._client_factory = client_factory
        self._poll_interval = poll_interval
        self._client: Any | None = None
        self._pubsub: Any | None = None
        self._task: asyncio.Task | None = None
        self._subscribed = False
        self._stopping = False
        self.published: list[dict[str, Any]] = []  # 观测 / 测试：本机广播出去的帧

    async def start(self) -> None:
        """接管 OutBound 消费入口，并起频道监听（须在 mq.start() 之前调用）。"""
        if not self._subscribed:
            self.mq.subscribe(OUTBOUND_TOPIC, CONNECTION_GROUP, self.handle)
            self._subscribed = True
        self._client = await self._build_client()
        self._pubsub = self._client.pubsub()
        await self._pubsub.subscribe(self.channel)
        self._task = asyncio.create_task(self._listen())

    async def _build_client(self) -> Any:
        if self._client_factory is not None:
            client = self._client_factory(self.url)
            return await client if asyncio.iscoroutine(client) else client
        import redis.asyncio as aioredis  # 延迟导入：不配广播就不需要 redis 包

        return aioredis.from_url(self.url, decode_responses=True)

    async def handle(self, payload: dict[str, Any]) -> None:
        """OutBound 消费入口：只广播，不在本地直接推——广播回环会推这一份。"""
        self.published.append(payload)
        try:
            await self._client.publish(self.channel, json.dumps(payload, default=str))
        except Exception:  # noqa: BLE001  broker 不可达：退回本地投递（=单副本口径），不丢帧
            await self.connections.route(payload)

    async def _listen(self) -> None:
        while not self._stopping:
            try:
                msg = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=self._poll_interval
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  频道读失败不该拖垮服务
                await asyncio.sleep(0.2)
                continue
            if msg is None:
                continue
            if msg.get("type") != "message":
                continue
            raw = msg.get("data")
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if self.connections.sockets_of(payload.get("session_id")):
                await self.connections.route(payload)

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        for closer in (self._pubsub, self._client):
            if closer is None:
                continue
            for name in ("aclose", "close", "disconnect"):
                fn = getattr(closer, name, None)
                if fn is None:
                    continue
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass
                break
        self._pubsub = None
        self._client = None
