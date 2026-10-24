"""MessageQueue 验证（不联网、不连 broker）：内存替身的 key 路由/有序/并发 + Kafka 构造。

运行：  python tests/test_mq.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.mq import (
    InMemoryMessageQueue,
    KafkaMessageQueue,
    build_message_queue,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def test_factory() -> None:
    check(isinstance(build_message_queue("memory"), InMemoryMessageQueue), "build memory → InMemory")
    check(isinstance(build_message_queue("kafka"), KafkaMessageQueue), "build kafka → Kafka")
    try:
        build_message_queue("redis")
        check(False, "未知 backend 应报错")
    except ValueError:
        check(True, "未知 backend 抛 ValueError")
    # Kafka 无 broker 也能构造，仅保存参数
    k = KafkaMessageQueue(bootstrap_servers="h:9092")
    check(k.bootstrap_servers == "h:9092" and k._producer is None, "Kafka 构造不连 broker")


async def test_basic_delivery() -> None:
    mq = InMemoryMessageQueue()
    got: list[dict] = []
    mq.subscribe("chat", "g1", lambda p: got.append(p))
    await mq.start()
    await mq.publish("chat", "u1:c1", {"message": "hi", "run_id": "r1"})
    await mq.drain()
    await mq.stop()
    check(len(got) == 1 and got[0]["run_id"] == "r1", "publish→handler 收到载荷")


async def test_per_key_order() -> None:
    mq = InMemoryMessageQueue()
    seq: list[int] = []
    mq.subscribe("chat", "g1", lambda p: seq.append(p["n"]))
    await mq.start()
    for n in (1, 2, 3, 4, 5):
        await mq.publish("chat", "same-key", {"n": n})
    await mq.drain()
    await mq.stop()
    check(seq == [1, 2, 3, 4, 5], "同一 key 严格有序")


async def test_cross_key_parallel() -> None:
    mq = InMemoryMessageQueue()
    done: list[str] = []

    async def handler(p):
        if p["slow"]:
            await asyncio.sleep(0.1)
        done.append(p["tag"])

    mq.subscribe("chat", "g1", handler)
    await mq.start()
    await mq.publish("chat", "A", {"tag": "A", "slow": True})
    await mq.publish("chat", "B", {"tag": "B", "slow": False})
    await mq.drain(timeout=3)
    await mq.stop()
    # 不同 key 并发：快的 B 应先于慢的 A 完成
    check(done == ["B", "A"], "不同 key 并发（B 不被 A 阻塞）")


async def test_each_group_once() -> None:
    mq = InMemoryMessageQueue()
    g1: list = []
    g2: list = []
    mq.subscribe("chat", "groupA", lambda p: g1.append(p["run_id"]))
    mq.subscribe("chat", "groupB", lambda p: g2.append(p["run_id"]))
    await mq.start()
    await mq.publish("chat", "k", {"run_id": "x"})
    await mq.drain()
    await mq.stop()
    check(g1 == ["x"] and g2 == ["x"], "每个消费组各收到一次")


async def main() -> None:
    for t in (test_factory, test_basic_delivery, test_per_key_order, test_cross_key_parallel, test_each_group_once):
        await t()
    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
