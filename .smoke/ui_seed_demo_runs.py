# -*- coding: utf-8 -*-
"""给 执行记录 面板造几条真 run 行（指针行 + 一致点链），供浏览器验证用。

不靠对话造 run：那要占模型密钥与分钟级渲染。这里直接用同一套存储 API 写
checkpoints / checkpoint_entries，形状与真实执行落库的一致。
它是验证夹具而不是断言型冒烟——点完记得收：写的是共享 PG。

跑法：
    python run_server.py --storage pg_minio --no-storyline --no-resume --port <空闲端口> …
    python .smoke/ui_seed_demo_runs.py http://127.0.0.1:<端口>     # 打印 SEED_OK 与新建 user_id
    …浏览器验证…
    python .smoke/ui_clean_demo_runs.py <上一步打印的 user_id>
"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
import httpx
from agent_framework.storage import build_storage

BASE = sys.argv[1]
PORT = BASE.rsplit(":", 1)[1]


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        reg = (await c.post("/register", json={"device_name": "ui-seed"})).json()
    user, token = reg["user_id"], reg["token"]
    conv = "c_ui_demo"
    sid = f"{user}:{conv}"
    st = build_storage("pg_minio")
    await st.start()
    try:
        await st.conversations.ensure(user, conv, "时间旅行演示")
        parent = "ui-parent-0001"
        child = "ui-child-0002"
        msgs = [
            [{"role": "system", "content": "你是剪辑助手。"}, {"role": "user", "content": "剪一条 12 秒短片，配 A 曲"}],
            [{"role": "assistant", "content": "先解析素材。", "tool_calls": [{"id": "t1", "name": "load_media", "arguments": {}}]},
             {"role": "tool", "name": "load_media", "content": '{"media":[{"id":"mat-1","path":"/a.mp4"}]}'}],
            [{"role": "assistant", "content": "切镜完成。", "tool_calls": [{"id": "t2", "name": "split_shots", "arguments": {}}]},
             {"role": "tool", "name": "split_shots", "content": '{"clips":3,"shot_count":3}'}],
            [{"role": "assistant", "content": "选乐完成。", "tool_calls": [{"id": "t3", "name": "select_BGM", "arguments": {"query": "A 曲"}}]},
             {"role": "tool", "name": "select_BGM", "content": '{"bgm":"/bgm/A.wav","filename":"A.wav"}'}],
            [{"role": "assistant", "content": "时间线已规划。", "tool_calls": [{"id": "t4", "name": "plan_timeline_pro", "arguments": {}}]},
             {"role": "tool", "name": "plan_timeline_pro", "content": '{"timeline_pro":{"duration":12}}'}],
            [{"role": "assistant", "content": "出片完成。", "tool_calls": [{"id": "t5", "name": "render_video", "arguments": {}}]},
             {"role": "tool", "name": "render_video", "content": '{"video":"/out/_default.mp4"}'},
             {"role": "assistant", "content": "成片已渲染完成。"}],
        ]
        now = int(time.time() * 1000)

        async def add(run_id: str, delta: list, *, iteration: int):
            """seq 由仓库按链尾自增，调用方只交「这一段新增消息」和类型判定。"""
            top = await st.checkpoints.load_entries(run_id)
            await st.checkpoints.append_entry(
                run_id, kind="full" if not top else "delta", payload=delta,
                iteration=iteration,
                parent_seq=int(top[-1]["seq"]) if top else None)

        await st.checkpoints.save({"run_id": parent, "session_id": sid, "message": "剪一条 12 秒短片，配 A 曲",
                                  "iteration": 5, "status": "superseded", "head_seq": -1,
                                  "scope": {"storyline_session": f"u:{user}:c:{conv}", "artifact_id": ""},
                                  "created_at_ms": now - 900_000, "updated_at_ms": now - 300_000})
        for i, delta in enumerate(msgs):
            await add(parent, delta, iteration=i)
        await st.db.update("checkpoints", {"head_seq": len(msgs) - 1, "status": "superseded"},
                           where={"run_id": parent})
        # 分叉出来的那一条：只带到 select_BGM 之前的三个一致点
        await st.checkpoints.save({"run_id": child, "session_id": sid, "message": "背景音乐换成 B 曲",
                                   "iteration": 2, "status": "completed", "head_seq": -1,
                                   "scope": {"storyline_session": f"u:{user}:c:{conv}", "artifact_id": "art-demo01"},
                                   "forked_from": parent, "forked_at_seq": 2,
                                   "created_at_ms": now - 300_000, "updated_at_ms": now - 60_000})
        for i, delta in enumerate(msgs[:3]):
            await add(child, delta, iteration=i)
        await add(child, [
            {"role": "assistant", "content": "换成 B 曲。", "tool_calls": [{"id": "t9", "name": "select_BGM", "arguments": {"query": "B 曲"}}]},
            {"role": "tool", "name": "select_BGM", "content": '{"bgm":"/bgm/B.wav","filename":"B.wav"}'}],
            iteration=3)
        await st.db.update("checkpoints", {"head_seq": 3}, where={"run_id": child})
        # 还挂着的一条，让面板出现「进行中」
        await st.checkpoints.save({"run_id": "ui-running-003", "session_id": sid, "message": "再来一版竖屏",
                                   "iteration": 1, "status": "running", "head_seq": 0, "scope": {},
                                   "created_at_ms": now - 30_000, "updated_at_ms": now - 10_000})
        await add("ui-running-003", msgs[0], iteration=0)
        print("SEED_OK", json.dumps({"user_id": user, "token": token, "conv": conv,
                                    "runs": [parent, child, "ui-running-003"]}))
    finally:
        await st.close()


asyncio.run(main())
