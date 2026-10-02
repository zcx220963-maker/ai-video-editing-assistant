"""跨实例回投广播验证（不联网）：OutBound → Redis 频道 → 各实例本地注册表 → WS。

单副本时 OutBound 一帧被本进程的 Connection Manager 消费到就直接推 WS；多副本
就不成立了——MQ 消费组保证一帧只被**一个**实例拿到，而 WS 挂在另一个实例的进程内
注册表上，帧被消费掉、本地查无连接，这一路流静默丢失。本文件钉的就是这一跳：

  - 帧产生在 A、WS 挂在 B：B 收到且**只收到一次**（消费入口被广播接管，不叠加本地直连）
  - 反向对照：不开广播时 B 一条也收不到——差距是真的，广播是补上它的那块
  - 频道里跑的仍是同一份 payload：字段不增不减，跨会话不串台
  - 收到帧的那个实例照样写环形缓冲，新连接重连能补看错过的进度
  - **没挂 WS 的那个实例也攒一份**（只攒不投）：浏览器被它接管后重连，补得回接管之前的进度
  - 这一份「影子缓冲」与两个观测计数都有**常数上界**（条数 / 合计帧数 / 最近 N 条），
    不随「会话数 × 副本数」或累计帧数无界涨
  - create_app 开广播时 OutBound 订阅者只有一个（同一 topic+group 两处都订阅会在
    消费组里随机分掉一帧，另一处永远看不到）
  - 频道读失败不拖垮服务：监听任务吞掉异常继续，后续帧照常投递
  - broker 不可达（PUBLISH 就抛）：退回本地投递——本实例挂着连接的会话照常收到，
    丢的只是跨副本那半程（= 广播之前的单副本口径，而不是整帧蒸发）；broker 恢复后
    同一条路径重新扇出，不退化成永久本地

运行：  python tests/test_outbound_broadcast.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi.testclient import TestClient

from agent_framework.agent import Agent, AgentConfig
from agent_framework.broadcast import OUTBOUND_CHANNEL, OutboundBroadcaster
from agent_framework.connection_manager import (
    OUTBOUND_TOPIC,
    ConnectionManager,
    _DEFAULT_OBS_LIMIT as _OBS_LIMIT,
)
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.hooks import CompositeHook, OutboundStreamHook
from agent_framework.llm import ScriptedLLM
from agent_framework.mq import InMemoryMessageQueue
from agent_framework.server import create_app
from agent_framework.session import SessionManager
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeWS:
    def __init__(self) -> None:
        self.received: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.received.append(payload)


# ----------------------------------------------------------------------
# 假 Redis pub/sub：一个进程内共享的频道表，模拟「N 个实例连同一个 broker」
# 形状只需 broadcast.py 用到的那一小撮：publish / pubsub().subscribe / get_message
# ----------------------------------------------------------------------


class FakeBus:
    def __init__(self) -> None:
        self.channels: dict[str, list[asyncio.Queue]] = {}
        self.publishes: list[tuple[str, str]] = []
        self.fail_next_read = False
        self.publish_raises = False      # True = broker 不可达（PUBLISH 直接抛）

    def subscribe(self, channel: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.channels.setdefault(channel, []).append(q)
        return q

    async def publish(self, channel: str, data: str) -> int:
        if self.publish_raises:
            raise ConnectionError("redis broker 不可达（模拟）")
        self.publishes.append((channel, data))
        subs = self.channels.get(channel, [])
        for q in subs:      # 真 pub/sub 也把自己的订阅者算在内（回环）
            q.put_nowait(data)
        return len(subs)


class FakePubSub:
    def __init__(self, bus: FakeBus, channel: str) -> None:
        self._bus = bus
        self._channel = channel
        self._q: asyncio.Queue | None = None

    async def subscribe(self, channel: str) -> None:
        self._q = self._bus.subscribe(channel)

    async def get_message(self, *, ignore_subscribe_messages: bool = True, timeout: float = 0.0):
        if self._bus.fail_next_read:
            self._bus.fail_next_read = False
            raise ConnectionError("redis 连接抖动（模拟）")
        try:
            data = await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "channel": self._channel, "data": data}

    async def aclose(self) -> None:
        return None


class FakeRedis:
    """redis.asyncio 的最小替身（decode_responses=True：data 是 str）。"""

    def __init__(self, bus: FakeBus) -> None:
        self._bus = bus

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self._bus, OUTBOUND_CHANNEL)

    async def publish(self, channel: str, data: str) -> int:
        return await self._bus.publish(channel, data)

    async def aclose(self) -> None:
        return None


def make_instance(bus: FakeBus) -> tuple[InMemoryMessageQueue, ConnectionManager, OutboundBroadcaster]:
    """一个副本：自己的进程内 MQ + WS 注册表 + 广播器（连同一个假 broker）。"""
    mq = InMemoryMessageQueue()
    cm = ConnectionManager(mq)
    bc = OutboundBroadcaster(mq, cm, url="redis://fake",
                             client_factory=lambda _url: FakeRedis(bus))
    return mq, cm, bc


async def wait_for(cond, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


# ----------------------------------------------------------------------
# ① + ② 帧产生在 A、WS 挂在 B；不开广播时同一条用例必须收不到
# ----------------------------------------------------------------------


async def part1_cross_instance() -> None:
    bus = FakeBus()
    mq_a, cm_a, bc_a = make_instance(bus)
    mq_b, cm_b, bc_b = make_instance(bus)
    ws_b = FakeWS()
    cm_b.connect("u1:c1", ws_b)          # 浏览器只挂在副本 B 上

    await bc_a.start()                   # A 接管 OutBound 消费入口（广播开启）
    await bc_b.start()
    await mq_a.start()
    await mq_b.start()

    await mq_a.publish(OUTBOUND_TOPIC, "u1:c1",
                       {"type": "delta", "session_id": "u1:c1", "text": "跨实例的一句话"})
    ok = await wait_for(lambda: len(ws_b.received) == 1)
    check(ok and ws_b.received[0]["text"] == "跨实例的一句话",
          "帧产生在 A、WS 挂在 B → B 的这条连接收到")
    check(cm_a.sockets_of("u1:c1") == 0 and list(cm_a.sent) == [],
          "A 本地没有该 session 的连接：不就地投递，只广播出去")
    check(cm_a.get_buffer("u1:c1") and cm_a.shadow_of() == ["u1:c1"],
          "但 A 攒下了这一帧（有界影子预算）——接管后重连补得回接管前的进度")
    check(len(bus.publishes) == 1, "一帧只上频道一次（消费入口被广播接管，不与本地直连叠加）")

    await bc_a.stop(); await bc_b.stop(); await mq_a.stop(); await mq_b.stop()

    # 反向对照：同样两个副本，都不开广播（单实例直连路径），B 必须一条也收不到
    mq_a2, mq_b2 = InMemoryMessageQueue(), InMemoryMessageQueue()
    cm_a2, cm_b2 = ConnectionManager(mq_a2), ConnectionManager(mq_b2)
    ws_b2 = FakeWS()
    cm_b2.connect("u1:c1", ws_b2)
    cm_a2.start()                        # A 走单副本直连：本地查连接，查不到就丢
    cm_b2.start()
    await mq_a2.start()
    await mq_b2.start()
    await mq_a2.publish(OUTBOUND_TOPIC, "u1:c1",
                        {"type": "delta", "session_id": "u1:c1", "text": "对照帧"})
    await mq_a2.drain()
    await asyncio.sleep(0.1)
    check(ws_b2.received == [], "反向对照：不开广播时 B 收不到 A 消费的帧（差距是真的）")
    check(cm_a2.sent and cm_a2.sockets_of("u1:c1") == 0,
          "对照帧确实被 A 消费掉并丢弃了（不是没投出来）")
    await mq_a2.stop(); await mq_b2.stop()


# ----------------------------------------------------------------------
# ③ 同形 payload + 跨会话不串台 + ④ 收到帧的实例写入环形缓冲
# ----------------------------------------------------------------------


async def part2_payload_shape_and_buffer() -> None:
    bus = FakeBus()
    mq_a, cm_a, bc_a = make_instance(bus)
    mq_b, cm_b, bc_b = make_instance(bus)
    ws_c1, ws_c2 = FakeWS(), FakeWS()
    cm_b.connect("u1:c1", ws_c1)
    cm_b.connect("u1:c2", ws_c2)         # 同实例的另一个会话
    await bc_a.start(); await bc_b.start()
    await mq_a.start(); await mq_b.start()

    frame = {"type": "media", "session_id": "u1:c1", "run_id": "r-7",
             "media_url": "http://minio/x.mp4", "title": "成片", "duration": 5.0}
    await mq_a.publish(OUTBOUND_TOPIC, "u1:c1", dict(frame))
    ok = await wait_for(lambda: len(ws_c1.received) == 1)
    check(ok and ws_c1.received[0] == frame, "频道里跑的是同一份 payload：字段不增不减不减值")
    check(ws_c2.received == [], "广播到每个实例，但按 session_id 找连接——别的会话不串台")

    ws_late = FakeWS()
    cm_b.connect("u1:c1", ws_late)       # 刷新页面后重连
    n = await cm_b.replay_to("u1:c1", ws_late)
    check(n == 1 and ws_late.received[0]["type"] == "media",
          "收到帧的实例写了环形缓冲：重连能补看断连期间的进度")
    check(cm_b.get_buffer("u1:c1") and cm_a.get_buffer("u1:c1"),
          "投递侧（B）照旧攒帧，没挂连接的 A 也攒一份：审计第 5 条的那句「A 侧不攒无用的帧」已作废")
    # 接管：浏览器从 B 换到 A（轮询/断线重连），A 上重连必须补得到接管之前的那一帧
    ws_on_a = FakeWS()
    cm_a.connect("u1:c1", ws_on_a)
    n_a = await cm_a.replay_to("u1:c1", ws_on_a)
    check(n_a == 1 and ws_on_a.received[0]["type"] == "media"
          and ws_on_a.received[0].get("replayed") is True,
          "被别的副本接管后重连：接管之前的进度补得回来（原来只能是空手）")
    check(cm_a.shadow_of() == [],
          "本地挂上连接之后不再占跨副本预算（上界回到每会话那条 deque）")

    await bc_a.stop(); await bc_b.stop(); await mq_a.stop(); await mq_b.stop()


# ----------------------------------------------------------------------
# ⑤ create_app：开广播时 OutBound 只有一个订阅者；⑥ 频道读失败不拖垮
# ----------------------------------------------------------------------


def _build_agent(mq: InMemoryMessageQueue, text: str) -> Agent:
    llm = ScriptedLLM(steps=[("answer", text)], stream_chunk_size=2)
    return Agent(llm=llm, registry=ToolRegistry(), session_manager=SessionManager(),
                 context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
                 hooks=CompositeHook([OutboundStreamHook(mq)]),
                 config=AgentConfig(max_iterations=2))


def part3_app_wiring() -> None:
    with tempfile.TemporaryDirectory() as td:
        storage = build_storage("memory", cache_root=Path(td) / "cache",
                                workspace_root=Path(td) / "ws")
        bus = FakeBus()
        mq = InMemoryMessageQueue()
        app = create_app(_build_agent(mq, "经广播回投的一句话"), mq, storage=storage,
                         broadcast_url="redis://fake",
                         broadcast_client_factory=lambda _u: FakeRedis(bus))
        with TestClient(app) as client:                  # lifespan：broadcaster 接管 OutBound
            cm, bc = app.state.connections, app.state.broadcaster
            check(bc is not None and bc._subscribed, "开广播：OutboundBroadcaster 订阅了 OutBound")
            check(cm._subscribed is False,
                  "开广播：Connection Manager 不再重复订阅同一 (topic, group)")
            tok = client.post("/register", json={}).json()["token"]
            headers = {"Authorization": f"Bearer {tok}"}
            with client.websocket_connect("/ws/bc?token=" + tok) as ws:
                first = ws.receive_json()
                sid = first["session_id"]
                bus.fail_next_read = True                # 频道读一次异常
                r = client.post("/chat", json={"conversation_id": "bc", "message": "说一句"},
                                headers=headers)
                kinds: list[str] = []
                frames: list[dict] = [first]
                deadline = time.time() + 10
                while time.time() < deadline:
                    msg = ws.receive_json()
                    frames.append(msg)
                    kinds.append(msg["type"])
                    if msg["type"] == "answer":
                        break
                check(r.status_code == 200 and kinds and kinds[-1] == "answer",
                      f"频道读异常被吞掉后照常投递（收到 {kinds}）")
                check("delta" in kinds, "delta 也经广播到达本实例的 WS")
                check(len(bus.publishes) >= 2, "帧是**上过频道**才回到连接的（不是本地直连绕过广播）")
                check(all(m.get("session_id") == sid for m in frames),
                      f"广播只加一跳、不改回投键：每帧仍带 {sid}")
        # 未配置 broadcast_url：回到单副本直连的老路径
        mq2 = InMemoryMessageQueue()
        app2 = create_app(_build_agent(mq2, "单副本"), mq2, storage=storage)
        with TestClient(app2):
            check(app2.state.broadcaster is None and app2.state.connections._subscribed,
                  "未配置 broadcast_url：单副本直连路径原样保留")


# ----------------------------------------------------------------------
# ④ broker 不可达：退回本地投递的退化范围（不是整帧蒸发）
# ----------------------------------------------------------------------


async def part4_broker_outage() -> None:
    bus = FakeBus()
    mq_a, cm_a, bc_a = make_instance(bus)
    mq_b, cm_b, bc_b = make_instance(bus)
    ws_local, ws_other = FakeWS(), FakeWS()
    cm_a.connect("u1:c1", ws_local)      # 帧会被 A 消费，A 上恰好挂着这条会话
    cm_b.connect("u1:c1", ws_other)      # 同一会话在另一副本上也有连接
    await bc_a.start(); await bc_b.start()
    await mq_a.start(); await mq_b.start()

    bus.publish_raises = True
    await mq_a.publish(OUTBOUND_TOPIC, "u1:c1",
                       {"type": "delta", "session_id": "u1:c1", "text": "断线时的一句话"})
    ok = await wait_for(lambda: len(ws_local.received) == 1)
    check(ok and ws_local.received[0]["text"] == "断线时的一句话",
          "broker 不可达：退回本地投递，消费那一帧的实例上挂着的连接照常收到")
    await asyncio.sleep(0.1)
    check(ws_other.received == [] and bus.publishes == [],
          "退化范围如实：跨实例那半程确实送不到（频道一条都没写，B 空手）")
    check(cm_a.get_buffer("u1:c1"), "退回的这份照样进环形缓冲，重连补看得到")

    bus.publish_raises = False           # broker 恢复
    await mq_a.publish(OUTBOUND_TOPIC, "u1:c1",
                       {"type": "delta", "session_id": "u1:c1", "text": "恢复后的一句话"})
    ok = await wait_for(lambda: len(ws_other.received) == 1
                        and len(ws_local.received) == 2)
    check(ok, f"broker 恢复后回到广播路径：两副本各收到一份（本地退回不是永久态，"
              f"实际 {len(ws_local.received)}/{len(ws_other.received)}）")
    check(len(bc_a.published) == 2, "广播器把两帧都记进观测计数（一帧一次 handle）")

    await bc_a.stop(); await bc_b.stop(); await mq_a.stop(); await mq_b.stop()


def part5_shadow_budget() -> None:
    """有界性单独测：攒是目的，不封顶是事故。"""
    def frame(sid: str, i: int, kind: str = "delta") -> dict:
        return {"type": kind, "session_id": sid, "run_id": f"r-{i}", "text": f"{sid}#{i}"}

    cm = ConnectionManager(InMemoryMessageQueue(), shadow_sessions=2, shadow_frames=6)
    for i in range(3):
        cm.buffer_only(frame("u1:c1", i))
    for i in range(3):
        cm.buffer_only(frame("u1:c2", i))
    check(cm.buffer_only(frame("u1:c3", 0)) is True, "第三条会话先进来（占名额）")
    check(cm.shadow_of() == ["u1:c2", "u1:c3"],
          f"会话条数上界生效：最久没人碰的整条淘汰（实际 {cm.shadow_of()}）")
    check(cm.get_buffer("u1:c1") == [] and len(cm.get_buffer("u1:c2")) == 3,
          "淘汰是整条丢，不留半截进度")

    for i in range(3, 6):                       # c2 再灌 3 帧 → 合计 6+1 > 6
        cm.buffer_only(frame("u1:c2", i))
    check(cm.shadow_of() == ["u1:c2"],
          f"总帧数上界生效：淘汰的是最久没人碰的那条（c3 又没被写过），实际 {cm.shadow_of()}")
    check([f["text"] for f in cm.get_buffer("u1:c2")] == [f"u1:c2#{i}" for i in range(6)],
          "留下的是最近碰过的那条，内容原样且按序（跨副本攒的就是同一份 payload）")
    check(cm.get_buffer("u1:c3") == [], "被淘汰那条整条清空，不留半截")

    ws = FakeWS()
    cm.connect("u1:c2", ws)
    check(cm.shadow_of() == [], "本地挂上连接 → 退出预算，改由每会话 deque 兜底")
    check(cm.buffer_only(frame("u1:c2", 9)) is False,
          "有本地连接时 buffer_only 不插手（那条路要走 route，得真投递）")
    cm.disconnect("u1:c2", ws)
    check(cm.shadow_of() == ["u1:c2"],
          "最后一条本地连接断开 → 并入同一预算（否则每来过一个会话永久留一条 deque）")

    off = ConnectionManager(InMemoryMessageQueue(), shadow_sessions=0)
    check(off.buffer_only({"type": "delta", "session_id": "u1:cX", "text": "x"}) is False
          and off.get_buffer("u1:cX") == [],
          "shadow_sessions=0 显式关掉：回到「整条跳过」的老口径，不当默认改掉")
    check(off.buffer_only({"type": "delta", "text": "没有 session_id"}) is False,
          "没有 session_id 的帧不攒（攒了也永远找不到归属）")

    cm.sent.extend(frame("u1:c1", i) for i in range(_OBS_LIMIT + 100))
    check(isinstance(cm.sent, deque) and cm.sent.maxlen == _OBS_LIMIT
          and len(cm.sent) == _OBS_LIMIT,
          f"观测计数有界（{len(cm.sent)} 条，上限 {_OBS_LIMIT}）：以前每帧永久留一份，长跑必漏")
    bc = OutboundBroadcaster(InMemoryMessageQueue(), ConnectionManager(InMemoryMessageQueue()),
                             url="redis://fake", obs_limit=3)
    check(bc.published.maxlen == 3, "广播器的 published 同样有界")


def main() -> int:
    for title, fn in [("① 跨实例投递 + 反向对照", part1_cross_instance),
                      ("② 同形 payload / 不串台 / 缓冲重放", part2_payload_shape_and_buffer),
                      ("③ create_app 装配与容错", part3_app_wiring),
                      ("④ broker 不可达时的退化范围", part4_broker_outage),
                      ("⑤ 跨副本影子预算的有界性", part5_shadow_budget)]:
        print(f"\n[{title}]")
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if not _fails else f"，失败 {_fails} 项"))
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
