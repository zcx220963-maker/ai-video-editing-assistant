"""Agent Team 工具装配：把 Message Center / Task Manager / SubAgent Manager 接进主 Agent。

对应设计文档「Agent Team」一节的协作三通道 + 常驻子 Agent 池：

    send_message / read_inbox   ← Message Center（A2A 收件箱，读后标记已读）
    plan_editing_team           ← 剪辑 DAG → Task Manager 可认领任务（拓扑序）
    list_tasks / claim_task / complete_task ← 任务看板
    start_subagent              ← 常驻子 Agent：裁剪工具集 + WORK/IDLE/SHUTDOWN 状态机，
                                  后台 asyncio 任务自驱认领任务或被收件箱消息唤醒
    team_status                 ← 子 Agent 状态 + 任务状态总览

状态全部在 PG（``inbox_messages`` / ``tasks`` + ``task_edges`` / ``subagents``），
作用域 = 当前执行身份的 ``{user}:{conv}``（见 identity.py）：后台子 Agent 任务的
上下文是从 ``start_subagent`` 复制来的，所以它天然继承创建者所在的会话作用域，
不同会话的队友彼此看不见。供 run_server 默认装配；关闭服务时通过 Team.shutdown()
取消全部常驻子 Agent 任务并把它们的状态写回 shutdown。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from .agent import AgentConfig, AgentOnceRun
from .context import ContextBuilder
from .editing_agent import plan_team_from_dag
from .hooks import AgentHook
from .identity import Identity, current_identity_or, use_identity
from .message_center import MessageCenter, register_message_tools
from .session import SessionManager
from .subagent_manager import (
    SHUTDOWN,
    ManagedSubAgent,
    SubAgentManager,
    build_subagent_registry,
)
from .task_manager import TaskManager
from .tool import Tool, ToolRegistry

MAIN_AGENT_NAME = "main_agent"

SUBAGENT_SYSTEM_PROMPT = (
    "你是 Agent Team 中的常驻子 Agent。无论任何情况，你必须始终用简体中文思考和回复。"
    "你在调用工具前对步骤的任何说明文字，也必须是简体中文，绝不允许输出英文句子——"
    "即使工具返回的是英文内容也不允许改变语言。"
    "工具失败处理：同一工具连续失败 2 次后，必须停止重试，向用户说明失败原因并询问如何处理，"
    "不要盲目改参数反复重试同一工具。"
    "依据任务指令使用可用的工具完成任务，完成后给出简洁结论；"
    "若收件箱或,任务板有新工作则继续。"
)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class FunctionTool(Tool):
    """把普通函数包装成 Tool，减少团队工具样板。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn,
        *,
        read_only: bool = False,
        display_name: str = "",
    ) -> None:
        self._name = name
        self._display = display_name
        self._description = description
        self._parameters = parameters
        self._fn = fn
        self._read_only = read_only

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    @property
    def read_only(self) -> bool:
        return self._read_only

    async def execute(self, **kwargs: Any) -> Any:
        return await self._fn(**kwargs)


