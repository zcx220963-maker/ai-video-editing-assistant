# -*- coding: utf-8 -*-
"""Agent Team 的文件后端（设计 §24）：文件为真相，PG 只做可选投影。

设计原文对三件套的落点写得很具体：

    tasks/{scope}/task_*.json        任务板（状态 pending → on_progress → completed）
    messages/{scope}/{name}.jsonl    收件箱（读后清空）
    agents/{scope}/{name}.json       子 Agent 档案（Prompt + 工作状态）

旧实现三件全在 PG：``tasks``/``task_edges``、``inbox_messages``（打 consumed_at 不删）、
``subagents``。功能等价，但「落到文件系统」这条设计没兑现，而且任务状态存的是
``claimed`` 而不是设计写的 ``on_progress``。

本模块给出三件套的文件实现，并保留 PG 作为多副本后端：``TEAM_STORE=pg``。

**边界（写清楚）**：文件后端只适用于单进程/单主机。跨主机没有原子 claim
（文件系统给不了 compare-and-swap），多副本必须显式切 ``TEAM_STORE=pg``。
这条是设计取舍，不是待办。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TEAM_ROOT_DEFAULT = ".runtime/team"
#: 任务板状态词：设计原文 §24「pending → on_progress → completed」。
PENDING = "pending"
ON_PROGRESS = "on_progress"
COMPLETED = "completed"
#: 老库里的列值；读取时归一成 on_progress，写入一律用新词。
_LEGACY_CLAIMED = "claimed"

_lock = asyncio.Lock()


def _safe(value: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))
    return keep or "_"


def _scope_parts(scope: str) -> tuple[str, str]:
    user, _, conv = str(scope).partition(":")
    return _safe(user), _safe(conv or user)


def _norm_status(status: str | None) -> str:
    return ON_PROGRESS if status == _LEGACY_CLAIMED else (status or "")


def _team_root(root: str | Path | None = None) -> Path:
    return Path(root or os.getenv("TEAM_ROOT", TEAM_ROOT_DEFAULT))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: Path, row: dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Message Center：messages/{scope}/{name}.jsonl
# --------------------------------------------------------------------------

class FileInbox:
    """一个 agent 一个 jsonl；``read`` 读出后清空（已读即删）。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, user_id: str, conv_id: str, agent: str) -> Path:
        return (self.root / "messages" / _safe(user_id) / _safe(conv_id)
                / f"{_safe(agent)}.jsonl")

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
                _atomic_write(path, "")
            return rows

    async def peek(self, user_id: str, conv_id: str, agent: str) -> list[dict[str, Any]]:
        return self._load(self._path(user_id, conv_id, agent))

    async def agents(self, user_id: str, conv_id: str) -> list[str]:
        folder = self.root / "messages" / _safe(user_id) / _safe(conv_id)
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("*.jsonl"))


# --------------------------------------------------------------------------
# Task Manager：tasks/{scope}/task_*.json
# --------------------------------------------------------------------------

