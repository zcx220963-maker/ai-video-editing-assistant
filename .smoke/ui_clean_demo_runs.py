# -*- coding: utf-8 -*-
"""收掉 .smoke/ui_seed_demo_runs.py 造的执行记录演示数据（含浏览器验证时分叉出来的那条）。

只按演示用的固定 session / user 定点删，删前删后各数一遍全库总量，
免得误伤同一 PG 里开发者 :8000 服务的真实数据。
"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from agent_framework.storage import build_storage

USER = sys.argv[1]
SID = f"{USER}:c_ui_demo"


async def main():
    st = build_storage("pg_minio")
    await st.start()
    try:
        before = {t: await st.db.count(t)
                  for t in ("checkpoints", "checkpoint_entries", "users")}
        print("删前全库总量:", before)
        runs = [r["run_id"] for r in await st.db.select("checkpoints", where={"session_id": SID})]
        print("本演示 session 的 run:", runs)
        for r in runs:
            await st.db.delete("checkpoint_entries", where={"run_id": r})
            await st.db.delete("checkpoints", where={"run_id": r})
        for t in ("artifacts", "render_jobs"):
            for sid in (SID, f"u:{USER}:c:c_ui_demo"):
                n = await st.db.delete(t, where={"session_id": sid})
                print(f"  {t}({sid}) 删 {n} 行")
        await st.db.delete("messages", where={"conv_id": "c_ui_demo"})
        await st.db.delete("materials", where={"owner_user_id": USER})
        for t in ("inbox_messages", "timelines", "memories", "app_secrets"):
            await st.db.delete(t, where={"user_id": USER})
        await st.db.delete("conversations", where={"id": "c_ui_demo"})
        await st.db.delete("users", where={"id": USER})
        after = {t: await st.db.count(t)
                 for t in ("checkpoints", "checkpoint_entries", "users")}
        print("删后全库总量:", after)
        left = await st.db.select("checkpoints", where={"session_id": SID})
        print("残留 run 行:", len(left))
    finally:
        await st.close()


asyncio.run(main())
