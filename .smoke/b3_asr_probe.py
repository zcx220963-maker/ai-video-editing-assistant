import asyncio, json, sys
sys.stdout.reconfigure(encoding="utf-8")
from agent_framework.storage import build_storage


async def main(sid: str):
    st = build_storage("pg_minio")
    await st.start()
    for node in ("asr", "speech_rough_cut"):
        row = await st.db.get_by_pk("artifacts", {"session_id": sid, "node": node,
                                                  "artifact_id": "_default"})
        print(node, "->", json.dumps(row["payload"] if row else None,
                                     ensure_ascii=False)[:500])
    await st.close()

asyncio.run(main(sys.argv[1]))
