"""SubAgent 设计：主 Agent 通过 SpawnTool 异步派生子 Agent 执行子任务。

对应设计文档「SubAgent 设计」：
  主 Agent → SpawnTool → 异步启动 SubAgent → 独立执行 → 结果回传主 Agent → 主 Agent 继续
- 主 / 子共用同一套 AgentOnceRun（ReAct 循环）。
- 关键约束：SubAgent 不能再派生 SubAgent —— 子 Agent 的工具注册表里【不含 SpawnTool】，
  从结构上杜绝无限递归。
"""

from __future__ import annotations

from typing import Any, Callable

from .agent import AgentConfig, AgentOnceRun
from .context import ContextBuilder
from .hooks import AgentHook, CompositeHook, ToolTraceHook, _current_hook_ctx, _current_hooks
from .llm import LLMClient
from .mq import MessageQueue
from .session import Session
from .tool import Tool, ToolRegistry

# 子 Agent 工具装配器：往传入的 registry 里注册一批工具（不含 spawn）。
ToolRegistrar = Callable[[ToolRegistry], None]

# 子 Agent 的 system 提示词也搬进了提示词库（2026-10-02，`prompts/subagent_system.md`）：
# 这里是**逐字相同的内联回落**，两份漂了由 tests/test_prompt_library.py 守卫。
SUBAGENT_PROMPT_FILE = "subagent_system.md"

_CHILD_SYSTEM_PROMPT_DEFAULT = (
    "你是被主 Agent 派生出来的子 Agent，负责专注完成下面这个子任务并给出简洁结论。"
    "无论任何情况，你必须始终用简体中文思考和回复。"
    "你在调用工具前对步骤的任何说明文字，也必须是简体中文，绝不允许输出英文句子——"
    "即使工具返回的是英文内容也不允许改变语言。"
    "工具失败处理：同一工具连续失败 2 次后，必须停止重试，向用户说明失败原因并询问如何处理，"
    "不要盲目改参数反复重试同一工具。"
    "不要偏离任务、不要再派生子任务。"
)
CHILD_SYSTEM_PROMPT = _CHILD_SYSTEM_PROMPT_DEFAULT


def refresh_subagent_prompt(library: Any) -> None:
    """按提示词库刷新子 Agent 的 system 提示词（就地改模块全局）。"""
    if library is None:
        return
    global CHILD_SYSTEM_PROMPT
    CHILD_SYSTEM_PROMPT = library.text(SUBAGENT_PROMPT_FILE, CHILD_SYSTEM_PROMPT)


class SubAgentRunner:
    """按需用同一 LLM 构建一个【无派生能力】的子 AgentOnceRun 并运行。"""

    def __init__(
        self,
        llm: LLMClient,
        child_tool_registrars: list[ToolRegistrar],
        *,
        max_iterations: int = 6,
        child_system_prompt: str | None = None,
        hooks: AgentHook | None = None,
        trace_mq: MessageQueue | None = None,
    ) -> None:
        self.llm = llm
        self._registrars = list(child_tool_registrars)
        self.config = AgentConfig(max_iterations=max_iterations)
        # None = 取模块全局（启动时 refresh_subagent_prompt 可能已换成磁盘版本）。
        # 形参默认值不能直接写 CHILD_SYSTEM_PROMPT：那会在 import 时定格成内联那份，
        # 磁盘上的版本永远读不到——正是本项目死过一次的那个「写了但没人接」。
        self.child_system_prompt = (child_system_prompt if child_system_prompt is not None
                                    else CHILD_SYSTEM_PROMPT)
        self._trace_hook = ToolTraceHook(trace_mq) if trace_mq else None
        if self._trace_hook:
            base = hooks or CompositeHook([])
            if isinstance(base, CompositeHook):
                base.add(self._trace_hook)
                self.hooks = base
            else:
                self.hooks = CompositeHook([base, self._trace_hook])
        else:
            self.hooks = hooks

    def _build_child_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for register in self._registrars:
            register(registry)
        # 注意：这里【不注册 SpawnTool】，因此子 Agent 无法继续派生。
        return registry

    async def run(self, task: str, *, parent_session_id: str = "") -> str:
        # 子 Agent 的工具调用追踪回投到父会话窗口。
        if self._trace_hook:
            self._trace_hook.session_override = parent_session_id or None
        registry = self._build_child_registry()
        child = AgentOnceRun(
            llm=self.llm,
            registry=registry,
            context_builder=ContextBuilder(self.child_system_prompt),
            hooks=self.hooks or CompositeHook([]),
            config=self.config,
        )
        # 子任务用独立会话上下文，避免污染主会话历史。
        session = Session(user_id="subagent", conversation_id=f"{parent_session_id or 'root'}#sub")
        return await child.run(session, task)


class SpawnTool(Tool):
    """暴露给主 Agent 的派生工具：给定子任务，运行一个 SubAgent 并回传结果。"""

    def __init__(self, runner: SubAgentRunner) -> None:
        self._runner = runner

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def display_name(self) -> str:
        return "派生子助手"

    @property
    def description(self) -> str:
        return (
            "派生一个子 Agent 异步完成一个可独立处理的子任务，并把子 Agent 的最终结论作为工具结果返回。"
            "适合把大任务拆成子步骤（如检索+汇总、分文件处理）。子 Agent 不能再派生孙 Agent。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交给子 Agent 的具体子任务描述"},
                "context": {"type": "string", "description": "子任务所需的必要背景信息（可选）"},
            },
            "required": ["task"],
        }

    # 子 Agent 可能产生副作用（写文件/联网），不并发、需串行观察结果。
    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, task: str, context: str = "", session_id: str = "") -> str:
        prompt = f"{task}\n\n背景：{context}" if context else task
        ctx = _current_hook_ctx.get()
        hooks = _current_hooks.get()
        if ctx is not None and hooks is not None:
            await hooks.on_subagent_spawn(ctx, task)
        result = await self._runner.run(prompt, parent_session_id=session_id)
        if ctx is not None and hooks is not None:
            await hooks.on_subagent_end(ctx, task, result)
        return f"[子 Agent 结论]\n{result}"


def make_spawn_tool(
    llm: LLMClient,
    child_tool_registrars: list[ToolRegistrar],
    *,
    max_iterations: int = 6,
    hooks: AgentHook | None = None,
    trace_mq: MessageQueue | None = None,
) -> SpawnTool:
    """便捷工厂：用子 Agent 可用的工具装配器构造 SpawnTool。"""
    runner = SubAgentRunner(
        llm, child_tool_registrars, max_iterations=max_iterations, hooks=hooks,
        trace_mq=trace_mq,
    )
    return SpawnTool(runner)
