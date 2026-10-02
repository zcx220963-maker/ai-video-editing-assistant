"""用真实 LLM（默认 DeepSeek）跑通 Search→Fetch 联网 ReAct 链路。

密钥只配置一处、全项目共用：页面「设置」里填的那把优先（按用户存 PG）；命令行跑本脚本
时没有页面可填，就设环境变量 `OPENAI_API_KEY`（也认 `DEEPSEEK_API_KEY` /
`SILICONFLOW_API_KEY`，按此顺序取第一把非空的）。
run_live.py 与剪辑链路都通过 get_default_llm() 拿同一个共享客户端。

  export OPENAI_API_KEY=sk-xxxx
  # 可选覆盖（不填即用默认值 https://api.deepseek.com / deepseek-chat）：
  export OPENAI_BASE_URL=https://api.deepseek.com
  export OPENAI_MODEL=deepseek-chat

命令行参数（--api-key / --base-url / --model）仅用于临时覆盖，未传则回落到环境变量。

示例问题会诱导模型走「先搜索、再抓取网页」的链路。运行日志会打印每一轮的
工具调用，便于观察 Agent Loop 行为。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework.agent import Agent, AgentConfig
from agent_framework.context import ContextBuilder, DEFAULT_SYSTEM_PROMPT
from agent_framework.hooks import AgentHook, AgentHookContext, CompositeHook
from agent_framework.llm_openai import get_default_llm
from agent_framework.messages import Message
from agent_framework.session import SessionManager
from agent_framework.storage import DEFAULT_WORKSPACE_ROOT
from agent_framework.tool import ToolRegistry
from agent_framework.tools.file import register_file_tools, session_workspace_root
from agent_framework.tools.web import register_web_tools


class VerboseHook(AgentHook):
    """把每轮迭代与工具调用打印出来，方便观察真实链路。"""

    async def before_iteration(self, context: AgentHookContext) -> None:
        print(f"\n── 第 {context.iteration + 1} 轮（上下文 {len(context.messages)} 条消息）")

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        # 最近若干条 role=tool 消息即本轮工具结果
        recent = [m for m in context.messages if m.get("role") == "tool"][-3:]
        for m in recent:
            content = str(m.get("content", "")).replace("\n", " ")
            print(f"   ⚙ {m.get('name')}: {content[:120]}{'…' if len(content) > 120 else ''}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="DeepSeek 真实 Search→Fetch 链路演示")
    p.add_argument("query", nargs="?", default="帮我搜一下 DeepSeek 最新的模型，并抓取官方文档页面告诉我要点。")
    # 不传则留空（None）→ 由共享工厂回落到环境变量 / 默认值，保证密钥只配置一处。
    p.add_argument("--api-key", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--max-iters", type=int, default=8)
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    try:
        llm = get_default_llm(
            model=args.model, api_key=args.api_key, base_url=args.base_url
        )
    except RuntimeError as exc:
        print(f"❌ {exc}")
        sys.exit(2)

    registry = ToolRegistry()
    register_file_tools(registry, session_workspace_root(DEFAULT_WORKSPACE_ROOT))
    register_web_tools(registry)  # 默认使用 DuckDuckGo 检索后端；如有专用搜索 API 可传 provider=...
    print("已注册工具:", registry.tool_names)
    print(f"模型: {llm.model} @ {llm.base_url}")
    print(f"用户问题: {args.query}")

    agent = Agent(
        llm=llm,
        registry=registry,
        session_manager=SessionManager(),
        context_builder=ContextBuilder(DEFAULT_SYSTEM_PROMPT),
        hooks=CompositeHook([VerboseHook()]),
        config=AgentConfig(max_iterations=args.max_iters),
    )

    answer = await agent.handle("demo_user", "demo_conv", args.query)
    print("\n===== 最终答复 =====")
    print(answer)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中断")
