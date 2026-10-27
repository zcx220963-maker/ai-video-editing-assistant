# -*- coding: utf-8 -*-
"""记忆的文件后端（R12）：`memory/{user}/User.md`、`Tool.md` 为 canonical。

设计 §19 的存储是「单 Markdown 文件、每轮携带、FileTool 全量覆盖」。这个类只实现
`MemoryStore` 真正用到的 6 个方法，所以 `MemoryStore` 只多一个「选后端」的分支。

两条约束：

* **只用标准库**：`filelock` 不在运行时依赖里，引进来会让启动失败。这里靠
  `os.replace` 的原子写 + 进程内锁；跨进程并发写同一用户的记忆不在本期范围
  （多副本要显式 `MEMORY_STORE=pg`）。
* **文件名固定**：`user → User.md`、`tool → Tool.md`。设计点名了这两个文件名，
  按分类名拼（`user.md`）会让「打开文件找记忆」的人找不到。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

_NAMES = {"user": "User.md", "tool": "Tool.md"}


def _safe(user_id: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in user_id)
    return keep or "default"


class FileMemoryBackend:
    """`MemoryStore._mem` 的文件版实现。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        # RLock 而不是 Lock：`replace_if` 持锁期间会调 `write`（同一个锁），
        # 普通 Lock 会在这里自死锁——测试里表现成「进程卡住、什么都不打印」。
        self._lock = threading.RLock()

    def _path(self, user_id: str, category: str) -> Path:
        name = _NAMES.get(category, f"{category}.md")
        return self.root / _safe(user_id) / name

    async def read(self, user_id: str, category: str) -> str:
        try:
            return self._path(user_id, category).read_text(encoding="utf-8")
        except OSError:
            return ""

    async def write(self, user_id: str, category: str, content: str) -> None:
        path = self._path(user_id, category)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)

    async def append(self, user_id: str, category: str, text: str) -> str:
        merged = ((await self.read(user_id, category)).rstrip() + "\n" + text).strip()
        await self.write(user_id, category, merged)
        return merged

    async def replace_if(self, user_id: str, category: str, before: str,
                         after: str) -> bool:
        """CAS：只有当前内容仍等于 `before` 才写。给压缩器用，防覆盖并发写入。"""
        with self._lock:
            current = await self.read(user_id, category)
            if current != before:
                return False
            await self.write(user_id, category, after)
            return True

    async def entries(self, user_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for cat in _NAMES:
            text = await self.read(user_id, cat)
            if text.strip():
                out[cat] = text
        return out

    async def stamps(self, user_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for cat in _NAMES:
            try:
                out[cat] = str(int(self._path(user_id, cat).stat().st_mtime))
            except OSError:
                continue
        return out


def backend_from_env(storage: Any) -> Any:
    """按 `MEMORY_STORE` 选后端；默认 pg（既有行为不变）。"""
    if os.getenv("MEMORY_STORE", "pg").lower() != "file":
        return storage.memories
    return FileMemoryBackend(os.getenv("MEMORY_ROOT", ".runtime/memory"))
