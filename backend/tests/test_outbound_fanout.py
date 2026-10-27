# -*- coding: utf-8 -*-
"""R2：OutBound 每实例唯一消费组 + 真 MQ 上的端到端扇出。

只比组名是不够的：固定组在多副本下「同一帧只到一个实例」，而每个实例都必须看到
**每一帧**才能按本地 socket 投递。这里起一个真 MQ，让两个 ConnectionManager 各用
自己的实例组建订阅，publish 一帧后断言：

  R2-a  两个实例都收到了这一帧（`sent` 各有一条）；
  R2-b  只有挂了连接的实例把它写进 WS；另一实例不投递、只攒进影子预算。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.connection_manager import (  # noqa: E402
    OUTBOUND_TOPIC,
    ConnectionManager,
    instance_group,
)
from agent_framework.mq import InMemoryMessageQueue  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class FakeWS:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.frames.append(payload)


def _frame(session_id: str, run_id: str, text: str) -> dict:
    return {"type": "delta", "session_id": session_id, "run_id": run_id, "text": text}


async def _fanout() -> None:
    mq = InMemoryMessageQueue()
    cm1 = ConnectionManager(mq, instance_id="i1")
    cm2 = ConnectionManager(mq, instance_id="i2")
    cm1.start()
    cm2.start()

    ws = FakeWS()
    cm1.connect("s-1", ws)

    await mq.start()
    try:
        await mq.publish(OUTBOUND_TOPIC, "s-1", _frame("s-1", "r1", "hello"))
        await mq.drain(2.0)
    finally:
        await mq.stop()

    # R2-a：每实例各收到一帧（不是「一个实例吞掉、另一个再也看不到」）
    check(len(cm1.sent) == 1, f"cm1 收到 1 帧（实际 {len(cm1.sent)}）")
    check(len(cm2.sent) == 1, f"cm2 收到 1 帧（实际 {len(cm2.sent)}）")
    check(all(p.get("session_id") == "s-1" for p in cm1.sent), "cm1 帧的 session_id 正确")
    check(all(p.get("session_id") == "s-1" for p in cm2.sent), "cm2 帧的 session_id 正确")

    # R2-b：只有挂连接的实例写 WS；另一实例只攒影子，不投递
    check(len(ws.frames) == 1, f"cm1 的 WS 收到 1 帧（实际 {len(ws.frames)}）")
    check(ws.frames and ws.frames[0].get("text") == "hello", "WS 帧内容原样到达")
    check(cm2.shadow_of() == ["s-1"], f"cm2 只攒影子（实际 {cm2.shadow_of()}）")
    check(cm1.shadow_of() == [], "cm1 本地有连接，不进影子预算")

    # 接管：cm2 挂上连接后，影子缓冲可直接重放给新连接
    ws2 = FakeWS()
    cm2.connect("s-1", ws2)
    replayed = await cm2.replay_to("s-1", ws2)
    check(replayed == 1, f"接管后重放 1 帧（实际 {replayed}）")
    check(bool(ws2.frames), "接管后的连接补看到了接管之前的帧")


async def _both_connected() -> None:
    """两个实例都挂着同一 session 的连接：两边都要推（多标签 / 轮询落在任一副本）。"""
    mq = InMemoryMessageQueue()
    cm1 = ConnectionManager(mq, instance_id="a")
    cm2 = ConnectionManager(mq, instance_id="b")
    cm1.start()
    cm2.start()
    w1, w2 = FakeWS(), FakeWS()
    cm1.connect("s-9", w1)
    cm2.connect("s-9", w2)
    await mq.start()
    try:
        await mq.publish(OUTBOUND_TOPIC, "s-9", _frame("s-9", "r9", "ok"))
        await mq.drain(2.0)
    finally:
        await mq.stop()
    check(len(w1.frames) == 1 and len(w2.frames) == 1,
          f"两实例各推一次（{len(w1.frames)} / {len(w2.frames)}）")
    check(len(cm1.sent) == 1 and len(cm2.sent) == 1, "两实例各消费一次")


def main() -> None:
    # ---- 两个实例拿到不同的组 ----
    a, b = instance_group("a"), instance_group("b")
    check(a != b, f"两个实例组名不同（{a} vs {b}）")
    check(a.startswith("conn-") and b.startswith("conn-"), "组名带 conn- 前缀")
    check(a == instance_group("a"), "同名幂等")

    # ---- 不给实例名时按 hostname:pid 兜底（同机多进程也是两个实例）----
    auto = instance_group(None)
    check(auto.startswith("conn-") and auto != instance_group("a"), "缺省组名唯一")

    # ---- ConnectionManager 默认就用实例组 ----
    mq = InMemoryMessageQueue()
    cm1 = ConnectionManager(mq, instance_id="i1")
    cm2 = ConnectionManager(mq, instance_id="i2")
    check(cm1.group != cm2.group, f"两个 manager 组不同（{cm1.group} / {cm2.group}）")
    check(cm1.group == "conn-i1", "组名 = conn-<instance_id>")
    check(ConnectionManager(mq).group == ConnectionManager(mq).group,
          "同进程缺省组名一致（进程才是实例）")

    # ---- 端到端：真 MQ 上的跨实例扇出 ----
    asyncio.run(_fanout())
    asyncio.run(_both_connected())

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
