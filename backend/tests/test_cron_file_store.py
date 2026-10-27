# -*- coding: utf-8 -*-
"""R14：Cron 文件后端——jobs.json 可读回、at/every 语义、抢占一次。"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.cron_store import CronFileStorage, FileCronStore  # noqa: E402
from agent_framework.tools.cron import CronScheduler  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


async def two_schedulers_single_claim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "jobs.json"

        # 两份 store 指着同一份文件（= 同机两个实例）：同一轮只有一个能认领
        sa, sb = FileCronStore(path), FileCronStore(path)
        await sa.upsert({"id": "j9", "name": "抢", "task": "t", "enabled": True,
                         "schedule": {"kind": "at", "at_ms": 1},
                         "state": {"next_run_at_ms": 1, "last_run_at_ms": None}})
        old = {"next_run_at_ms": 1, "last_run_at_ms": None}
        new = {"next_run_at_ms": None, "last_run_at_ms": 5}
        first, second = await asyncio.gather(
            sa.claim_run("j9", old, new_state=new, enabled=False),
            sb.claim_run("j9", old, new_state=new, enabled=False),
        )
        check(sorted([first, second]) == [False, True],
              f"同一轮两次认领只有一个赢（{first} / {second}）")
        row = (await sa.list())[0]
        check(row["state"] == new and row["enabled"] is False,
              f"赢家写入新状态并关掉单次任务（{row['state']} / {row['enabled']}）")

        # 调度器层：两个 CronScheduler 各持一份 store，同一到期任务只跑一次
        sched_a = CronScheduler(CronFileStorage(path.parent / "a-jobs.json"))
        sched_b = CronScheduler(CronFileStorage(path.parent / "b-jobs.json"))
        fired: list[str] = []

        async def runner(job) -> None:
            fired.append(job.id)
            await asyncio.sleep(0.02)

        sched_a._runner = runner
        sched_b._runner = runner
        job = await sched_a.create_job("剪片", name="once", kind="at", run_at=1)
        # 让两个调度器看到同一个文件：b 也指向 a 的文件
        sched_b._jobs = sched_a._jobs
        counts = await asyncio.gather(sched_a.run_due_now(), sched_b.run_due_now())
        check(sum(counts) == 1, f"两个调度器合计只执行一轮（{counts}）")
        check(fired == [job.id], f"runner 只被调一次（{fired}）")
        check(await sched_a.list_jobs() == [], "单次任务跑完按 delete_after_run 删掉")

        # every 任务：领取时把 next_run_at_ms 推到未来，立刻再 tick 不会重跑
        every = await sched_a.create_job("周期", name="tick", kind="every",
                                         every_seconds=60)
        await sched_a._jobs.set_state(every.id, {"next_run_at_ms": 1})
        fired.clear()
        check(await sched_a.run_due_now() == 1, "every 任务到期先跑一轮")
        check(await sched_a.run_due_now() == 0,
              "同一轮立刻再 tick 不重跑（next_run 已推进）")
        got = await sched_a._jobs.get_by_name("tick")
        check((got.get("state") or {}).get("next_run_at_ms", 0) > 1,
              "next_run_at_ms 已推进到未来")
        check(got.get("enabled") is True, "every 任务跑完仍启用")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "jobs.json"
        store = FileCronStore(path)

        # ---- 落盘可读回 ----
        row = {"id": "j1", "name": "demo", "task": "剪片", "enabled": True,
               "schedule": {"kind": "at", "at_ms": 1},
               "state": {"next_run_at_ms": 1, "last_run_at_ms": None}}
        await store.upsert(row)
        check(path.exists(), "jobs.json 已落盘")
        data = json.loads(path.read_text(encoding="utf-8"))
        check(len(data["jobs"]) == 1, "文件里有一行")
        check((await store.list())[0]["name"] == "demo", "list 读得回")

        # ---- upsert 不重复 ----
        await store.upsert({**row, "name": "demo2"})
        check(len(await store.list()) == 1, "同 id upsert 不产生第二行")
        check((await store.get_by_name("demo2"))["id"] == "j1", "按名查得到")

        # ---- due：只挑 enabled 且到期的 ----
        due = await store.due(now_ms=1000)
        check(len(due) == 1, f"到期任务被挑出（{len(due)}）")
        await store.set_state("j1", {"next_run_at_ms": 10 ** 12}, enabled=True)
        check(not await store.due(now_ms=1000), "未到期不再挑出")
        await store.set_state("j1", {"next_run_at_ms": 1}, enabled=False)
        check(not await store.due(now_ms=1000), "disabled 不挑出")

        # ---- claim_run 是 CAS：期望状态匹配才写新状态 ----
        await store.set_state("j1", {"next_run_at_ms": 1}, enabled=True)
        ok = await store.claim_run(
            "j1", {"next_run_at_ms": 1},
            new_state={"next_run_at_ms": 5, "last_run_at_ms": 3})
        check(ok is True, "期望状态匹配 → 领取成功")
        got = await store.get_by_name("demo2")
        check(got["state"] == {"next_run_at_ms": 5, "last_run_at_ms": 3},
              f"领取时写的是**新**状态（实际 {got['state']}）")
        stale = await store.claim_run(
            "j1", {"next_run_at_ms": 1}, new_state={"next_run_at_ms": 9})
        check(stale is False, "期望状态已过期 → 领不到（不再恒 True）")
        check((await store.get_by_name("demo2"))["state"]["next_run_at_ms"] == 5,
              "领不到时状态不被覆盖")

        # ---- drop ----
        check(await store.drop_id("j1") is True, "drop 删掉一行")
        check(not await store.list(), "删完为空")

        # ---- 外壳：CronScheduler 只读 .jobs ----
        shell = CronFileStorage(path)
        check(hasattr(shell, "jobs"), "CronFileStorage 暴露 .jobs")

        # ---- 坏文件不炸 ----
        path.write_text("{not json", encoding="utf-8")
        check(await store.list() == [], "坏 JSON 退化成空表而不是抛错")

    # ---- R14-c：两个调度器/两个 store 抢同一轮，只有一个执行 ----
    await two_schedulers_single_claim()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
