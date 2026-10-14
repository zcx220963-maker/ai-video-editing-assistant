"""SubAgent 设计：主 Agent 通过 SpawnTool 派生一个子 Agent 执行子任务。

对应设计文档「SubAgent 设计」：
  主 Agent → SpawnTool → 启动 SubAgent → 独立执行 → 结果回传主 Agent → 主 Agent 继续
- 主 / 子共用同一套 AgentOnceRun（ReAct 循环）。
- 关键约束：SubAgent 不能再派生 SubAgent —— 子 Agent 的工具注册表里【不含 SpawnTool】，
  从结构上杜绝无限递归。这条今天由 ``tests/test_team_wiring.py`` 的断言守着
  （原来只有 ``_build_child_registry`` 上面那句注释在守，注释不构成判据）。
- **本工具是同步等待**（Q65）：``SpawnTool.execute`` 直接 await 子 Agent 跑完，
  子结论作为本次工具回执进入本轮消息序列，父计划在它回来之前不许收尾。
  骨架说「异步派生」，本仓在这一点上**不同但更强**（回调语义因此成立）；
  真正要「派出去就不等」的活，走 ``subagent_manager.py`` 的常驻 WORK/IDLE 腿。
"""

from __future__ import annotations

from typing import Any, Callable

from .agent import AgentConfig, AgentOnceRun
from .context import ContextBuilder
from .hooks import AgentHook, CompositeHook, ToolTraceHook, _current_hook_ctx, _current_hooks
from .llm import LLMClient
from .mq import MessageQueue
from .prompts import read_prompts_file
from .session import Session
from .tool import Tool, ToolRegistry

# 子 Agent 工具装配器：往传入的 registry 里注册一批工具（不含 spawn）。
ToolRegistrar = Callable[[ToolRegistry], None]

# 子 Agent 的 system 提示词在提示词库里（2026-10-02 起在盘上，2026-10-14 删掉内联副本）。
# 名字保留为「默认值」，但它的值来自磁盘、**不是**内联文本——子 Agent 提示词同样不该
# 在代码里留第二份（同一段文案改一处漏一处）。
SUBAGENT_PROMPT_FILE = "subagent_system.md"

_CHILD_SYSTEM_PROMPT_DEFAULT = read_prompts_file(SUBAGENT_PROMPT_FILE)
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
        # 描述必须与**实现**一致（T49/Q65）。这里此前写的是「要多路子任务同时跑就在
        # 同一次回复里一起调用（系统会并发执行）」——**这句不成立**：SpawnTool 声明
        # read_only=False 且没有读写集，`concurrency_safe` 恒为 False，
        # `tool.plan_batches` 会给每个 spawn 各开一批，实际是**串行**。
        # 描述撒谎的代价不是慢，是**计划错**：模型据此认为「同时发几个就能同时跑」，
        # 于是把「先等 A 的结果再决定怎么发 B」这种本当串行的活也一股脑发出来。
        # 所以这里如实写「一次只跑一个」，并把真正的并行腿指出来（常驻子 Agent 那条，
        # 它才是真并发的）。
        return (
            "派生一个子 Agent 去完成一个可独立处理的子任务，并**同步等待它结束**，"
            "把子 Agent 的最终结论作为本次工具调用的结果返回。"
            "适合把大任务拆成子步骤（如检索+汇总、分文件处理）。"
            "注意：本工具**一次只跑一个**——同一条回复里写多个调用**不会**让它们同时跑，"
            "子 Agent 可能写文件或联网，本来就得串行观察。"
            "需要真正并行的后台子 Agent，用工具面里的 start_subagent"
            "（那条腿是派出去就返回、各自在后台跑）。"
            "子 Agent 不能再派生孙 Agent。"
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
