# -*- coding: utf-8 -*-
"""R4：外层循环有名字（AgentWorker）+ 迭代上限 + 同会话串行的兜底计数。"""

from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.agent import AgentConfig  # noqa: E402
from agent_framework.worker import AgentWorker, StopReason  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def main() -> None:
    # ---- 迭代上限：默认 200（与运行期一致）----
    check(AgentConfig().max_iterations == 200,
          f"max_iterations 默认 200（实际 {AgentConfig().max_iterations}）")
    check(AgentConfig(max_iterations=3).max_iterations == 3, "可配")

    # ---- 停止原因枚举化 ----
    for reason in ("SHUTDOWN", "ERROR", "BROKER_DOWN", "CANCELLED"):
        check(hasattr(StopReason, reason), f"StopReason 有 {reason}")

    # ---- AgentWorker：订阅 + 生命周期 ----
    class FakeMQ:
        def __init__(self) -> None:
            self.subs = []
            self.started = 0

        def subscribe(self, topic, group, handler):
            self.subs.append((topic, group, handler))

        async def start(self):
            self.started += 1

        async def stop(self):
            pass

    mq = FakeMQ()
    worker = AgentWorker(mq, handler=lambda p: None, topic="chat", group="agent")
    worker.subscribe()
    check(mq.subs and mq.subs[0][0] == "chat", "subscribe 注册到 chat")

    # ---- broker 起不来 → BROKER_DOWN + 可动作的错误 ----
    class DeadMQ(FakeMQ):
        async def start(self):
            raise ConnectionError("no broker")

    import asyncio
    dead = AgentWorker(DeadMQ(), handler=lambda p: None)
    try:
        asyncio.run(dead.start())
        check(False, "broker 不可达应抛错")
    except RuntimeError as exc:
        check(dead.stop_reason is StopReason.BROKER_DOWN, "停止原因记为 BROKER_DOWN")
        check("KAFKA_BOOTSTRAP_SERVERS" in str(exc) and "--mq memory" in str(exc),
              "错误信息可动作（给出两个排查方向）")

    # ---- 同会话串行由 actor 队列保证：agent 里有 per-session 锁兜底 ----
    src = (BACKEND / "agent_framework" / "agent.py").read_text(encoding="utf-8")
    check("_session_locks" in src or "per-session" in src or "_lock" in src,
          "agent 侧仍有 per-session 兜底锁")
    check("AgentWorker" in (BACKEND / "agent_framework" / "server.py").read_text(encoding="utf-8"),
          "server 用 AgentWorker 订阅（外层循环有名字）")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
