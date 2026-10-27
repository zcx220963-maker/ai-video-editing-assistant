"""SubAgent 设计：主 Agent 通过 SpawnTool 派生一个子 Agent 执行子任务。

对应设计文档「SubAgent 设计」：
  主 Agent → SpawnTool → 启动 SubAgent → 独立执行 → 结果回传主 Agent → 主 Agent 继续
- 主 / 子共用同一套 AgentOnceRun（ReAct 循环）。
- 关键约束：SubAgent 不能再派生 SubAgent —— 子 Agent 的工具注册表里【不含 SpawnTool】，
  从结构上杜绝无限递归。这条今天由 ``tests/test_team_wiring.py`` 的断言守着
  （原来只有 ``_build_child_registry`` 上面那句注释在守，注释不构成判据）。
- **默认异步**（设计 §15 / R7）：``SpawnTool.execute`` 默认立刻返回 ``task_id``，
  子 Agent 在后台跑，结论通过 Message Center 回投主 Agent 的收件箱；
  短任务要「一次调用拿结论」时显式传 ``wait=true``。
  真正要「派出去就不等」的常驻实例，走 ``subagent_manager.py`` 的 WORK/IDLE 腿。
"""

from __future__ import annotations

from typing import Any, Callable

import asyncio
import logging
import uuid

from .agent import AgentConfig, AgentOnceRun
from .context import ContextBuilder
from .hooks import AgentHook, CompositeHook, ToolTraceHook, _current_hook_ctx, _current_hooks
from .llm import LLMClient
from .mq import MessageQueue
from .prompts import read_prompts_file
from .session import Session
from .tool import Tool, ToolRegistry

logger = logging.getLogger("agent.subagent")

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
        max_iterations: int = 10,
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
        # 后台任务必须留强引用：只 create_task 不持有，事件循环可能把它回收掉，
        # 表现为「派发了但结论永远不回投」。
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        return "spawn_subagent"

    @property
    def display_name(self) -> str:
        return "派生子助手"

    @property
    def description(self) -> str:
        return (
            "派生一个子 Agent 去完成一个可独立处理的子任务。**默认异步**：立刻返回 task_id，"
            "子 Agent 在后台跑，结论稍后以消息形式回投；你可以在等待期间继续做别的步骤。"
            "只有任务很短、必须拿到结论才能继续时，传 wait=true 让它同步返回结论。"
            # 能力边界写在**前面**：真机里模型据此派过「执行 build_spec.py」这类必败的活。
            "**能力边界**：它没有 shell，不能执行任何命令；也拿不到只有主 Agent 才有的"
            "节点工具。适合 ≤5 个文件的小任务；上限 10 轮，超了会带着过程摘要停下"
            "（中间结果不会自动交付）。"
            "注意：本工具**一次只跑一个**——同一条回复里写多个调用**不会**让它们同时跑，"
            "子 Agent 可能写文件或联网，本来就得串行观察。"
            "需要真正并行的后台子 Agent，用工具面里的 start_subagent"
            "（那条腿是常驻实例、各自在后台跑）。"
            "它不会派生孙 Agent（工具表里没有派生口）。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交给子 Agent 的具体子任务描述"},
                "context": {"type": "string", "description": "子任务所需的必要背景信息（可选）"},
                "wait": {
                    "type": "boolean",
                    "description": "false=立刻返回 task_id、结果稍后回投（默认）；"
                                   "true=等子 Agent 跑完直接给结论（只用于短任务）",
                },
            },
            "required": ["task"],
        }

    # 子 Agent 可能产生副作用（写文件/联网），不并发、需串行观察结果。
    @property
    def read_only(self) -> bool:
        return False

    async def execute(
        self, task: str, context: str = "", session_id: str = "",
        wait: bool = False,
    ) -> str:
        """派生子 Agent。默认异步：立刻返回 `task_id`，结果回投收件箱（设计 §15 / R7）。

        设计要的是「异步启动 SubAgent，结果回传主 Agent」；同步等待会让主 Agent 的
        一次工具调用挂十几分钟。短任务确需结论时显式 ``wait=True``（保留旧语义）。
        """
        prompt = f"{task}\n\n背景：{context}" if context else task
        ctx = _current_hook_ctx.get()
        hooks = _current_hooks.get()
        if ctx is not None and hooks is not None:
            await hooks.on_subagent_spawn(ctx, task)

        if not wait:
            task_id = f"sub-{uuid.uuid4().hex[:8]}"

            async def _bg() -> None:
                try:
                    out = await self._runner.run(prompt, parent_session_id=session_id)
                except Exception as exc:  # noqa: BLE001  异步分支不能把异常吞成静默
                    out = f"Error: 子 Agent 失败：{type(exc).__name__}: {exc}"
                if ctx is not None and hooks is not None:
                    try:
                        await hooks.on_subagent_end(ctx, task, out)
                    except Exception:  # noqa: BLE001  钩子失败不该影响回投
                        logger.exception("子 Agent 收尾钩子失败：%s", task_id)
                await self._notify(task_id, task, out)

            bg = asyncio.create_task(_bg())
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
            return (f"[已派发] task_id={task_id}。子 Agent 在后台跑，"
                    "结果会以消息形式回投；不要立刻重复派同一个任务。")

        result = await self._runner.run(prompt, parent_session_id=session_id)
        if ctx is not None and hooks is not None:
            await hooks.on_subagent_end(ctx, task, result)
        return f"[子 Agent 结论]\n{result}"

    async def _notify(self, task_id: str, task: str, result: str) -> None:
        """异步结果回投：有 MessageCenter 就投，没有就只记日志。"""
        center = getattr(self._runner, "message_center", None)
        text = f"[子 Agent 完成] task_id={task_id}\n任务：{task}\n结论：{result}"
        if center is None:
            logger.info(text)
            return
        try:
            await center.send("subagent", "main_agent", text)
        except Exception:  # noqa: BLE001  回投失败不该让后台任务崩掉
            logger.exception("子 Agent 结果回投失败：%s", task_id)


def make_spawn_tool(
    llm: LLMClient,
    child_tool_registrars: list[ToolRegistrar],
    *,
    max_iterations: int = 10,
    hooks: AgentHook | None = None,
    trace_mq: MessageQueue | None = None,
) -> SpawnTool:
    """便捷工厂：用子 Agent 可用的工具装配器构造 SpawnTool。"""
    runner = SubAgentRunner(
        llm, child_tool_registrars, max_iterations=max_iterations, hooks=hooks,
        trace_mq=trace_mq,
    )
    return SpawnTool(runner)
