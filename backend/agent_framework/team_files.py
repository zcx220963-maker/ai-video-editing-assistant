# -*- coding: utf-8 -*-
"""Message Center 的文件后端（R19，仅收件箱）：`{scope}/{agent}.jsonl` 为 canonical。

设计 §24 对 Message Center 的要求最具体：**jsonl 收件箱 + 已读即删**。
PG 版是 `inbox_messages` 打 `consumed_at`，读不删——历史会一直涨。

本模块只实现 `MessageCenter` 用到的 4 个方法（send / read / peek / agents），
所以 `MessageCenter` 只多一个「选后端」的分支。

**边界（写清楚，别当已完成）**：任务板与 SubAgent 登记仍是 PG——它们的接口
还牵到 `task_edges` 的图查询（`TaskManager._deps` 直接读 `storage.db`），
不是换 4 个方法能收口的。这条记在文档 X3。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

_lock = asyncio.Lock()


def _safe(value: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))
    return keep or "_"


class FileInbox:
    """`MessageCenter._inbox` 的文件版：一个 agent 一个 jsonl。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, user_id: str, conv_id: str, agent: str) -> Path:
        return self.root / _safe(user_id) / _safe(conv_id) / f"{_safe(agent)}.jsonl"

    # ---- 读盘/写盘 ----

    def _load(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue          # 半行/坏行跳过，不让一行坏数据毁掉整个收件箱
        return rows

    def _rewrite(self, path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
        os.replace(tmp, path)

    # ---- MessageCenter 用到的 4 个方法 ----

    async def send(self, user_id: str, conv_id: str, to: str,
                   content: dict[str, Any], *, type_: str = "note",
                   sender: str = "", dedup_key: str | None = None) -> dict[str, Any]:
        path = self._path(user_id, conv_id, to)
        async with _lock:
            rows = self._load(path)
            if dedup_key:
                for row in rows:
                    if row.get("dedup_key") == dedup_key:
                        return {**row, "_deduplicated": True}
            row = {
                "id": uuid.uuid4().hex[:12],
                "type": type_,
                "from": sender,
                "to": to,
                "content": content,
                "dedup_key": dedup_key,
                "created_at": time.time(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row

    async def read(self, user_id: str, conv_id: str, agent: str) -> list[dict[str, Any]]:
        """读出并**清空**（设计：已读即删）。"""
        path = self._path(user_id, conv_id, agent)
        async with _lock:
            rows = self._load(path)
            if rows:
                self._rewrite(path, [])
            return rows

    async def peek(self, user_id: str, conv_id: str, agent: str) -> list[dict[str, Any]]:
        return self._load(self._path(user_id, conv_id, agent))

    async def agents(self, user_id: str, conv_id: str) -> list[str]:
        folder = self.root / _safe(user_id) / _safe(conv_id)
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("*.jsonl"))


def inbox_from_env(storage: Any) -> Any:
    """`TEAM_STORE=file` 用文件收件箱，否则沿用 PG（默认，行为不变）。"""
    if os.getenv("TEAM_STORE", "pg").lower() != "file":
        return storage.inbox
    return FileInbox(os.getenv("TEAM_ROOT", ".runtime/team"))
