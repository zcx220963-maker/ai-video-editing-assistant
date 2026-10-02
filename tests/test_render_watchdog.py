# -*- coding: utf-8 -*-
"""渲染停滞看门狗：进程活着时，非终态的 render_jobs 行必须有一条收口路径。

钉住五件事（判据全部离线，内存替身存储 + 假渲染体）：
① 崩溃遗留由启动对账收（``reap_hanging``），**进程活着但渲染再也不推进**的那一半由
   看门狗收：``reap_stalled`` 把超阈值仍非终态的行置 failed 并回这些行；
② 被收口时本地那个后台任务要取消——不取消就等于并发槽永久被占、活儿还在漏；
③ 排在并发槽外的 queued 行**不算停滞**：它没进展是因为活儿还没开始，每轮先续期；
④ 收口后的视图形状不变：轮询端（``render_status`` 与 ``GET /render_status`` 共用
   ``render_view``）拿到的是一张终态卡，不是永久的「请继续轮询」；
⑤ ``render_stall_sec=0`` 时看门狗不起（关掉就是关掉，不留半个循环在读库）。

真机形状：``running/encoding/70`` 挂了 23 分钟，``reap_hanging`` 只在 :8001 启动时跑过，
于是「未达终态不许收尾」等价于「这条 run 永不收尾」——本模块就是为了不再出现那种局面。
"""

from __future__ import annotations

import sys as _sys
from datetime import datetime, timedelta, timezone
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from agent_framework.storage import build_storage  # noqa: E402
from storyline_server.render_jobs import (TERMINAL, RenderDispatcher,  # noqa: E402
                                          tool_view)

sys.stdout.reconfigure(encoding="utf-8")

FAILS = 0
STALL = 600.0


def check(cond, label):
    global FAILS
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


async def age(storage, sid: str, art: str, minutes: int = 30) -> None:
    """把行的 updated_at 推到过去：等价于「渲染体已经这么久没写过任何东西」。"""
    await storage.db.update("render_jobs", {
        "updated_at": datetime.now(timezone.utc) - timedelta(minutes=minutes)},
        where={"session_id": sid, "artifact_id": art})


def blocked(stop: asyncio.Event):
    """一个永不返回的渲染体：真机卡死就是这种形状（任务活着，行不再被写）。"""
    async def work() -> None:
        await stop.wait()
    return work


async def case_reap_stalled_rows(storage) -> None:
    print("\n=== ① 超阈值仍非终态：置 failed 并回行 ===")
    jobs = storage.render_jobs
    await jobs.open("w:reap", "a1")
    await age(storage, "w:reap", "a1")
    rows = await jobs.reap_stalled(STALL, reason="渲染停滞 600 秒无进度，已按失败收口")
    check([r["artifact_id"] for r in rows] == ["a1"],
          f"回的是被收口的行（看门狗要靠它取消本地任务）："
          f"{[r['artifact_id'] for r in rows]}")
    row = await jobs.get("w:reap", "a1")
    check(row["status"] == "failed" and "停滞" in row["error"],
          f"非终态行收口成 failed 并写明理由：{row['status']} / {str(row['error'])[:24]}")
    check(not await jobs.reap_stalled(STALL, reason="不该再收到"),
          "终态行不在收口范围内（同一行不被收两次）")
    # 未超阈值的行不受影响：阈值判的是「多久没写」，不是「跑了多久」
    await jobs.open("w:reap", "slow")
    await jobs.progress("w:reap", "slow", "encoding", 71)
    check(not await jobs.reap_stalled(STALL, reason="误伤"),
          "刚写过进度的慢渲染不被收（判据是无进展，不是总耗时）")


async def case_dispatcher_cancels_and_exempts(storage) -> None:
    print("\n=== ②③④ 收口的同时取消本地任务；排队等槽的行先续期 ===")
    stop_a, stop_b = asyncio.Event(), asyncio.Event()
    disp = RenderDispatcher(storage, max_concurrent=2, poll_interval_sec=0.05,
                            stall_sec=STALL)
    await disp.submit("w:slot", "a", blocked(stop_a))
    await asyncio.sleep(0.05)
    check(disp.key("w:slot", "a") in disp._active, "拿到槽的 key 记进 _active")
    await disp.submit("w:slot", "b", blocked(stop_b))
    check(disp.key("w:slot", "b") not in disp._active,
          "两个槽都占着：后提交的那个只在排队，不算在跑")
    await age(storage, "w:slot", "a")      # 在跑但再也不推进
    await age(storage, "w:slot", "b")      # 排队中，同样「很久没写」
    n = await disp.reap_stalled()
    row_a = await storage.render_jobs.get("w:slot", "a")
    row_b = await storage.render_jobs.get("w:slot", "b")
    check(n == 1 and row_a["status"] == "failed",
          f"卡住的在跑行被收口：{row_a['status']} / {str(row_a['error'])[:20]}")
    check(row_b["status"] == "queued",
          f"排队等槽的行只续期不判死：{row_b['status']}")
    fresh = await storage.db.select("render_jobs", where={"artifact_id": "b"}, limit=1)
    check(fresh[0]["updated_at"] > datetime.now(timezone.utc) - timedelta(minutes=1),
          "续期真的写进了 updated_at（下一轮它还是先续期）")
    check(disp.key("w:slot", "a") not in disp._tasks,
          "本地任务已从登记表里摘掉（槽位不再被死渲染占着）")
    snap = await disp.snapshot("w:slot", "a")
    check(snap["status"] in TERMINAL, f"轮询端立刻看得到终态：{snap['status']}")
    view = tool_view(snap)
    check("hint" not in view and view["render"]["status"] == "failed",
          "终态视图不再带「请继续轮询」的提示（等待方有理由收场）")
    stop_b.set()
    await disp.cancel_all()


async def case_watchdog_toggle(storage) -> None:
    print("\n=== ⑤ stall_sec<=0 时看门狗不起 ===")
    off = RenderDispatcher(storage, stall_sec=0.0)
    off.start_watchdog()
    check(off._watchdog is None, "render_stall_sec=0：不建循环")
    on = RenderDispatcher(storage, stall_sec=STALL)
    on.start_watchdog()
    check(on._watchdog is not None, "有阈值：起一个循环")
    second = on._watchdog
    on.start_watchdog()
    check(on._watchdog is second, "重复 start 不起第二个循环")
    await on.stop_watchdog()
    check(on._watchdog is None, "stop 之后循环撤掉（退出过程里不再改库）")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="render_watchdog_"))
    storage = build_storage("memory", cache_root=tmp / "cache",
                            workspace_root=tmp / "ws")
    await storage.start()
    try:
        await case_reap_stalled_rows(storage)
        await case_dispatcher_cancels_and_exempts(storage)
        await case_watchdog_toggle(storage)
    finally:
        await storage.close()
    print()
    print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
