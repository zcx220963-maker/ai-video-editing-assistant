"""LLM 客户端抽象与可运行的 stub 实现。

真实接入时（OpenAI / 兼容网关等）只需实现 LLMClient 协议即可无缝替换，
Agent 循环不感知底层模型差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from .messages import Message, ToolCall


@dataclass
class LLMResponse:
    """一次 LLM 调用的归一化返回。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None    # {prompt_tokens, completion_tokens, total_tokens}

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class StreamChunk:
    """流式输出的一个片段。

    - delta：本次新增的文本增量（工具调用轮通常为 None/空）。
    - tool_calls：流结束时给出的完整工具调用（模型是逐块下发、由客户端聚合）。
    模型本身无状态，把碎片聚合成一条响应是客户端（循环/适配器）的职责。
    """

    delta: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: dict[str, int] | None = None    # 流末块附带（需 stream_options include_usage）


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用接口。

    tools 为 ToolRegistry.get_definitions() 产出的 OpenAI 格式 schema 列表。
    complete_stream 为可选能力：实现者提供后，Agent 循环可在 stream=True 时逐块消费。
    """

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...

    def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


class ScriptedLLM:
    """按预设脚本逐轮返回响应的 stub，用于无需真实密钥即可跑通循环。

    steps 里的每个元素可以是：
      - LLMResponse：直接返回
      - ("tool", name, arguments[, content])：返回一个工具调用（可附带思考文本）
      - ("answer", content)：返回最终答复
    取完脚本后默认返回最后一步，保证循环可收敛。
    """

    def __init__(self, steps: list[Any] | None = None, stream_chunk_size: int = 3) -> None:
        self._steps = list(steps or [])
        self._i = 0
        self.stream_chunk_size = max(1, stream_chunk_size)
        self.calls: list[list[Message]] = []  # 记录每次入参，便于测试断言

    def _next(self) -> LLMResponse:
        idx = min(self._i, len(self._steps) - 1) if self._steps else -1
        self._i += 1
        if idx < 0:
            return LLMResponse(content="(no scripted steps) ")
        step = self._steps[idx]
        if isinstance(step, LLMResponse):
            return step
        kind = step[0]
        if kind == "tool":
            _, name, args = step[0], step[1], step[2]
            content = step[3] if len(step) > 3 else None
            return LLMResponse(
                content=content,
                tool_calls=[ToolCall(id=f"call_{self._i}", name=name, arguments=args)],
            )
        if kind == "answer":
            return LLMResponse(content=step[1])
        raise ValueError(f"unknown scripted step: {step!r}")

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        return self._next()

    async def complete_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ):
        """把脚本响应按 stream_chunk_size 切块下发，末块携带完整 tool_calls。"""
        self.calls.append(messages)
        resp = self._next()
        text = resp.content or ""
        for i in range(0, len(text), self.stream_chunk_size):
            yield StreamChunk(delta=text[i : i + self.stream_chunk_size])
        yield StreamChunk(tool_calls=resp.tool_calls)
