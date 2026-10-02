# -*- coding: utf-8 -*-
"""B3 冒烟卡点取证：只看 PG 三张真相表当前形状，不打印任何密钥。"""

from __future__ import annotations

import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.storage import build_storage


async def main() -> None:
    st = build_storage("pg_minio")
    await st.start()
    try:
        jobs = await st.db.select("render_jobs")
        print(f"--- render_jobs ({len(jobs)}) ---")
        for r in jobs:
            print("   ", {k: r[k] for k in ("session_id", "artifact_id", "status", "stage",
                                            "percent", "video_object_key", "error", "updated_at")})
        arts = await st.db.select("artifacts")
        print(f"--- artifacts ({len(arts)}) ---")
        for r in sorted(arts, key=lambda a: str(a["updated_at"])):
            payload = r.get("payload") or {}
            print(f"   {r['updated_at']}  {r['session_id']}/{r['artifact_id']}  {r['node']}"
                  f"  keys={sorted(payload)[:8]}")
        mats = await st.db.select("materials")
        print(f"--- materials ({len(mats)}) ---")
        for r in mats:
            print("   ", {k: r.get(k) for k in ("id", "origin", "filename", "object_key",
                                                 "duration_sec", "created_at")})
        for t in ("users", "conversations"):
            try:
                rows = await st.db.select(t)
                print(f"--- {t} ({len(rows)}) ---", [list(r)[:6] for r in rows[:5]])
            except Exception as e:  # noqa: BLE001
                print(f"--- {t} 查询失败: {type(e).__name__}: {e}")
    finally:
        await st.close()


asyncio.run(main())
