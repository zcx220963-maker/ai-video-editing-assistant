"""Task Manager：把剪辑 DAG 拆解为可认领、带依赖的任务，落在 PG 任务板上。

对应设计文档「Agent Team —— Task Manager」：
- 一行任务一条记录（``tasks`` 表），id 取自 ``task_seq`` 序列（spec §3.9，
  取代「文件名反推 id」）；作用域 = ``{user}:{conv}``，不同会话各有一块任务板。
- 状态流转：pending → claimed → completed。
- 依赖：``task_edges`` 表存单向边（task_id → depends_on）。blockedBy = 本任务的上游，
  blocks = 下游，二者是同一批边的正/反向视图，读取时现算，不再靠读-改-写维护两份副本。
  边一旦写下不随完成而删除 —— 「还被谁阻塞」由状态算出来（``open_deps``）。
- 认领判定：status=pending 且所有前置已 completed 才算可认领；认领走原子 claim，
  两个子 Agent 抢同一条时只有一个成功，另一个拿到「已被抢先认领」。

设计取舍：本模块只做任务语义（校验、闭包、渲染），SQL 形状全在 TasksRepo 里；
纯图工具（拓扑序、DAG 环检测）保持同步纯函数，便于单测。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .identity import current_identity_or

PENDING = "pending"
CLAIMED = "claimed"
COMPLETED = "completed"


class TaskError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 纯图工具（不落盘）
# --------------------------------------------------------------------------

def topo_order(node_required: dict[str, list[str]]) -> list[str]:
    """按拓扑序给出节点名：任一节点的前置都排在它前面（建任务时按此顺序插入）。"""
    indeg = {n: len(reqs) for n, reqs in node_required.items()}
    children: dict[str, list[str]] = {n: [] for n in node_required}
    for n, reqs in node_required.items():
        for d in reqs:
            children.setdefault(d, []).append(n)
    q = deque(sorted(k for k, v in indeg.items() if v == 0))
    out: list[str] = []
    while q:
        cur = q.popleft()
        out.append(cur)
        for nxt in sorted(children.get(cur, [])):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                q.append(nxt)
    if len(out) != len(node_required):
        raise TaskError("DAG 存在环，无法生成任务计划")
    return out


# --------------------------------------------------------------------------
# 任务板
# --------------------------------------------------------------------------

class TaskManager:
    """任务总线：拆解、认领、完成、依赖解锁与清单渲染，全部落在 ``tasks``/``task_edges``。"""

    def __init__(self, storage: Any, *, user_id: str = "default",
                 conversation_id: str = "default") -> None:
        self._tasks = storage.tasks
        self.storage = storage
        self.default_user_id = user_id
        self.default_conversation_id = conversation_id

    @property
    def scope(self) -> str:
        ident = current_identity_or(self.default_user_id, self.default_conversation_id)
        return f"{ident.user_id}:{ident.conversation_id}"

    async def create(self, name: str, description: str = "",
                     blocked_by: Iterable[int] = ()) -> dict[str, Any]:
        """新建 pending 任务；id 由 task_seq 取号，blocked_by 写成依赖边。"""
        return await self._tasks.create(self.scope, name, description,
                                        blocked_by=list(blocked_by))

    async def get(self, task_id: int) -> dict[str, Any] | None:
        row = await self._tasks.get(int(task_id))
        if row is None or row["scope"] != self.scope:
            return None
        return await self._with_open_deps(row)

    async def list_all(self) -> list[dict[str, Any]]:
        # 一次把「已完成集合」算好向下传：原先 _with_open_deps 每行都自己 select 一次
        # （每条任务一次查询），任务板一多就是 N+1。
        done = await self._done_ids()
        rows = await self._tasks.list_all(self.scope)
        return [self._fill_open_deps(t, done) for t in rows]

    async def _done_ids(self) -> set[int]:
        """本 scope 内已完成的任务 id 集合。"""
        return {int(r["id"]) for r in await self.storage.db.select(
            "tasks", where={"scope": self.scope, "status": COMPLETED})}

    @staticmethod
    def _fill_open_deps(task: dict[str, Any], done: set[int]) -> dict[str, Any]:
        """补上「还没完成的前置」：blockedBy 是图真相，open_deps 才是当前阻塞。"""
        deps = task.get("blockedBy") or []
        task["open_deps"] = [d for d in deps if d not in done]
        return task

    async def _with_open_deps(self, task: dict[str, Any]) -> dict[str, Any]:
        return self._fill_open_deps(task, await self._done_ids())

    # ---- 认领：pending → claimed；前置未完成者不可认领 ----
    async def claim(self, task_id: int, owner: str) -> dict[str, Any]:
        task = await self._require(task_id)
        if task["status"] == COMPLETED:
            raise TaskError(f"任务 #{task_id} 已完成，无法认领")
        if task["status"] == CLAIMED and task["owner"] not in (None, "", owner):
            raise TaskError(f"任务 #{task_id} 已被 {task['owner']} 认领")
        if task["open_deps"]:
            raise TaskError(f"任务 #{task_id} 仍被 {task['open_deps']} 阻塞，暂不可认领")
        got = await self._tasks.claim_one(self.scope, owner, int(task_id))
        if got is None:
            raise TaskError(f"任务 #{task_id} 已被他人抢先认领")
        return await self._with_open_deps(got)

    # ---- 完成：置 completed，其下游此前置即视为满足 ----
    async def complete(self, task_id: int, owner: str) -> dict[str, Any]:
        """完成任务。生产语义（任务幂等 + 锁）：

        * 只有**认领者本人**能交付——原子条件改写（claimed 且 owner 匹配），
          两个协程/实例同时 complete 同一条，只有一个生效；
        * 重复交付（同 owner 再 complete 已完成任务）**幂等**：原样返回、不报错，
          崩溃恢复重放这条指令不会二次解锁下游；
        * pending 不能跳过认领直接交付；他人认领/他人已交付都给出精确错误。
        """
        task = await self._require(task_id)
        if task["status"] == COMPLETED and task["owner"] == owner:
            return {**task, "_unlocked": []}
        if task["status"] == PENDING:
            raise TaskError(f"任务 #{task_id} 尚未认领，先 claim 再 complete")
        if task["status"] == CLAIMED and task["owner"] != owner:
            raise TaskError(f"任务 #{task_id} 由 {task['owner']} 认领，只有认领者可交付")
        row = await self._tasks.complete(self.scope, int(task_id), owner=owner)
        if row is None:
            # 检查与改写之间有竞态：重读一次，给出当下的事实而不是笼统失败
            after = await self._require(task_id)
            if after["status"] == COMPLETED and after["owner"] == owner:
                return {**after, "_unlocked": []}
            raise TaskError(f"任务 #{task_id} 无法由 {owner} 交付"
                            f"（当前 {after['status']} @ {after['owner'] or '无主'}）")
        after = {t["id"]: t for t in await self.list_all()}
        done = after[int(task_id)]
        # 本次解锁了谁：下游里此前只被本任务挡着、现在可认领的
        unlocked = [d for d in task["blocks"]
                    if d in after and after[d]["status"] == PENDING and not after[d]["open_deps"]]
        done["_unlocked"] = unlocked
        return done

    # ---- 查询：可认领 / 依赖闭包 ----
    async def ready(self) -> list[dict[str, Any]]:
        """当前可被认领的任务：pending 且所有前置已 completed。"""
        done = await self._done_ids()
        rows = await self._tasks.ready(self.scope)
        return [self._fill_open_deps(t, done) for t in rows]

    async def descendants(self, task_id: int) -> list[int]:
        """BFS 沿 blocks 边求下游闭包（对应文档“出度方向 BFS”）。"""
        return await self._closure(task_id, "blocks")

    async def ancestors(self, task_id: int) -> list[int]:
        """BFS 沿 blockedBy 边求上游闭包（对应文档“入度方向 BFS”）。"""
        return await self._closure(task_id, "blockedBy")

    async def _closure(self, task_id: int, edge: str) -> list[int]:
        """BFS 求依赖闭包（edge='blocks' 下游 / 'blockedBy' 上游）。

        先把本 scope 的任务一次性读成 id→行 再纯内存遍历：原先每访问一个节点就
        ``await self.get(nid)``，而 ``get`` 内部又查一次已完成集合——每节点两次查询，
        整张图走下来是 O(V·查询)。闭包是高频操作（认领前判断、任务板渲染都要走）。
        """
        all_rows = {int(t["id"]): t for t in await self._tasks.list_all(self.scope)}
        root = all_rows.get(int(task_id))
        seen: set[int] = set()
        q = deque(root[edge] if root else ())
        while q:
            nid = int(q.popleft())
            if nid in seen:
                continue
            seen.add(nid)
            row = all_rows.get(nid)
            if row:
                q.extend(row.get(edge) or [])
        return sorted(seen)

    async def _require(self, task_id: int) -> dict[str, Any]:
        task = await self.get(task_id)
        if task is None:
            raise TaskError(f"任务 #{task_id} 不存在")
        return task

    # ---- 渲染：文档 list_all() 的检查清单风格 ----
    async def render(self) -> str:
        lines: list[str] = []
        for t in await self.list_all():
            mark = {PENDING: " ", CLAIMED: ">", COMPLETED: "x"}[t["status"]]
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['open_deps']})" if t["open_deps"] else ""
            lines.append(f"[{mark}] #{t['id']}: {t['name']}{owner}{blocked}")
        return "\n".join(lines)
