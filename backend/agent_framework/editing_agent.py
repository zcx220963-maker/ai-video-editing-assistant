"""剪辑 Agent 组装工厂：把散落的骨架拼成一个能跑的“视频剪辑 Agent”，并对接 Agent Team。

对应设计文档「视频剪辑的 MCP 和 Skill 设计 / 基于 DAG 的编排引擎 / Agent Team」的**集成层**：
前面每个模块各自实现并单测过，本模块负责把它们**装配到一起**——

    ContextBuilder(基础提示 + <skills> 清单 + 记忆 + runtime)
        + ToolRegistry(剪辑节点工具 + load_skill + read_node_history + 记忆工具)
        + Interceptor(DAG 依赖补齐，包裹每个节点工具)
        + Checkpoint(崩溃恢复) + CompositeHook(LoggingHook/MetricsHook)
        ↓
    AgentOnceRun —— 一个真正“按剪辑流程行动”的 Agent

并提供 ``plan_team_from_dag`` 把剪辑 DAG 一键拆解成 Task Manager 的可认领任务，
供 Agent Team（SubAgent Manager）分工执行；Store 作为跨节点/跨 Agent 的数据总线解耦各步。

说明：真实音视频处理与远程 MCP Server 均按文档归音视频团队；这里的节点是 mock，
Storyline 以“本地节点 + 拦截器”方式跑通编排，不发起网络。
"""

from __future__ import annotations

from typing import Any

from .agent import AgentConfig, AgentOnceRun
from .checkpoint import CheckpointManager
from .context import ContextBuilder
from .hooks import AgentHook, CompositeHook, LoggingHook, MetricsHook
from .memory import MemoryContextSource, MemoryStore, register_memory_tools
from .orchestration import NodeRegistry, NodeState
from .prompts import PromptLibrary, build_prompt_library, read_prompts_file
from .skill import SkillLoader, SkillManifestContextSource, register_skill_tools
from .task_manager import TaskManager, topo_order
from .video_editing import build_agent_registry, build_node_registry


from .context import prompt_fingerprint

# 剪辑副链的系统提示内核。**真相源是磁盘**：`prompts/editing_round.md`。
#
# 2026-10-14 起这里不再留内联副本：原先它与 `prompts/editing_round.md` 各存一份
# （靠 test_prompt_library 的空白归一对比守），那正是「同一段文案两份」的形状——
# 同一条纪律（「配乐只能从曲库选」）改一处不改另一处，两条链就会对同一件事说两句话。
# 现在这里只是**把磁盘那份读进来**的别名，读者（``__init__`` 的导出、测试）照旧。
# 缺失即抛（read_prompts_file）：文件随代码分发，缺了是部署坏了，不是该静默回落的场景。
EDITING_SYSTEM_PROMPT = read_prompts_file("editing_round.md")

EDITING_PROMPT_FINGERPRINT = prompt_fingerprint(EDITING_SYSTEM_PROMPT)


def node_required_map(node_registry: NodeRegistry) -> dict[str, list[str]]:
    """本地注册表 → 依赖映射（离线夹具用）。生产路径的映射来自剪辑服务端契约。"""
    return {n.name: list(n.required_nodes) for n in node_registry.all()}


async def plan_team_from_dag(
    required: dict[str, list[str]], task_manager: TaskManager
) -> list[int]:
    """把剪辑 DAG 一键拆成 Task Manager 的可认领任务，返回按拓扑序分配的 task id 列表。

    主 Agent 的 Prompt→任务拆解这一步在文档里由 LLM 完成；这里用确定性映射（节点名→任务、
    required_nodes→blockedBy）先把协作层打通，便于 Agent Team 直接认领。
    id 由 ``task_seq`` 在插队时现取，所以拓扑序保证前置一定排在后置之前。
    """
    ids: dict[str, int] = {}
    for name in topo_order(required):
        row = await task_manager.create(
            name, description=f"剪辑节点任务：{name}",
            blocked_by=[ids[d] for d in required[name] if d in ids],
        )
        ids[name] = row["id"]
    return list(ids.values())


def build_editing_agent(
    llm,
    *,
    storage,
    skill_loader: SkillLoader | None = None,
    use_memory: bool = True,
    use_checkpoint: bool = True,
    node_registry: NodeRegistry | None = None,
    state: NodeState | None = None,
    max_iterations: int = 40,
    observability: bool = True,
    prompt_library: PromptLibrary | None = None,
) -> dict[str, Any]:
    """装配一个剪辑 AgentOnceRun，并返回其关键部件供调用/测试观测。

    storage 是运行时状态的唯一落点（spec D1：无本地兜底）——这里用于 checkpoints 与
    memories 两张表。
    返回字段：agent, state, interceptor, registry, skill_loader, memory_store,
    checkpoint, context_builder, metrics。

    ``prompt_library``（T42）：副链与主链**共用同一个提示词库**。不传就是按默认目录现建
    一个（`build_prompt_library(None)`：仓库自带目录在就用它），传进来则连热替换一起共享
    ——两条链从此读同一批 ``prompts/*.md``，「改一句文案要动两处代码」这件事到此为止。
    """
    node_registry = node_registry or build_node_registry()
    state = state or NodeState(session_id="edit:default", artifact_id="")

    registry, interceptor = build_agent_registry(node_registry, state)

    context_sources: list[Any] = []
    if skill_loader is not None:
        register_skill_tools(registry, skill_loader)
        context_sources.append(SkillManifestContextSource(skill_loader))

    memory_store: MemoryStore | None = None
    if use_memory:
        # 记忆在 memories 表里按 (user_id, category) 归属，不需要本地目录
        memory_store = MemoryStore(storage)
        register_memory_tools(registry, memory_store)
        context_sources.append(MemoryContextSource(memory_store))

    # 系统提示内核从**磁盘**读（`prompts/editing_round.md`），读不到才用内联副本回落。
    # 显式传字符串给 ContextBuilder 是刻意的：它的 ``system_prompt=None`` 那条默认路径
    # 读的是 ``system_prompt.md``（主链那份），而副链要的是自己这一份。
    library = prompt_library or build_prompt_library(None)
    context_builder = ContextBuilder(
        library.text("editing_round.md", EDITING_SYSTEM_PROMPT),
        context_sources=context_sources, prompt_library=library)

    hooks: AgentHook | None = None
    metrics: MetricsHook | None = None
    if observability:
        metrics = MetricsHook()
        hooks = CompositeHook([LoggingHook(), metrics])

    checkpoint = CheckpointManager(storage) if use_checkpoint else None

    agent = AgentOnceRun(
        llm,
        registry,
        context_builder=context_builder,
        hooks=hooks,
        config=AgentConfig(max_iterations=max_iterations),
        checkpoint=checkpoint,
    )
    return {
        "agent": agent,
        "state": state,
        "interceptor": interceptor,
        "registry": registry,
        "skill_loader": skill_loader,
        "memory_store": memory_store,
        "checkpoint": checkpoint,
        "context_builder": context_builder,
        "metrics": metrics,
    }
