import asyncio, sys
sys.stdout.reconfigure(encoding="utf-8")
from agent_framework.storage import build_storage

async def main():
    st = build_storage("pg_minio")
    await st.start()
    rows = await st.db.select("artifacts", where={"session_id": "u:u_direct:c:c_b3d_1789953233"})
    for r in rows:
        print(r["node"], r["artifact_id"], len(str(r["payload"])))
    print("count", len(rows))
    await st.close()

asyncio.run(main())
