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

    async def claim_run(self, job_id: str, expect_state: dict[str, Any], *,
                        new_state: dict[str, Any] | None = None,
                        enabled: bool = True, **_kw: Any) -> bool:
        """比较-交换领取（与 PG 同一份合同）：state 没被别人动过才写入新一轮。

        早先这里把 ``expect_state`` 当新状态写回、且恒返回 True——文件后端于是
        既没做 CAS，也没把 ``next_run_at_ms`` 推进/关掉单次任务：到期任务每个
        调度 tick 都会再触发一次。这一层必须与 `QuotaRequest` 那类 CAS 同口径：
        **状态没变过**才认领，新状态由调用方给。

        跨主机仍不成立（两个进程各自的 threading.Lock 管不到对方）——那是 X3
        记的边界，要跨副本请用 `--cron-store pg`；但同机多实例与「先读 due 再
        领取之间被别人插进来」这两种，这里就能挡住。
        """
        new = dict(new_state if new_state is not None else expect_state)

        def _norm(d: Any) -> dict[str, Any]:
            # 老文件里可能只写了 next_run_at_ms：缺键补 None 再比，
            # 否则与 JobState.to_dict() 的完整两键永远不相等，CAS 恒失败。
            got = dict(d or {})
            return {k: got.get(k) for k in ("next_run_at_ms", "last_run_at_ms")}

        with self._lock:
            data = self._read()
            for row in data.get("jobs", []):
                if row.get("id") != job_id:
                    continue
                if not row.get("enabled"):
                    return False
                if _norm(row.get("state")) != _norm(expect_state):
                    return False
                row["state"] = new
                row["enabled"] = bool(enabled)
                self._write(data)
                return True
        return False


class CronFileStorage:
    """给 `CronScheduler(storage)` 用的最小外壳：它只读 `storage.jobs`。"""

    def __init__(self, path: str | Path) -> None:
        self.jobs = FileCronStore(path)
