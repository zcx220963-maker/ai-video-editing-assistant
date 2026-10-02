"""端到端跑通 Agent Loop 骨架的最小示例。

运行：  python demo.py
无需任何真实密钥——使用 ScriptedLLM 预置「先调用 echo 工具，再给最终答复」。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # Windows GBK 控制台兜底
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import (
    Agent,
    AgentConfig,
    CompositeHook,
    EchoTool,
    SessionManager,
    ToolRegistry,
)
from agent_framework.hooks import AgentHook, AgentHookContext
from agent_framework.llm import ScriptedLLM


class TraceHook(AgentHook):
    """演示 Hook 接缝：打印每轮迭代与工具执行后的状态。"""

    async def before_iteration(self, context: AgentHookContext) -> None:
        print(f"  [hook] 第 {context.iteration} 轮，上下文 {len(context.messages)} 条消息")

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        print(f"  [hook] 工具执行完成，当前 {len(context.messages)} 条消息")

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return (content or "").strip() + " ✨"


async def main() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    print("已注册工具:", registry.tool_names)

    # LLM 剧本：第 1 轮调 echo，第 2 轮给最终答复
    llm = ScriptedLLM(
        steps=[
            ("tool", "echo", {"text": "你好，创作助手"}),
            ("answer", "已为你回显，随时可以开始创作。"),
        ]
    )

    agent = Agent(
        llm=llm,
        registry=registry,
        session_manager=SessionManager(),
        hooks=CompositeHook([TraceHook()]),
        config=AgentConfig(max_iterations=5),
    )

    answer = await agent.handle("user_a", "chat_1", "帮我测试回显功能")
    print("\n最终答复:", answer)

    session = agent.session_manager.get("user_a:chat_1")
    print("会话历史条数:", len(session.messages) if session else 0)


if __name__ == "__main__":
    asyncio.run(main())
