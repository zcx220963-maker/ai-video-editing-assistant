"""Storyline MCP Server：把真实剪辑节点暴露为 MCP 工具（streamableHttp）。

对应设计文档「MCP 和 Skill 设计」与流程图①：
- wrapper/register 模板：每个节点包一层 async handler 注册为 MCP tool，工具名/描述/
  inputSchema 直接取节点的 DAG 契约（不另起炉灶）。
- 请求进入后先解析 session 身份（X-Storyline-Session-Id 头，缺省回落参数/默认会话），
  从 artifacts 表重建该会话的 Store（进程重启也能续上），再交给**服务端 Interceptor**
  ——即 agent_framework.orchestration.Interceptor——按 required_nodes 递归补齐依赖。
- 运行：python -m storyline_server.server --config examples/storyline/config.toml
"""

from __future__ import annotations

import argparse
import inspect
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

from agent_framework.orchestration import (
    ArtifactStore,
    Interceptor,
    NodeState,
    topological_order,
)
from agent_framework.storage import Storage, build_storage

from .nodes.core_nodes import build_real_registry
from .nodes.core_nodes import _flag_on
from .providers import build_providers
from .render_jobs import (STALL_CHECK_SEC, TERMINAL, RenderDispatcher, tool_view)
from .settings import Settings

SESSION_HEADER = "X-Storyline-Session-Id"
DEFAULT_SESSION = "storyline:default"
HANGING_TIMEOUT_SEC = 30 * 60   # spec §9：running 超 30 分钟视为进程崩溃遗留
# 分钟级耗时的节点：改成「提交 + 轮询」，不让一次 JSON-RPC 往返等一部片子。
# 出片与局部改都在这一集合里——局部差的那一版通常只重烧几镜，但「几镜」也可能是
# 全部（用户改了全局字号那类），按最坏耗时归类。后续若 ASR/VL 批也顶不住，加进来即可。
LONG_RUNNING_NODES = frozenset({"render_video", "render_motion_video",
                                "patch_motion_video"})

_JSON_TYPE_MAP = {"string": str, "integer": int, "number": float,
                  "boolean": bool, "array": list, "object": dict}


