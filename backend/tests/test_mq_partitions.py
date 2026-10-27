# -*- coding: utf-8 -*-
"""R1 的判据：分区只做路由，session 才是并发单元；offset 水位；死信也 ack。

    docker exec creation-app python tests/test_mq_partitions.py

**不需要 broker**：并发语义用 `InMemoryMessageQueue` 验（它和 Kafka 共用同一套
`SessionDispatcher`），水位用 `_Watermark` 直接验。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.dispatch import QueueFull, SessionDispatcher, session_of  # noqa: E402
from agent_framework.mq import InMemoryMessageQueue, _Watermark  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


async def main() -> None:
    # ---- 1. session_of：两种载荷形状都认 ----
    check(session_of({"session_id": "u1:c1"}) == "u1:c1", "session_of 优先 session_id")
    check(session_of({"user_id": "u1", "conversation_id": "c1"}) == "u1:c1",
          "session_of 退化为 user:conv")

    # ---- 2. 同 session 串行：三条消息的执行区间不重叠 ----
    spans: dict[str, list[tuple[float, float]]] = {}

    async def slow(payload: dict) -> None:
        sid = session_of(payload)
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        spans.setdefault(sid, []).append((t0, time.monotonic()))

    d = SessionDispatcher(slow, max_concurrent=8)
    await d.start()
    for i in range(3):
        await d.submit({"session_id": "A", "run_id": f"a{i}"})
    await d.drain(timeout=5)
    a = spans.get("A", [])
    overlap = any(a[i][1] > a[i + 1][0] for i in range(len(a) - 1))
    check(len(a) == 3 and not overlap, f"同 session 严格串行（{len(a)} 条，重叠={overlap}）")

    # ---- 3. 不同 session 并行（同分区也要并行）----
    mq = InMemoryMessageQueue(num_partitions=1)   # 故意只留 1 个分区
    seen: list[tuple[str, float, float]] = []

    async def handler(payload: dict) -> None:
        t0 = time.monotonic()
        await asyncio.sleep(0.08)
        seen.append((session_of(payload), t0, time.monotonic()))

    mq.subscribe("chat", "agent", handler)
    await mq.start()
    await mq.publish("chat", "A", {"session_id": "A"})
    await mq.publish("chat", "B", {"session_id": "B"})
    await mq.drain(timeout=5)
    await mq.stop()
    check(len(seen) == 2, f"两条消息都被处理（实际 {len(seen)}）")
    if len(seen) == 2:
        (_, s0, e0), (_, s1, e1) = seen
        check(s1 < e0 or s0 < e1,
              f"**同分区**的两个 session 并行执行（区间 [{s0:.3f},{e0:.3f}] vs [{s1:.3f},{e1:.3f}]）")

    # ---- 4. 队列上界：超界抛 QueueFull，不阻塞分区 ----
    block = asyncio.Event()

    async def hang(_payload: dict) -> None:
        await block.wait()

    d2 = SessionDispatcher(hang, max_concurrent=4, queue_max=1)
    await d2.start()
    await d2.submit({"session_id": "Q"})
    await asyncio.sleep(0.01)          # 让第一条进 actor
    await d2.submit({"session_id": "Q"})
    raised = False
    try:
        await d2.submit({"session_id": "Q"})
    except QueueFull:
        raised = True
    check(raised, "队列满时抛 QueueFull（调用方据此落死信并 ack）")
    block.set()
    await d2.stop()

    # ---- 5. offset 水位：只提交到最小未完成 ----
    wm = _Watermark()
    wm.track(10)
    wm.track(11)
    wm.track(12)
    check(wm.commit_offset() == 10, "有未完成时提交最小 offset")
    wm.ack(10)
    check(wm.commit_offset() == 11, "ack 前一条后水位前移")
    wm.ack(12)                          # 中间那条还没完成
    check(wm.commit_offset() == 11, "跳过中间未完成的 offset 不前进")
    wm.ack(11)
    check(wm.commit_offset() == 13, "全部完成后退到已读末尾 + 1")

    # ---- 6. 死信也要 ack（否则水位永久钉住）----
    wm2 = _Watermark()
    wm2.track(20)
    wm2.track(21)
    wm2.ack(20)                         # 20 落死信后 ack
    check(wm2.commit_offset() == 21, "死信 ack 后水位前进（不钉在 20）")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
