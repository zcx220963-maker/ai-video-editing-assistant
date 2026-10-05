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
from typing import Any

from .messages import Message, assistant, user


@dataclass
class Session:
    user_id: str
    conversation_id: str
    messages: list[Message] = field(default_factory=list)
    qa_log: list[dict[str, Any]] = field(default_factory=list)

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

    def __init__(self, storage: Any | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        if storage is None:
            # 无参构造 = 会话只在本进程活（demo / 单测）：注入内存替身，历史表空着
            from .storage import build_storage
            storage = build_storage("memory")
        self.storage = storage

    async def get_or_create(self, user_id: str, conversation_id: str) -> Session:
        key = f"{user_id}:{conversation_id}"
        session = self._sessions.get(key)
        if session is None:
            session = Session(user_id=user_id, conversation_id=conversation_id)
            await self._load_history(session)
            self._sessions[key] = session
        return session

    async def _load_history(self, session: Session) -> None:
        """PG → Session：把既有对话行读回为 QA 历史与对话消息（按 seq 顺序）。"""
        rows = await self.storage.messages.history(session.user_id, session.conversation_id)
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
