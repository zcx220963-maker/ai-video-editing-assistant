"""记忆系统：按 (user_id, category) 归属的长期记忆，落在存储层 memories 表。

对应设计文档「记忆系统」+ spec §3.12（取代 User.md / Tool.md 全文覆盖）：
- 分类白名单：user=用户偏好，tool=工具使用反馈；与表上的 CHECK 同源。
- 每次与 LLM 交互都携带记忆内容（读侧用 ContextSource 注入 System）。
- 增删由 Agent 通过工具自主控制；写入采用【全量覆盖】以删除旧记忆。
- 归属在调用时从执行身份（identity contextvar）解析：工具实例是进程级单例、
  所有会话共用，记忆必须跟着「这次是谁在跑」走，否则用户之间串味。
- 演进方向（RAG 召回）由 MemoryContextSource.render(query) 的参数预留，当前全量携带。

安全：category 走白名单（PG 端还有 CHECK 兜底），user_id 不取自模型入参。
"""

from __future__ import annotations

from typing import Any

from .identity import current_identity_or
from .tool import Tool

# 分类 → 注入 System 时的标题（白名单；PG 侧 memories.category 有同名 CHECK）
CATEGORIES: dict[str, str] = {
    "user": "用户偏好记忆",
    "tool": "工具使用记忆",
}


class MemoryStore:
    """memories 表的用户侧门面：绑定归属、校验分类、异步读写。"""

    def __init__(self, storage: Any, *, user_id: str | None = None) -> None:
        self._mem = storage.memories
        self._fixed_user = user_id

    @property
    def user_id(self) -> str:
        """本次调用的归属：构造时指定则固定，否则取当前 run 身份（离线直调退回 default）。"""
        return current_identity_or(self._fixed_user or "default").user_id

    @staticmethod
    def _check(category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError(f"未知记忆分类 {category!r}，仅支持：{sorted(CATEGORIES)}")

    async def read(self, category: str) -> str:
        self._check(category)
        return await self._mem.read(self.user_id, category)

    async def write(self, category: str, content: str) -> None:
        """全量覆盖写入（用于删除/替换旧记忆）。

        归属必须是已登记身份（``/register`` 签发或启动期内部登记）——
        ``memories.user_id`` 有外键，来路不明的 id 会如实失败而不是悄悄补一行。
        """
        self._check(category)
        await self._mem.write(self.user_id, category, content)

    async def append(self, category: str, text: str) -> str:
        """追加一条记忆（换行分隔）；仓储层用比较-交换保证并发下不丢更新。"""
        self._check(category)
        return await self._mem.append(self.user_id, category, text)

    async def entries(self, categories: list[str] | None = None) -> dict[str, str]:
        """返回非空的分类记忆：category -> content，按白名单顺序。"""
        cats = categories or list(CATEGORIES)
        for cat in cats:
            self._check(cat)
        got = await self._mem.entries(self.user_id)
        return {cat: got[cat].strip() for cat in cats if got.get(cat, "").strip()}


class MemoryContextSource:
    """实现 ContextSource 协议：把记忆内容注入 System Prompt。

    当前策略是全量携带，但**带上限**：render(query) 的 query 参数为未来 RAG 相关性召回预留。

    为什么必须有上限：记忆是 Agent 自己 append 的，schema 上没有任何长度约束，
    而它每一轮都整段注进 system，压缩器又不看 system 段。实测 append 200 次就有
    30489 字符，且只增不减——每轮 prompt 稳定膨胀，最后必然打爆上下文。
    超限时按分类截断并明确写出「已省略多少字」，让模型知道自己看到的不是全部
    （而不是静默丢内容、让它以为记忆就这么多）。
    """

    name = "memory"
    # 单条分类的上限与全量上限：按「中文字≈1 token」估，约合 1.5k~2k token。
    per_category_chars = 2000
    total_chars = 4000

    def __init__(self, store: MemoryStore, categories: list[str] | None = None,
                 *, per_category_chars: int | None = None,
                 total_chars: int | None = None) -> None:
        self._store = store
        self._categories = categories
        if per_category_chars is not None:
            self.per_category_chars = int(per_category_chars)
        if total_chars is not None:
            self.total_chars = int(total_chars)

    def _clip(self, content: str, limit: int) -> str:
        """超出上限时**保留最近写的部分**（记忆后写的通常更相关）。"""
        if len(content) <= limit:
            return content
        kept = content[-limit:]
        return f"（更早的记忆已省略 {len(content) - limit} 字）\n{kept}"

    async def render(self, query: str) -> str | None:  # noqa: ARG002 (query 预留给 RAG)
        entries = await self._store.entries(self._categories)
        if not entries:
            return None
        sections: list[str] = []
        used = 0
        for cat, content in entries.items():
            room = min(self.per_category_chars, max(0, self.total_chars - used))
            if room <= 0:
                sections.append(f"### {CATEGORIES[cat]}（memories.category='{cat}'）\n"
                                f"（本次未注入：记忆总量已达上限 {self.total_chars} 字，"
                                f"可用 update_memory 精简）")
                continue
            body = self._clip(content, room)
            used += len(body)
            sections.append(
                f"### {CATEGORIES[cat]}（memories.category='{cat}'）\n{body}")
        return "\n\n".join(sections)


# --------------------------------------------------------------------------
# 暴露给 Agent 的记忆读写工具
# --------------------------------------------------------------------------

class UpdateMemoryTool(Tool):
    """让 Agent 自主更新记忆；replace 全量覆盖即删除旧记忆。"""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "update_memory"

    @property
    def display_name(self) -> str:
        return "更新记忆"

    @property
    def description(self) -> str:
        return (
            "更新当前用户的分类记忆（存 memories 表）。category='user' 存用户偏好，"
            "'tool' 存工具使用反馈。mode='replace' 全量覆盖（用于改写/删除旧记忆），"
            "'append' 追加一条。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES), "description": "记忆分类"},
                "content": {"type": "string", "description": "要写入/追加的记忆内容"},
                "mode": {"type": "string", "enum": ["replace", "append"], "description": "默认 replace 全量覆盖"},
            },
            "required": ["category", "content"],
        }

    async def execute(self, category: str, content: str, mode: str = "replace") -> str:
        if category not in CATEGORIES:
            return f"Error: 未知记忆分类 {category!r}，仅支持 {sorted(CATEGORIES)}"
        if mode == "append":
            await self._store.append(category, content)
            return f"已追加到 {category} 记忆（{len(content)} 字符）"
        await self._store.write(category, content)
        return f"已全量覆盖 {category} 记忆（{len(content)} 字符）"


class ReadMemoryTool(Tool):
    """让 Agent 读取当前记忆内容。"""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "read_memory"

    @property
    def display_name(self) -> str:
        return "读取记忆"

    @property
    def description(self) -> str:
        return "读取当前用户的分类记忆（可选 category 指定，缺省返回全部分类的非空记忆）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES), "description": "可选，指定分类"}
            },
            "required": [],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, category: str | None = None) -> str:
        if category and category not in CATEGORIES:
            return f"Error: 未知记忆分类 {category!r}"
        entries = await self._store.entries([category] if category else None)
        if not entries:
            return "（当前没有记忆）"
        return "\n\n".join(f"[{cat}] {CATEGORIES[cat]}\n{content}"
                           for cat, content in entries.items())


def register_memory_tools(registry, store: MemoryStore) -> None:
    registry.register(UpdateMemoryTool(store))
    registry.register(ReadMemoryTool(store))
