"""Message Center：PG 收件箱上的 Agent 间通信（A2A）。

对应设计文档「Agent Team —— Message Center」流程图（收件箱三级作用域
``用户 → 会话 → Box``）：落到 ``inbox_messages`` 表的 ``(user_id, conv_id, agent)``
三元组（spec §3.8），不再写 ``{user}/{conv}/{agent}.jsonl``。两个关键动作：

  * send：插一行 —— ``type`` / ``sender`` 是路由列，消息正文原样进 ``content`` jsonb。
  * read_inbox：读出未消费的行并打上 ``consumed_at``。文档的「消费即删」改成
    「标记已读」：一条消息仍只被消费一次，但历史可重放、可审计。

作用域取自当前执行身份（contextvar，见 identity.py）：同一会话里主 Agent 与常驻
子 Agent 共用一个消息空间，不同会话天然隔离；离线直调（未进入任何 run）退回缺省作用域。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .identity import current_identity_or
from .tool import Tool


class MessageCenter:
    """一条 A2A 总线：会话作用域内每个 Agent 一个收件箱（``agent`` 列）。"""

    def __init__(self, storage: Any, *, user_id: str = "default",
                 conversation_id: str = "default") -> None:
        self._inbox = storage.inbox
        self.default_user_id = user_id
        self.default_conversation_id = conversation_id

    def _scope(self) -> tuple[str, str]:
        ident = current_identity_or(self.default_user_id, self.default_conversation_id)
        return ident.user_id, ident.conversation_id

    async def send(self, sender: str, to: str, content: str,
                   msg_type: str = "message",
                   dedup_key: str | None = None) -> str:
        """往收件人 ``to`` 的收件箱插一条消息，返回确认串供 LLM 感知发送成功。

        dedup_key（可选）是投递幂等键：同一收件箱里已有未消费的同键消息时不再重复
        投递（崩溃恢复/重试重放同一条指令不会翻倍）。
        """
        user_id, conv_id = self._scope()
        row = await self._inbox.send(user_id, conv_id, to, {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }, type_=msg_type, sender=sender, dedup_key=dedup_key)
        suffix = "（同键未消费消息已存在，未重复投递）" \
            if dedup_key and row.get("_deduplicated") else ""
        return f"Sent {msg_type} to {to}{suffix}"

    async def read_inbox(self, name: str) -> list[dict[str, Any]]:
        """读出并标记已读（原子领取：一条消息只被一个消费者拿到），返回消息正文列表。

        每条消息额外带 ``_id``（收件箱行号），消费方可据此做本地去重/审计对账。
        """
        user_id, conv_id = self._scope()
        rows = await self._inbox.read(user_id, conv_id, name)
        out: list[dict[str, Any]] = []
        for row in rows:
            content = row.get("content")
            item = dict(content) if isinstance(content, dict) else {"body": content}
            item["_id"] = row.get("id")
            out.append(item)
        return out

    async def peek(self, name: str) -> list[dict[str, Any]]:
        """只读不标记（诊断/测试用）。"""
        user_id, conv_id = self._scope()
        return [row["content"] for row in await self._inbox.peek(user_id, conv_id, name)]

    async def agents(self) -> list[str]:
        """本作用域内有收件箱的 Agent 名单。"""
        user_id, conv_id = self._scope()
        return await self._inbox.agents(user_id, conv_id)


class SendMessageTool(Tool):
    """把 Message Center 暴露成 Agent 工具：让 LLM 主动给队友/主 Agent 发消息。"""

    def __init__(self, center: MessageCenter, sender: str) -> None:
        self._center = center
        self._sender = sender

    @property
    def name(self) -> str:
        return "send_message"

    @property
    def display_name(self) -> str:
        return "发送消息"

    @property
    def description(self) -> str:
        return (
            "给另一个 Agent（如 main_agent 或某个 sub_agent）发送一条消息，写入其收件箱，"
            "用于 Agent 间协作通信（A2A）。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人 Agent 名字"},
                "content": {"type": "string", "description": "消息正文"},
                "type": {"type": "string", "description": "消息类型，默认 message"},
                "dedup_key": {
                    "type": "string",
                    "description": "可选幂等键：同一收件箱已有未消费的同键消息时不重复投递",
                },
            },
            "required": ["to", "content"],
        }

    async def execute(self, to: str, content: str, type: str = "message",  # noqa: A002
                      dedup_key: str | None = None) -> str:
        return await self._center.send(self._sender, to, content, type,
                                       dedup_key=dedup_key)


class ReadInboxTool(Tool):
    """读取并标记已读自己的收件箱（一条消息只消费一次）。"""

    def __init__(self, center: MessageCenter, name: str) -> None:
        self._center = center
        self._name = name

    @property
    def name(self) -> str:
        return "read_inbox"

    @property
    def display_name(self) -> str:
        return "读收件箱"

    @property
    def description(self) -> str:
        return "读取自己收件箱里的全部新消息；读取后会被标记已读（一条消息只消费一次）。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    # read_inbox 会改写行状态（打 consumed_at），是有副作用的动作，默认串行执行。
    @property
    def read_only(self) -> bool:
        return False

    async def execute(self) -> str:
        msgs = await self._center.read_inbox(self._name)
        return json.dumps(msgs, ensure_ascii=False)


def register_message_tools(registry, center: MessageCenter, name: str) -> None:
    """把 send_message / read_inbox 两个工具注册进 registry（以 name 为当前 Agent 身份）。"""
    registry.register(SendMessageTool(center, name))
    registry.register(ReadInboxTool(center, name))
