"""多副本执行语义验证（不联网）：跨实例认领 run（owner/lease）+ 续租 + 归还。

对应「崩溃恢复的接手权」：单机时每个实例都恢复全部未完成 run；多副本下那会把同一条
在途执行重放两遍。这里钉的是服务端判据——两步纯 AND 的原子认领（先无主、再租约过期），
同一 run 同一时刻只被一个实例持有。HITL 审批挂起态天然不在恢复候选里。

运行：  python tests/test_replica_claim.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.checkpoint import (
    Checkpoint,
    CheckpointManager,
    STATUS_AWAITING_APPROVAL,
    STATUS_RUNNING,
)
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
    mgr = CheckpointManager(storage)

    # ---- 认领：无主 run 被一个实例领走，另一个抢不到（跨实例互斥）----
    await mgr.save(Checkpoint(run_id="a", session_id="u:c", message="x", iteration=0, messages=[]))
    await mgr.save(Checkpoint(run_id="b", session_id="u:c", message="y", iteration=0, messages=[]))

    got1 = {c.run_id for c in await mgr.claim_recoverable("inst-1")}
    check(got1 == {"a", "b"}, f"实例1 认领全部无主 run: {sorted(got1)}")

    got2 = {c.run_id for c in await mgr.claim_recoverable("inst-2")}
    check(got2 == set(), f"实例2 抢不到已被实例1 持有的 run（租约未过期）: {sorted(got2)}")

    row = await storage.db.get_by_pk("checkpoints", {"run_id": "a"})
    check(row["owner_instance_id"] == "inst-1" and row["lease_expires_at"] is not None,
          "认领写进 owner_instance_id / lease_expires_at")
    check(row["status"] == STATUS_RUNNING, "认领不改状态（只换归属）")

    # ---- 续租：只对持有者生效 ----
    check(await mgr.renew_lease("a", "inst-1") is True, "持有者能续自己那条的租约")
    check(await mgr.renew_lease("a", "inst-2") is False, "非持有者续不动别人的租约")

    # ---- 归还：释放后另一实例能接手 ----
    await mgr.release("a", "inst-1")
    row_a = await storage.db.get_by_pk("checkpoints", {"run_id": "a"})
    check(row_a["owner_instance_id"] is None and row_a["lease_expires_at"] is None,
          "release 清空 owner/lease")
    got3 = {c.run_id for c in await mgr.claim_recoverable("inst-2")}
    check(got3 == {"a"}, f"归还后实例2 能接手: {sorted(got3)}")

    # ---- 租约过期：持有者长时间没续租，别的实例可抢 ----
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    await storage.db.update("checkpoints", {"lease_expires_at": past}, where={"run_id": "b"})
    got4 = {c.run_id for c in await mgr.claim_recoverable("inst-2")}
    check(got4 == {"b"}, f"租约过期后可被其他实例接手: {sorted(got4)}")

    # ---- 认领的 run 在迭代边界顺带续租（内存里那份 owner 被带去续）----
    cp_a = await mgr.load("a")
    cp_a.owner_instance_id = "inst-2"
    before = (await storage.db.get_by_pk("checkpoints", {"run_id": "a"}))["lease_expires_at"]
    await mgr.save_progress(cp_a, iteration=1, messages=[])
    after = (await storage.db.get_by_pk("checkpoints", {"run_id": "a"}))["lease_expires_at"]
    check(after >= before, "save_progress 顺带续租（迭代边界就是心跳点）")

    # ---- 收尾归还：completed 后不再持有 ----
    await mgr.complete(cp_a, messages=[])
    row_done = await storage.db.get_by_pk("checkpoints", {"run_id": "a"})
    check(row_done["owner_instance_id"] is None, "complete 后归还认领")

    # ---- HITL 审批挂起不在崩溃恢复候选里 ----
    await mgr.save(Checkpoint(run_id="p", session_id="u:c", message="z", iteration=0, messages=[]))
    cp_p = await mgr.load("p")
    await mgr.await_approval(
        cp_p, iteration=1, messages=[],
        pending_calls=[{"id": "c1", "name": "render_video"}], reason="渲染需人工确认")
    check(cp_p.status == STATUS_AWAITING_APPROVAL, "await_approval 把 run 挂成 awaiting_approval")
    reloaded = await mgr.load("p")
    check(reloaded.approval.get("pending_calls") and reloaded.approval.get("reason") == "渲染需人工确认",
          "待批动作与理由落进 approval 列")
    pend = {c.run_id for c in await mgr.pending()}
    check("p" not in pend, "审批挂起的不在崩溃恢复候选里（等人工，不等重启）")
    check(await mgr.pending_approval("p") is not None, "pending_approval 能读回挂起的审批 run")
    check(await mgr.pending_approval("b") is None, "非审批态的 run 读不出待批")

    # ---- 批准/拒绝后放回 running ----
    await mgr.clear_approval(reloaded)
    check(reloaded.status == STATUS_RUNNING and reloaded.approval == {},
          "clear_approval 放回 running 并清空 approval")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())