"""对话消息与工具调用的数据模型。

采用贴近 OpenAI Chat Completions 的字典格式，方便后续直接替换为真实 LLM SDK。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# role: "system" | "user" | "assistant" | "tool"
Message = dict[str, Any]


@dataclass
class ToolCall:
    """LLM 发起的一次工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        # OpenAI 规范：function.arguments 是 JSON 字符串，而非对象。
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


def system(content: str) -> Message:
    return {"role": "system", "content": content}


def user(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant(
    content: str | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> Message:
    msg: Message = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [tc.to_openai() for tc in tool_calls]
    return msg


def tool_result(tool_call_id: str, name: str, content: Any) -> Message:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": content if isinstance(content, str) else str(content),
    }
