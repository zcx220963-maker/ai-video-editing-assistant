# -*- coding: utf-8 -*-
"""KafkaMessageQueue._commit 的四条边界（**不需要 broker**，假 consumer）。

2026-10-27 用真 broker（本机 KRaft 单节点）跑多 topic 时抓到两处真缺陷：

① **提交别人的分区**：同一个 MQ 上通常挂两个 consumer（chat 与 outbound 各一个组），
   而水位表按 ``(topic, partition)`` 全局共享。原实现把两个 topic 的分区一起塞进
   ``commit()``，aiokafka 回 ``IllegalStateError: Partition ... is not assigned``，
   **整个提交失败，两个 topic 的 offset 都永远不前进**——单副本（默认形态）必踩，
   重启后从 ``earliest`` 全量重放。
② **提交期间的 ack 被清脏吞掉**：``await commit()`` 成功后才 ``dirty = False``，
   而这期间新到的 ack 也把 dirty 置了 True——随后被无条件清掉。真机读数：
   内存水位 3/5，组里只到 2/4，每个分区永远差最后一条。

修法见 ``mq._commit``；这份用例只钉行为，不起 broker。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.mq import KafkaMessageQueue, _Watermark  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class FakeConsumer:
    """只实现 _commit 用到的两个方法：assignment() 与 commit()。"""

    def __init__(self, assigned, *, fail: bool = False, on_commit=None):
        self.assigned = set(assigned)
        self.fail = fail
        self.on_commit = on_commit
        self.calls: list[dict] = []

    def assignment(self):
        return set(self.assigned)

    async def commit(self, offsets):
        self.calls.append(dict(offsets))
        if self.on_commit is not None:
            self.on_commit()
        if self.fail:
            raise RuntimeError("IllegalStateError: Partition ... is not assigned")


def _mq() -> KafkaMessageQueue:
    """不 start()：_commit 只碰水位表与传进来的 consumer。"""
    return KafkaMessageQueue(bootstrap_servers="unused:9092")


def _tp(d):
    return {(t.topic, t.partition): o.offset for t, o in d.items()}


async def main() -> None:
    # ---- 1. 只提交本 consumer 分配到的分区 ----
    mq = _mq()
    wm_chat, wm_out = _Watermark(), _Watermark()
    wm_chat.track(0)
    wm_chat.ack(0)
    wm_out.track(0)
    wm_out.ack(0)
    mq._watermarks[("chat-t", 0)] = wm_chat
    mq._watermarks[("out-t", 0)] = wm_out
    c = FakeConsumer({("chat-t", 0)})
    await mq._commit(c)
    check(len(c.calls) == 1, "有脏水位时就提交一次")
    check(_tp(c.calls[0]) == {("chat-t", 0): 1},
          f"提交里只有自己分配到的分区（实际 {_tp(c.calls[0])}）")
    check(wm_chat.dirty is False, "提交成功后本分区清脏")
    check(wm_out.dirty is True, "别人的分区保持脏（下一轮由它自己的 consumer 提交）")

    # ---- 2. rebalance 窗口：assignment 为空 → 一次都不提交 ----
    mq2 = _mq()
    wm = _Watermark()
    wm.track(0)
    wm.ack(0)
    mq2._watermarks[("t", 0)] = wm
    c2 = FakeConsumer(set())
    await mq2._commit(c2)
    check(c2.calls == [], "没有分配（rebalance 中）不提交")
    check(wm.dirty is True, "跳过时不把脏位清掉")

    # ---- 3. 提交失败：保留脏位，下一轮重试 ----
    mq3 = _mq()
    wm3 = _Watermark()
    wm3.track(7)
    wm3.ack(7)
    mq3._watermarks[("t", 0)] = wm3
    bad = FakeConsumer({("t", 0)}, fail=True)
    await mq3._commit(bad)
    check(len(bad.calls) == 1, "失败那一轮确实尝试过")
    check(wm3.dirty is True, "提交失败后脏位保留（否则这条 offset 再没人提）")
    ok = FakeConsumer({("t", 0)})
    await mq3._commit(ok)
    check(_tp(ok.calls[-1]) == {("t", 0): 8}, f"下一轮重试提交到 8（实际 {_tp(ok.calls[-1])}）")
    check(wm3.dirty is False, "重试成功后清脏")

    # ---- 4. await commit() 期间新到的 ack 不能被清脏吞掉 ----
    mq4 = _mq()
    wm4 = _Watermark()
    wm4.track(0)
    wm4.track(1)
    wm4.ack(0)                       # 快照：commit_offset=1，version=3
    mq4._watermarks[("t", 0)] = wm4

    def late_ack():                  # 提交往返途中，第 2 条跑完了
        wm4.ack(1)

    c4 = FakeConsumer({("t", 0)}, on_commit=late_ack)
    await mq4._commit(c4)
    check(_tp(c4.calls[0]) == {("t", 0): 1}, "第一次提交只到 1")
    check(wm4.dirty is True, "期间有新 ack：脏位必须留着（旧实现会吞掉）")
    c4b = FakeConsumer({("t", 0)})
    await mq4._commit(c4b)
    check(_tp(c4b.calls[-1]) == {("t", 0): 2}, f"下一轮补到 2（实际 {_tp(c4b.calls[-1])}）")
    check(wm4.dirty is False, "补齐后清脏")

    total = _pass + _fail
    print(f"\n{_pass}/{total} 通过" + ("" if not _fail else f"，失败 {_fail}"))
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
