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

        # ---- claim_run 落 state ----
        await store.set_state("j1", {"next_run_at_ms": 1}, enabled=True)
        ok = await store.claim_run("j1", {"next_run_at_ms": 5, "last_run_at_ms": 3})
        check(ok is True, "claim_run 返回 True")
        got = await store.get_by_name("demo2")
        check(got["state"]["next_run_at_ms"] == 5, "state 被写回")

        # ---- drop ----
        check(await store.drop_id("j1") is True, "drop 删掉一行")
        check(not await store.list(), "删完为空")

        # ---- 外壳：CronScheduler 只读 .jobs ----
        shell = CronFileStorage(path)
        check(hasattr(shell, "jobs"), "CronFileStorage 暴露 .jobs")

        # ---- 坏文件不炸 ----
        path.write_text("{not json", encoding="utf-8")
        check(await store.list() == [], "坏 JSON 退化成空表而不是抛错")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
