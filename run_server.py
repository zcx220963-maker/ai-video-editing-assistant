"""顶层装配：把架构图整条链路串成一个可运行的 FastAPI 服务（全链路默认开启）。

    用户 → Web 接口层(FastAPI) → Message Queue InBound → 会话分区路由 → Session Manager
         → AgentLoop(AgentOnceRun) → LLM Provider(get_default_llm)
    LLM delta → on_stream Hook(OutboundStreamHook) → MQ OutBound
         → Connection Manager → 按 session_id 找回 WS 连接 → 对应会话窗口
    ContextBuilder ← Bootstrap 文件 + Memory + Skill Loader + 分级上下文压缩
    Tool Registry  ← Spawn / Search(web) / fetch_media / File / Cron / Memory / Skill /
                     Agent Team(send_message·任务板·常驻子 Agent) / rerun_from /
                     剪辑节点（只来自 Storyline MCP；未连通即无剪辑能力）/
                     MCP 外部工具(mcp.json)
    Scheduling     ← Cron(工具) + Heartbeat(周期唤醒)
    Storage        ← build_storage(--storage)：pg_minio=PG 元数据 + MinIO 媒体字节（生产），
                     memory=内存替身（离线测试）；启动即校验连通性与建表，连不上直接退出
    Startup        ← 存储层校验 → checkpoint 未完成执行自动恢复 + MCP/Storyline 异步接入（失败仅告警）

运行时状态落点：会话历史/收件箱/任务板/子 Agent/checkpoint/定时任务与心跳/长期记忆/技能库
（正文 + 附件）全在 PG 与 MinIO（spec §8），本地目录只作为可弃工作区和技能导入源。
密钥只配置一处：日常在页面「设置」里填（按用户存 PG，压过环境）；没配时按环境变量
`OPENAI_API_KEY` → `DEEPSEEK_API_KEY` → `SILICONFLOW_API_KEY` 取第一把非空的（全项目共用）。
PG_DSN / MINIO_* 五项只从环境变量或 .env 读，代码不落盘也不打印值。

用法：
    python run_server.py                       # 内存 MQ（无需 broker），监听 127.0.0.1:8000
    python run_server.py --storage pg_minio    # 上线存储层（先 docker compose up -d + .env）
    python run_server.py --mq kafka --bootstrap-servers localhost:9092
    python run_server.py --mcp-config mcp.json --storyline-config examples/storyline/config.toml
    python run_server.py --no-memory --no-skills --no-team --no-checkpoint --no-mcp --no-storyline
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import subprocess as _sp
    _orig_popen_init = _sp.Popen.__init__
    _NO_WIN = 0x08000000
    def _popen_init_no_window(self, *a, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | _NO_WIN
        _orig_popen_init(self, *a, **kw)
    _sp.Popen.__init__ = _popen_init_no_window

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_framework.agent import Agent, AgentConfig
from agent_framework.broadcast import OUTBOUND_CHANNEL
from agent_framework.checkpoint import CheckpointManager
from agent_framework.compress import ContextCompressor, make_llm_summarizer
from agent_framework.context import ContextBuilder
from agent_framework.prompts import build_prompt_library
from agent_framework.agent import _refresh_prompt_texts as refresh_prompt_texts
from agent_framework.ask_user import AskUserTool
from agent_framework.consumer import CHAT_TOPIC, session_key
from agent_framework.heartbeat import Heartbeat
from agent_framework.hooks import (
    ApprovalHook,
    CompositeHook,
    LoggingHook,
    MediaCardHook,
    MetricsHook,
    OutboundStreamHook,
    QuotaHook,
    ToolTraceHook,
    UsageHook,
)
from agent_framework.llm import LLMClient
from agent_framework.llm_openai import get_default_llm
from agent_framework.memory import MemoryContextSource, MemoryStore, register_memory_tools
from agent_framework.mq import MessageQueue, build_message_queue
from agent_framework.server import BGM_LIBRARY_USER, create_app
from agent_framework.session import SessionManager
from agent_framework.storage import Storage, build_storage
from agent_framework.skill import (
    SkillLoader,
    SkillManifestContextSource,
    register_skill_tools,
)
from agent_framework.subagent import make_spawn_tool, refresh_subagent_prompt
from agent_framework.team_tools import Team, register_team_tools
from agent_framework.tool import ToolRegistry
from agent_framework.tools.cron import CronScheduler, register_cron_tools
from agent_framework.tools.file import register_file_tools, session_workspace_root
from agent_framework.tools.mcp import (
    MCPClient,
    MCPServerConfig,
    connect_server,
    load_mcp_config,
    register_mcp_tools,
)
from agent_framework.editing_contract import ContractSlot, load_contract
from agent_framework.plan import PlanCardHook, PlanGate, PlanReconcileHook
from agent_framework.catalog import get_catalog
from agent_framework.media_fetch import FetchPolicy
from agent_framework import uploads
from agent_framework.tools.fetch_media import register_fetch_media_tools
from agent_framework.tools.runs import register_run_tools
from agent_framework.tools.web import register_web_tools
from agent_framework.video_editing import (
    load_storyline_config,
    storyline_available_nodes,
    storyline_server_url,
)

RUNTIME_DIR = Path(".runtime")
DEFAULT_MCP_CONFIG = Path("mcp.json")
DEFAULT_STORYLINE_CONFIG = Path("examples") / "storyline" / "config.toml"
DEFAULT_MAX_CONTEXT_TOKENS = 8_000
# 主服务侧会话工作区的过期回收线（与 Storyline 的 [storage].workspace_ttl_sec 对齐）：
# 崩溃/异常退出遗留的 {会话}/{产物} 目录（含 _files 沙箱）在启动时按 mtime 扫掉。
WORKSPACE_TTL_SEC = float(os.getenv("WORKSPACE_TTL_SEC", "21600"))


def _warn(msg: str) -> None:
    print(f"[startup][warn] {msg}")


def _info(msg: str) -> None:
    print(f"[startup] {msg}")


def sync_catalog(registry: ToolRegistry, skills: dict[str, str] | None = None) -> int:
    """把注册表声明的中文名并入进程级词表（WS/HTTP 出口读的就是这一个对象）。

    装配期填、出口处替换，不往每个 handler 透传。可重复调用：MCP/Storyline 的工具
    在 `_startup` 里才注册进来，那时再 sync 一次把远程节点的中文名补上。
    """
    catalog = get_catalog()
    catalog.update(registry.displays())
    if skills:
        catalog.update(skills)
    return len(catalog.names)


async def skill_displays(loader: SkillLoader | None) -> dict[str, str]:
    """技能名的中文名：取库里 frontmatter 声明过 display 的那些。

    取不到只是界面退回机器名（块 A 的既定降级），不该阻断启动，所以异常只告警。
    """
    if loader is None:
        return {}
    try:
        return {s.name: s.display for s in await loader.discover() if s.display}
    except Exception as exc:  # noqa: BLE001
        _warn(f"技能中文名读取失败（界面退回机器名）：{exc}")
        return {}


def plan_skill_source(loader: SkillLoader | None):
    """计划门第三条（技能存在 / 可用 / 有中文名）的取数入口。

    返回 ``{技能名: manifest}`` 而不是中文名串：校验要读 ``available`` 与
    ``unavailable_reason``。没接技能库时给 None——门里那一路会降级成警告，
    「没接」和「读不到」都不该让整张卡直接作废。
    """
    if loader is None:
        return None

    async def _skills() -> dict[str, Any]:
        return {s.name: s for s in await loader.discover()}

    return _skills


BGM_ENUM_MAX_ITEMS = 200


def bgm_enum_source(storage: Storage):
    """``select_BGM`` 的 query 参数没有 enum：能反查的枚举源只有曲库里真实存在的曲目。

    返回值会当作卡面选项，所以给**去扩展名的曲名**（``search_media`` 按 2-gram 命中
    文件名，带不带后缀都搜得到）。曲库读不到就回空列表——按门的口径这是「这组开关
    无从核验」，模型只能把它留给计划卡的「其他」，不会凭空造一个曲风标签。
    """

    async def _options(node: str, key: str) -> list[str]:
        if "bgm" not in (node or "").lower() or (key or "").strip() != "query":
            return []
        rows = await storage.materials.list_visible(
            BGM_LIBRARY_USER, None, origin="bgm", kinds=("audio",))
        stems: list[str] = []
        for r in rows[:BGM_ENUM_MAX_ITEMS]:
            name = Path(str(r.get("filename") or "")).stem.strip()
            if name and name not in stems:
                stems.append(name)
        return stems

    return _options


def _sweep_workspace(storage: Storage, ttl_sec: float) -> int:
    """启动期回收主服务侧过期的会话工作区目录（README §6：此前只有 Storyline 扫）。

    只依赖本地目录 mtime、不碰网络，所以内存替身也能跑。清扫失败仅告警，
    绝不抛穿 _startup 阻断启动——回收遗留临时文件是尽力而为，不是服务可用的前置条件。
    """
    try:
        n = storage.workspace.sweep_stale(ttl_sec)
    except Exception as exc:  # noqa: BLE001 - 回收失败不影响启动
        _warn(f"会话工作区过期清扫失败（忽略，不阻断启动）：{exc}")
        return 0
    if n:
        _info(f"会话工作区过期清扫：回收 {n} 个文件（TTL {ttl_sec:.0f}s）")
    return n


def _resolve_dir(value: str | Path | bool | None, default: Path) -> Path:
    """sentinel：True/None/"" → 默认路径；显式路径 → 原样；False 的禁用判断在调用方。"""
    if isinstance(value, (bool, type(None))) or value == "":
        return Path(default)
    return Path(value)


@dataclass
class Runtime:
    agent: Agent
    mq: MessageQueue
    heartbeat: Heartbeat
    cron: CronScheduler
    registry: ToolRegistry
    app: object  # FastAPI
    team: Team | None = None
    checkpoint: CheckpointManager | None = None
    skill_loader: SkillLoader | None = None
    storage: Storage | None = None
    plan_gate: PlanGate | None = None
    mcp_clients: list[MCPClient] = field(default_factory=list)
    startup: Any = None   # async callable()：create_app 的 on_startup 同一实现，测试可直调
    shutdown: Any = None  # async callable()：create_app 的 on_shutdown


def _default_instance_id() -> str:
    """本实例标识 hostname-pid：多副本下崩溃恢复的认领权按它归属。"""
    import os
    import socket
    try:
        host = socket.gethostname()
    except Exception:  # noqa: BLE001
        host = "host"
    return f"{host}-{os.getpid()}"


def build_runtime(
    *,
    llm: LLMClient | None = None,
    mq_backend: str = "memory",
    bootstrap_servers: str = "localhost:9092",
    storage_backend: str = "memory",                 # 生产用 "pg_minio"；"memory" 是离线替身
    storage: Storage | None = None,                  # 已构造的句柄（测试注入），优先于 storage_backend
    cache_root: str | Path | None = None,            # localize 内容缓存根（可删光）
    workspace_root: str | Path | None = None,        # 渲染临时工作区根（可删光）
    cache_max_gb: float = 20.0,                      # 内容缓存 LRU 上限（CLI 名为 --workspace-max-gb）
    workspace_ttl_sec: float = WORKSPACE_TTL_SEC,    # 会话工作区过期回收线（启动时扫一次）
    topic: str = CHAT_TOPIC,
    skills_dir: str | Path | bool | None = None,     # 导入源目录；False=关闭技能系统
    use_memory: bool = True,                         # 长期记忆存 PG memories 表
    bootstrap_dir: str | Path | None = None,
    # 提示词目录（prompts/*.md）；None = 用仓库自带的 prompts/，False = 关闭（全走内联）
    prompts_dir: str | Path | bool | None = None,
    use_checkpoint: bool = True,                       # False = 关闭崩溃恢复快照
    heartbeat_interval: int = 30,
    max_iterations: int = 40,
    # 渲染前确认门：编排就绪、即将渲染时先把编排结果给用户看，确认后才渲。
    # 用户明确要求「不确认绝不渲染」，所以默认为 True。置 False 即回到直接渲染的老行为
    # （不是砍功能——是给「我已经确认过、别再问我」留一条明确的路）。
    gate_render: bool = True,
    observability: bool = True,
    static_dir: str | Path | None = None,
    mcp_config: str | Path | bool | None = None,     # None=存在即用；False=关闭
    storyline_config: str | Path | bool | None = None,
    team: str | Path | bool | None = None,           # False = 禁用 Agent Team（状态在 PG 协作三表）
    max_context_tokens: int | None = None,           # None/<=0 = 不压缩
    auto_resume: bool = True,
    max_upload_mb: int = 1024,
    upload_ttl_sec: float = uploads.DEFAULT_SESSION_TTL_SEC,
    upload_sweep_sec: float = 600.0,
    fetch_policy: FetchPolicy | None = None,      # None = 用 media_fetch 的默认护栏
    broadcast_url: str | None = None,             # Redis DSN：多副本回投广播，None = 单副本直连
    broadcast_channel: str = OUTBOUND_CHANNEL,
    instance_id: str | None = None,               # 本实例标识：多副本崩溃恢复认领权的归属
    approve_tools: str = "",                       # 逗号分隔：执行前需人工审批的工具名（HITL）
    quota_limit: int = 0,                          # 每用户配额上限（token 数），0 = 不限
    quota_window: int = 86400,                     # 配额统计窗口（秒），默认 24 小时
) -> Runtime:
    llm = llm or get_default_llm()
    instance_id = instance_id or _default_instance_id()

    # 提示词库：默认接仓库自带的 prompts/（存在就用），所以「提示词搬出 .py」
    # 默认生效，而不是又一个要记得打开的开关——Bootstrap 机制当年就是死在
    # 「写了但没人接」上（bootstrap_dir 一直没传）。System prompt、规划轮整段、
    # 子 Agent 的 system、几条纠错说明都从这里读；文件缺失则逐段回落到内联默认值。
    # **必须排在所有消费者之前**：下面的 make_spawn_tool 会把子 Agent 提示词
    # 在构造时就固化进 SubAgentRunner，PlanGate 也要拿这份引用——晚一步就变成
    # 「磁盘上有一份、跑的永远是内联那份」。
    prompt_library = build_prompt_library(False if prompts_dir is False else prompts_dir)
    # 让 agent.py 里的纠错提示也换成磁盘版本（内联那几段是回落值）
    refresh_prompt_texts(prompt_library)
    # 子 Agent 的 system 提示词同理（CHILD_SYSTEM_PROMPT 那份内联是回落值）
    refresh_subagent_prompt(prompt_library)

    mq = build_message_queue(mq_backend, bootstrap_servers=bootstrap_servers)
    if storage is None:
        inject = {"cache_root": cache_root, "workspace_root": workspace_root}
        storage = build_storage(storage_backend,
                                **{k: v for k, v in inject.items() if v is not None},
                                cache_max_gb=cache_max_gb)
    use_team = team is not False
    use_skills = skills_dir is not False

    # ---- Tool Registry ----
    registry = ToolRegistry()
    # HITL：按名指定「执行前需人工审批」的工具（逗号分隔）；空 = 不启用审批断点。
    registry.set_approval([t.strip() for t in approve_tools.split(",") if t.strip()])
    register_file_tools(registry, session_workspace_root(storage.workspace.root))
    register_web_tools(registry)
    # 剪辑节点**只**来自 Storyline MCP：DAG、依赖补齐与产物存储都在那台服务上，
    # 本地再无第二套图（video_editing 里的 mock 节点只剩离线测试夹具）。
    # 连不上 Storyline = 本服务没有剪辑能力，启动时会明确告警，不静默降级成 mock。
    editing_contract = ContractSlot()
    register_fetch_media_tools(registry, storage=storage, policy=fetch_policy)
    # rerun_from：从某节点回退并重跑（上游产物复用、产物作用域换新）
    register_run_tools(registry, editing_contract)

    # spawn：子 Agent 可用工具（联网检索）；trace_mq 让子 Agent 工具调用也回投到父会话窗口。
    registry.register(make_spawn_tool(llm, [lambda r: register_web_tools(r)], trace_mq=mq))

    # 主动提问：把「需要用户拿主意」的时刻变成选项卡弹窗（不再让用户纯打字回答）。
    # 模型调它 → 本轮挂起 → 用户点选 → 从断点续跑。与审批共用同一套挂起/续跑机制。
    ask_tool = AskUserTool()
    registry.register(ask_tool)

    # 执行轮（与闲聊轮）的「本轮为什么没有这个工具」说明，与规划轮那条对称
    # （见 agent_framework/plan/vocab.py 的 planning_registry）。
    # 真机事故：执行轮里模型调了 submit_plan，只拿到一句 tool 'submit_plan' not found，
    # 看不出这是「按设计不提供」，于是弹窗问用户「本会话没有 submit_plan 工具，
    # 你希望怎么处理？」——用户被问了一个他答不了的问题，而正确答案是「这轮不用再提计划」。
    registry.unknown_tool_hint = (
        "提交/确认计划的两只工具（submit_plan、confirm_plan）只在**规划轮**"
        "（用户提新剪辑诉求、系统弹出计划卡那一轮）注册，执行轮按设计不再提交候选计划——"
        "要确认的计划已经在卡上确认过了。请直接用本轮真实的剪辑节点把剩余步骤跑完，"
        "或如实收尾本轮；**不要**为了一个本轮不存在的工具调用 ask_user 把锅抛给用户。\n"
        "如果这个名字不属于上面两只，那它就是臆造的（拼错、或把技能正文里的说法当成了"
        "工具名）：请从本轮的工具清单里选一个真实存在的工具，不要换个拼法重试。")

    # ---- Cron（到点投递到 MQ）：任务行在 PG scheduled_jobs，scheduler 的 runner 需要 mq ----

    # ---- Context sources（记忆与技能都在 PG，本地目录仅是技能的导入源）----
    context_sources: list = []
    skills_import_dir = (_resolve_dir(skills_dir, Path("examples") / "skills")
                         if use_skills else None)
    skill_loader: SkillLoader | None = None
    if use_skills:
        skill_loader = SkillLoader(storage)
        register_skill_tools(registry, skill_loader)
        context_sources.append(SkillManifestContextSource(skill_loader))
    memory_store: MemoryStore | None = None
    if use_memory:
        memory_store = MemoryStore(storage)
        register_memory_tools(registry, memory_store)
        context_sources.append(MemoryContextSource(memory_store))

    # ---- 计划门（块 B）：规划轮的唯一出口 + 执行帧的服务端校验 ----
    # 依赖全是引用（注册表与契约要到 _startup 才填齐），所以在这里构造就够了：
    # Storyline 没连上时 whitelist() 为空，Agent.plan() 自己退回普通 handle——
    # 「没有可规划的节点」不该变成一道白拦的空门。
    plan_gate = PlanGate(
        registry=registry,
        contract=editing_contract,
        skills=plan_skill_source(skill_loader),
        extra_options=bgm_enum_source(storage),
        catalog=get_catalog(),
        prompt_library=prompt_library,
    )

    # ---- 分级上下文压缩（默认开启：折叠工具结果 → 淘汰 → LLM 摘要；<=0 显式关闭）----
    compressor = None
    budget = max_context_tokens if max_context_tokens is not None else DEFAULT_MAX_CONTEXT_TOKENS
    if budget > 0:
        compressor = ContextCompressor(budget, summarizer=make_llm_summarizer(llm))

    context_builder = ContextBuilder(
        None,                       # None = 按提示词库解析；显式字符串仍可覆盖
        bootstrap_dir=bootstrap_dir,
        context_sources=context_sources,
        compressor=compressor,
        prompt_library=prompt_library,
    )

    # OutboundStreamHook 是功能链路（LLM delta → on_stream → MQ OutBound），
    # MediaCardHook 是成片回投（render_video 结果 → after_execute_tools → 播放卡片），
    # checkpoint 一致点在 PG checkpoints 表：重启后未完成的 run 还在（原来是一堆 .json）
    checkpoint = CheckpointManager(storage) if use_checkpoint else None
    # ToolTraceHook 是工具调用追踪（before/after_tool_call → 前端实时展示工具链路），
    # PlanCardHook 把候选计划回投成计划卡（并让当轮 assistant 行带住它，刷新能重放），
    # PlanReconcileHook 在事后算「批准的计划 vs 实际调用」的偏差（只观测不拦截），
    # 均不受 observability 开关限制；Logging/Metrics 才是纯观测。
    hook_list: list = [OutboundStreamHook(mq), MediaCardHook(mq), ToolTraceHook(mq),
                       PlanCardHook(mq, cp_mgr=checkpoint), PlanReconcileHook(mq), ApprovalHook(mq)]
    # 配额：用量落库 + 超限拦截（limit=0 时不拦截，UsageHook 仍记录用量供观测）
    hook_list.append(UsageHook(storage.db))
    if quota_limit > 0:
        hook_list.append(QuotaHook(storage.db, limit=quota_limit, window=quota_window))
    if observability:
        hook_list += [LoggingHook(), MetricsHook()]
    hooks = CompositeHook(hook_list)
    agent = Agent(
        llm=llm,
        registry=registry,
        context_builder=context_builder,
        hooks=hooks,
        config=AgentConfig(max_iterations=max_iterations, gate_render=gate_render),
        checkpoint=checkpoint,
        session_manager=SessionManager(storage),
        storage=storage,
        plan_gate=plan_gate,
        skill_loader=skill_loader,
        memory_store=memory_store,
    )

    # ---- Agent Team：消息中心 / 任务板 / 常驻子 Agent 池（状态在 PG 协作三表）----
    team_obj: Team | None = None
    if use_team:
        team_obj = register_team_tools(
            registry,
            llm=llm,
            storage=storage,
            node_deps=editing_contract.required_map,
            hooks=hooks,
            max_iterations=max_iterations,
        )

    # Cron 工具需要已构造的 scheduler；scheduler 的 runner 需要 mq —— 先建 scheduler 再注册。
    HEARTBEAT_JOB = "heartbeat"

    async def _runner(job) -> None:
        if job.name == HEARTBEAT_JOB:
            await heartbeat.handle(job)     # 同表同调度器：心跳行只数拍子，不当对话投递
            return
        await mq.publish(
            topic,
            session_key("cron", job.id),
            {
                "user_id": "cron",
                "conversation_id": job.id,
                "message": job.task,
                "run_id": f"cron-{job.id}",
                # 默认入口是规划轮（出卡等用户确认）。到点投递的这条没有人来点确认，
                # 所以显式声明走直接执行——否则定时任务会永远停在那张没人看的卡上。
                "action": {"op": "execute"},
            },
        )

    scheduler = CronScheduler(storage, runner=_runner)
    register_cron_tools(registry, scheduler)

    # 本地工具此刻已注册齐；远程节点要到 _startup 才进来，届时再补一次。
    sync_catalog(registry)

    heartbeat = Heartbeat(storage, on_beat=None, interval_seconds=heartbeat_interval,
                          name=HEARTBEAT_JOB, scheduler=scheduler)

    # ---- 启动钩子：checkpoint 自动恢复 + MCP/Storyline 外部工具接入（失败仅告警）----
    mcp_clients: list[MCPClient] = []
    mcp_path = None if mcp_config is False else _resolve_dir(mcp_config, DEFAULT_MCP_CONFIG)
    storyline_path = None if storyline_config is False else _resolve_dir(
        storyline_config, DEFAULT_STORYLINE_CONFIG
    )

    async def _startup() -> None:
        # (0) 存储层校验：连不上就抛出让进程退出——绝不带病服务、不退回本地磁盘
        await storage.start()
        if storage.backend == "memory":
            _warn("storage=memory 是测试替身，数据随进程消失；上线请用 --storage pg_minio")
        else:
            _info(f"storage={storage.backend} 已连通并完成建表校验")

        # (0·a) 内部身份登记：定时任务（cron）与未进入 run 的缺省作用域（default）不是
        #        注册用户，但要写进有 user_id 外键的表——一次性登记，此后写入路径不自建用户行
        _info(f"内部身份已登记：{await storage.provision_internal()}")

        # (0·a2) 会话工作区过期清扫：Storyline 在自己的 lifespan 里扫过，主服务此前从不扫，
        #        导致本根下崩溃遗留的 {会话}/{产物} 目录与 _files 沙箱永不被回收（README §6）。
        #        只碰本地目录、失败只告警，不阻断启动。
        _sweep_workspace(storage, workspace_ttl_sec)

        # (0a) 调度循环：心跳行由 lifespan 的 heartbeat.start() 落表，这里才把循环跑起来
        #      —— 定时任务与心跳共用一个调度器，到点各自分流（心跳数拍子，任务投 MQ）
        await scheduler.start()

        # (0b) 技能库导入：本地目录只是源，正文进 skills 表、附件进对象存储
        if skill_loader is not None and skills_import_dir is not None:
            if skills_import_dir.is_dir():
                try:
                    names = await skill_loader.sync_from_dir(skills_import_dir)
                    _info(f"技能库已按 {skills_import_dir} 刷新 {len(names)} 个技能：{names}")
                except Exception as exc:  # noqa: BLE001 - 导入失败仍以库里已有清单服务
                    _warn(f"技能库导入失败（沿用库里已有内容）：{exc}")
            else:
                _info(f"技能导入源 {skills_import_dir} 不存在，仅使用库里已有的技能")

        # (0c) 子 Agent 租约对账：上个进程被 kill 时正在干活的行不再谎报 working
        if team_obj is not None:
            n = await team_obj.subagent_manager.reset_expired()
            if n:
                _info(f"{n} 个租约过期的子 Agent 状态已从 working 归位 idle")

        # (a) 崩溃恢复：原子认领本实例可接手的未完成 run 再续跑（跨实例互斥）。
        #     领不到 = 已被别的实例持有且租约未过期，跳过即可，不再「谁都恢复全部」。
        if checkpoint is not None and auto_resume:
            claimed = await checkpoint.claim_recoverable(instance_id)
            for cp in claimed:
                user, _, conv = cp.session_id.partition(":")
                _info(f"认领并恢复未完成执行 run={cp.run_id} session={cp.session_id} "
                      f"instance={instance_id}")
                asyncio.create_task(_resume_guarded(cp.run_id, user or "default", conv or cp.run_id))
            _info(f"崩溃恢复认领完成：接手 {len(claimed)} 条（instance={instance_id}）")
        elif checkpoint is not None:
            n = await checkpoint._repo.mark_running_as_failed()
            if n:
                _info(f"--no-resume：已把 {n} 条残留 running checkpoint 标记为 failed（消除幽灵任务）")

        # (b) 通用 MCP：mcp.json / config.yaml 里的 stdio 与 StreamableHttp server 列表
        if mcp_path is not None and mcp_path.is_file():
            try:
                cfg = load_mcp_config(mcp_path)
            except Exception as exc:  # noqa: BLE001
                _warn(f"MCP 配置 {mcp_path} 解析失败：{exc}")
                cfg = None
            if cfg is not None:
                for sc in cfg.list_servers():
                    try:
                        client = await connect_server(sc)
                        names = await register_mcp_tools(registry, client, sc)
                        mcp_clients.append(client)
                        _info(f"MCP server「{sc.name}」已接入 {len(names)} 个工具：{names}")
                    except Exception as exc:  # noqa: BLE001
                        _warn(f"MCP server「{sc.name}」连接失败，已跳过：{exc}")

        # (c) Storyline MCP：剪辑节点的**唯一**来源。连不上 = 没有剪辑能力，明确告警。
        if storyline_path is None:
            _info("已禁用 Storyline 探测（--no-storyline）：本服务无剪辑能力")
        elif not storyline_path.is_file():
            _warn(f"未找到 {storyline_path}：本服务无剪辑能力（剪辑节点只由 Storyline 提供）")
        else:
            try:
                cfg = load_storyline_config(storyline_path)
                sc = MCPServerConfig(
                    name="storyline",
                    type="streamableHttp",
                    url=storyline_server_url(cfg),
                    enabled_tools=storyline_available_nodes(cfg) or ["*"],
                    # 真实节点渲染可达分钟级——超时预算由 config [local_mcp_server].timeout 决定
                    tool_timeout=int(
                        cfg.get("local_mcp_server", {}).get("timeout", 600)),
                )
                client = await connect_server(sc)
                # 契约先取：节点的读写集（并发分批）与下游集（rerun_from 作废范围）都靠它
                contract = await load_contract(client, timeout=sc.tool_timeout)
                names = await register_mcp_tools(
                    registry, client, sc, name_prefix=False, contract=contract)
                if names:
                    editing_contract.fill(contract)
                    mcp_clients.append(client)
                    _info(f"Storyline 已接入 {len(names)} 个剪辑节点"
                          f"（DAG 契约 {len(contract.nodes)} 节点，rerun_from 可用）")
                else:
                    await client.close()
                    _warn("Storyline 未返回任何白名单工具：本服务无剪辑能力")
            except Exception as exc:  # noqa: BLE001
                _warn(f"Storyline MCP（{storyline_path}）未连通：本服务无剪辑能力。{exc}")

        # (d) 出口词表补全：远程节点的 title 与技能的 display 到这一步才拿得到。
        #     没装上也不影响服务——界面退回机器名是块 A 的既定降级。
        sync_catalog(registry, await skill_displays(skill_loader))
        _cat = get_catalog()
        _info(f"中文名词表已装载：{len(_cat.names)} 条（工具 / 剪辑节点 / 技能），"
              f"参数标签 {len(_cat.params_display())} 条"
              f"（工具帧的 arg_labels 与 /tools 的 params_display 都从这张表出）")

        # (e) 计划门就绪度：白名单为空 = 没有可规划的节点，默认入口如实退回普通对话。
        n_plan = len(plan_gate.whitelist())
        if n_plan:
            _info(f"计划门已就绪：{n_plan} 个可规划节点（默认入口先出计划卡再执行）")
        else:
            _warn("计划门无可用节点：剪辑诉求不会出计划卡（本服务无剪辑能力）")

        # (e2) 提示词来源：让「提示词到底是从磁盘读的还是内联默认值」一眼可见。
        # 这一条是给「动态加载」做证：以前没人能从这个日志判断提示词来自哪。
        try:
            if prompt_library is not None and prompt_library.loaded_from_disk():
                files = prompt_library.loaded_from_disk()
                _info(f"提示词库已接线：{len(files)} 份来自 {prompt_library.dir}"
                      f"（系统提示 + 规划轮段 + 子 Agent + 纠错说明，改文案不必动代码）")
            else:
                _warn("提示词库未接目录：系统提示、规划轮段、子 Agent 与纠错说明都用内联默认值"
                      "（把 prompts/*.md 放好即可生效）")
        except Exception as exc:  # noqa: BLE001
            _warn(f"提示词库状态未知（忽略）：{exc}")

        # (f) 装配期一致性：提示词/技能里点名的工具必须真的存在。
        # 这一步挡的是「模型照提示词调一个不存在的工具」——启动期不报错，
        # 运行期变成 UnknownToolError、调用从未发生、白烧一轮迭代预算，
        # 表现成「流程走到一半莫名其妙断了」。真机事故：提示词写 read_node_artifact，
        # 真实工具叫 read_node_history。四份清单互不校验，只能靠这里核一次。
        try:
            from agent_framework import consistency as _cons
            from agent_framework.agent import (NO_CARD_NUDGE_TEXT, NO_CARD_NOTE_TEXT,
                                               NO_CARD_STRUCTURAL_TEXT, STEP_NUDGE_TEXT,
                                               STEP_NOTE_TEXT)
            from agent_framework.plan import planning_section as _planning_section
            # known 必须是**模型真能调到的**工具全集。规划轮的工具（submit_plan /
            # confirm_plan）只在规划注册表里，不在主注册表——漏了它们就会把
            # 提示词里正确的 submit_plan 误报成假工具（第一次跑就是这么误报的）。
            known = (set(agent.runner.registry.tool_names)
                     | set(plan_gate.whitelist())
                     | {"submit_plan", "confirm_plan"})
            # 提示面：系统提示 + 规划轮段 + 各纠错 nudge（这些都会整段进模型上下文）
            prompt_texts: list[str] = [getattr(context_builder, "system_prompt", "") or ""]
            try:
                prompt_texts.append(await _planning_section(plan_gate))
            except Exception as exc:  # noqa: BLE001
                _warn(f"一致性检查：规划轮段取不到（跳过这段）：{exc}")
            prompt_texts += [NO_CARD_NUDGE_TEXT, NO_CARD_NOTE_TEXT,
                             NO_CARD_STRUCTURAL_TEXT, STEP_NUDGE_TEXT, STEP_NOTE_TEXT]
            # 技能面：正文是自然语言，里面的工具名可以直接与真实集合比对
            skill_bodies: dict[str, str] = {}
            try:
                for sk in await skill_loader.discover():
                    skill_bodies[sk.name] = sk.body or ""
            except Exception as exc:  # noqa: BLE001
                _warn(f"一致性检查：技能正文取不到（跳过这段）：{exc}")

            watched = _cons.missing_watched(prompt_texts, known)
            # 技能正文里的参数键（keep_clips/custom_groups/…）不是工具名，按真实 schema 排掉。
            # 来源必须收在 consistency.skill_field_names 这一处：装配处曾经只扫主注册表，
            # 而 submit_plan / confirm_plan 只活在**规划注册表**里（按调用懒建），
            # 于是 plans[].steps[].param_options 这个字段名被判成「臆造工具」，
            # 启动日志挂了一条假告警——假告警比不报更坏，它教人忽略这个检查本身。
            try:
                from agent_framework.plan import ConfirmPlanTool, SubmitPlanTool
                param_keys = _cons.skill_field_names(
                    node_param_keys=plan_gate.param_keys(),
                    tools=[registry.all_tools(),
                           [SubmitPlanTool(plan_gate), ConfirmPlanTool()]])
            except Exception as exc:  # noqa: BLE001 - 取不到就按原样检查
                _warn(f"一致性检查：字段名来源取不全（可能误报字段名为假工具）：{exc}")
                param_keys = set()
            skills_bad = _cons.unknown_tools_in_skills(
                skill_bodies, known, param_keys=param_keys)
            report = _cons.format_report(watched, skills_bad, known)
            if report:
                _warn(report)
            else:
                _info(f"一致性检查通过：提示面与 {len(skill_bodies)} 份技能正文点名的工具"
                      f"都在真实的 {len(known)} 个工具里")
        except Exception as exc:  # noqa: BLE001 - 检查本身不该拖垮启动
            _warn(f"一致性检查未能完成（忽略）：{exc}")

    async def _resume_guarded(run_id: str, user: str, conv: str) -> None:
        try:
            await agent.resume(run_id, user, conv)
        except Exception as exc:  # noqa: BLE001
            _warn(f"恢复 run={run_id} 失败：{exc}")

    async def _shutdown() -> None:
        if team_obj is not None:
            await team_obj.shutdown()
        for client in mcp_clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await storage.close()
        except Exception as exc:  # noqa: BLE001
            _warn(f"存储层关闭异常（进程即将退出，忽略）：{exc}")

    app = create_app(
        agent,
        mq,
        topic=topic,
        heartbeat=heartbeat,
        static_dir=static_dir,
        on_startup=_startup,
        on_shutdown=_shutdown,
        storage=storage,
        max_upload_mb=max_upload_mb,
        upload_ttl_sec=upload_ttl_sec,
        upload_sweep_sec=upload_sweep_sec,
        fetch_policy=fetch_policy,
        skill_loader=skill_loader,
        editing_contract=editing_contract,
        broadcast_url=broadcast_url or None,
        broadcast_channel=broadcast_channel,
    )
    return Runtime(
        agent=agent,
        mq=mq,
        heartbeat=heartbeat,
        cron=scheduler,
        registry=registry,
        app=app,
        team=team_obj,
        checkpoint=checkpoint,
        skill_loader=skill_loader,
        storage=storage,
        plan_gate=plan_gate,
        mcp_clients=mcp_clients,
        startup=_startup,
        shutdown=_shutdown,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="智能创作助手 · 服务层装配（FastAPI + MQ + Heartbeat）")
    p.add_argument("--mq", default=os.getenv("MQ_BACKEND", "memory"), choices=["memory", "kafka"])
    p.add_argument("--bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"))
    p.add_argument("--storage", default=os.getenv("STORAGE_BACKEND", "pg_minio"),
                   choices=["pg_minio", "memory"],
                   help="pg_minio=上线存储层（PG 元数据 + MinIO 媒体字节，读 .env 五项配置）；"
                        "memory=内存替身，仅供离线测试/演示")
    p.add_argument("--broadcast-redis-url", default=os.getenv("BROADCAST_REDIS_URL", ""),
                   help="多副本回投广播：OutBound 帧经该 Redis 频道扇出到每个实例的 WS 注册表；"
                        "留空 = 单副本直连（帧只推本进程登记的连接，别的副本收不到）")
    p.add_argument("--broadcast-channel",
                   default=os.getenv("BROADCAST_REDIS_CHANNEL", OUTBOUND_CHANNEL))
    p.add_argument("--cache-root", default=os.getenv("OBJECT_CACHE_ROOT"),
                   help="localize 内容缓存根目录（默认 .runtime/object_cache，可随时删光）")
    p.add_argument("--workspace-root", default=os.getenv("WORKSPACE_ROOT"),
                   help="渲染临时工作区根目录（默认 .runtime/workspace，可随时删光）")
    p.add_argument("--workspace-max-gb", type=float,
                   default=float(os.getenv("WORKSPACE_MAX_GB", "20")),
                   help="localize 内容缓存 LRU 容量上限（GB，spec §6）")
    p.add_argument("--workspace-ttl-sec", type=float, default=WORKSPACE_TTL_SEC,
                   help="会话工作区过期回收线（秒），启动时扫掉早于此的遗留目录（默认 21600）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--skills-dir", default=None,
                   help="技能导入源目录（默认 examples/skills；启动时刷进 PG skills 表 + MinIO 附件）")
    p.add_argument("--no-skills", action="store_true", help="禁用技能系统")
    p.add_argument("--no-memory", action="store_true",
                   help="禁用长期记忆（默认启用，内容在 PG memories 表按用户归属）")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="禁用崩溃恢复 checkpoint（一致点快照存 PG checkpoints 表）")
    p.add_argument("--no-resume", action="store_true", help="启动时不自动恢复未完成执行")
    p.add_argument("--instance-id", default=None,
                   help="本实例标识（多副本崩溃恢复认领权用）；默认 hostname-pid")
    p.add_argument("--approve-tools", default="",
                   help="执行前需人工审批的工具名（逗号分隔，HITL 断点）；空=不启用")
    p.add_argument("--quota-limit", type=int, default=0,
                   help="每用户配额上限（token 数，按 --quota-window 统计）；0=不限")
    p.add_argument("--quota-window", type=int, default=86400,
                   help="配额统计窗口（秒），默认 86400=24 小时")
    p.add_argument("--mcp-config", default=None,
                   help="通用 MCP 配置文件（默认 mcp.json，存在即接入）")
    p.add_argument("--no-mcp", action="store_true", help="禁用通用 MCP 接入")
    p.add_argument("--storyline-config", default=None,
                   help="Storyline config.toml（默认 examples/storyline/config.toml；剪辑节点唯一来源，未连通即无剪辑能力）")
    p.add_argument("--no-storyline", action="store_true", help="禁用 Storyline 远程节点探测")
    p.add_argument("--no-team", action="store_true", help="禁用 Agent Team 工具（消息/任务/常驻子 Agent）")
    p.add_argument("--max-context-tokens", type=int, default=DEFAULT_MAX_CONTEXT_TOKENS,
                   help="上下文压缩阈值 token（<=0 表示不压缩）")
    p.add_argument("--heartbeat-interval", type=int, default=30)
    # 默认值与 build_runtime 的 max_iterations=40 对齐：原先这里是 20、build_runtime 是 40，
    # 而 main() 用 args 覆盖 → 实际永远跑 20，比函数默认值更小。整链剪辑（切镜头 → 理解 →
    # 筛选 → 分组 → 文案 → 配音 → 时间线 → 渲染，再加轮询与纠错）十几步工具调用、每步一轮
    # LLM，20 轮必然在半路耗尽，用户看到的是「[已达最大迭代次数 20，提前结束]」。
    p.add_argument("--max-iterations", type=int, default=40,
                   help="单次请求内 Agent 的最大迭代轮数；整链剪辑（十余次工具调用）需调大")
    p.add_argument("--no-render-gate", dest="gate_render", action="store_false",
                   help="关掉「渲染前确认」：编排完直接渲染，不再先给用户看编排结果")
    p.set_defaults(gate_render=True)
    p.add_argument("--max-upload-mb", type=int, default=1024,
                   help="单条素材大小上限（MB）：/upload 与分片续传（/upload/init·complete）都按它拒")
    p.add_argument("--upload-ttl-sec", type=float, default=uploads.DEFAULT_SESSION_TTL_SEC,
                   help="分片上传会话静置多久视为放弃（秒）；到期由后台清扫删净桶里的分片")
    p.add_argument("--upload-sweep-sec", type=float, default=600.0,
                   help="分片清扫的轮询周期（秒）；<=0 完全不起清扫任务（含启动那一次）")
    p.add_argument("--max-fetch-mb", type=int, default=int(os.getenv("MAX_FETCH_MB", "512")),
                   help="按链接取料（/fetch_media、fetch_media 工具）单条素材大小上限（MB）")
    p.add_argument("--no-ytdlp", action="store_true",
                   help="禁用 yt-dlp 兜底解析：只认直链与页面里的 <video>/og:video 地址")
    p.add_argument(
        "--static-dir",
        default=str(Path(__file__).parent / "frontend" / "dist"),
        help="前端构建产物目录；不存在时只提供 API。设空字符串可禁用托管。",
    )
    return p.parse_args()


def main() -> None:
    import uvicorn

    args = parse_args()
    print(f"装配中：MQ={args.mq} storage={args.storage}")
    runtime = build_runtime(
        mq_backend=args.mq,
        bootstrap_servers=args.bootstrap_servers,
        storage_backend=args.storage,
        cache_root=args.cache_root or None,
        workspace_root=args.workspace_root or None,
        cache_max_gb=args.workspace_max_gb,
        workspace_ttl_sec=args.workspace_ttl_sec,
        skills_dir=False if args.no_skills else args.skills_dir,
        use_memory=not args.no_memory,
        use_checkpoint=not args.no_checkpoint,
        auto_resume=not args.no_resume,
        instance_id=args.instance_id,
        approve_tools=args.approve_tools,
        quota_limit=args.quota_limit,
        quota_window=args.quota_window,
        mcp_config=False if args.no_mcp else args.mcp_config,
        storyline_config=False if args.no_storyline else args.storyline_config,
        team=False if args.no_team else None,
        max_context_tokens=args.max_context_tokens,
        heartbeat_interval=args.heartbeat_interval,
        max_iterations=args.max_iterations,
        static_dir=args.static_dir or None,
        max_upload_mb=args.max_upload_mb,
        upload_ttl_sec=args.upload_ttl_sec,
        upload_sweep_sec=args.upload_sweep_sec,
        fetch_policy=FetchPolicy(max_mb=args.max_fetch_mb,
                                 allow_ytdlp=not args.no_ytdlp),
        broadcast_url=args.broadcast_redis_url or None,
        broadcast_channel=args.broadcast_channel,
    )
    print(f"已注册工具: {runtime.registry.tool_names}")
    print(f"MQ 后端: {args.mq} | 存储后端: {runtime.storage.backend} | 心跳间隔: {args.heartbeat_interval}s")
    print(f"实例标识: {args.instance_id or '(自动 hostname-pid)'}（崩溃恢复按它认领 run）")
    if args.approve_tools:
        print(f"HITL 审批断点：执行前需人工审批的工具 = {args.approve_tools}")
    if args.quota_limit > 0:
        print(f"配额闸：每用户 {args.quota_limit} token / {args.quota_window} 秒，超限拦截")
    else:
        print("配额闸：关闭（token 用量仍记录到 token_usage 表，不拦截）")
    if args.broadcast_redis_url:
        mode = f"经 Redis {args.broadcast_redis_url} 频道 {args.broadcast_channel}（多副本）"
    else:
        mode = "关闭（单副本直连：OutBound 帧只推本进程登记的 WS 连接）"
    print(f"回投广播: {mode}")
    print(f"监听 http://{args.host}:{args.port}  (POST /register, POST /chat, POST /chat/sync, "
          f"POST /upload, POST /fetch_media, WS /ws/{{conversation_id}}?token=…, "
          f"GET /health, GET /sessions, GET /whoami, GET /convs)")
    print(f"按链接取料：上限 {args.max_fetch_mb}MB | yt-dlp 兜底 "
          f"{'启用' if not args.no_ytdlp else '已禁用（--no-ytdlp）'}")
    uvicorn.run(runtime.app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
