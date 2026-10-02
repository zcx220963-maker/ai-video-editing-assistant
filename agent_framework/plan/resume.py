"""续跑查账：本会话最近一条「确认过、但计划没跑完」的执行轮。

规划轮靠它把未履行清单写进提示段（``prompt.planning_section`` 的 ``pending`` 形参）——
否则用户对那句提问的回答会落进一轮物理不含剪辑节点的规划轮，一张确认过的计划就此续不上。
"""
from __future__ import annotations

from typing import Any, Mapping

from .support import clean

async def pending_continuation(checkpoint: Any, session_id: str) -> Mapping[str, Any] | None:
    """本会话最近一条「确认过、但计划没跑完」的执行轮；没有就回 None。

    真机踩到的死胡同：执行轮跑到一半问了一句「A 还是 B？」就收尾（对账如实记
    「未履行 时间线编排、成片渲染」），用户对 A/B 的回答按默认入口落进**新一轮规划轮**，
    而那张注册表物理不含剪辑节点——模型只能回「我这边没有调用它们的执行入口」，
    一张确认过的计划就此续不上。这里把那份未履行状态查回来，交给规划轮当续跑依据。

    只看最近一条执行轮：它跑完了就没有待续，不会把更早的旧账翻出来拦今天的新诉求。
    """
    if checkpoint is None:
        return None
    for row in await checkpoint.list_for_session(session_id):
        plan_run_id = str(row.get("plan_run_id") or "")
        if not plan_run_id:
            continue                     # 规划轮与普通轮：它们不是「确认后的执行」
        audit = (row.get("plan") or {}).get("audit") or {}
        unfulfilled = [str(n) for n in (audit.get("unfulfilled") or []) if n]
        if not unfulfilled:
            return None                  # 最近一条执行轮把计划跑完了
        candidates = ((await checkpoint.row(plan_run_id) or {}).get("plan") or {}) \
            .get("candidates") or []
        wanted = str(audit.get("plan_id") or "")
        label = next((str(c.get("label") or "") for c in candidates
                      if str(c.get("plan_id") or "") == wanted), "")
        return {"run_id": str(row.get("run_id") or ""), "plan_run_id": plan_run_id,
                "plan_id": wanted, "label": label, "unfulfilled": unfulfilled,
                "asked": clean(audit.get("reason"))[:300]}
    return None
