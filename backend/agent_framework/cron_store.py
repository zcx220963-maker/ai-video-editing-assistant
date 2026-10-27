# -*- coding: utf-8 -*-
"""Cron 的文件后端（R14）：`.runtime/cron/{scope}/jobs.json` 为 canonical。

设计 §16 要求「用文件系统保存 CronJob」。这个类只实现 `CronScheduler` 真正用到的
7 个方法（get_by_name / upsert / list / drop_id / set_state / due / claim_run），
所以 `CronScheduler` 一行都不用改——它拿到的仍是「一个带 `.jobs` 的对象」。

两条实现约束，都写在这儿免得后人踩：

* **只用标准库**。`filelock` 不是运行时必备（镜像里没有），引进来会让启动直接失败；
  这里用 `threading.Lock` + `os.replace` 的原子写。**单机单进程**成立；
  多副本必须显式 `--cron-store pg`（跨主机文件没有原子 claim）。
* **写整份文件**。定时任务是个位数到几十条的规模，整份重写比做增量简单得多，
  而且 `os.replace` 之后读者永远看到一份完整 JSON。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class FileCronStore:
    """`CronScheduler` 用的 jobs 仓库（文件版）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"jobs": []})

    # ---- 读写 ----

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"jobs": []}

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- CronScheduler 用到的 7 个方法 ----

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        for row in self._read().get("jobs", []):
            if row.get("name") == name:
                return row
        return None

    async def upsert(self, row: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            jobs = [j for j in data.get("jobs", []) if j.get("id") != row.get("id")]
            jobs.append(dict(row))
            data["jobs"] = jobs
            self._write(data)
        return row

    async def list(self) -> list[dict[str, Any]]:
        return list(self._read().get("jobs", []))

    async def drop_id(self, job_id: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data.get("jobs", []))
            data["jobs"] = [j for j in data.get("jobs", []) if j.get("id") != job_id]
            self._write(data)
            return len(data["jobs"]) != before

    async def set_state(self, job_id: str, state: dict[str, Any],
                        *, enabled: bool | None = None) -> None:
        with self._lock:
            data = self._read()
            for row in data.get("jobs", []):
                if row.get("id") != job_id:
                    continue
                row["state"] = dict(state)
                if enabled is not None:
                    row["enabled"] = bool(enabled)
            self._write(data)

    async def due(self, now_ms: int) -> list[dict[str, Any]]:
        out = []
        for row in self._read().get("jobs", []):
            if not row.get("enabled"):
                continue
            nxt = (row.get("state") or {}).get("next_run_at_ms")
            if nxt and now_ms >= int(nxt):
                out.append(row)
        return out

    async def claim_run(self, job_id: str, state: dict[str, Any], **_kw: Any) -> bool:
        """单进程内天然互斥（同一个 scheduler）；这里只负责把 state 落盘。

        多副本时两个进程会各自读到同一个 due job——这正是 X3 记的「文件模式
        跨主机没有原子 claim」，用 `--cron-store pg` 才有 CAS。
        """
        await self.set_state(job_id, state)
        return True


class CronFileStorage:
    """给 `CronScheduler(storage)` 用的最小外壳：它只读 `storage.jobs`。"""

    def __init__(self, path: str | Path) -> None:
        self.jobs = FileCronStore(path)
