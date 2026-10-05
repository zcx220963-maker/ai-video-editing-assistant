"""Heartbeat：复用 CronScheduler 的周期任务，做「空闲 Agent 周期唤醒 / 巡检」。

设计文档里 HEARTBEAT 与 Cron 并列在 Scheduling 下，机制同构：持久化到 PG + asyncio 定时器。
这里不重造调度器，只把 CronScheduler 的一个 kind="every" 循环任务包成心跳——每次触发调用
注入的 on_beat 回调（装配时接「唤醒空闲 Agent / 检查到期任务 / 巡检会话健康」等具体动作）。
落点就是 ``scheduled_jobs`` 表里 ``name='heartbeat'`` 的那一行（spec §3.11：两机制同表）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from .storage.db import IntegrityConflict
from .tools.cron import CronJob, CronScheduler

# on_beat(job) -> None | awaitable
BeatCallback = Callable[[CronJob], "Awaitable[None] | None"]


class Heartbeat:
    def __init__(
        self,
        storage: Any,
        on_beat: BeatCallback | None = None,
        *,
        interval_seconds: int = 30,
        name: str = "heartbeat",
        scheduler: CronScheduler | None = None,
    ) -> None:
        self._storage = storage
        # 默认自建一个只跑心跳的调度器；装配方给了就共用（定时任务与心跳本就同表同机制）。
        self._sched = scheduler or CronScheduler(storage, runner=self.handle)
        self._owns_sched = scheduler is None
        self._on_beat = on_beat
        self._interval = interval_seconds
        self._name = name
        self.beats = 0  # 已触发次数，观测 / 测试用

    @property
    def scheduler(self) -> CronScheduler:
        return self._sched

    async def handle(self, job: CronJob) -> None:
        """心跳行到点时该做的事：只数拍子 + 回调，绝不把「heartbeat」当对话投进 MQ。"""
        self.beats += 1
        if self._on_beat:
            result = self._on_beat(job)
            if asyncio.iscoroutine(result):
                await result

    async def start(self) -> None:
        await self._claim_or_create()
        if self._owns_sched:
            await self._sched.start()

    async def _claim_or_create(self) -> None:
        """认领或新建那行心跳，**绝不删了重建**。

        「先 drop 同名再 create」是单实例口径，多副本共用一张 scheduled_jobs 时会出两件事：
        第二个实例的 drop 把第一个实例正在用的那行删掉（它的节拍从此停），而两边同时 create
        会在 name 唯一键上撞车、启动直接失败。至于「一轮被跑两次」——那不归这里管，
        cron 层的 `claim_run` 是比较-交换领取，本就只有一个赢家。
        """
        existing = await self._storage.jobs.get_by_name(self._name)
        if existing is None:
            try:
                await self._sched.create_job(
                    task=self._name,
                    name=self._name,
                    kind="every",
                    every_seconds=self._interval,
                    delete_after_run=False,
                )
                return
            except IntegrityConflict:
                # 另一个实例在同一瞬间建好了同名行：回读认领，不再建第二条
                existing = await self._storage.jobs.get_by_name(self._name)
                if existing is None:
                    raise
        every_ms = int(self._interval) * 1000
        row = dict(existing)
        sched = dict(row.get("schedule") or {})
        state = dict(row.get("state") or {})
        reschedule = sched.get("kind") != "every" or sched.get("every_ms") != every_ms
        sched.update({"kind": "every", "at_ms": None, "every_ms": every_ms})
        if reschedule:      # 间隔改了才重排下一次，否则沿用别的实例排好的时间
            state = {"next_run_at_ms": int(time.time() * 1000) + every_ms,
                     "last_run_at_ms": state.get("last_run_at_ms")}
        row.update({"schedule": sched, "state": state,
                    "enabled": True, "delete_after_run": False})
        await self._storage.jobs.upsert(row)

    async def beat_now(self) -> int:
        """立即触发一次当前到期（测试 / 手动心跳用）。"""
        return await self._sched.run_due_now()

    def stop(self) -> None:
        self._sched.stop()
