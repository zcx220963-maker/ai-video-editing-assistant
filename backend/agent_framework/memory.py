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

import re
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

    async def stamps(self, categories: list[str] | None = None) -> dict[str, str]:
        """分类 → 该分类最后写入时间（ISO 串）。注入回执用它说清「最早一条起于哪里」。"""
        cats = categories or list(CATEGORIES)
        got = await self._mem.stamps(self.user_id)
        return {cat: str(got[cat]) for cat in cats if got.get(cat)}


class MemoryContextSource:
    """实现 ContextSource 协议：**按相关度召回**记忆条目注入 System Prompt。

    召回（R9-T39，取代此前的「全量携带 + 超限保尾部」）：

    - ``query``（本轮用户消息 + 计划步骤名，由 ``ContextBuilder.build(recall_terms=)``
      拼好递进来）按词打分，取命中的前 ``top_k`` 条；**命中 0 条的条目不注入**——
      「每轮全量 dump」正是要被换掉的东西。
    - 一条都没命中时**仍然发一段**：只留回执行说明「有记忆但本轮没命中，可 read_memory 读」。
      为什么不干脆整段不发：模型会以为这个用户没有记忆，从此不再写、不再读。
    - 取舍**按条，不截字**：装不下的整条不注入，并在末尾留一行「未注入 N 条」。
      旧行为是「保尾部」——头部被悄悄丢掉，模型看到的是一份**看起来完整**的记忆，
      这是 §5.7 第④条点名要改掉的静默截断。

    为什么必须有上限：记忆是 Agent 自己 append 的，schema 上没有任何长度约束，
    而它每一轮都注进 system，压缩器又不看 system 段。实测 append 200 次就有 30489 字符，
    且只增不减——每轮 prompt 稳定膨胀，最后必然打爆上下文。

    为什么连「怎么用这份记忆」也一起注入（``USAGE_NOTE``）：只有工具清单里真有
    update_memory/read_memory 时才谈得上写，而这两件工具与这段注入同开同关
    （``--no-memory`` 时两者都不存在）。写在系统提示词里就成了永久陈述句，
    关着记忆时反而教模型去调一个不存在的工具。它每次常发，所以按 §5.7 第④条压到 ≤120 B
    （原本 344 字那种「怎么理解计划卡生成行」的教程不在这里讲——那些行本身自带前缀，
    语义写在 §5.4 的规划段与 USAGE_NOTE 之外的地方）。
    """

    name = "memory"
    # 注入预算**按字节**（UTF-8），不是按字：账面与 §5.4.1 的断值都是字节，
    # 两个口径混用就会出现「账面说没超、实际超了」。全 CJK 最坏 3 B/字，
    # 下面这两个数就是「最坏情况下也顶不穿 §5.4.1 那个 2 KB 断值」的上界。
    per_category_bytes = 700
    total_bytes = 1400
    # 相关度召回取前几条（默认 6，与 §5.7 第④条的 k 一致）。
    top_k = 6
    # 账面（§5.7 第①条）自报：这一段是**每轮无条件**发的，读它的是模型手上那两件
    # 记忆工具——注入的正文正是它们读写的对象，USAGE_NOTE 也只在它们存在时才写。
    ledger_kind = "always"
    ledger_referenced_by = ("update_memory", "read_memory")

    USAGE_NOTE = ("用法：偏好→user、教训→tool；新偏好立刻 update_memory，"
                  "重复行 mode=replace 合并。")

    def __init__(self, store: MemoryStore, categories: list[str] | None = None,
                 *, per_category_bytes: int | None = None,
                 total_bytes: int | None = None, top_k: int | None = None) -> None:
        self._store = store
        self._categories = categories
        if per_category_bytes is not None:
            self.per_category_bytes = int(per_category_bytes)
        if total_bytes is not None:
            self.total_bytes = int(total_bytes)
        if top_k is not None:
            self.top_k = int(top_k)

    # ---- 召回 ----

    @staticmethod
    def _split(content: str) -> list[str]:
        """分类正文 → 条目：一行一条（``append`` 就是按换行拼的）。"""
        return [ln.strip() for ln in str(content or "").splitlines() if ln.strip()]

    @classmethod
    def terms(cls, query: str) -> set[str]:
        """查询词 → 判据项：ASCII 词（小写，≥2 字）+ CJK 连续段的二元组。

        用二元组而不是分词：没有词典、没有依赖，中文里「文案」这种词本来就会
        跨词边界（「写文案」「文案风格」），二元组对这类匹配反而更稳。
        单字 CJK 段（如「跑」）本身就是一个词，直接收。
        """
        text = str(query or "")
        out = {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(text)}
        for run in _CJK_RUN_RE.findall(text):
            if len(run) == 1:
                out.add(run)
            else:
                out.update(run[i:i + 2] for i in range(len(run) - 1))
        return out

    @classmethod
    def _score(cls, entry: str, terms: set[str]) -> int:
        """命中的**去重**判据项个数。

        去重而不是数出现次数：一条记忆把「文案」写十遍，不表示它比只写一遍的更相关；
        重复计数会让啰嗦的条目挤掉真正切题的条目。
        """
        if not terms:
            return 0
        low = entry.lower()
        words = {m.group(0).lower() for m in _ASCII_WORD_RE.finditer(entry)}
        return sum(1 for t in terms if (t in words if t.isascii() else t in low))

    async def render(self, query: str, *, user_id: str | None = None) -> str | None:  # noqa: ARG002 (user_id 由 MemoryStore 自身绑定)
        entries = await self._store.entries(self._categories)
        if not entries:
            return None

        terms = self.terms(query)
        # (分类, 条目序号, 文本, 得分)：序号即写入先后（append 往后加）。
        scored: list[tuple[str, int, str, int]] = []
        for cat, content in entries.items():
            for idx, line in enumerate(self._split(content)):
                scored.append((cat, idx, line, self._score(line, terms)))

        hits = [row for row in scored if row[3] > 0]
        cat_order = {cat: i for i, cat in enumerate(entries)}
        if hits:
            # 同分时短的先上：同样切题，短的那条占的预算少，能让更多切题的条目挤进来。
            hits.sort(key=lambda r: (-r[3], len(r[2]), r[0], -r[1]))
            picked = hits[:self.top_k]
            fallback = False
        else:
            # 一条都没命中 → 退回**每类最近 top_k 条**（不是「什么都不给」）。
            # 为什么必须有这一腿：服务端自动生成的那些行（「计划卡：跳过第 N 步…」
            # 「成片退回重做…」）记的是用户在上一版计划上的**动作**，它们跟这一轮
            # 用户新提的诉求往往不同词——纯相关度召回会把这些「状态」判成无关而丢掉，
            # 于是 USAGE_NOTE 里「被跳过的那步默认不再排」就成了空话。
            # 按类取最近（而不是全局取最近）：两类独立，工具教训不会被用户偏好挤没。
            picked = [row for cat in entries
                      for row in [r for r in scored if r[0] == cat][-self.top_k:]]
            fallback = True

        # 预算内挑（选谁按相关度、摆谁按时间顺序：记忆读起来该像一本流水账）
        kept: list[tuple[str, int, str, int]] = []
        used_per: dict[str, int] = {}
        used = 0
        for cat, idx, line, score in picked:
            n = len(line.encode("utf-8"))
            if used_per.get(cat, 0) + n > self.per_category_bytes:
                continue
            if used + n > self.total_bytes:
                continue
            kept.append((cat, idx, line, score))
            used_per[cat] = used_per.get(cat, 0) + n
            used += n
        kept.sort(key=lambda r: (cat_order.get(r[0], 0), r[1]))

        injected: dict[str, list[str]] = {}
        for cat, _idx, line, _score in kept:
            injected.setdefault(cat, []).append(line)

        sections: list[str] = []
        for cat in entries:
            lines = injected.get(cat)
            if not lines:
                continue
            sections.append(f"### {CATEGORIES[cat]}（memories.category='{cat}'）\n"
                            + "\n".join(lines))

        dropped = len(scored) - len(kept)
        if not sections:
            # 连最近几条都装不下：**不能整段消失**，否则模型以为这个用户没有记忆。
            sections.append(f"### 记忆（共 {len(scored)} 条，本轮未注入）")
        sections.append(await self._receipt(len(scored), len(kept), dropped,
                                           recent=fallback))
        return "\n\n".join(sections) + "\n\n" + self.USAGE_NOTE

    async def _receipt(self, total: int, kept: int, dropped: int, *,
                       recent: bool = False) -> str:
        """未注入回执。**只说真话**：有多少、为什么、最早的那条起于哪里。

        「最早一条 <时间戳>」这条要求（§5.7 第④条）在现有 schema 下只能给到分类粒度：
        ``memories`` 表是 (user_id, category) 一行一块文本，**没有逐条时间戳**，
        而 T39 明令只动读侧、不许给写侧加列。所以回执给的是「最早一条在该分类正文开头，
        该分类最后更新于 <ISO>」——时间是从表里读的真值，不是编的（见 §9-Q102）。
        """
        why = "关键词未命中，已改注入最近几条" if recent else "关键词未命中或超预算"
        if dropped <= 0:
            return (f"（本轮注入 {kept} 条，共 {total} 条）" if not recent else
                    f"（本轮关键词未命中，改注入最近 {kept} 条，共 {total} 条）")
        stamp = ""
        getter = getattr(self._store, "stamps", None)
        if callable(getter):
            try:
                got = await getter()
            except Exception:  # noqa: BLE001 - 回执是增益，读不到时间戳不影响注入
                got = {}
            for cat in self._categories or list(CATEGORIES):
                if got.get(cat):
                    stamp = f"；最早一条在 `{cat}` 正文开头，该分类最后更新于 {got[cat]}"
                    break
        return (f"（本轮注入 {kept} 条、未注入 {dropped} 条：{why}"
                f"{stamp}；全量见 read_memory）")


_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


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