class FileTaskBoard:
    """任务板文件后端。接口与 ``storage.repositories.TasksRepo`` 对齐。

    依赖关系写在每行 JSON 的 ``blockedBy`` / ``blocks`` 里（设计原文的形状）；
    每次变更后重算一遍：已完成的前置从 ``blockedBy`` 自动移除，``blocks`` 由
    ``blockedBy`` 的反向视图现算——两份副本不会漂移，因为它们在同一次重排里写。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, scope: str) -> Path:
        user, conv = _scope_parts(scope)
        return self.root / "tasks" / user / conv

    def _edges_path(self, scope: str) -> Path:
        return self._dir(scope) / "edges.json"

    # ---- 读盘/写盘 ----

    def _load_edges(self, scope: str) -> list[list[int]]:
        """原始依赖边（task_id → depends_on）。不随完成删除，BFS 闭包靠它。"""
        path = self._edges_path(scope)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return []
        out: list[list[int]] = []
        for pair in data if isinstance(data, list) else []:
            try:
                out.append([int(pair[0]), int(pair[1])])
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def _load(self, scope: str) -> list[dict[str, Any]]:
        folder = self._dir(scope)
        if not folder.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(folder.glob("task_*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            row.setdefault("id", int(path.stem.split("_")[-1]))
            row["scope"] = scope
            row["status"] = _norm_status(row.get("status"))
            row.setdefault("owner", "")
            row.setdefault("description", "")
            row.setdefault("blockedBy", [])
            rows.append(row)
        rows.sort(key=lambda r: int(r["id"]))
        return rows

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row["blockedBy"] = [int(d) for d in row.get("blockedBy") or []]
        row["blocks"] = [int(d) for d in row.get("blocks") or []]
        row["status"] = _norm_status(row.get("status"))
        return row

    def _view_from_edges(self, rows: list[dict[str, Any]], edges: list[list[int]],
                         scope: str) -> list[dict[str, Any]]:
        done = {int(r["id"]) for r in rows if r["status"] == COMPLETED}
        deps: dict[int, set[int]] = defaultdict(set)
        reverse: dict[int, set[int]] = defaultdict(set)
        for task_id, dep in edges:
            deps[task_id].add(dep)
            reverse[dep].add(task_id)
        out = []
        for row in rows:
            tid = int(row["id"])
            row["scope"] = scope
            row["status"] = _norm_status(row["status"])
            row["blockedBy"] = sorted(d for d in deps.get(tid, set()) if d not in done)
            row["blocks"] = sorted(reverse.get(tid, set()))
            out.append(self._view(row))
        return out

    def _persist(self, scope: str, rows: list[dict[str, Any]],
                 edges: list[list[int]] | None = None) -> list[dict[str, Any]]:
        """把视图落盘。真值是 edges.json；任务 JSON 里的 blockedBy/blocks 只是给人看的。"""
        if edges is None:
            edges = self._load_edges(scope)
        views = self._view_from_edges(rows, edges, scope)
        for row in views:
            _write_json(self._dir(scope) / f"task_{int(row['id'])}.json", row)
        return views

    # ---- 与 TasksRepo 同形的接口 ----

    async def create(self, scope: str, name: str, description: str = "", *,
                     blocked_by: Iterable[int] = ()) -> dict[str, Any]:
        deps = sorted({int(d) for d in blocked_by})
        async with _lock:
            rows = self._load(scope)
            ids = {int(r["id"]) for r in rows}
            missing = [d for d in deps if d not in ids]
            if missing:
                raise ValueError(f"前置任务 {missing} 不在 scope {scope} 内")
            row = {
                "id": max(ids, default=0) + 1,
                "scope": scope,
                "name": name,
                "description": description,
                "status": PENDING,
                "owner": "",
                "blockedBy": [],
                "blocks": [],
            }
            rows.append(row)
            edges = self._load_edges(scope) + [[int(row["id"]), d] for d in deps]
            _write_json(self._edges_path(scope), sorted(edges))
            out = self._persist(scope, rows, edges)
            return next(r for r in out if int(r["id"]) == int(row["id"]))

    async def get(self, task_id: int, scope: str | None = None) -> dict[str, Any] | None:
        tid = int(task_id)
        if scope is not None:
            views = self._view_from_edges(self._load(scope), self._load_edges(scope), scope)
            return next((r for r in views if int(r["id"]) == tid), None)
        for folder in sorted((self.root / "tasks").glob("*/*")):
            found_scope = self._scope_of(folder)
            views = self._view_from_edges(self._load(found_scope),
                                          self._load_edges(found_scope), found_scope)
            for row in views:
                if int(row["id"]) == tid:
                    return row
        return None

    def _scope_of(self, folder: Path) -> str:
        rows = self._load(f"{folder.parent.name}:{folder.name}")
        return rows[0]["scope"] if rows else f"{folder.parent.name}:{folder.name}"

    async def list_all(self, scope: str) -> list[dict[str, Any]]:
        return self._view_from_edges(self._load(scope), self._load_edges(scope), scope)

    async def claim_one(self, scope: str, owner: str, task_id: int) -> dict[str, Any] | None:
        async with _lock:
            rows = self._load(scope)
            edges = self._load_edges(scope)
            view = next((r for r in self._view_from_edges(rows, edges, scope)
                         if int(r["id"]) == int(task_id)), None)
            if view is None or view["status"] != PENDING or view["blockedBy"]:
                return None
            row = next(r for r in rows if int(r["id"]) == int(task_id))
            row["status"] = ON_PROGRESS
            row["owner"] = owner
            out = self._persist(scope, rows, edges)
            return next(r for r in out if int(r["id"]) == int(task_id))

    async def complete(self, scope: str, task_id: int, *,
                       owner: str | None = None) -> dict[str, Any] | None:
        async with _lock:
            rows = self._load(scope)
            row = next((r for r in rows if int(r["id"]) == int(task_id)), None)
            if row is None:
                return None
            if owner is not None and (row["status"] != ON_PROGRESS or row["owner"] != owner):
                return None
            row["status"] = COMPLETED
            out = self._persist(scope, rows)
            return next(r for r in out if int(r["id"]) == int(task_id))

    async def ready(self, scope: str) -> list[dict[str, Any]]:
        rows = self._load(scope)
        views = self._view_from_edges(rows, self._load_edges(scope), scope)
        return [r for r in views if r["status"] == PENDING and not r["blockedBy"]]

    async def done_ids(self, scope: str) -> set[int]:
        return {int(r["id"]) for r in self._load(scope) if r["status"] == COMPLETED}

    async def raw_edges(self, scope: str) -> list[list[int]]:
        """原始依赖边（不随完成删除），供 BFS 闭包使用。"""
        return self._load_edges(scope)


# --------------------------------------------------------------------------
# SubAgent Manager：agents/{scope}/{name}.json
# --------------------------------------------------------------------------

class FileSubagentRegistry:
    """子 Agent 档案文件后端。接口与 ``SubagentsRepo`` 对齐。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, scope: str) -> Path:
        user, conv = _scope_parts(scope)
        return self.root / "agents" / user / conv

    def _path(self, scope: str, name: str) -> Path:
        return self._dir(scope) / f"{_safe(name)}.json"

    def _read(self, scope: str, name: str) -> dict[str, Any] | None:
        path = self._path(scope, name)
        if not path.exists():
            return None
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        row.setdefault("scope", scope)
        row.setdefault("name", name)
        row.setdefault("prompt", "")
        row.setdefault("status", "idle")
        row.setdefault("lease_expires_at", None)
        return row

    def _rows(self, scope: str) -> list[dict[str, Any]]:
        folder = self._dir(scope)
        if not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob("*.json")):
            row = self._read(scope, path.stem)
            if row is not None:
                out.append(row)
        out.sort(key=lambda r: r["name"])
        return out

    async def register(self, scope: str, name: str, prompt: str,
                       lease_sec: float = 600, status: str = "idle") -> dict[str, Any]:
        async with _lock:
            old = self._read(scope, name) or {}
            row = {
                "scope": scope,
                "name": name,
                "prompt": prompt,
                "status": status,
                "lease_expires_at": (time.time() + lease_sec) if status == "working" else None,
                "created_at": old.get("created_at") or time.time(),
                "updated_at": time.time(),
            }
            _write_json(self._path(scope, name), row)
            return row

    async def set_status(self, scope: str, name: str, status: str,
                         lease_sec: float = 600) -> None:
        async with _lock:
            row = self._read(scope, name) or {
                "scope": scope, "name": name, "prompt": "",
                "created_at": time.time(),
            }
            row["status"] = status
            row["lease_expires_at"] = (time.time() + lease_sec) if status == "working" else None
            row["updated_at"] = time.time()
            _write_json(self._path(scope, name), row)

    async def list(self, scope: str) -> list[dict[str, Any]]:
        return self._rows(scope)

    async def reset_expired(self, scope: str | None = None) -> int:
        """租约到期的 working 一律归位 idle。scope=None 扫全部作用域。"""
        scopes = [scope] if scope is not None else [
            self._scope_of(folder)
            for folder in sorted((self.root / "agents").glob("*/*"))
        ]
        count = 0
        for one in scopes:
            for row in self._rows(one):
                lease = row.get("lease_expires_at")
                if row["status"] == "working" and lease is not None and float(lease) <= time.time():
                    await self.set_status(one, row["name"], "idle")
                    count += 1
        return count

    def _scope_of(self, folder: Path) -> str:
        rows = self._rows(f"{folder.parent.name}:{folder.name}")
        return rows[0]["scope"] if rows else f"{folder.parent.name}:{folder.name}"


