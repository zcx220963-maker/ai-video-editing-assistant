"""Task Manager 验证（不联网）：依赖边、认领/完成解锁、BFS 闭包、原子认领、拓扑建板、渲染。

对应设计文档「Agent Team —— Task Manager」，状态落在 PG ``tasks`` + ``task_edges``
（spec §3.9），取代原来的 ``task_{id}.json`` 整文件覆写：
blockedBy/blocks 是同一批边的正/反向视图（读取时现算），「还被谁阻塞」由状态推导
成 ``open_deps``，认领走原子 claim（两人抢一条只有一个成功）。

运行：  python tests/test_task_manager.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.identity import use_identity
from agent_framework.storage import build_storage
from agent_framework.task_manager import (
    CLAIMED,
    COMPLETED,
    PENDING,
    TaskError,
    TaskManager,
    topo_order,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage = build_storage("memory")
        await storage.start()
        tm = TaskManager(storage, user_id="u1", conversation_id="c1")

        # 文档生命周期示例：#3 依赖 #1、#2；#4、#5 依赖 #3。id 由 task_seq 取号。
        t1 = await tm.create("整理原始素材并分类")
        t2 = await tm.create("导出音频并完成降噪处理")
        t3 = await tm.create("剪辑主采访片段", blocked_by=[t1["id"], t2["id"]])
        t4 = await tm.create("添加字幕和文字动画", blocked_by=[t3["id"]])
        t5 = await tm.create("调色和最终导出", blocked_by=[t3["id"]])
        check([t1["id"], t2["id"], t3["id"], t4["id"], t5["id"]] == [1, 2, 3, 4, 5],
              "id 取自 task_seq 序列（不再靠文件名反推）")
        rows = await storage.db.select("task_edges", order_by=["task_id"])
        check(len(rows) == 4, f"每条依赖一行边（2+1+1）：实际 {len(rows)} 行")

        # 反向边：声明 3 依赖 [1,2] → 1、2 的 blocks 自动含 3。
        check((await tm.get(t1["id"]))["blocks"] == [3]
              and (await tm.get(t2["id"]))["blocks"] == [3],
              "blockedBy 自动登记反向 blocks（同一批边的另一视图）")
        check((await tm.get(3))["blockedBy"] == [1, 2], "正向 blockedBy 保留")
        got3 = await tm.get(3)
        check(got3["status"] == PENDING and not got3["owner"], "新建任务为 pending 且无 owner")
        check(got3["scope"] == "u1:c1", f"任务带作用域：{got3['scope']}")

        # ---- BFS 闭包（在完成解锁前先验证依赖边）----
        check(await tm.ancestors(3) == [1, 2], f"#3 上游闭包 {await tm.ancestors(3)}")
        check(await tm.descendants(3) == [4, 5], f"#3 下游闭包 {await tm.descendants(3)}")

        # ---- 认领被阻塞的任务应被拒（open_deps 还指着 #1、#2）----
        try:
            await tm.claim(3, "SubAgent A")
            check(False, "认领被阻塞任务应抛错")
        except TaskError as e:
            check("阻塞" in str(e), f"认领被阻塞任务被拒：{e}")

        # ---- 可认领集合：初始仅 #1、#2 ----
        ready_ids = {t["id"] for t in await tm.ready()}
        check(ready_ids == {1, 2}, f"初始可认领为无依赖的 #1、#2（实为 {ready_ids}）")

        # 认领 #1 → claimed
        c1 = await tm.claim(1, "SubAgent B")
        check(c1["status"] == CLAIMED and c1["owner"] == "SubAgent B", "认领后 claimed 且写入 owner")

        # ---- 完成 #1、#2 → 解锁 #3（blockedBy 边留着，open_deps 清空才可认领）----
        await tm.complete(1, "SubAgent B")  # 认领者本人交付
        check((await tm.get(3))["blockedBy"] == [1, 2], "依赖边不随完成删除（图真相不变）")
        check((await tm.get(3))["open_deps"] == [2], "完成 #1 后 #3 仍被 [2] 阻塞（open_deps 现算）")
        await tm.claim(2, "SubAgent A")
        unlocked = (await tm.complete(2, "SubAgent A"))["_unlocked"]
        check(unlocked == [3], f"#2 完成解锁下游 #3：{unlocked}")
        check((await tm.get(3))["open_deps"] == [], "#1、#2 都完成后 #3 的 open_deps 清空")
        check(3 in {t["id"] for t in await tm.ready()}, "open_deps 清空后 #3 变为可认领")

        # ---- 抢认领：同一瞬间两个子 Agent 抢 #3，只有一个成功 ----
        results = await asyncio.gather(tm.claim(3, "SubAgent A"), tm.claim(3, "SubAgent C"),
                                       return_exceptions=True)
        ok = [r for r in results if isinstance(r, dict)]
        bad = [r for r in results if isinstance(r, TaskError)]
        check(len(ok) == 1 and len(bad) == 1,
              f"原子认领下恰好一人成功：{[type(r).__name__ for r in results]}")
        check(ok and ok[0]["owner"] in ("SubAgent A", "SubAgent C")
              and ("抢先" in str(bad[0]) or ok[0]["owner"] in str(bad[0])),
              f"败者拿到「谁占了这条」的提示：{bad[0] if bad else ''}")
        check((await tm.get(3))["status"] == CLAIMED, "#3 已被认领")

        # ---- 完成 #3 → 解锁 #4、#5 ----
        un3 = (await tm.complete(3, ok[0]["owner"]))["_unlocked"]
        check(set(un3) == {4, 5}, f"#3 完成解锁 #4、#5：{un3}")
        check({t["id"] for t in await tm.ready()} >= {4, 5}, "#4、#5 现在可认领")

        # ---- 状态终局 + 幂等 ----
        check((await tm.get(1))["status"] == COMPLETED, "#1 已 completed")
        try:
            await tm.claim(1, "X")
            check(False, "认领已完成任务应抛错")
        except TaskError:
            check(True, "认领已完成任务被拒")

        # ---- #1 的下游闭包（blocks 边不被解锁清除）----
        check(set(await tm.descendants(1)) == {3, 4, 5},
              f"#1 下游闭包含整链：{await tm.descendants(1)}")

        # ---- 认领解锁后的 #4（保持 claimed 用于渲染验证）----
        await tm.claim(4, "SubAgent D")

        # ---- 渲染清单（文档风格）----
        rendered = await tm.render()
        check("[x] #1: 整理原始素材并分类 @SubAgent B" in rendered,
              "completed 任务渲染为 [x] 并带 owner")
        check("[>] #4: 添加字幕和文字动画 @SubAgent D" in rendered,
              "claimed 任务渲染为 [>]")
        check("[ ] #5: 调色和最终导出" in rendered, "解锁后的 pending 任务渲染为 [ ]")

        # ---- 跨会话隔离：另一块任务板看不到本板任务 ----
        tm_other = TaskManager(storage, user_id="u2", conversation_id="c9")
        check(await tm_other.list_all() == [], "不同 用户:会话 各一块任务板")
        check(await tm_other.get(1) is None, "跨作用域按 id 取他人任务返回 None")
        # 身份 contextvar 覆盖构造缺省
        with use_identity("u3", "c3"):
            check(await tm.list_all() == [], "run 内取的是当前身份那块板，不是实例缺省")

    # ---- 第二个临时目录：DAG 拓扑建板（id 拓扑有序 + 反向边 + 仅链首可认领）----
    with tempfile.TemporaryDirectory() as tmp2:
        storage2 = build_storage("memory")
        await storage2.start()
        order = topo_order({
            "load_media": [],
            "split_shots": ["load_media"],
            "understand_clips": ["split_shots"],
            "group_clips": ["understand_clips"],
        })
        check(order == ["load_media", "split_shots", "understand_clips", "group_clips"],
              f"topo_order 给出拓扑序：{order}")
        tm2 = TaskManager(storage2, user_id="u9", conversation_id="c9")
        ids: dict[str, int] = {}
        for name in order:
            req = {"split_shots": ["load_media"], "understand_clips": ["split_shots"],
                   "group_clips": ["understand_clips"]}.get(name, [])
            row = await tm2.create(name, blocked_by=[ids[r] for r in req])
            ids[name] = row["id"]
        check(ids["load_media"] < ids["split_shots"] < ids["group_clips"],
              "按拓扑序插入即得到拓扑有序的 id 段")
        check((await tm2.get(ids["load_media"]))["blocks"] == [ids["split_shots"]],
              "由 DAG 建任务后反向边正确")
        check({t["id"] for t in await tm2.ready()} == {ids["load_media"]},
              "仅链首 load_media 初始可认领")
        try:
            topo_order({"a": ["b"], "b": ["a"]})
            check(False, "成环应抛错")
        except TaskError:
            check(True, "DAG 成环时拒绝生成任务计划")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
