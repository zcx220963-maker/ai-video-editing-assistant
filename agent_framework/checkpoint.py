"""Checkpoint 崩溃恢复 + 时间旅行：一次执行的中间状态落成「一致点增量链」。

对应设计文档「Checkpoint 崩溃恢复机制」：重点不是保存最终答案，而是保存执行过程中的
中间状态——尤其是 Tool Call / Tool Result / 当前进度（迭代轮次）/ 当前状态——以便恢复。

核心设计
--------
* 持久化时机只在“迭代边界”——即每次即将调用 LLM 之前的、消息列表自洽的一致点。
  一个一致点要么是本轮初始上下文，要么是上一批工具结果刚回填之后。
* 因为只在一致点落盘，恢复时只会**重新发起一次 LLM 调用**，绝不会重复执行已提交的
  工具调用 → 避免副作用被重放。
* 存储：``checkpoints`` 是指针行（这个 run 到哪了 / 成没成 / 占哪份产物作用域），
  ``checkpoint_entries`` 是一致点链，一行只存自上一致点**新增**的消息（kind='delta'）。
  上下文压缩会就地改写旧消息，那种情况下该条退化成整条基准（kind='full'），
  重建时从最近的 full 往后拼——写放大是「每次压缩一次」而不是「每轮一次整条上下文」。
* 时间旅行与分叉：``load(run_id, at_seq)`` 能重建到任意一致点；``fork(run_id, at_seq)``
  从那里开一条新 run，并**把父 run 的剪辑产物集整份复制**给新作用域
  （见 ``storage.artifacts(...).clone_from``）。所以「换 BGM 从 plan_timeline 重跑」
  不必重做切镜/ASR/画面理解：拦截器在新作用域里看到上游产物已在 Store，天然跳过补齐。

状态机：running →（正常结束）completed；（异常/需人工介入）failed。

同步→异步：写表是 IO，``CheckpointManager`` 的每个动作都是 await 的；调用点都在
``AgentOnceRun._drive`` 的迭代边界上，本来就在 async 里。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .messages import Message, user

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# 分叉让位：父 run 被中途时间旅行接管后不再属于任何待恢复状态。
STATUS_SUPERSEDED = "superseded"
# HITL 审批断点：执行到标注「需人工审批」的工具时挂起，等用户批准/拒绝后从同一一致点继续。
# 它是**活的等待态**（等人工），故意不进 _UNFINISHED——崩溃恢复不该把等人工的 run 拽起来跑。
STATUS_AWAITING_APPROVAL = "awaiting_approval"
_UNFINISHED = {STATUS_RUNNING, STATUS_FAILED}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_dt() -> datetime:
    """多副本租约用的 UTC aware 时刻；与 PG timestamptz 对齐（内存引擎同为 aware）。"""
    return datetime.now(timezone.utc)


@dataclass
class Checkpoint:
    """一次 AgentOnceRun 执行的可恢复快照（指针行 + 已落链的一致点数）。"""

    run_id: str
    session_id: str
    message: str                       # 触发本次执行的用户输入（便于审计/重建）
    iteration: int                     # 下一个待处理的迭代下标（进度）
    messages: list[Message]            # 一致点上的完整工作消息列表（含 system/工具结果）
    status: str = STATUS_RUNNING
    created_at_ms: int = field(default_factory=_now_ms)
    updated_at_ms: int = field(default_factory=_now_ms)
    head_seq: int = 0                  # 链尾 seq（-1 = 还没有任何一致点）
    scope: dict[str, Any] = field(default_factory=dict)   # 剪辑产物作用域 {storyline_session, artifact_id}
    forked_from: str | None = None
    forked_at_seq: int | None = None
    # 计划门（块 B）：plan_run_id 是 run B → run A 的谱系；plan 装本 run 的计划侧产物
    # （run A 的 candidates 候选计划 / run B 的 audit 对账结论），普通轮两者皆空。
    plan_run_id: str | None = None
    plan: dict[str, Any] = field(default_factory=dict)
    # HITL：挂起等待人工审批时的待批动作（待批工具调用、理由、请求 id）；其余为空。
    approval: dict[str, Any] = field(default_factory=dict)
    # 多副本：认领该 run 的实例 id 与租约到期时刻；单副本恒为 None，行为不变。
    owner_instance_id: str | None = None
    lease_expires_at: Any = None
    # 已经进了链的那段消息（不是持久化字段）：下一致点据此算增量，并校验前缀没被改写。
    chain: list[Message] = field(default_factory=list, repr=False, compare=False)

    def to_row(self) -> dict[str, Any]:
        """指针行。正文在 checkpoint_entries，这里刻意不带 messages。"""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "message": self.message,
            "iteration": self.iteration,
            "head_seq": self.head_seq,
            "status": self.status,
            "scope": dict(self.scope),
            "forked_from": self.forked_from,
            "forked_at_seq": self.forked_at_seq,
            "plan_run_id": self.plan_run_id,
            "plan": dict(self.plan),
            "approval": dict(self.approval),
            "owner_instance_id": self.owner_instance_id,
            "lease_expires_at": self.lease_expires_at,
            "created_at_ms": self.created_at_ms,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any], messages: list[Message] | None = None,
                 chain: list[Message] | None = None) -> "Checkpoint":
        return cls(
            run_id=row["run_id"],
            session_id=row["session_id"],
            message=row.get("message", ""),
            iteration=int(row.get("iteration") or 0),
            messages=list(messages or []),
            status=row.get("status", STATUS_RUNNING),
            created_at_ms=int(row.get("created_at_ms") or 0),
            updated_at_ms=int(row.get("updated_at_ms") or 0),
            head_seq=int(row.get("head_seq") or 0),
            scope=dict(row.get("scope") or {}),
            forked_from=row.get("forked_from"),
            forked_at_seq=row.get("forked_at_seq"),
            plan_run_id=row.get("plan_run_id"),
            plan=dict(row.get("plan") or {}),
            approval=dict(row.get("approval") or {}),
            owner_instance_id=row.get("owner_instance_id"),
            lease_expires_at=row.get("lease_expires_at"),
            chain=list(chain or messages or []),
        )

    @property
    def finished(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def artifact_id(self) -> str:
        return str(self.scope.get("artifact_id") or "")


def rebuild(entries: list[dict[str, Any]]) -> list[Message]:
    """一致点链 → 完整消息列表。遇到 full 基准就重新起算，delta 往后追加。"""
    msgs: list[Message] = []
    for e in entries:
        payload = list(e.get("payload") or [])
        msgs = payload if e.get("kind") == "full" else [*msgs, *payload]
    return msgs


class CheckpointManager:
    """指针行 + 增量链上的「创建/推进/收尾/回看/分叉」小面。"""

    def __init__(self, storage: Any, *, auto_cleanup: bool = False,
                 prune_keep: int = 200, lease_sec: float = 600.0) -> None:
        self._repo = storage.checkpoints
        self._storage = storage
        self.auto_cleanup = auto_cleanup
        self.prune_keep = prune_keep
        # 多副本：被认领的 run 在每次迭代边界顺带续租，避免长任务中途被别的实例抢走。
        self.lease_sec = lease_sec

    # ---- 落盘 ----

    async def save(self, cp: Checkpoint) -> Checkpoint:
        """写指针行，并按需追加一个一致点（没有新消息就不写）。"""
        cp.updated_at_ms = _now_ms()
        await self._append_entry(cp)
        await self._repo.save(cp.to_row())
        return cp

    async def _append_entry(self, cp: Checkpoint) -> None:
        """增量优先：前缀与已落链一致 → 只写新增段；被压缩改写 → 写整条基准。"""
        chain = cp.chain
        msgs = cp.messages
        untouched = len(msgs) >= len(chain) and msgs[:len(chain)] == chain
        delta = msgs[len(chain):] if untouched else msgs
        if not delta and chain:
            return                      # 没有任何新内容，白写一行
        kind = "delta" if (untouched and chain) else "full"
        parent = cp.head_seq if chain else None
        entry = await self._repo.append_entry(
            cp.run_id, kind=kind, payload=list(delta), iteration=cp.iteration,
            parent_seq=parent)
        cp.head_seq = int(entry["seq"])
        cp.chain = list(msgs)

    async def begin(self, session_id: str, message: str, messages: list[Message],
                    run_id: str | None, scope: dict[str, Any] | None = None,
                    plan_run_id: str | None = None) -> Checkpoint:
        cp = Checkpoint(
            run_id=run_id or uuid.uuid4().hex,
            session_id=session_id,
            message=message,
            iteration=0,
            messages=list(messages),
            status=STATUS_RUNNING,
            head_seq=-1,
            scope=dict(scope or {}),
            plan_run_id=plan_run_id,
        )
        return await self.save(cp)

    async def save_progress(self, cp: Checkpoint, *, iteration: int,
                            messages: list[Message]) -> None:
        cp.iteration = iteration
        cp.messages = list(messages)
        cp.status = STATUS_RUNNING
        await self.save(cp)
        # 多副本：认领过的 run 顺带续租——迭代边界本就是天然的心跳点。
        if cp.owner_instance_id:
            await self._repo.renew_lease(cp.run_id, cp.owner_instance_id,
                                         _now_dt() + timedelta(seconds=self.lease_sec))

    async def complete(self, cp: Checkpoint, *, messages: list[Message]) -> None:
        cp.messages = list(messages)
        cp.status = STATUS_COMPLETED
        await self.save(cp)
        # 收尾即归还认领：完成后这条 run 不再是「在途」，别让租约白占着。
        if cp.owner_instance_id:
            await self._repo.release_lease(cp.run_id, cp.owner_instance_id)
        if self.auto_cleanup:
            await self._repo.drop(cp.run_id)
            await self.prune()

    async def mark_failed(self, cp: Checkpoint, *, iteration: int,
                          messages: list[Message]) -> None:
        cp.iteration = iteration
        cp.messages = list(messages)
        cp.status = STATUS_FAILED
        await self.save(cp)
        # 失败仍归还认领：failed 还是崩溃恢复候选，但「谁持有」该让出来，
        # 否则 owner 非空且租约未过期时两个 claim 都命中不了，这条 run 永远没人接手。
        if cp.owner_instance_id:
            await self._repo.release_lease(cp.run_id, cp.owner_instance_id)

    # ---- HITL 审批断点 ----

    async def await_approval(self, cp: Checkpoint, *, iteration: int,
                             messages: list[Message],
                             pending_calls: list[dict[str, Any]],
                             reason: str = "",
                             fallback_options: list[dict[str, Any]] | None = None,
                             ask: dict[str, Any] | None = None) -> None:
        """挂起到审批态：待批动作记进 approval，status=awaiting_approval 落盘。

        挂在「即将执行被审批工具」的一致点上：该一致点里 assistant(tool_calls) 已 append、
        工具结果还没回填。批准后按同一批 tool_calls 继续执行（不重放更早的工具）。

        ``fallback_options`` 非空时，审批帧带可选方案列表给前端渲染为选项按钮，
        用户点选的方案 key 作为 decision 回喂。

        ``ask`` 非空时是一次**结构化提问**（``ask_user`` 或渲染前的编排确认）：
        它也落盘。落盘是必要的——前端刷新/重连之后要靠它把选项卡重新弹出来，
        否则用户会看到一个「等待确认」但没有任何可点东西的界面。
        """
        cp.iteration = iteration
        cp.messages = list(messages)
        cp.status = STATUS_AWAITING_APPROVAL
        cp.approval = {
            "pending_calls": list(pending_calls),
            "reason": reason,
            "created_at_ms": _now_ms(),
            "fallback_options": list(fallback_options) if fallback_options else [],
        }
        if ask:
            cp.approval["ask"] = dict(ask)
        await self.save(cp)

    async def pending_approval(self, run_id: str) -> Checkpoint | None:
        """读回一个挂起的审批 run（不改状态，交给调用方决定批准/拒绝）。"""
        cp = await self.load(run_id)
        if cp is None or cp.status != STATUS_AWAITING_APPROVAL:
            return None
        return cp

    async def awaiting_approval_for_session(self, session_id: str) -> Checkpoint | None:
        """本会话最近一条**正等着用户确认**的 run（刷新后重弹选项卡用）。"""
        rows = await self._repo.list_awaiting_approval_by_session(session_id)
        return await self.load(rows[0]["run_id"]) if rows else None

    async def clear_approval(self, cp: Checkpoint) -> None:
        """把 run 从审批态放回 running（批准/拒绝后、续跑前由调用方调）。"""
        cp.status = STATUS_RUNNING
        cp.approval = {}
        await self.save(cp)

    # ---- 多副本：实例认领与租约 ----

    async def claim_recoverable(self, instance_id: str, *,
                                lease_sec: float = 120.0,
                                limit: int = 100) -> list[Checkpoint]:
        """原子认领本实例可接手的未完成 run（跨实例互斥）。

        两步都是纯 AND 条件（条件 DSL 不支持 OR）：先领「无主」的（owner IS NULL），
        再领「租约过期」的（owner 非空但 lease < now）。别的实例刚领走且租约未到期的
        run，两步都命中不了——于是「恢复哪个 run」从人工约定变成了服务端判据。
        """
        lease = _now_dt() + timedelta(seconds=lease_sec)
        rows = await self._repo.claim_recoverable(instance_id, lease, limit=limit)
        out: list[Checkpoint] = []
        for r in rows:
            cp = await self.load(r["run_id"])
            if cp is not None:
                out.append(cp)
        return out

    async def renew_lease(self, run_id: str, instance_id: str, *,
                          lease_sec: float = 120.0) -> bool:
        """续租：长任务执行期间定期调用，避免租约到期被别的实例抢走。"""
        lease = _now_dt() + timedelta(seconds=lease_sec)
        return await self._repo.renew_lease(run_id, instance_id, lease)

    async def release(self, run_id: str, instance_id: str) -> None:
        """归还认领：清空 owner/lease，让该 run 不再被当成在途（收尾后调）。"""
        await self._repo.release_lease(run_id, instance_id)

    # ---- 读回 ----

    async def load(self, run_id: str, at_seq: int | None = None) -> Checkpoint | None:
        """重建某个 run（或它某个一致点）上的状态。at_seq 是时间旅行的截断点（含）。"""
        row = await self._repo.load(run_id)
        if row is None:
            return None
        upto = int(row["head_seq"]) if at_seq is None else at_seq
        entries = await self._repo.load_entries(run_id, upto_seq=upto)
        msgs = rebuild(entries)
        cp = Checkpoint.from_row(row, messages=msgs, chain=msgs)
        cp.head_seq = int(entries[-1]["seq"]) if entries else upto
        return cp

    async def resume(self, run_id: str, at_seq: int | None = None) -> Checkpoint | None:
        cp = await self.load(run_id, at_seq)
        if cp is None or cp.status == STATUS_COMPLETED:
            return None
        return cp

    async def pending(self) -> list[Checkpoint]:
        """崩溃恢复入口：所有未正常结束（running/failed）的执行快照，按更新时间升序。"""
        out: list[Checkpoint] = []
        for row in await self._repo.list_unfinished():
            cp = await self.load(row["run_id"])
            if cp is not None:
                out.append(cp)
        return out

    async def pending_for_session(self, session_id: str) -> Checkpoint | None:
        """查某个会话最近一个仍在运行的 checkpoint（用户显式要求「继续」时调用）。

        只查 status='running'：failed 的 run 已失败，用户下条消息应开新 run 而非恢复。
        """
        rows = await self._repo.list_unfinished_by_session(session_id)
        return await self.load(rows[0]["run_id"]) if rows else None

    async def interrupted_execution_for_session(self, session_id: str) -> Checkpoint | None:
        """同会话最近一条**被打断的**执行轮；没有就回 None。

        用户在同一个会话窗口里发消息时，后端自动调这个方法：如果最近一条执行轮
        被打断了，就续跑它而不是开新规划轮。如果最近一条已经 completed（成片已出），
        就不续跑——用户发的是新需求或修改意见，应该走规划轮处理。

        判据必须是「status ∈ {running, failed} 且 plan_run_id 非空」，**不能**先按
        updated_at 排序再逐行看：``CheckpointManager.fork`` 会把父 run 置成 superseded，
        而父行的 updated_at 完全可能仍排在新的 running 子 run 之前——旧写法碰到这条
        superseded 行就 ``return None``，于是消费方以为「没有被打断的执行轮」，
        静默退回 ``agent.plan()`` 开新规划轮（那张注册表物理不含剪辑节点，
        模型只能答「我这边没有执行入口」）。查询下推到 repo 层就不受排序影响。
        """
        rows = await self._repo.list_interrupted_executions_by_session(session_id)
        return await self.load(rows[0]["run_id"]) if rows else None

    async def pending_plan_for_session(self, session_id: str) -> dict[str, Any] | None:
        """同会话最近一条规划轮，且它的候选计划仍待确认（plan.candidates 非空）。

        用户在输入框直接打「就按这版执行」时，前端走普通 /chat 而非 /plans/{id}/confirm，
        后端默认又开规划轮——这里让 consumer 在开新规划轮前先查一下：如果同会话已有
        待确认的计划卡，自动走 execute_plan 路径，而不是把用户困在规划轮里出不去。
        """
        rows = await self._repo.list_by_session(session_id)
        for row in rows:
            if row.get("plan_run_id"):
                continue
            # 只认待确认的那一态：superseded（被分叉取代）与 failed（已废）的旧卡
            # 不能当成「还有一张卡等你确认」，否则用户每发一句话都被推回一张废卡。
            if str(row.get("status") or "") != STATUS_RUNNING:
                continue
            candidates = ((row.get("plan") or {}).get("candidates") or [])
            if candidates:
                return {
                    "plan_run_id": row["run_id"],
                    "candidates": candidates,
                }

        return None

    async def history(self, run_id: str) -> list[dict[str, Any]]:
        """这个 run 走过哪些一致点（时间旅行选择面的数据源）。

        ``tools`` 记下这一致点里刚落下结果的节点名：只给「第 4 步 / 2 条消息」的话，
        选分叉点的人无从知道那一步是选乐还是渲染，等于让人蒙。
        """
        out: list[dict[str, Any]] = []
        for e in await self._repo.load_entries(run_id):
            payload = e.get("payload") or []
            out.append({
                "seq": int(e["seq"]), "iteration": int(e.get("iteration") or 0),
                "kind": e.get("kind"), "parent_seq": e.get("parent_seq"),
                "messages": len(payload),
                "tools": [str(m.get("name")) for m in payload
                          if isinstance(m, dict) and m.get("role") == "tool"
                          and m.get("name")],
            })
        return out

    async def row(self, run_id: str) -> dict[str, Any] | None:
        """指针行原样（不含消息正文）——HTTP 列执行清单、校验归属用，不必重建整条链。"""
        return await self._repo.load(run_id)

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """本会话的全部 run（含已完成的与分叉出来的），最近的在前。"""
        return await self._repo.list_by_session(session_id)

    async def seq_before_tool(self, run_id: str, tool_name: str) -> int:
        """「该工具还没执行过」的那个一致点 seq（分叉点）。

        链上找不到它的结果 → KeyError 并列出可分叉的工具名；结果落在首条一致点上
        → 前面没东西可留，同样回绝（那等于重开一个 run，不是分叉）。
        """
        entries = await self._repo.load_entries(run_id)
        seen: list[str] = []
        for e in entries:
            payload = e.get("payload") or []
            if any(m.get("role") == "tool" and m.get("name") == tool_name
                   for m in payload if isinstance(m, dict)):
                if int(e["seq"]) == 0:
                    raise KeyError(
                        f"「{tool_name}」的结果落在链首，之前没有可复用的一致点；"
                        f"要重做全部流程请直接发起新一轮。")
                return int(e["seq"]) - 1
            seen += [m.get("name", "") for m in payload
                     if isinstance(m, dict) and m.get("role") == "tool"]
        raise KeyError(f"这个 run 里没有「{tool_name}」的执行记录。已有工具结果："
                       f"{sorted({s for s in seen if s}) or '（空）'}")

    # ---- 分叉 ----

    async def fork(self, run_id: str, at_seq: int, *,
                   run_id_new: str | None = None,
                   invalidate: Iterable[str] = (),
                   message: str = "") -> Checkpoint:
        """从某个一致点开一条新 run：新产物作用域 + 父产物集整份复制过来。

        新 run 换一份 artifact_id，所以试另一种 BGM 不会把父 run 的产物集改花；
        而复制过来的上游产物让拦截器在新作用域里照样命中 ``store.has(dep)``，
        于是只有分叉点之后的节点重跑。

        ``invalidate`` 是「这次要真正重做」的节点集（重跑点本身 + 它的下游，
        由 ``EditingContract.downstream`` 给出）：复制完就把这些产物删掉，
        否则拦截器会认为它们已完成、恰好跳过用户要求重跑的那一步。

        ``message`` 是这次分叉要带进去的新诉求：回退后的上下文只到一致点为止，
        末尾仍是父 run 那一句旧诉求——模型照着旧话重做一遍参数，分叉就白分了。
        所以这里把它当作子 run 的最新一条 user 消息接在回退点之后，两条入口共用。

        父 run 就地置 superseded：分叉出去后它已让位，留在 running 里就会被崩溃恢复
        重播一遍用户不要的分支。两条分叉入口（模型的 rerun_from、HTTP/CLI 的 fork）
        都收敛在这里，所以不放给调用方各自记得做。
        """
        parent = await self.load(run_id, at_seq)
        if parent is None:
            raise KeyError(f"没有可分叉的 checkpoint: {run_id}")
        new_message = message.strip()
        child = Checkpoint(
            run_id=run_id_new or uuid.uuid4().hex,
            session_id=parent.session_id,
            message=new_message or parent.message,
            iteration=parent.iteration,
            messages=[*parent.messages, user(new_message)] if new_message
            else list(parent.messages),
            status=STATUS_RUNNING,
            head_seq=-1,
            scope=dict(parent.scope),
            forked_from=run_id,
            forked_at_seq=at_seq,
            plan_run_id=parent.plan_run_id,   # 分叉继承计划谱系；对账结论不跟（那是父 run 那一次的）
        )
        child.scope = await self._clone_artifacts(parent, child, invalidate)
        saved = await self.save(child)
        await self.supersede(run_id)
        return saved

    async def _clone_artifacts(self, parent: Checkpoint, child: Checkpoint,
                               invalidate: Iterable[str] = ()) -> dict[str, Any]:
        """复制父 run 的产物集到子 run 的新作用域，并作废本次要重跑的节点。

        父 run 没显式 artifact_id 时产物落在服务端的 ``_default`` 集里——那也要复制：
        子 run 一律换新 id，否则分叉试错会把父 run 的产物集改花，时间旅行就失去意义。
        """
        sid = str(parent.scope.get("storyline_session") or "")
        if not sid:
            return dict(parent.scope)
        src = self._storage.artifacts(sid, str(parent.scope.get("artifact_id") or ""))
        dst_artifact = new_artifact_id()
        dst = self._storage.artifacts(sid, dst_artifact)
        await dst.clone_from(src)
        await dst.delete_nodes(invalidate)
        return {"storyline_session": sid, "artifact_id": dst_artifact}

    async def supersede(self, run_id: str) -> None:
        """父 run 让位：状态改 superseded，从此不在崩溃恢复与「继续」的候选里。"""
        row = await self._repo.load(run_id)
        if row is None:
            return
        row["status"] = STATUS_SUPERSEDED
        await self._repo.save(row)

    async def delete(self, run_id: str) -> None:
        await self._repo.drop(run_id)

    async def prune(self) -> int:
        """收尾后按量截断旧的成功/失败行（连带它们的一致点链）。"""
        return await self._repo.prune(self.prune_keep)


def new_artifact_id() -> str:
    """一次剪辑产物集的 id：短、可读、与 render_jobs.artifact_id 同形状。"""
    return f"art-{uuid.uuid4().hex[:8]}"
