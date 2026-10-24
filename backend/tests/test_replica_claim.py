"""多副本执行语义验证（不联网）：跨实例认领 run（owner/lease）+ 续租 + 归还。

对应「崩溃恢复的接手权」：单机时每个实例都恢复全部未完成 run；多副本下那会把同一条
在途执行重放两遍。这里钉的是服务端判据——两步纯 AND 的原子认领（先无主、再租约过期），
同一 run 同一时刻只被一个实例持有。HITL 审批挂起态天然不在恢复候选里。

自动重放的候选只含 ``running``：被 kill 的 run 来不及写状态，留在 running 才是恢复的正主；
写成 ``failed`` 的已被收口处理器定性、并向用户报过失败，重启就重放等于替用户重按「继续」
还附满预算（末段钉这条，连同「用户显式的两条继续路径仍看得见 failed」）。

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

    # ---- 定性过的失败**不**进自动重放 ----
    # 真机炸过一次：force-recreate 之后崩溃恢复认领了 8 条 run，其中一条 03:43 被配额
    # 拦下、零模型调用的 failed run 在 05:29 被自动重放，给了它整 40 迭代预算
    # （它停在 iteration=0），烧掉用户从没要求过的 1,953,686 token、一张计划卡都没出。
    # 分界线是「这行有没有被谁定性过」：被 kill 的来不及写状态、留在 running（恢复的正主，
    # 上面 a/b/p 三条都是 running 且都进得了认领）；写成 failed 的已经向用户报过失败，
    # 重放它等于替用户重按一次「继续」。
    await mgr.save(Checkpoint(run_id="quota", session_id="u:c", message="薄肌理论",
                              iteration=0, messages=[], plan_run_id="plan-x"))
    cp_q = await mgr.load("quota")
    await mgr.mark_failed(cp_q, iteration=0, messages=[])

    got5 = {c.run_id for c in await mgr.claim_recoverable("inst-3")}
    check("quota" not in got5,
          f"failed 的 run 不在启动认领候选里（不自动重放）：{sorted(got5)}")
    check("b" in got5 or "p" in got5,
          f"收窄没有误伤 running：{sorted(got5)}")
    row_q = await storage.db.get_by_pk("checkpoints", {"run_id": "quota"})
    check(row_q["owner_instance_id"] is None,
          "没被认领的 failed 行仍无主（不会被谎报成某实例在途）")
    # 两条用户发起的路各自按 run_id / 按会话查，都不经过 claim_recoverable。
    check(await mgr.resume("quota") is not None,
          "「继续」按 run_id 仍捞得回它（/runs/{run_id}/resume 那条口）")
    back = await mgr.interrupted_execution_for_session("u:c")
    check(back is not None and back.run_id == "quota",
          f"同会话发消息时仍认它是被打断的执行轮：{back.run_id if back else None}")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())