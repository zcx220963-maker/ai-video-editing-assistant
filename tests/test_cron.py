"""CronTool 验证（不联网）：PG scheduled_jobs + 真实 asyncio 定时器 + 单次/周期 + 崩溃恢复。

对应 spec §8「Cron 定时任务 → PG scheduled_jobs」：原来是一个整 JSON 文件原子覆写，
多副本会互踩；现在一行一个任务，领取一轮走 state 比较-交换。

运行：  python tests/test_cron.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry, is_tool_error
from agent_framework.tools.cron import CronJob, CronScheduler, register_cron_tools

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def by_name(rows: list[dict], name: str) -> dict | None:
    return next((r for r in rows if r["name"] == name), None)


async def main() -> None:
    storage = build_storage("memory")
    await storage.start()
    fired: list[str] = []

    async def runner(job):
        fired.append(job.task)

    sched = CronScheduler(storage, runner=runner)

    # 读写并发标记
    reg = ToolRegistry()
    register_cron_tools(reg, sched)
    check(reg.get("list_cron_jobs").concurrency_safe, "list_cron_jobs 可并发 (read_only)")
    check(not reg.get("create_cron_job").concurrency_safe, "create_cron_job 不可并发")
    check(not reg.get("delete_cron_job").concurrency_safe, "delete_cron_job 不可并发")

    # ---- 单次任务：建 → 落表 → 到点执行 → 执行后删除 ----
    job = await sched.create_job(task="发周报", name="weekly", kind="at",
                                 run_at=None, delete_after_run=True)
    rows = await storage.db.select("scheduled_jobs")
    check(len(rows) == 1 and rows[0]["name"] == "weekly" and rows[0]["id"] == job.id,
          "create 就是 scheduled_jobs 的一行（不再有 cron_jobs.json）")
    check(rows[0]["schedule"] == {"kind": "at", "at_ms": job.schedule.at_ms, "every_ms": None}
          and rows[0]["state"]["next_run_at_ms"] == job.schedule.at_ms,
          "schedule/state 作为 jsonb 子文档原样往返")
    n = await sched.run_due_now()
    check(n == 1 and fired == ["发周报"], f"单次任务执行一次: fired={fired}")
    check(await sched.list_jobs() == [], "delete_after_run 触发后自动删除该行")

    # ---- CRUD 工具：创建周期任务 + 列表 + 删除 ----
    r = await reg.execute("create_cron_job", {"task": "每5秒打卡", "kind": "every", "every_seconds": 5})
    check("已创建" in r, f"create 工具: {r[:30]}")
    r = await reg.execute("list_cron_jobs", {})
    check("每5秒打卡" in r, "list 工具能列出任务")
    jid = (await sched.list_jobs())[0].id
    r = await reg.execute("delete_cron_job", {"job_id": jid})
    check("已删除" in r and await sched.list_jobs() == [], "delete 工具删除成功")
    r = await reg.execute("delete_cron_job", {"job_id": "nope"})
    check(is_tool_error(r), "delete 不存在任务报错")

    # ---- 撞名：name 是唯一约束，第二个同名任务加短后缀共存 ----
    await sched.create_job(task="甲", name="dup", kind="every", every_seconds=60)
    await sched.create_job(task="乙", name="dup", kind="every", every_seconds=60)
    names = [j.name for j in await sched.list_jobs()]
    check("dup" in names and any(x.startswith("dup#") for x in names),
          f"同名任务不覆盖、不炸唯一约束：{names}")
    for j in await sched.list_jobs():
        await sched.delete_job(j.id)

    # ---- 周期任务：真实定时器驱动，应触发多次 ----
    sched2 = CronScheduler(storage, runner=runner)
    await sched2.create_job(task="tick", name="ticker", kind="every",
                            every_seconds=0.05, delete_after_run=False)
    await sched2.start()
    await asyncio.sleep(0.3)
    sched2.stop()
    check(fired.count("tick") >= 3, f"周期任务在定时器下多次触发: {fired.count('tick')} 次")
    tick_row = by_name(await storage.db.select("scheduled_jobs"), "ticker")
    check(tick_row is not None and tick_row["enabled"] and tick_row["state"]["last_run_at_ms"],
          "每轮领取就把 next_run 推到下一轮，行保留且仍启用")

    # ---- 新调度器读同一张表（进程重启后任务还在）----
    reloaded = CronScheduler(storage)
    jobs = await reloaded.list_jobs()
    check([j.name for j in jobs] == ["ticker"], "新调度器从 scheduled_jobs 恢复任务")
    check(isinstance(jobs[0], CronJob) and jobs[0].task == "tick", "行 → CronJob 反序列化一致")

    # ---- 崩溃恢复：过期的 every 任务向前推进，不风暴补跑 ----
    await storage.jobs.set_state(jobs[0].id, {"next_run_at_ms": int(time.time() * 1000) - 10000,
                                              "last_run_at_ms": None})
    rec = CronScheduler(storage, runner=runner)
    before = len(fired)
    await rec.start()          # 恢复：把 next_run 推进到未来
    await asyncio.sleep(0.05)
    rec.stop()
    j = (await rec.list_jobs())[0]
    check(j.state.next_run_at_ms >= int(time.time() * 1000) - 100,
          f"过期任务被推进到未来: 下次={j.state.next_run_at_ms}")
    check(len(fired) - before <= 1, f"没有风暴式补跑：start 后额外触发 {len(fired) - before} 次")

    # ---- 多副本：同一轮到点的 job 只被执行一次（state 比较-交换）----
    two = build_storage("memory")
    await two.start()
    got1: list[str] = []
    got2: list[str] = []
    s_a = CronScheduler(two, runner=lambda j: got1.append(j.name))
    s_b = CronScheduler(two, runner=lambda j: got2.append(j.name))
    await s_a.create_job(task="抢", name="race", kind="at", run_at=None, delete_after_run=False)
    await asyncio.gather(s_a.run_due_now(), s_b.run_due_now())
    check(got1 == ["race"] and got2 == [], f"两个副本只有一个领到这一轮：{got1} / {got2}")
    raced = await two.jobs.get_by_name("race")
    check(raced is not None and not raced["enabled"] and raced["state"]["next_run_at_ms"] is None,
          "单次任务跑完置灰禁用（enabled 只做用户意图，不当锁用）")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
