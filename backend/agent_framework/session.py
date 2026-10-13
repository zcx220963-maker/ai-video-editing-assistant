"""Session 与会话历史管理。

这里解决的是设计文档中「这个会话之前聊了什么」的问题（区别于 MQ 用 session_id
做路由）。Session 唯一键 = user_id + conversation_id，持有该会话的历史消息。

对应 Context 构建流程图的左侧：会话历史的真相是 PG ``messages`` 表（spec §3.3，
取代 ``.runtime/sessions/{user}/{conv}.jsonl``）。表里的 ``qa`` 列原样存文档的 QA
结构 ``{"parts": [{"type": "think"|"tool call"|"answer", ...}]}``，装载时按 ``seq``
读回，与前面的 user 行配成一条 QA —— 进程重启、换实例都照样能接上这段对话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .messages import Message, assistant, user
from .summary_store import SummaryStore

#: 历史窗口默认条数（R10-T48）。口径按附录 B：取 §8.1 十二支夹具里最长会话的 P95。
#: **十二支夹具还没跑**，所以这个数是起步值、不是标定值——这一点如实写在这里：
#: 跑完按真 P95 替换，并同批改 §5.4 的 `<history>` 行。
HISTORY_WINDOW = 40
#: 历史窗口的**字节**线（1 B ≤ 1 token，与 ``DEFAULT_MAX_CONTEXT_TOKENS=65536``
#: 同一把尺）。**它是参考线，不是断值**：越过它该发生的事是压缩链瘦身，
#: **不是** ㉓ 拒发这次请求。所以它**不进** ``SECTION_BUDGET_BYTES``，只在
#: ``ledger["history"]["compress_at"]`` 里露个面（context.py 引这个常量，
#: 不另抄一个数——两处各写一遍就是下一个「两份真相」）。把压缩该管的事写成「不许发」，
#: 会让长对话在压缩来得及之前就硬失败。
HISTORY_WINDOW_BYTES = 65536


def render_history(messages: Sequence[Message]) -> str:
    """会话历史 → 账面用的一段文本（R10-T48 的 ``<history>`` 块）。

    **只用于记账**：真发出去的是 ``messages`` 本身（LLM API 要的是多轮结构，
    不是一段拼好的文本）。这里按 ``<role>…</role>`` 包一层再拼，口径与装配出口
    别的块一致（标签也算字节），所以账面上的 ``<history>`` 与 §5.4 别的行可以
    放在一起比。
    """
    return "\n\n".join(
        f"<{m.get('role') or '?'}>\n{str(m.get('content') or '')}\n</{m.get('role') or '?'}>"
        for m in messages)


@dataclass
class Session:
    user_id: str
    conversation_id: str
    messages: list[Message] = field(default_factory=list)
    qa_log: list[dict[str, Any]] = field(default_factory=list)
    # ---- R10-T48：历史窗口的回执（装载时填，装配方读它记 `<history>` 的账）----
    history_window: int = 0        # 这次真装进内存的历史条数
    history_dropped: int = 0       # 被窗口截掉（换成前缀摘要）的条数；0 = 没截
    history_prefix: str = ""       # 截掉那一段的摘要正文；没截就是空串

    @property
    def session_id(self) -> str:
        return f"{self.user_id}:{self.conversation_id}"

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def extend(self, messages: list[Message]) -> None:
        self.messages.extend(messages)

    def add_qa(self, question: str, parts: list[dict[str, Any]]) -> None:
        """记录一条结构化 QA 历史（内存镜像）；入库由 Agent 写 messages 表负责。"""
        self.qa_log.append({"question": question, "answer": parts})


class SessionManager:
    """按 session_id 维护活跃 Session；历史从 ``messages`` 表回填，不写任何本地文件。"""

    def __init__(self, storage: Any | None = None, *,
                 history_window: int = HISTORY_WINDOW,
                 summaries: SummaryStore | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        if storage is None:
            # 无参构造 = 会话只在本进程活（demo / 单测）：注入内存替身，历史表空着
            from .storage import build_storage
            storage = build_storage("memory")
        self.storage = storage
        # 0 或负数 = 不设窗口，全量拉（今天是这个行为；想退回旧口径就传 0）。
        self.history_window = int(history_window)
        # 摘要的存取口（T47）。None 时现造一个——它只是 ``checkpoint_entries``
        # 上面的一层薄封装，没有可缓存的状态，所以「不传」与「传」行为一致。
        self.summaries = summaries or SummaryStore(storage)

    async def get_or_create(self, user_id: str, conversation_id: str) -> Session:
        key = f"{user_id}:{conversation_id}"
        session = self._sessions.get(key)
        if session is None:
            session = Session(user_id=user_id, conversation_id=conversation_id)
            await self._load_history(session)
            self._sessions[key] = session
        return session

    async def _load_history(self, session: Session) -> None:
        """PG → Session：把既有对话行读回为 QA 历史与对话消息（按 seq 顺序）。

        R10-T48：**只拉尾部一段**。原先无 LIMIT 全量拉，长会话下这段既进 token 账
        也进 ㉓ 的字节账，却从未在任何预算表里出现过——「没人记账」不等于「没花钱」。
        被截掉的头部换成**一条持久化的前缀摘要**（T47 落的行），并在正文里写明
        截了多少条：丢头部这件事必须有回执，不许静默（同 P24 的纪律）。
        """
        window = self.history_window
        rows = await self.storage.messages.history(
            session.user_id, session.conversation_id,
            limit=(window + 1) if window > 0 else None, tail=window > 0)
        # 多拉一条只为了判断「到底截没截」：拿到 window+1 条就说明后面还有更早的。
        if window > 0 and len(rows) > window:
            # 丢掉多少条不必再跑一次 count：seq 在这个会话里从 1 起连续自增
            # （``append`` 取 max+1），所以最旧那一条的 seq **就是它前面有多少条 + 1**；
            # 而保下来的是这 window+1 条里最后 window 条，**它自己也在丢掉之列**，
            # 两头一抵消，丢掉的条数正好等于它的 seq。
            # （写 `seq - 1` 会少报一条——最旧那条自己没算进去。测试钉的就是这个。）
            session.history_dropped = max(0, int(rows[0]["seq"]))
            rows = rows[-window:]
        session.history_window = len(rows)
        if session.history_dropped:
            session.history_prefix = await self.summaries.latest(session.session_id) or ""
            if session.history_prefix:
                session.add({"role": "system", "content": (
                    f"<history_summary>\n更早的 {session.history_dropped} 条对话已压缩为本段摘要"
                    f"（原文仍可用 read_memory / 检索类工具按需取回）：\n"
                    f"{session.history_prefix}\n</history_summary>")})
        question = ""
        for row in rows:
            if row["role"] == "user":
                question = row["content"]
                session.add(user(question))
                continue
            parts = list((row.get("qa") or {}).get("parts") or [])
            if not parts:
                parts = [{"type": "answer", "content": row["content"]}]
            session.qa_log.append({"question": question, "answer": parts})
            answer = next((p.get("content", "") for p in parts
                           if p.get("type") == "answer"), row["content"])
            session.add(assistant(answer))

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def ids(self) -> list[str]:
        """当前活跃会话的 session_id 列表（观测 / 接口用）。"""
        return list(self._sessions.keys())

    def __len__(self) -> int:
        return len(self._sessions)