# --------------------------------------------------------------------------
# 选后端 + PG 投影重建
# --------------------------------------------------------------------------

def select_team_store(storage: Any, *, store: str | None = None,
                      root: str | Path | None = None) -> tuple[Any, Any, Any]:
    """返回 ``(inbox, tasks, subagents)`` 三个后端。

    ``store="file"``（默认）：文件为真相。
    ``store="pg"``：沿用 storage 的三张表（多副本必须选它）。
    """
    kind = (store or os.getenv("TEAM_STORE", "file")).lower()
    if kind == "pg":
        return storage.inbox, storage.tasks, storage.subagents
    root_path = _team_root(root)
    return FileInbox(root_path), FileTaskBoard(root_path), FileSubagentRegistry(root_path)


def inbox_from_env(storage: Any, *, store: str | None = None,
                   root: str | Path | None = None) -> Any:
    """兼容旧调用点：只取收件箱后端。"""
    return select_team_store(storage, store=store, root=root)[0]


async def rebuild_projection(storage: Any, *, root: str | Path | None = None) -> dict[str, int]:
    """把文件真相投影回 PG（``tasks/task_edges/subagents``），返回写入行数。

    文件是 canonical，PG 是给多副本/看板用的投影；损坏或清空后随时可以从文件重建。
    收件箱不投影：它是「读后清空」的一次性队列，没有可重建的历史语义。
    """
    from .storage.db import in_

    root_path = _team_root(root)
    counts = {"tasks": 0, "edges": 0, "subagents": 0}

    for folder in sorted((root_path / "tasks").glob("*/*")):
        scope = f"{folder.parent.name}:{folder.name}"
        board = FileTaskBoard(root_path)
        rows = board._load(scope)
        if not rows:
            continue
        real_scope = rows[0].get("scope") or scope
        for row in rows:
            await storage.db.upsert("tasks", {
                "id": int(row["id"]), "scope": real_scope,
                "name": row.get("name", ""),
                "description": row.get("description", ""),
                "status": _norm_status(row.get("status")),
                "owner": row.get("owner", "") or "",
            })
            counts["tasks"] += 1
        ids = [int(r["id"]) for r in rows]
        await storage.db.delete("task_edges", where={"task_id": in_(ids)})
        for task_id, dep in board._load_edges(real_scope):
            await storage.db.upsert("task_edges", {
                "task_id": int(task_id), "depends_on": int(dep)})
            counts["edges"] += 1

    registry = FileSubagentRegistry(root_path)
    for folder in sorted((root_path / "agents").glob("*/*")):
        scope = f"{folder.parent.name}:{folder.name}"
        for row in registry._rows(scope):
            lease = row.get("lease_expires_at")
            await storage.db.upsert("subagents", {
                "scope": row.get("scope") or scope,
                "name": row["name"],
                "prompt": row.get("prompt", ""),
                "status": row.get("status", "idle"),
                "lease_expires_at": (
                    datetime.fromtimestamp(float(lease), tz=timezone.utc)
                    if lease is not None else None),
            })
            counts["subagents"] += 1
    return counts