class Team:
    """团队组件集合 + 生命周期（常驻子 Agent 的后台任务登记与取消）。"""

    def __init__(
        self,
        *,
        message_center: MessageCenter,
        task_manager: TaskManager,
        subagent_manager: SubAgentManager,
        llm: Any,
        parent_registry: ToolRegistry,
        session_manager: SessionManager,
        node_deps: Callable[[], dict[str, list[str]]],
        hooks: AgentHook | None = None,
        max_iterations: int = 8,
        idle_window_sec: float | None = None,
        idle_poll_sec: float | None = None,
    ) -> None:
        self.message_center = message_center
        self.task_manager = task_manager
        self.subagent_manager = subagent_manager
        self._llm = llm
        self._parent_registry = parent_registry
        self._session_manager = session_manager
        # DAG 只在剪辑服务端有一份；这里拿的是**现取**的依赖映射（契约接入前为空）。
        self._node_deps = node_deps
        self._hooks = hooks
        self._max_iterations = max_iterations
        # 子 Agent 干完一轮后在内存里待多久才认输（None = 用模块默认）。
        # 做成可注入是因为它有两种正当取值：生产要「常驻复用」（别每任务重建），
        # 而生命周期测试要一个短窗口好在秒级内看到 WORK→IDLE→SHUTDOWN 全流程。
        self._idle_window_sec = idle_window_sec
        self._idle_poll_sec = idle_poll_sec
        self.subagents: dict[str, ManagedSubAgent] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._idents: dict[str, Identity] = {}
        self._status_writes: set[asyncio.Task] = set()

    # ---- 工具实现 ----

    async def plan_editing_team(self) -> str:
        required = self._node_deps()
        if not required:
            return _json({"created_tasks": [], "error":
                          "剪辑 DAG 尚未接入（Storyline 未连通），无从按流程拆解任务。"})
        ids = await plan_team_from_dag(required, self.task_manager)
        return _json({"created_tasks": ids, "board": await self.task_manager.render()})

    async def list_tasks(self) -> str:
        return _json(await self.task_manager.list_all())

    async def claim_task(self, task_id: int, owner: str) -> str:
        return _json(await self.task_manager.claim(int(task_id), owner))

    async def complete_task(self, task_id: int, owner: str = MAIN_AGENT_NAME) -> str:
        return _json(await self.task_manager.complete(int(task_id), owner))

    async def start_subagent(
        self, name: str, prompt: str, allow_tools: list[str] | None = None
    ) -> str:
        if name in self.subagents and name in self._tasks and not self._tasks[name].done():
            return f"子 Agent {name} 已在运行中。"
        allow = list(allow_tools or [])
        sub_registry = build_subagent_registry(self._parent_registry, allow) \
            if allow else self._parent_registry
        sub_agent = AgentOnceRun(
            self._llm,
            sub_registry,
            context_builder=ContextBuilder(SUBAGENT_SYSTEM_PROMPT, runtime_context=None),
            hooks=self._hooks,
            config=AgentConfig(max_iterations=self._max_iterations),
            # 同一个存储：子 Agent 的 QA 历史进 messages 表，附件按 owner 校验也才有效
            storage=self._session_manager.storage,
        )
        session = await self._session_manager.get_or_create("team", name)
        _kw: dict[str, Any] = {}
        if self._idle_window_sec is not None:
            _kw["loop_times"] = max(1, int(self._idle_window_sec
                                           / max(self._idle_poll_sec or 1.0, 1e-6)))
        if self._idle_poll_sec is not None:
            _kw["poll_interval"] = self._idle_poll_sec
        managed = ManagedSubAgent(
            name,
            sub_agent,
            self.subagent_manager,
            message_center=self.message_center,
            task_manager=self.task_manager,
            session=session,
            **_kw,
        )
        await self.subagent_manager.register(name, prompt)
        self.subagents[name] = managed
        # create_task 复制当前上下文 —— 子 Agent 因此继承调用者所在会话的作用域，
        # 它读写收件箱/任务板时天然落在同一个 scope 上。显式再包一层，让离线直调
        # （上下文里没有身份）也稳定落在同一个作用域，与 send_message 侧一致。
        ident = current_identity_or(self.subagent_manager.default_user_id,
                                    self.subagent_manager.default_conversation_id)
        self._idents[name] = ident
        task = asyncio.create_task(self._run_subagent(managed, prompt, ident))
        task.add_done_callback(lambda t, n=name: self._on_subagent_done(n, t))
        self._tasks[name] = task
        return _json({"started": name, "allow_tools": allow or "（全量父工具集）"})

    async def _run_subagent(self, managed: ManagedSubAgent, prompt: str,
                            ident: Identity) -> dict[str, Any]:
        with use_identity(ident.user_id, ident.conversation_id):
            return await managed.run(prompt)

    def _on_subagent_done(self, name: str, task: asyncio.Task) -> None:
        """被 cancel 的子 Agent 在自己的 await 处直接抛出，来不及写状态——这里补写。

        回调运行在事件循环的默认上下文（没有身份 contextvar），所以显式带着
        创建时捕获的 ident 写回，否则会落到缺省作用域去。
        """
        if task.cancelled():
            ident = self._idents.get(name)
            if ident is not None:
                self._status_writes.add(
                    asyncio.create_task(self._set_status(ident, name, SHUTDOWN)))

    async def _set_status(self, ident: Identity, name: str, status: str) -> None:
        with use_identity(ident.user_id, ident.conversation_id):
            await self.subagent_manager.set_status(name, status)

    async def team_status(self) -> str:
        return _json(
            {
                "agents": await self.subagent_manager.list_agents(),
                "running": {n: not t.done() for n, t in self._tasks.items()},
                "tasks": await self.task_manager.list_all(),
            }
        )

    # ---- 生命周期 ----

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        pending = [t for t in self._tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        # 状态补写要等完：存储层紧接着就 close 了，fire-and-forget 会写空。
        if self._status_writes:
            await asyncio.gather(*self._status_writes, return_exceptions=True)
            self._status_writes.clear()


def register_team_tools(
    registry: ToolRegistry,
    *,
    llm: Any,
    storage: Any,
    node_deps: Callable[[], dict[str, list[str]]] | None = None,
    hooks: AgentHook | None = None,
    max_iterations: int = 8,
    idle_window_sec: float | None = None,
    idle_poll_sec: float | None = None,
) -> Team:
    """构造团队组件并把 7 个协作工具注册进 registry，返回 Team（含 shutdown）。

    ``node_deps`` 现取剪辑 DAG 的依赖映射（生产里就是 Storyline 的 ``dag_contract`` 结果，
    启动后才到）；不给就等于 DAG 未接入，``plan_editing_team`` 会如实回绝。

    ``idle_window_sec`` 控制子 Agent 干完一轮后**在内存里等多久**才 shutdown。
    不给就用模块默认（常驻复用，别每任务重建）；生命周期测试给个短值即可。
    """
    center = MessageCenter(storage)
    tasks = TaskManager(storage)
    sub_manager = SubAgentManager(storage)
    session_manager = SessionManager(storage)

    team = Team(
        message_center=center,
        task_manager=tasks,
        subagent_manager=sub_manager,
        llm=llm,
        parent_registry=registry,
        session_manager=session_manager,
        node_deps=node_deps or (lambda: {}),
        hooks=hooks,
        max_iterations=max_iterations,
        idle_window_sec=idle_window_sec,
        idle_poll_sec=idle_poll_sec,
    )

    register_message_tools(registry, center, MAIN_AGENT_NAME)
    registry.register(
        FunctionTool(
            "plan_editing_team",
            "把视频剪辑 DAG 自动拆解为 Agent Team 可认领的任务（按拓扑序写入任务板）。",
            {"type": "object", "properties": {}, "required": []},
            team.plan_editing_team,
            display_name="拆解剪辑任务",
        )
    )
    registry.register(
        FunctionTool(
            "list_tasks",
            "查看任务板上的全部任务及其状态。",
            {"type": "object", "properties": {}, "required": []},
            team.list_tasks,
            read_only=True,
            display_name="查看任务板",
        )
    )
    registry.register(
        FunctionTool(
            "claim_task",
            "认领一个待办任务（owner 标记归属）。",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务 id"},
                    "owner": {"type": "string", "description": "认领者名字"},
                },
                "required": ["task_id", "owner"],
            },
            team.claim_task,
            display_name="认领任务",
        )
    )
    registry.register(
        FunctionTool(
            "complete_task",
            "把一个已认领的任务标记为完成，其下游任务随之解锁。"
            "只有认领者本人能交付；重复交付幂等（原样返回）。",
            {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务 id"},
                    "owner": {"type": "string",
                              "description": "交付者（认领者）名字，缺省 main_agent"},
                },
                "required": ["task_id"],
            },
            team.complete_task,
            display_name="完成任务",
        )
    )
    registry.register(
        FunctionTool(
            "start_subagent",
            "启动一个常驻子 Agent（后台运行 WORK/IDLE/SHUTDOWN 状态机）："
            "先执行 prompt，之后被收件箱消息或可认领任务唤醒。"
            "allow_tools 给出工具名白名单可收敛其权限。",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子 Agent 名字"},
                    "prompt": {"type": "string", "description": "首轮任务指令"},
                    "allow_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "允许子 Agent 使用的工具名白名单（缺省为全量）",
                    },
                },
                "required": ["name", "prompt"],
            },
            team.start_subagent,
            display_name="启动子助手",
        )
    )
    registry.register(
        FunctionTool(
            "team_status",
            "查看团队总览：子 Agent 状态机（working/idle/shutdown）+ 任务板状态。",
            {"type": "object", "properties": {}, "required": []},
            team.team_status,
            read_only=True,
            display_name="查看团队状态",
        )
    )
    return team
