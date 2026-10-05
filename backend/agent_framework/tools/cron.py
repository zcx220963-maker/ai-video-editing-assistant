"""CronTool 定时任务：PG `scheduled_jobs` 持久化 + asyncio 定时器调度。

对应设计文档「CronTool 设计」：
  LLM → CronTool(CRUD) → Storage(保存 CronJob) → 定时调度器 → 找到到期 Job
     → 执行 Job → 更新状态 → 重新设置下一次定时器
核心思想：把定时任务持久化（原来是一个整 JSON 文件），用定时器负责唤醒与执行。

单次 vs 周期：
  schedule.kind == "at"    → 只执行一次；delete_after_run 决定执行后是删除还是禁用
  schedule.kind == "every" → 周期执行；每次执行后按 interval 计算下一次时间

「到点做什么」通过可注入的 runner 回调解耦（生产里通常调用 agent.handle 派发任务），
本模块只负责调度与状态管理，不绑定具体执行语义。

多实例：整文件覆写会被两个进程互相踩掉，所以领取一轮走 ``state`` 的比较-交换——
领取时就把 next_run_at_ms 推进到下一轮，抢到的人独占这一轮执行（spec §7）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..tool import Tool


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class Schedule:
    kind: str                 # "at" | "every"
    at_ms: int | None = None      # kind=at：绝对触发时间（epoch 毫秒）
    every_ms: int | None = None   # kind=every：间隔毫秒

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at_ms": self.at_ms, "every_ms": self.every_ms}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Schedule":
        return cls(kind=d["kind"], at_ms=d.get("at_ms"), every_ms=d.get("every_ms"))


@dataclass
class JobState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"next_run_at_ms": self.next_run_at_ms, "last_run_at_ms": self.last_run_at_ms}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobState":
        return cls(next_run_at_ms=d.get("next_run_at_ms"), last_run_at_ms=d.get("last_run_at_ms"))


@dataclass
class CronJob:
    id: str
    name: str
    task: str
    schedule: Schedule
    enabled: bool = True
    delete_after_run: bool = False
    state: JobState = field(default_factory=JobState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task": self.task,
            "enabled": self.enabled,
            "delete_after_run": self.delete_after_run,
            "schedule": self.schedule.to_dict(),
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CronJob":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            task=d.get("task", ""),
            enabled=d.get("enabled", True),
            delete_after_run=d.get("delete_after_run", False),
            schedule=Schedule.from_dict(d["schedule"]),
            state=JobState.from_dict(d.get("state", {})),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task": self.task,
            "enabled": self.enabled,
            "delete_after_run": self.delete_after_run,
            "schedule": self.schedule.to_dict(),
            "state": self.state.to_dict(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CronJob":
        return cls.from_dict(row)

    def summary(self) -> str:
        kind = self.schedule.kind
        when = (
            _fmt_ms(self.schedule.at_ms) if kind == "at" else f"每 {self.schedule.every_ms // 1000}s"
        )
        next_run = _fmt_ms(self.state.next_run_at_ms) if self.state.next_run_at_ms else "-"
        return (
            f"id={self.id} name={self.name!r} enabled={self.enabled} "
            f"kind={kind}({when}) next_run={next_run} task={self.task!r}"
        )


def _fmt_ms(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def _parse_at_to_ms(value: Any) -> int | None:
    """把 run_at 解析成 epoch 毫秒：支持 epoch 秒 / epoch 毫秒 / ISO 字符串 / None(=立即)。"""
    if value is None:
        return _now_ms()
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 1_000_000_000_000 else v * 1000  # 已是毫秒则直接用
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return _parse_at_to_ms(int(s))
        try:
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            return None
    return None


# runner(job) -> None，到点执行的回调
JobRunner = Callable[[CronJob], "Awaitable[None] | None"]


# --------------------------------------------------------------------------
# 调度器
# --------------------------------------------------------------------------

class CronScheduler:
    def __init__(self, storage: Any, runner: JobRunner | None = None) -> None:
        self._jobs = storage.jobs
        self._runner = runner
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._running = False
        self._lock = asyncio.Lock()
        self.executed: list[tuple[str, int]] = []  # (job_id, fired_at_ms) 观测用

    # ---- CRUD（供工具调用）----

    async def create_job(
        self,
        task: str,
        name: str = "",
        kind: str = "at",
        run_at: Any = None,
        every_seconds: int | None = None,
        delete_after_run: bool = True,
    ) -> CronJob:
        job_id = uuid.uuid4().hex[:12]
        if kind == "at":
            at_ms = _parse_at_to_ms(run_at)
            if at_ms is None:
                raise ValueError(f"无法解析 run_at={run_at!r}")
            schedule = Schedule(kind="at", at_ms=at_ms)
            next_run = at_ms
        elif kind == "every":
            if not every_seconds or every_seconds <= 0:
                raise ValueError("kind=every 需要正数 every_seconds")
            every_ms = int(every_seconds * 1000)
            schedule = Schedule(kind="every", every_ms=every_ms)
            next_run = _now_ms() + every_ms
        else:
            raise ValueError(f"未知 schedule.kind={kind!r}，仅支持 'at' / 'every'")

        job = CronJob(
            id=job_id,
            name=name or task[:20],
            task=task,
            schedule=schedule,
            delete_after_run=delete_after_run,
            state=JobState(next_run_at_ms=next_run),
        )
        # name 在表里是唯一约束：撞名就带上下缀，保证两个同任务名能共存
        if await self._jobs.get_by_name(job.name) is not None:
            job.name = f"{job.name}#{job_id[:6]}"
        await self._jobs.upsert(job.to_row())
        self._nudge()
        return job

    async def list_jobs(self) -> list[CronJob]:
        return [CronJob.from_row(r) for r in await self._jobs.list()]

    async def delete_job(self, job_id: str) -> bool:
        gone = await self._jobs.drop_id(job_id)
        self._nudge()
        return gone > 0

    # ---- 调度核心 ----

    async def _next_wake_ms(self) -> int | None:
        times = [(j.state.next_run_at_ms) for j in await self.list_jobs()
                 if j.enabled and j.state.next_run_at_ms is not None]
        return min(times) if times else None

    def _nudge(self) -> None:
        """CRUD 后叫醒调度循环去重算下一次时间（可能新增了更早的任务，也可能清空了）。"""
        if self._running:
            self._wake.set()
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self._running:
            next_wake = await self._next_wake_ms()
            if next_wake is None:
                await self._wake.wait()        # 无可跑任务：停在这里等 CRUD 叫醒
                self._wake.clear()
                continue
            delay_s = max(0, next_wake - _now_ms()) / 1000
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay_s)
                self._wake.clear()             # 被 CRUD 打断 → 重算
                continue
            except asyncio.TimeoutError:
                pass                           # 到点
            async with self._lock:
                await self._fire_due(_now_ms())

    async def start(self) -> None:
        self._running = True
        await self._recover_overdue()
        self._nudge()

    async def _recover_overdue(self) -> None:
        """崩溃恢复：重启时把已过期的 every 任务向前推进，避免风暴式补跑。"""
        now = _now_ms()
        for j in await self.list_jobs():
            if not (j.enabled and j.schedule.kind == "every" and j.state.next_run_at_ms):
                continue
            next_ms = j.state.next_run_at_ms
            while next_ms <= now:
                next_ms += j.schedule.every_ms or 0
            if next_ms != j.state.next_run_at_ms:
                j.state.next_run_at_ms = next_ms
                await self._jobs.set_state(j.id, j.state.to_dict(), enabled=j.enabled)

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            self._task = None

    async def _fire_due(self, now: int) -> int:
        """执行当前所有到期的 job；每个 job 先比较-交换领取，再触发 runner。"""
        fired = 0
        for row in await self._jobs.due(now):
            job = CronJob.from_row(row)
            if job.schedule.kind == "every":
                next_ms, enabled, drop_after = now + (job.schedule.every_ms or 0), True, False
            else:                       # 单次：领走即不再有下一轮
                next_ms, enabled, drop_after = None, False, job.delete_after_run
            state = JobState(next_run_at_ms=next_ms, last_run_at_ms=now)
            claimed = await self._jobs.claim_run(job.id, job.state.to_dict(),
                                                 new_state=state.to_dict(), enabled=enabled)
            if claimed is None:
                continue                     # 别的副本已领走这一轮
            job.state = state
            fired += 1
            self.executed.append((job.id, now))
            if self._runner:
                result = self._runner(job)
                if asyncio.iscoroutine(result):
                    await result
            if drop_after:
                await self._jobs.drop_id(job.id)
        return fired

    # ---- 便捷：立即处理一批到期（测试 / 手动触发用）----

    async def run_due_now(self) -> int:
        """同步地执行当前所有到期任务，返回执行数量。便于测试与手动触发。"""
        async with self._lock:
            return await self._fire_due(_now_ms())


# --------------------------------------------------------------------------
# 暴露给 LLM 的 CRUD 工具（读写分离并发标记）
# --------------------------------------------------------------------------

class CronCreateTool(Tool):
    def __init__(self, scheduler: CronScheduler) -> None:
        self._sched = scheduler

    @property
    def name(self) -> str:
        return "create_cron_job"

    @property
    def display_name(self) -> str:
        return "创建定时任务"

    @property
    def description(self) -> str:
        return (
            "创建一个定时任务。kind='at' 在指定时间执行一次（run_at 支持 ISO 时间或 epoch 秒）；"
            "kind='every' 按 every_seconds 周期执行。to 参数含义：到点会把 task 文本派发执行。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "到点要执行的任务描述"},
                "name": {"type": "string", "description": "任务名称（可选）"},
                "kind": {"type": "string", "enum": ["at", "every"], "description": "at=单次, every=周期"},
                "run_at": {"type": "string", "description": "kind=at 的触发时间，ISO 或 epoch 秒"},
                "every_seconds": {"type": "integer", "description": "kind=every 的间隔秒数"},
                "delete_after_run": {"type": "boolean", "description": "单次任务执行后是否删除，默认 true"},
            },
            "required": ["task", "kind"],
        }

    async def execute(
        self,
        task: str,
        kind: str,
        name: str = "",
        run_at: str | None = None,
        every_seconds: int | None = None,
        delete_after_run: bool = True,
    ) -> str:
        try:
            job = await self._sched.create_job(
                task=task,
                name=name,
                kind=kind,
                run_at=run_at,
                every_seconds=every_seconds,
                delete_after_run=delete_after_run,
            )
        except ValueError as e:
            return f"Error: {e}"
        return f"已创建定时任务：{job.summary()}"


class CronListTool(Tool):
    def __init__(self, scheduler: CronScheduler) -> None:
        self._sched = scheduler

    @property
    def name(self) -> str:
        return "list_cron_jobs"

    @property
    def display_name(self) -> str:
        return "查看定时任务"

    @property
    def description(self) -> str:
        return "列出当前所有定时任务及其下次执行时间。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self) -> str:
        jobs = await self._sched.list_jobs()
        if not jobs:
            return "当前没有任何定时任务。"
        return "\n".join(j.summary() for j in jobs)


class CronDeleteTool(Tool):
    def __init__(self, scheduler: CronScheduler) -> None:
        self._sched = scheduler

    @property
    def name(self) -> str:
        return "delete_cron_job"

    @property
    def display_name(self) -> str:
        return "删除定时任务"

    @property
    def description(self) -> str:
        return "按 id 删除一个定时任务。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "要删除的任务 id"}},
            "required": ["job_id"],
        }

    async def execute(self, job_id: str) -> str:
        ok = await self._sched.delete_job(job_id)
        return f"已删除任务 {job_id}" if ok else f"Error: 未找到任务 {job_id}"


def register_cron_tools(registry, scheduler: CronScheduler) -> None:
    """把 create/list/delete 三个定时任务工具注册到 registry。"""
    registry.register(CronCreateTool(scheduler))
    registry.register(CronListTool(scheduler))
    registry.register(CronDeleteTool(scheduler))