class StorylineServer:
    """FastMCP 实例 + 节点注册表 + Interceptor 的装配体（测试可直接调用 handler）。"""

    def __init__(self, settings: Settings, storage: Storage | None = None) -> None:
        self.settings = settings
        self.providers = build_providers(settings.caps)
        self.storage = storage or build_storage(
            settings.storage.backend,
            cache_root=settings.storage.cache_root,
            workspace_root=settings.storage.workspace_root,
            cache_max_gb=settings.storage.workspace_max_gb)
        self.registry = build_real_registry(settings, self.providers, self.storage,
                                            settings.server.available_nodes or None)
        self.interceptor = Interceptor(self.registry)
        # 长耗时节点（渲染）的后台执行位：并发上限是配置项，不靠"反正超时会重试"兜
        self.renders = RenderDispatcher(
            self.storage, max_concurrent=settings.caps.render_max_concurrent,
            stall_sec=settings.caps.render_stall_sec)
        # 启停只该做一次，但 mcp SDK 把 lifespan 挂在 low-level Server 上（每会话跑一遍）
        # ——闩在进程这边，会话重连不重做对账、也不拆掉在跑的活儿。
        self._booted = False
        self.mcp = FastMCP(
            settings.server.server_name,
            host=settings.server.host,
            port=settings.server.port,
            streamable_http_path=settings.server.path,
            json_response=settings.server.json_response,
            stateless_http=settings.server.stateless_http,
            lifespan=self._lifespan,
        )
        self.handlers: dict[str, Any] = {}
        self._register_tools()
        self._register_read_node_history()
        self._register_render_status()
        self._register_dag_contract()

    async def _boot(self) -> None:
        """启动即校验存储层（spec §9）：连不上就让 server 起不来，不带病服务。

        校验通过后做一次孤儿对账：崩溃遗留的 running 渲染任务判死、过期工作区目录扫掉。

        必须晚于任何 asyncio.run：asyncpg 的连接池绑死创建它的事件循环，提前起的池在
        服务循环里必炸——所以启停由 ASGI 的 lifespan 触发，且用 ``_booted`` 闩住，
        同一个进程里「N 个客户端会话」只对应「1 次对账」。
        """
        if self._booted:
            return
        self._booted = True
        try:
            await self.storage.start()
            # 素材登记进 materials.owner_user_id（外键）：owner 可能是 cron / 缺省作用域，
            # 一次性登记内部身份，写入路径就不再自建用户行
            await self.storage.provision_internal()
            hung = await self.storage.render_jobs.reap_hanging(HANGING_TIMEOUT_SEC)
            stale = self.storage.workspace.sweep_stale(self.settings.storage.workspace_ttl_sec)
            if hung or stale:
                print(f"[storyline] 启动对账：悬挂渲染任务置 failed {hung} 条，"
                      f"过期工作区文件回收 {stale} 个")
            self.renders.start_watchdog()
            stall = self.settings.caps.render_stall_sec
            print("[storyline] 渲染停滞看门狗："
                  + ("已关闭（render_stall_sec=0）" if stall <= 0 else
                     f"每 {STALL_CHECK_SEC:g}s 查一次，连续 {stall:g}s 无进度即按失败收口"))
        except Exception:
            self._booted = False   # 起不来就让它下次还能再来一次（而不是永久谎报已启动）
            raise

    async def _shutdown(self) -> None:
        if not self._booted:
            return            # 没收过口：拆第二次只会把已关的池再关一遍，或拆掉别人开的池
        self._booted = False
        # 后台渲染先收：连接池关掉之后还在写 render_jobs 的任务只会留下谎报的行
        await self.renders.stop_watchdog()
        await self.renders.cancel_all()
        await self.storage.close()

    @asynccontextmanager
    async def _lifespan(self, _app: Any) -> Any:
        """交给 low-level Server 的钩子：实际语义是「每会话」，因此只负责补一次启动。

        ``finally`` 里刻意不收尾——主服务重启会关掉旧 MCP 会话，那一刻正在跑的渲染不该
        被连带取消、连接池不该被关（别的会话还在用）。进程退出时的收尾走 ASGI lifespan
        （见 ``run()`` 里的 ``_mount_process_lifespan``）；崩溃场景交给下次启动的
        ``reap_hanging``。
        """
        await self._boot()
        yield {}

    # ---- 文档 wrapper/register 模板 ----

    async def _make_state(self, session_id: str, artifact_id: str) -> NodeState:
        repo = self.storage.artifacts(session_id, artifact_id)
        store = await ArtifactStore.open(repo, session_id, artifact_id)
        return NodeState(session_id=session_id, artifact_id=artifact_id, store=store)

    def _register_tools(self) -> None:
        for node in self.registry.all():
            self._register_one(node)

    def _register_one(self, node) -> None:
        server = self

        async def handler(ctx: Context, **kwargs: Any) -> str:
            args = {k: v for k, v in kwargs.items() if v is not None}
            sid = DEFAULT_SESSION
            try:  # 请求拦截①：从传输层找回会话身份
                request = ctx.request_context.request
                if request is not None:
                    sid = request.headers.get(SESSION_HEADER, "") or sid
            except Exception:
                pass  # 无请求上下文（stdio / 直调）时回落
            sid = str(args.pop("session_id", "") or sid)
            artifact_id = str(args.pop("artifact_id", "") or "")
            # 身份由主 Agent 的 MCP 包装层注入（模型自报的值会被它覆写）
            uid = str(args.pop("user_id", "") or "")
            cid = str(args.pop("conversation_id", "") or "")
            if sid == DEFAULT_SESSION and uid:
                # 会话作用域取注入身份，不能跟 user_request 走：模型逐轮改写请求文本，
                # 作用域随之漂移会让后续节点看不到上游产物、补齐时重跑根节点而报缺参。
                sid = f"u:{uid}:c:{cid}"
            elif sid == DEFAULT_SESSION and args.get("user_request"):
                sid = f"storyline:{args['user_request'][:16]}"  # 匿名调用也彼此隔离
            state = await server._make_state(sid, artifact_id)
            state.user_request = str(args.get("user_request") or "")
            state.user_id = uid
            state.conversation_id = cid
            state.lang = str(args.pop("lang", "zh") or "zh")
            state.mode = str(args.pop("mode", "auto") or "auto")
            if node.name in LONG_RUNNING_NODES:
                wait_sec = args.pop("wait_sec", None)
                # dry-run 不走「提交 + 轮询」：submit 会先落一条 queued 任务行，而 dry-run
                # 的节点体刻意不开任务行、也不推进度——那一行于是永远停在 queued（界面显示
                # 「正在出片」、看门狗按停滞判死），而它真正该回的出片计划账被进度视图盖掉。
                dry = args.get("dry_run")
                if dry is None and "dry_run" in (node.input_schema.get("properties") or {}):
                    # 只有**声明了这个参数**的节点才认跨节点兜底：``state.flags`` 会累积
                    # 本会话每一次调用的全部入参，先前一次 render_video(dry_run=true)
                    # 留下的开关，不该让没有 dry_run 参数的出片节点也走内联执行——
                    # 那条路不开后台执行位，一次调用等一部片子，等于把超时请回来。
                    dry = state.flags.get("dry_run")
                if _flag_on(dry):
                    result = await server.interceptor.invoke(node.name, state, **args)
                    return json.dumps(result, ensure_ascii=False)
                return await server._invoke_long(node, state, args, wait_sec)
            result = await server.interceptor.invoke(node.name, state, **args)
            return json.dumps(result, ensure_ascii=False)

        # 让 FastMCP 按节点契约（而非 Python 签名）生成 inputSchema
        handler.__signature__ = self._signature_for(node)
        handler.__name__ = node.name
        deps = f"（需先完成：{', '.join(node.required_nodes)}）" if node.required_nodes else ""
        self.mcp.tool(name=node.name, title=node.display_name,
                      description=f"{node.description}{deps}")(handler)
        self.handlers[node.name] = handler

    async def _invoke_long(self, node, state: NodeState, args: dict[str, Any],
                           wait_sec: Any) -> str:
        """长耗时节点：提交到后台执行位，内联等一小段时间，没跑完就回任务视图。

        内联等待被 ``render_wait_max_sec`` 夹住——它兜的是「调用方要了一次比传输超时
        还长的等待」这种把阻塞换个数字的用法。
        """
        caps = self.settings.caps
        try:
            grace = float(wait_sec) if wait_sec is not None else caps.render_grace_sec
        except (TypeError, ValueError):
            grace = caps.render_grace_sec
        grace = max(0.0, min(grace, caps.render_wait_max_sec))
        snap = await self.renders.submit(
            state.session_id, state.artifact_id,
            lambda: self.interceptor.invoke(node.name, state, **args))
        if snap.get("status") not in TERMINAL and grace > 0:
            snap = await self.renders.wait(state.session_id, state.artifact_id, grace)
        return json.dumps(tool_view(snap, node=node.name), ensure_ascii=False)

    @staticmethod
    def _signature_for(node) -> inspect.Signature:
        """inputSchema 按节点契约生成：节点自有参数 + 会话公共参数（无杂散 kwargs 字段）。

        真实函数仍带 **kwargs——FastMCP 只把校验过的声明参数传进来，未知键被忽略，
        所以声明必须穷传参入口；Common 键（session_id/artifact_id/…）保证兜底路由可用。
        """
        params = [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                    annotation=Context)]
        props = dict((node.input_schema or {}).get("properties", {}))
        for cname in ("session_id", "artifact_id", "user_request", "lang", "mode",
                      "user_id", "conversation_id"):
            props.setdefault(cname, {"type": "string"})
        for pname, spec in props.items():
            jtype = spec.get("type") if isinstance(spec, dict) else None
            base = _JSON_TYPE_MAP.get(jtype if isinstance(jtype, str) else "", Any)
            anno = Optional[base] if base is not Any else Any
            params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY,
                                            default=None, annotation=anno))
        return inspect.Signature(params, return_annotation=str)

    def _register_dag_contract(self) -> None:
        """机器可读的 DAG 契约：节点 → 前置依赖 + reducer（+ 拓扑序供观测）。

        主服务据此给每个远程节点工具声明读写集（并发分批）与「重跑某节点要作废哪些
        后继产物」（分叉重跑）。以前这些只能从描述里的「（需先完成：…）」正则抠回来——
        契约写成散文再解析，改文案就断。
        """
        server = self

        async def dag_contract(ctx: Context) -> str:
            return json.dumps({
                "nodes": {n.name: {"requires": n.upstream, "reducer": n.reducer,
                                   "display": n.display_name,
                                   # 会不会被主服务自动补齐：主服务的计划门要在**出卡**
                                   # 阶段就知道「漏了哪个前置会导致执行注定失败」，所以
                                   # 这个标记必须随契约走，不能只留在服务端实现里。
                                   "explicit": bool(getattr(
                                       n, "require_explicit_call", False)),
                                   "params": dict((n.input_schema or {}).get(
                                       "properties", {}))}
                          for n in server.registry.all()},
                "order": topological_order(server.registry),
            }, ensure_ascii=False)

        self.mcp.tool(name="dag_contract",
                      description="返回剪辑节点 DAG 的结构化契约（节点→前置依赖与合并语义）")(
            dag_contract)
        self.handlers["dag_contract"] = dag_contract

    def _resolve_session(self, ctx: Context, session_id: str, user_id: str,
                         conversation_id: str) -> str:
        """控制面工具的会话找回：显式 > 请求头 > 注入身份 > 匿名（与节点工具同规则）。"""
        sid = session_id or DEFAULT_SESSION
        if sid == DEFAULT_SESSION:
            try:
                request = getattr(getattr(ctx, "request_context", None), "request", None)
                if request is not None:
                    sid = request.headers.get(SESSION_HEADER, "") or sid
            except Exception:
                pass  # 无请求上下文（stdio / 直调）
        if sid == DEFAULT_SESSION and user_id:
            sid = f"u:{user_id}:c:{conversation_id}"
        return sid if sid != DEFAULT_SESSION else "storyline:anonymous"

    def _register_render_status(self) -> None:
        """渲染任务的轮询口：一次调用不再等一部片子，进度与成片都按 (会话, 产物) 查。"""
        server = self

        async def render_status(ctx: Context, artifact_id: str = "",
                                session_id: str = "", user_id: str = "",
                                conversation_id: str = "") -> str:
            sid = server._resolve_session(ctx, session_id, user_id, conversation_id)
            snap = await server.renders.snapshot(sid, artifact_id)
            return json.dumps(tool_view(snap), ensure_ascii=False)

        self.mcp.tool(
            name="render_status",
            title="渲染进度查询",
            description=("查询出片节点（render_video / render_motion_video /"
                         " patch_motion_video）提交的渲染任务："
                         "返回 status/stage/percent，"
                         "status=done 时同批回成片对象键、现签播放直链与时长（与直接渲染同形状），"
                         "status=failed 时回错误原因。渲染未完成就接着轮询，不要提前收尾"))(
            render_status)
        self.handlers["render_status"] = render_status

    def _register_read_node_history(self) -> None:
        """CAPABILITY 技能用的 Store 读取工具（与主 Agent 侧同名，服务端同样提供）。"""
        server = self

        async def read_node_history(ctx: Context, key: str,
                                    session_id: str = "", artifact_id: str = "",
                                    user_id: str = "", conversation_id: str = "") -> str:
            sid = server._resolve_session(ctx, session_id, user_id, conversation_id)
            state = await server._make_state(sid, artifact_id)
            store = state.store
            if not await store.has(key):
                return json.dumps({"error": f"Store 中还没有 {key}",
                                   "executed": await store.executed()}, ensure_ascii=False)
            return json.dumps({"node": key, "payload": await store.get(key)},
                              ensure_ascii=False)

        self.mcp.tool(name="read_node_history",
                      title="读取节点产物",
                      description="按节点名读取该节点已产出的结果（服务端 Store 数据总线）")(
            read_node_history)
        self.handlers["read_node_history"] = read_node_history

    # ---- 启动 ----

    def _mount_process_lifespan(self) -> None:
        """把启停挂到 ASGI app 的**进程级** lifespan 上（uvicorn 拉起/关掉各一次）。

        mcp SDK 只用 ``FastMCP(lifespan=...)`` 喂 low-level Server，而后者由 streamable
        HTTP 的 session manager **每个客户端会话**跑一遍；且 ``run_streamable_http_async``
        服务时现场新建 Starlette app。两个后果：「启动即校验存储层」要等到第一个客户端连上
        才发生（进程带着坏存储照常监听端口，spec §9 的口径没了），而 finally 的拆除会在主服务
        重启关掉旧会话那一刻杀掉正在跑的渲染。所以要改的是那台 app 的**工厂**，不是手上实例。
        """
        build_app = self.mcp.streamable_http_app
        if getattr(build_app, "_storyline_mounted", False):
            return

        def mounted() -> Any:
            app = build_app()
            run_sessions = app.router.lifespan_context  # lambda app: session_manager.run()

            @asynccontextmanager
            async def _process(_app: Any) -> Any:
                await self._boot()
                try:
                    async with run_sessions(_app):
                        yield
                finally:
                    await self._shutdown()

            app.router.lifespan_context = _process
            return app

        mounted._storyline_mounted = True
        self.mcp.streamable_http_app = mounted

    def run(self) -> None:
        order = topological_order(self.registry)
        print(f"[storyline] nodes={len(self.registry.names())} tools={len(self.handlers)}")
        print(f"[storyline] storage={self.storage.backend}")
        print(f"[storyline] DAG 拓扑序: {' -> '.join(order)}")
        print(f"[storyline] streamable-http on {self.settings.server.host}:"
              f"{self.settings.server.port}{self.settings.server.path}")
        self._mount_process_lifespan()
        self.mcp.run(transport="streamable-http")


def make_server(settings: Settings | None = None,
                storage: Storage | None = None) -> StorylineServer:
    return StorylineServer(settings or Settings.load(), storage)


def main() -> None:
    parser = argparse.ArgumentParser(description="Storyline 视频剪辑 MCP Server")
    parser.add_argument("--config", default="examples/storyline/config.toml")
    a = parser.parse_args()
    settings = Settings.load(Path(a.config))
    # 把 HTTP 绑定端口/路径同步进 FastMCP（Settings.load 已填，run 时读取）
    make_server(settings).run()


if __name__ == "__main__":
    main()
