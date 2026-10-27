# -*- coding: utf-8 -*-
"""R1-c：broker 不可达时 10 秒内快速失败，且错误可动作。

生产默认 Kafka（D1）。这条用例钉的是「启动失败要说人话」：
  * 10 秒内抛错，不挂在那里等默认重试；
  * 错误里带 `KAFKA_BOOTSTRAP_SERVERS` 与 `--mq memory`（用户照着就能改）；
  * stop_reason 落 BROKER_DOWN，供上层决定退出码/重启策略；
  * 半建的 producer 被收掉（不留下 Unclosed 噪音）。

同时给一个正控（memory 队列能正常起停），证明失败不是「所有后端都起不来」。

运行：  python tests/test_mq_startup.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import asyncio

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.mq import (InMemoryMessageQueue, KafkaMessageQueue,
                                build_message_queue)
from agent_framework.worker import AgentWorker, StopReason

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def broker_down() -> None:
    print("\n[1] broker_down：不可达 broker 在 10s 内失败且错误可动作")
    # 端口 1 必然拒连：不依赖本机 9092 上是否真有 Kafka
    q = KafkaMessageQueue(bootstrap_servers="127.0.0.1:1")
    worker = AgentWorker(q, handler=lambda payload: None)

    started = time.monotonic()
    err: BaseException | None = None
    try:
        await worker.start()
    except BaseException as exc:  # noqa: BLE001
        err = exc
    elapsed = time.monotonic() - started

    check(err is not None, "broker 不可达时 start() 抛错（不静默挂起）")
    check(elapsed < 10.0, f"10 秒内失败（实测 {elapsed:.2f}s）")
    msg = str(err or "")
    check("KAFKA_BOOTSTRAP_SERVERS" in msg,
          f"错误里点名 KAFKA_BOOTSTRAP_SERVERS：{msg[:120]}")
    check("--mq memory" in msg, "错误里给出本地单进程退路 --mq memory")
    check(worker.stop_reason == StopReason.BROKER_DOWN,
          f"stop_reason=BROKER_DOWN（实际 {worker.stop_reason}）")
    check(q._producer is None, "半建的 producer 已收掉（不泄漏连接）")


async def memory_control() -> None:
    print("\n[2] 正控：memory 后端正常起停")
    q = InMemoryMessageQueue()
    got: list[dict] = []
    q.subscribe("chat", "g", got.append)
    await q.start()
    await q.publish("chat", "s1", {"message": "hi"})
    await q.drain(timeout=2.0)
    await q.stop()
    check([p["message"] for p in got] == ["hi"], f"memory 队列投递成功：{got}")


def cli_defaults() -> None:
    print("\n[3] CLI 默认：生产 Kafka（D1）、团队/定时任务文件真相（R14/R19）")
    import run_server

    saved = sys.argv
    saved_env = {k: os.environ.pop(k) for k in
                 ("MQ_BACKEND", "KAFKA_BOOTSTRAP_SERVERS", "TEAM_STORE", "CRON_STORE")
                 if k in os.environ}
    try:
        sys.argv = ["run_server.py"]
        args = run_server.parse_args()
        check(args.mq == "kafka", f"不传 --mq 时默认 kafka（实际 {args.mq}）")
        check(args.bootstrap_servers == "localhost:9092",
              f"bootstrap 默认 localhost:9092（实际 {args.bootstrap_servers}）")
        check(args.team_store == "file", f"团队默认文件真相（实际 {args.team_store}）")
        check(args.cron_store == "file", f"定时任务默认文件真相（实际 {args.cron_store}）")

        os.environ["MQ_BACKEND"] = "memory"
        os.environ["KAFKA_BOOTSTRAP_SERVERS"] = "kafka:9092"
        sys.argv = ["run_server.py"]
        args = run_server.parse_args()
        check(args.mq == "memory" and args.bootstrap_servers == "kafka:9092",
              "环境变量可覆盖默认（部署不改命令行）")
    finally:
        sys.argv = saved
        for k, v in saved_env.items():
            os.environ[k] = v
        for k in ("MQ_BACKEND", "KAFKA_BOOTSTRAP_SERVERS"):
            os.environ.pop(k, None)
        for k, v in saved_env.items():
            os.environ[k] = v


def factory() -> None:
    print("\n[4] 工厂：按名字构造，未知后端立即报错")
    check(isinstance(build_message_queue("memory"), InMemoryMessageQueue), 'build_message_queue("memory")')
    check(isinstance(build_message_queue("kafka"), KafkaMessageQueue), 'build_message_queue("kafka")')
    try:
        build_message_queue("rabbit")
        check(False, "未知后端应报错")
    except ValueError as exc:
        check("memory" in str(exc) and "kafka" in str(exc),
              f"未知后端报错列出可选值：{exc}")


async def main() -> None:
    await broker_down()
    await memory_control()
    cli_defaults()
    factory()
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if _fails == 0 else f"，失败 {_fails}"))
    if _fails:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
