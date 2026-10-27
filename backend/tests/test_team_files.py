# -*- coding: utf-8 -*-
"""R19：Agent Team 文件真相（设计 §24）。

覆盖：
  * 收件箱 messages/{scope}/{name}.jsonl：落盘、peek 不清、read 后清空、dedup、隔离。
  * 任务板 tasks/{scope}/task_*.json：状态 pending → on_progress → completed、
    依赖完成后 blockedBy 自动清空、blocks 反向视图、BFS 闭包。
  * 子 Agent 档案 agents/{scope}/{name}.json：Prompt + 状态 + 租约归还。
  * PG 投影可重建：文件是真相，清空/损坏后能从文件重建协作三表。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.task_manager import (  # noqa: E402
    COMPLETED,
    ON_PROGRESS,
    PENDING,
    TaskError,
    TaskManager,
)
from agent_framework.team_files import (  # noqa: E402
    FileInbox,
    FileSubagentRegistry,
    FileTaskBoard,
    rebuild_projection,
    select_team_store,
)

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


async def inbox_checks(tmp: pathlib.Path) -> None:
    inbox = FileInbox(tmp)

    await inbox.send("u1", "c1", "writer", {"content": "hello"}, sender="main")
    path = tmp / "messages" / "u1" / "c1" / "writer.jsonl"
    check(path.exists(), "收件箱落到 messages/{user}/{conv}/{agent}.jsonl")
    check(len(path.read_text(encoding="utf-8").strip().splitlines()) == 1, "一行一条消息")

    got = await inbox.peek("u1", "c1", "writer")
    check(len(got) == 1 and got[0]["content"]["content"] == "hello", "peek 读到正文")
    check(len(await inbox.peek("u1", "c1", "writer")) == 1, "peek 不消费")

    rows = await inbox.read("u1", "c1", "writer")
    check(len(rows) == 1, "read 拿到消息")
    check(rows[0]["id"], "消息带 id（消费方可去重）")
    check(await inbox.peek("u1", "c1", "writer") == [], "read 之后收件箱清空")
    check(path.read_text(encoding="utf-8") == "", "R19-c 文件本身被清空（不是打标记）")

    await inbox.send("u1", "c1", "writer", {"content": "a"}, dedup_key="k1")
    again = await inbox.send("u1", "c1", "writer", {"content": "a"}, dedup_key="k1")
    check(again.get("_deduplicated") is True, "同键未消费消息不重复投递")
    check(len(await inbox.peek("u1", "c1", "writer")) == 1, "只留一条")

    await inbox.send("u1", "c1", "editor", {"content": "x"})
    check(await inbox.agents("u1", "c1") == ["editor", "writer"], "agents 列全部收件箱")
    check(await inbox.peek("u2", "c1", "writer") == [], "别的用户看不到")


async def task_checks(tmp: pathlib.Path) -> None:
    board = FileTaskBoard(tmp)
    tm = TaskManager(None, board=board, user_id="u1", conversation_id="c1")

    # 1 → 2 → 3 的链
    t1 = await tm.create("整理素材", "分类")
    t2 = await tm.create("降噪", "音频", blocked_by=[t1["id"]])
    t3 = await tm.create("剪辑", "主采访", blocked_by=[t2["id"]])

    path1 = tmp / "tasks" / "u1" / "c1" / f"task_{t1['id']}.json"
    check(path1.exists(), "R19-a 任务落到 tasks/{scope}/task_*.json")
    raw = json.loads(path1.read_text(encoding="utf-8"))
    check(raw["status"] == PENDING and raw["blockedBy"] == [] and raw["blocks"] == [t2["id"]],
          f"文件里的字段是设计形状：{raw}")
    check(t2["status"] == PENDING and t2["blockedBy"] == [t1["id"]], "新任务带 blockedBy")

    claimed = await tm.claim(t1["id"], "worker")
    check(claimed["status"] == ON_PROGRESS and claimed["owner"] == "worker",
          "R19-a claim 后状态是 on_progress（不是 claimed）")
    raw = json.loads(path1.read_text(encoding="utf-8"))
    check(raw["status"] == "on_progress", "磁盘上的状态词同样是 on_progress")

    again = await tm.claim(t1["id"], "worker")
    check(again["status"] == ON_PROGRESS, "同一 owner 重复认领幂等")

    try:
        await tm.claim(t2["id"], "other")
        check(False, "被阻塞的任务不能认领")
    except TaskError:
        check(True, "被阻塞的任务不能认领")

    done = await tm.complete(t1["id"], "worker")
    check(done["status"] == COMPLETED, "R19-a complete 后 completed")
    t2_now = await tm.get(t2["id"])
    check(t2_now["blockedBy"] == [], "R19-b 前置完成后 blockedBy 自动清空")
    check(t2_now["open_deps"] == [], "open_deps 现算为空")
    ready = {t["id"] for t in await tm.ready()}
    check(t2["id"] in ready, "下游进入可认领列表")

    # BFS 闭包
    check(await tm.descendants(t1["id"]) == [t2["id"], t3["id"]], "下游闭包 BFS")
    check(await tm.ancestors(t3["id"]) == [t1["id"], t2["id"]], "上游闭包 BFS")

    await tm.claim(t2["id"], "worker")
    await tm.complete(t2["id"], "worker")
    t3_now = await tm.get(t3["id"])
    check(t3_now["blockedBy"] == [], "两级依赖依次解锁")
    board_text = await tm.render()
    check("[x]" in board_text and "[ ]" in board_text, f"清单渲染标记正确：{board_text!r}")

    # 跨作用域隔离
    other = TaskManager(None, board=board, user_id="u2", conversation_id="c9")
    check(await other.list_all() == [], "任务板按 用户:会话 隔离")
    check(await other.get(t1["id"]) is None, "别的 scope 取不到本 scope 的任务")


async def subagent_checks(tmp: pathlib.Path) -> None:
    reg = FileSubagentRegistry(tmp)
    row = await reg.register("u1:c1", "worker", "完成第一件任务", status="idle")
    path = tmp / "agents" / "u1" / "c1" / "worker.json"
    check(path.exists(), "子 Agent 档案落到 agents/{scope}/{name}.json")
    check(row["prompt"] == "完成第一件任务" and row["status"] == "idle", "登记带 Prompt 与状态")

    await reg.set_status("u1:c1", "worker", "working", lease_sec=-1)
    working = await reg.list("u1:c1")
    check(working[0]["status"] == "working" and working[0]["prompt"] == "完成第一件任务",
          "set_status 保留 Prompt")
    reset = await reg.reset_expired()
    check(reset == 1, "租约到期归还 idle")
    check((await reg.list("u1:c1"))[0]["status"] == "idle", "归还后状态是 idle")


async def projection_checks(tmp: pathlib.Path) -> None:
    storage = build_storage("memory")
    counts = await rebuild_projection(storage, root=tmp)
    check(counts["tasks"] == 3, f"任务投影 3 行（实际 {counts['tasks']}）")
    check(counts["edges"] == 2, f"边投影 2 行（实际 {counts['edges']}）")
    check(counts["subagents"] == 1, f"子 Agent 投影 1 行（实际 {counts['subagents']}）")
    rows = await storage.db.select("tasks", where={"scope": "u1:c1"})
    statuses = {r["id"]: r["status"] for r in rows}
    check(statuses[1] == COMPLETED and statuses[2] == COMPLETED and statuses[3] == PENDING,
          f"投影状态与文件一致：{statuses}")


async def default_backend_checks() -> None:
    storage = build_storage("memory")
    inbox, tasks, agents = select_team_store(storage)
    check(isinstance(inbox, FileInbox) and isinstance(tasks, FileTaskBoard)
          and isinstance(agents, FileSubagentRegistry),
          "默认后端是文件（设计 §24），不是 PG")
    pg = select_team_store(storage, store="pg")
    check(pg[1] is storage.tasks and pg[2] is storage.subagents, "TEAM_STORE=pg 才走协作三表")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        await inbox_checks(root)
        await task_checks(root)
        await subagent_checks(root)
        await projection_checks(root)
        await default_backend_checks()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
