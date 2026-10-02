"""Heartbeat 验证（不联网）：心跳就是 scheduled_jobs 里 name='heartbeat' 的那一行 every 任务。

对应 spec §3.11（cron 与心跳同表）与 §8（Heartbeat 落点：同表，name='heartbeat'）。

运行：  python tests/test_heartbeat.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.heartbeat import Heartbeat
from agent_framework.storage import build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def main() -> None:
    storage = build_storage("memory")
    await storage.start()
    beats: list = []

    async def on_beat(job):
        beats.append(job.name)

    hb = Heartbeat(storage, on_beat=on_beat, interval_seconds=999, name="heartbeat")
    await hb.start()

    rows = await storage.db.select("scheduled_jobs")
    check([r["name"] for r in rows] == ["heartbeat"], "心跳是 scheduled_jobs 的一行，不写 hb.json")
    check(rows[0]["schedule"] == {"kind": "every", "at_ms": None, "every_ms": 999000},
          "every 间隔落进 schedule jsonb")
    jobs = await hb.scheduler.list_jobs()
    hb_jobs = [j for j in jobs if j.name == "heartbeat"]
    check(len(hb_jobs) == 1 and hb_jobs[0].schedule.kind == "every", "start 建了一个 every 心跳任务")

    # 再放一个「已到期」的 at 任务（run_at=1 → 1970 年，正数且在过去的时刻），
    # 手动触发一次到期批处理，验证 _fire→on_beat 链路。
    await hb.scheduler.create_job(task="tick", name="manual", kind="at", run_at=1)
    fired = await hb.beat_now()
    check(fired >= 1, "beat_now 触发到期任务")
    check(hb.beats >= 1, "Heartbeat.beats 计数递增")
    check(len(beats) >= 1, "on_beat 回调被调用")

    # 重启幂等：同名心跳不叠加
    hb2 = Heartbeat(storage, on_beat=None, interval_seconds=999, name="heartbeat")
    await hb2.start()
    rows2 = await storage.db.select("scheduled_jobs")
    check(sum(1 for r in rows2 if r["name"] == "heartbeat") == 1,
          f"重启后仍只有一行 heartbeat：{[r['name'] for r in rows2]}")
    check((await storage.jobs.get_by_name("heartbeat"))["state"]["next_run_at_ms"] is not None,
          "重建后心跳重新排上下一次")

    # 多副本：两个实例共用一张 scheduled_jobs，并发启动不许撞唯一键、不许拆别人的台
    before = await storage.jobs.get_by_name("heartbeat")
    hb_a = Heartbeat(storage, on_beat=None, interval_seconds=999, name="heartbeat")
    hb_b = Heartbeat(storage, on_beat=None, interval_seconds=999, name="heartbeat")
    try:
        await asyncio.gather(hb_a.start(), hb_b.start())
        crashed = ""
    except Exception as exc:  # noqa: BLE001
        crashed = str(exc)
    rows3 = await storage.db.select("scheduled_jobs", where={"name": "heartbeat"})
    check(not crashed, f"两个副本并发 start 不报唯一键冲突（{crashed or '无异常'}）")
    check(len(rows3) == 1, f"并发启动后仍只有一行 heartbeat：{[r['name'] for r in rows3]}")
    check(rows3 and rows3[0]["id"] == before["id"],
          "start 是**原地认领**已有行、不是删了重建（删了重建会停掉别的实例的节拍）")
    same = (await storage.jobs.get_by_name("heartbeat"))
    check(same["state"]["next_run_at_ms"] == before["state"]["next_run_at_ms"],
          "间隔没变就沿用别的实例排好的时间，不重排")
    hb_c = Heartbeat(storage, on_beat=None, interval_seconds=42, name="heartbeat")
    await hb_c.start()
    changed = (await storage.jobs.get_by_name("heartbeat"))
    check(changed["id"] == before["id"] and changed["schedule"]["every_ms"] == 42000
          and changed["state"]["next_run_at_ms"] != before["state"]["next_run_at_ms"],
          "间隔改了才按新值重排（同一行、不新增第二条心跳）")

    hb.stop()
    hb2.stop()
    await asyncio.sleep(0)   # 让被 cancel 的调度循环收尾

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
