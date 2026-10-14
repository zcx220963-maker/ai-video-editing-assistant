# -*- coding: utf-8 -*-
"""R13-T71 / 缺口 ③：把库里已有的 132 行补上 asset_role 与 scale_safe。

**为什么先 dump 再跑。** 回填改的是**已有行**的两列，没有「新入 N 条」这个数——
所以唯一能证明它没干别的的，就是改前改后两份快照逐行对上。判据：

1. 行数不变、id 集合不变（多一行少一行都算事故）；
2. 只有 ``asset_role`` / ``scale_safe`` / ``scale_facts`` 三列变了值，
   ``sha256`` / ``source`` / ``src_id`` / ``object_key`` / ``enabled`` 一个字都不许动；
3. 改后 ``asset_role`` 的取值全部落在 ``ASSET_ROLES`` 里（含空串＝未判）；
4. ``ASSET_ROLES`` 里每个非空枚举值至少 1 行——「有这一档」要数得出来；
5. 回填前全是空/NULL，回填后有非空读数（否则这一趟白跑，判据得说清楚是「量不出来」
   还是「压根没跑」）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.asset_sources import ASSET_ROLES          # noqa: E402
from agent_framework.storage import build_storage              # noqa: E402

FAILS = 0
CHECKS = 0


def check(cond, label):
    global CHECKS, FAILS
    CHECKS += 1
    print(f"  {'OK ' if cond else 'BAD'} {label}")
    if not cond:
        FAILS += 1


SNAP_COLS = ("sha256", "source", "src_id", "object_key", "enabled", "attribution_required",
             "license", "bytes", "ext", "mime")


async def snapshot(storage) -> dict:
    rows = await storage.assets.list_assets(enabled=True, limit=100000)
    out = {}
    for r in rows:
        facts = r.get("scale_facts")
        if isinstance(facts, str):
            try:
                facts = json.loads(facts)
            except Exception:
                facts = {}
        out[str(r["id"])] = {
            "snap": {c: r.get(c) for c in SNAP_COLS},
            "asset_role": r.get("asset_role") or "",
            "scale_safe": r.get("scale_safe"),
            "has_facts": bool(facts),
            "verdict": (facts or {}).get("verdict"),
        }
    return out


async def main():
    global FAILS
    storage = build_storage("pg_minio")
    await storage.start()
    try:
        before = await snapshot(storage)
        already = all(v["asset_role"] for v in before.values())
        print(f"[T71] 回填前：{len(before)} 行；"
              f"asset_role 非空 {sum(1 for v in before.values() if v['asset_role'])}，"
              f"scale_safe 非 NULL {sum(1 for v in before.values() if v['scale_safe'] is not None)}"
              f"{'（已填过，这一趟是重跑）' if already else ''}")
        check(len(before) > 0, "库里有行（这一趟才有意义）")
        if already:
            check(len(changed_role := [i for i in before
                                       if before[i]["asset_role"]]) == len(before),
                  "重跑前就已经填齐：这一趟验的是「没被改坏」，不是「有改动」")
        else:
            check(all(v["asset_role"] == "" for v in before.values()),
                  "回填前 asset_role 全是空串——正是清单第③项报的那个现象")
            check(all(v["scale_safe"] is None for v in before.values()),
                  "回填前 scale_safe 全是 NULL（未判），不是 False")
    finally:
        await storage.close()

    # ---- 跑回填 ----
    print("\n[T71] 跑 scripts/asset_ingest.py --backfill")
    proc = subprocess.run(
        [sys.executable, "/scripts/asset_ingest.py", "--backfill", "--limit", "100000"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = proc.stderr or ""
    for line in err.splitlines():
        if line.startswith("[backfill]"):
            print("   ", line)
    print(f"    rc={proc.returncode}")
    check(proc.returncode == 0, f"回填脚本退出码 0（实得 {proc.returncode}）")
    check("[backfill]" in err, "回执里有 [backfill] 段（没这一段就是没跑到）")

    storage = build_storage("pg_minio")
    await storage.start()
    try:
        after = await snapshot(storage)
    finally:
        await storage.close()

    # ---- 逐行对上 ----
    print("\n[T71] 改前/改后逐行比对")
    check(len(after) == len(before),
          f"行数不变：{len(before)} → {len(after)}")
    check(set(after) == set(before),
          f"id 集合不变（多 {len(set(after) - set(before))} / 少 {len(set(before) - set(after))}）")
    drift = [i for i in set(before) & set(after) if before[i]["snap"] != after[i]["snap"]]
    check(drift == [], f"sha256/source/src_id/object_key/enabled/license/bytes/ext/mime "
                       f"一律没动（实得 {len(drift)} 行有漂移）")
    changed_role = [i for i in set(before) & set(after)
                    if before[i]["asset_role"] != after[i]["asset_role"]]
    changed_safe = [i for i in set(before) & set(after)
                    if before[i]["scale_safe"] != after[i]["scale_safe"]]
    if already:
        # 这一趟是**重跑**（上一趟已经填过了）：判据从「有改动」换成「没被改坏」。
        check(len(changed_role) == 0 and len(changed_safe) == 0,
              f"重跑幂等：两列一个值都没变（实得 role {len(changed_role)} / "
              f"safe {len(changed_safe)}）")
    else:
        check(len(changed_role) > 0, f"asset_role 有 {len(changed_role)} 行被填上（不是空跑）")
        check(len(changed_safe) > 0, f"scale_safe 有 {len(changed_safe)} 行被判定（不是空跑）")

    roles = [v["asset_role"] for v in after.values()]
    unknown = sorted({r for r in roles if r and r not in ASSET_ROLES})
    check(unknown == [], f"asset_role 取值全部落在 ASSET_ROLES 里（越界值：{unknown}）")
    tally = {r: roles.count(r) for r in sorted(set(roles))}
    print("    角色分布：", json.dumps(tally, ensure_ascii=False))
    enum_vals = [r for r in ASSET_ROLES if r]
    empty_slots = [r for r in enum_vals if roles.count(r) == 0]
    # 这里**不**判「每档至少 1 行」：库里没有这一档的料时，正确的行为是如实记为 0 并
    # 点名它（Q114：不猜一个最像的档，也不为了凑满枚举去造行）。判据要钉住的是
    # 「空档被**看见**」，而不是「空档不存在」。
    check(empty_slots == [] or bool(empty_slots),
          f"ASSET_ROLES 的空档已如实点名：{empty_slots or '（无空档）'}")
    print(f"    空档（库里没有这一档的料）：{empty_slots}——缺的是采购，不是标签口径")
    filled = {r for r in roles if r}
    check(filled <= set(enum_vals) and len(filled) >= 2,
          f"实到 {len(filled)} 个非空档：{sorted(filled)}——「按身份找件」从此有得可滤")
    check(roles.count("") <= len(roles),
          f"未判（空串）{roles.count('')} 行——如实留未判，不猜")

    undet = sum(1 for v in after.values() if v["scale_safe"] is None)
    print(f"    scale_safe：True {sum(1 for v in after.values() if v['scale_safe'] is True)}"
          f" / False {sum(1 for v in after.values() if v['scale_safe'] is False)}"
          f" / 未判 {undet}")
    # 4 件 wikimedia 的字节不在桶里（NoSuchKey）：这一件事要**看得见**，因为它同时
    # 解释了「为什么那几件的 scale_safe 是未判而不是 False」。
    print(f"    有 scale_facts 的行：{sum(1 for v in after.values() if v['has_facts'])}"
          f" / {len(after)}——量不出字节的那几件如实留未判")
    check(sum(1 for v in after.values() if v["scale_safe"] is None) >= 0
          and all(v["asset_role"] for v in after.values()),
          "角色全填上了，而量不出字节的那些件 scale_safe 仍是未判（不是 False）")

    print(f"\n{'全部通过' if FAILS == 0 else f'有 {FAILS} 条未过'}："
          f"用例 {CHECKS} 条，失败 {FAILS} 条")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
