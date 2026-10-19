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
import hashlib
import inspect
import json
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Union

from mcp.server.fastmcp import Context, FastMCP

from agent_framework.orchestration import (
    ArtifactStore,
    Interceptor,
    NodeState,
    topological_order,
)
from agent_framework.storage import Storage, build_storage, to_ref
from agent_framework import style_templates as style_tpl

from .motion import caption_preview as cpreview
from .motion import code_spec as cspec
from .motion import remotion_client as rc
from .motion import selfcheck as sc
from .nodes.core_nodes import build_real_registry
from .nodes.core_nodes import _flag_on, new_patch_scope_id
from .providers import build_providers
from .render_jobs import (STALL_CHECK_SEC, TERMINAL, RenderDispatcher, tool_view)
from .settings import Settings

SESSION_HEADER = "X-Storyline-Session-Id"
DEFAULT_SESSION = "storyline:default"
HANGING_TIMEOUT_SEC = 30 * 60   # spec §9：running 超 30 分钟视为进程崩溃遗留
# 分钟级耗时的节点：改成「提交 + 轮询」，不让一次 JSON-RPC 往返等一部片子。
# 出片与局部改都在这一集合里——局部差的那一版通常只重烧几镜，但「几镜」也可能是
# 全部（用户改了全局字号那类），按最坏耗时归类。后续若 ASR/VL 批也顶不住，加进来即可。
LONG_RUNNING_NODES = frozenset({"render_video", "render_motion_code",
                                "patch_motion_code", "patch_video"})

_JSON_TYPE_MAP = {"string": str, "integer": int, "number": float,
                  "boolean": bool, "array": list, "object": dict}

#: 试听用的那一句。**固定**（理由见 `_register_voice_preview`）。带数字与标点：
#: 电话号码、逗号与句号处的停顿、以及「一」「不」的变调，正是音色之间差别最大的地方，
#: 而「你好」听不出男声女声以外的任何信息。改这一句会让缓存键自然翻新（键里含它的哈希），
#: 所以改话术不需要谁去清缓存。
VOICE_PREVIEW_TEXT = "这是一段试听，用来比较不同音色朗读中文的效果：每分钟大约 240 个字。"

#: 音色名的合规形状（与 `code_spec._VOICE_RE` 同一个形状）。这里再写一次而不是 import
#: 那一份：那一份是**契约层**的（决定什么能进 spec），这一份是**试听口**的输入校验，
#: 两者约束的是不同的东西，合并不划算。形状一致由 tests/test_storyline_server.py 断言。
_VOICE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,})?-[A-Za-z]+Neural$")


def _json_default(obj: Any) -> Any:
    """`json.dumps` 撞上不认识的类型时的**唯一**一次兜底。

    只认集合。集合同 `sorted(...)` 的列表——顺序稳定，两次调用、两个进程之间可比。
    **其余类型照旧抛** `TypeError`：兜底该兜的是「内部算得对、只是形状进不了 JSON」
    （集合就是这一类），不是「什么都别报」。把一切 `str()` 掉，等于让一个真坏掉的对象
    悄悄变成一串谁都读不懂的文本——那正是这仓库反复点名的「看着还在、其实没了」。
    """
    if isinstance(obj, (set, frozenset)):
        return sorted(str(x) for x in obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def tool_json(value: Any) -> str:
    """工具返回值的**唯一出口**：所有 handler 都从这儿把话交出去。

    **为什么值得一条出口。** 原来这里散着十来处手写的 `json.dumps(result,
    ensure_ascii=False)`。散着写的时候，「返回值里有没有一个进不了 JSON 的东西」是每个
    工具**各自**的事，谁也没测过——2026-10-16 真机就栽在一处：`check_film` 回的整片
    风格账里 `all_channels[sid]` 是个集合，于是**每次**调用都回
    `Error: Object of type set is not JSON serializable`（run 9272c7f59501，
    seq 41/50/53/59，模型连试四次才放弃）。判据一个字没错，工具却是死的；而离线用例
    全绿，因为它只比返回值里的字符串列表，**从来没把这个返回值真的序列化过一次**。

    `default=` 只在不认识的**叶子对象**上被调（dict / list 由 json 自己走），所以这层
    不复制任何结构遍历——它只补最后那一跳。与 `motion/selfcheck.py::_jsonable` 是**两层、
    各管各的一段**，不是两处各算一遍：那一层掰直的是**某一个内部分析函数的返回形状**
    （集合在那里是自然的），这一层兜的是**整条工具面**——今天以后新写的工具，
    返回值里混进一个集合也不会再让整个工具变成死的。
    """
    return json.dumps(value, ensure_ascii=False, default=_json_default)


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
        self._register_voice_preview()
        self._register_caption_preview()
        self._register_selfcheck()

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
            # 模板库：种子只补缺；已发布模板推给渲染容器（失败只打印，用到时补推）。
            try:
                added = await style_tpl.seed_system_templates(self.storage)
                pushed = await style_tpl.sync_validated(self.storage)
                if added or pushed.get("pushed") or pushed.get("failed"):
                    print(f"[storyline] 模板库：新增种子 {added} 条，推送 "
                          f"{pushed.get('pushed')} 条"
                          + (f"，失败 {pushed['failed']}" if pushed.get("failed") else ""))
            except Exception as exc:  # noqa: BLE001 - 模板库同步不该拦服务启动
                print(f"[storyline] 模板库同步失败（沿用库里已有内容）：{exc}")
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
            # 风格包只在声明了它的节点（今天只有 plan_motion）上出现；其余节点
            # args 里根本没有这个键，pop 的缺省空串不会改变任何既有行为。
            style_skill = str(args.pop("style_skill", "") or "")
            if sid == DEFAULT_SESSION and uid:
                # 会话作用域取注入身份，不能跟 user_request 走：模型逐轮改写请求文本，
                # 作用域随之漂移会让后续节点看不到上游产物、补齐时重跑根节点而报缺参。
                sid = f"u:{uid}:c:{cid}"
            elif sid == DEFAULT_SESSION and args.get("user_request"):
                sid = f"storyline:{args['user_request'][:16]}"  # 匿名调用也彼此隔离
            if node.name in LONG_RUNNING_NODES and getattr(node, "forks_artifact_scope", False):
                # 预发分叉作用域号：执行位（``renders.submit``）在**提交那一刻**就要决定渲染
                # 任务行开在哪个 artifact_id 上，而节点体要到分叉那一步才产生新号。模型调
                # 局部改通常不带 artifact_id（带了也常是 base 那一版的号），于是任务行开在
                # ``_default``＝base 上：base 的出片账被抹回 queued、终态却写在新版行里，
                # 轮询端等不到终态、看门狗把一次成功的局部改按停滞判死。
                # 前端那条路早就自己做这件事（提交前就分配好产物号），这里补的是 MCP 入口。
                base_art = str(args.get("base_artifact_id") or "")
                if artifact_id in ("", "_default") or artifact_id == base_art:
                    artifact_id = new_patch_scope_id()
            state = await server._make_state(sid, artifact_id)
            state.user_request = str(args.get("user_request") or "")
            state.user_id = uid
            state.conversation_id = cid
            state.style_skill = style_skill
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
                    return tool_json(result)
                return await server._invoke_long(node, state, args, wait_sec)
            result = await server.interceptor.invoke(node.name, state, **args)
            return tool_json(result)

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
        return tool_json(tool_view(snap, node=node.name))

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
            # type 可以是联合（如 ["string", "number"]）：逐项映射后取 Union，
            # 否则 FastMCP 会按单一 string 校验，模型传 0.4 这类数字直接被 pydantic 拒掉。
            if isinstance(jtype, list):
                members = tuple(dict.fromkeys(
                    _JSON_TYPE_MAP.get(str(t), Any) for t in jtype))
                base = Union[members] if members else Any
            else:
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
            return tool_json({
                "nodes": {n.name: {"requires": n.upstream, "reducer": n.reducer,
                                   "display": n.display_name,
                                   # 这个节点的参数是不是「创作决策、本该由调用方给」。
                                   # 主服务的计划门据此在**出卡**阶段提一句「写进去才由
                                   # 你定参数」，工具说明也据此附一行「哪个前置的选择权
                                   # 还没定」——所以这个标记必须随契约走，不能只留在
                                   # 服务端实现里。（2026-10-16 之前它还是一道硬判据：
                                   # 漏了就打回。撤了，见 orchestration.Interceptor。）
                                   "explicit": bool(getattr(
                                       n, "require_explicit_call", False)),
                                   "params": dict((n.input_schema or {}).get(
                                       "properties", {}))}
                          for n in server.registry.all()},
                "order": topological_order(server.registry),
                # 「文字库」的取值清单（命名字幕样式档 + 音色分组 + 各维枚举与中文标签）。
                # 它不是节点契约，但走同一条通道：都由**剪辑服务端**声明、只读、一次带回。
                # 前端要这份东西才能把面板画出来，而它自己不许列第二遍取值——多一个拉取口
                # 就多一次「连上了但那份没取到」的半截状态，所以并进这一份。
                "text_library": cspec.library_props(),
            })

        self.mcp.tool(name="dag_contract",
                      description="返回剪辑节点 DAG 的结构化契约（节点→前置依赖与合并语义）")(
            dag_contract)
        self.handlers["dag_contract"] = dag_contract

    def _register_voice_preview(self) -> None:
        """试听：用某个音色读一句固定的话，回一个可直接播的对象引用。

        **为什么话术固定、不接用户输入**：这是一个试听口，不是一个合成口。接自由文本
        就等于把「免费 TTS 的额度」交给任何登录用户去刷，而试听要回答的问题只有一个
        ——「这个音色读中文是什么味道」。固定那一句里带着数字与标点（停顿时长是音色
        差异最明显的地方），比一句「你好」有信息量。

        **缓存按内容寻址**：键是 ``voicepreview/{音色}/{语速}/{话术哈希}.mp3``。同一个
        音色第二次点就直接命中，一次网络都不打；改掉话术会自然产生新键，而不是把旧的
        朗读当新的发出去（这正是「缓存键要覆盖内容」那条纪律在极小尺度上的样子）。

        它不是 DAG 节点，没有 ``NodeState``——所以字节不经 ``_keep`` 出版，
        直接用 ``workspace.publish`` 写一个自定的键：试听音频不属于任何一次出片，
        按会话/产物作用域去放反而会在成片被清掉时一起消失。

        **它必须在 ``available_nodes`` 里各占一行**：那一份表同时决定主服务注册表里
        有谁（``enabled_tools`` 就是它），少一行，主服务的 ``/tts/preview`` 会在
        ``reg.execute("preview_voice")`` 这一步找不到工具，而报出来的是
        「剪辑服务还没连上」——口径不对，会把人往连接问题上带。
        """
        server = self

        async def preview_voice(ctx: Context, voice: str = "", rate: str = "") -> str:
            caps = self.settings.caps
            voice = (voice or "").strip() or caps.tts_voice
            rate = (rate or "").strip() or caps.tts_rate
            if not _VOICE_RE.match(voice):
                raise ValueError(f"音色 {voice!r} 不是 edge-tts 的中文音色名")
            if rate not in cspec.RATE_CHOICES:
                raise ValueError(f"语速 {rate!r} 不在可选档位里：{'、'.join(cspec.RATE_CHOICES)}")
            tag = hashlib.sha1(VOICE_PREVIEW_TEXT.encode("utf-8")).hexdigest()[:8]
            key = f"voicepreview/{voice}/{rate.replace('%', 'p')}/{tag}.mp3"
            if await server.storage.objects.head(key) is None:
                with tempfile.TemporaryDirectory() as td:
                    dst = Path(td) / "preview.mp3"
                    await server.providers.tts_timed(VOICE_PREVIEW_TEXT, dst,
                                                     voice=voice, rate=rate)
                    await server.storage.workspace.publish(dst, key,
                                                           content_type="audio/mpeg")
            return tool_json({"ref": to_ref(key), "voice": voice, "rate": rate,
                              "text": VOICE_PREVIEW_TEXT})

        self.mcp.tool(name="preview_voice", title="音色试听",
                      description="试听：用指定音色读一句固定的话，返回可播放的音频引用")(
            preview_voice)
        self.handlers["preview_voice"] = preview_voice

    def _register_caption_preview(self) -> None:
        """字幕样式样张：把**一对**（字幕样式 × 高亮样式）渲成一张静帧，回一个可看的对象引用。

        **为什么值得一条链**：挑字幕样式这件事，光看档名（「大字压屏」「顶部横幅」）
        与一句说明，与真看到那一帧之间差着一整部片子——渲完才发现字幕压在画面正中，
        几分钟已经烧掉了，而改一次就是再烧一遍。样张用的是与成片同一条渲染链
        （同一份样片代码、同一件字幕元件、同一套注入的 props），所以它没有「预览与成片
        不符」的余地：图上那一帧就是成片里的那一帧。

        **一次一对**：2026-10-16 面板重做之后，字幕是一张「字幕样式 × 高亮样式」的网
        （7 × 5），不再是一排可以逐档对比的档。所以这一口收**一对**、渲一张——用户点/看
        哪一对就渲哪一对，渲过的那一对按内容寻址留在库里。

        **色板是入参**：成片里每一镜跟着它自己的色板走，而样张要回答的是「这一对在
        深海蓝下长什么样」——所以调用方点一套色板，这一张就按那套渲。空串是「默认」，
        即 `tokens.ts` 的底色与按亮度二档的字幕配色。

        **缓存按内容寻址**：键是 `captionpreview/{母版哈希}/{色板}/{字幕}+{高亮}.jpg`。
        第二次点同一对直接命中，一次渲染都不打；零件换了（哈希变）会自然落到
        新键上，而不是把旧零件渲出来的样张当新的发出去。

        它不是 DAG 节点，没有 ``NodeState``——字节不经 ``_keep`` 出版，直接
        ``workspace.publish`` 写一个自定的键（与试听音频同规：样张不属于任何一次出片，
        按会话/产物作用域放反而会在成片被清掉时一起消失）。

        **它必须在 ``available_nodes`` 里各占一行**：那一份表同时决定主服务注册表里
        有谁（``enabled_tools`` 就是它），少一行，主服务的 ``/motion/caption-preview``
        会在 ``reg.execute("preview_caption")`` 这一步找不到工具，而报出来的是
        「剪辑服务还没连上」——口径不对，会把人往连接问题上带。
        """
        server = self

        async def preview_caption(ctx: Context, palette: str = "",
                                  caption_style: str = "",
                                  highlight_style: str = "",
                                  highlight_color: str = "") -> str:
            palette = (palette or "").strip()
            known = cspec.palettes() or {}
            if palette and palette not in known:
                raise ValueError(
                    f"色板 {palette!r} 不在可选清单里：{'、'.join(known) or '（读不到色板表）'}")
            keys = await cpreview.render_one(client=rc.endpoints(),
                                             storage=server.storage,
                                             palette_key=palette,
                                             caption_style=caption_style,
                                             highlight_style=highlight_style,
                                             highlight_color=highlight_color)
            if not keys:
                raise ValueError(
                    "样张没渲出来：渲染服务没起来，或镜像里没带上样片代码"
                    "（remotion/src/preview/caption-sample.tsx）。字幕与音色照常能挑，"
                    "只是这一对暂时看不见样子")
            return tool_json({"palette": palette,
                              "stills": [{"key": k, "ref": to_ref(v)}
                                         for k, v in keys.items()]})

        self.mcp.tool(
            name="preview_caption", title="字幕样式样张",
            description="按【字幕样式 × 高亮样式】渲一张静帧样张，可用色板名指定按哪套色板渲、"
                        "highlight_color 指定高亮词的自定义颜色（空＝跟色板走）；"
                        "两个样式名认不出时落缺省（贴画面 · 关键词换色）。返回可播放的对象引用")(
            preview_caption)
        self.handlers["preview_caption"] = preview_caption

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
            return tool_json(tool_view(snap))

        self.mcp.tool(
            name="render_status",
            title="渲染进度查询",
            description=("查询出片节点（render_video / render_motion_code /"
                         " patch_motion_code / patch_video）"
                         "提交的渲染任务："
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
                # 必须以 ``Error: `` 开头：主 Agent 的 ToolRegistry 只按这一个前缀把
                # 工具返回值判成失败（``is_tool_error``）。真机执行轮曾因这里回的是
                # ``{"error": ...}`` 这种 JSON 串被记成「成功」，连续失败守卫一次都没
                # 触发，模型连猜 25 个键名、26 秒烧掉 121 万 token 直到配额闸炸掉整条 run。
                executed = await store.executed()
                return (f"Error: Store 中还没有 {key} 的产物。"
                        f"已执行节点：{executed or '（空）'}。"
                        + ("空列表＝这条片子的产物还没生成——**别再换键名试**，"
                           "下一个键也不会因此存在。要拿到它的产物就**调用产出它的节点**"
                           "（如 plan_motion），读产物是「上一步已经跑过、这一步要用它的结果」"
                           "时才用的。" if not executed else
                           "请调用对应节点或其依赖。"))
            return tool_json({"node": key, "payload": await store.get(key)})

        self.mcp.tool(name="read_node_history",
                      title="读取节点产物",
                      description="按节点名读取该节点已产出的结果（服务端 Store 数据总线）")(
            read_node_history)
        self.handlers["read_node_history"] = read_node_history

    def _register_selfcheck(self) -> None:
        """三个自检工具：把闸的判据交到模型手里（逻辑在 `motion/selfcheck.py`）。

        **为什么值得一条链。** 闸的判据本身没变过，变的是「**谁能跑它**」——原来只有
        服务端跑得到，模型只能交完等打回，而一次打回就是整轮重来。这三个口让模型在
        **交之前**自己查：① 这段 TSX 过不过闸①；② 整片九道闸怎么判；③ 这一镜渲出来
        长什么样。

        **回话不是「过/不过」两态。** 渲不出来、服务不可达时答案是「**未判**」——既不
        是通过也不是失败。把未判写成绿，就是把「没量过」记成「量过」（同 `judge_channel`
        的 P5、`_prelint` 那次修静默通过）。

        **它们不带 DAG 契约**，所以不做成节点、也不进 `dag_contract` 的 `nodes`：主服务
        侧 `plan/round._kept` 对 `contract is None` 的工具一律保留，于是**两轮都看得见、
        永不被按计划裁剪**——与 `dag_contract` / `read_node_history` 同一条路。

        **必须在 `available_nodes` 里各占一行**：那份表同时决定主服务注册表里有谁
        （`enabled_tools` 就是它），少一行就谁也调不到，而报出来的还是「剪辑服务还没
        连上」——口径不对，会把人往连接问题上带（同 `preview_caption` 那条注释）。
        """

        async def check_shot_code(ctx: Context, code: str,
                                  declared_fields: Optional[list[str]] = None) -> str:
            return tool_json(sc.check_shot_code(code, declared_fields=declared_fields))

        async def check_film(ctx: Context, shots: list[dict]) -> str:
            return tool_json(sc.check_film(shots))

        async def preview_shot(ctx: Context, code: str,
                               shot: Optional[dict] = None,
                               spec: Optional[dict] = None,
                               duration_sec: float = sc.DEFAULT_PREVIEW_SEC,
                               question: str = "",
                               session_id: str = "", user_id: str = "",
                               conversation_id: str = "") -> str:
            # user_id / conversation_id 必须**声明在签名里**：主服务的 MCP 包装层只对
            # 「schema 里点过名的键」注入身份（`tools/mcp.py` 的 `"user_id" in props`）。
            # 不声明，这次子调用就没有用户身份，`resolve_api_key` 查不到那位用户的配置。
            got = await sc.preview_shot(code=code, shot=shot, spec=spec,
                                        duration_sec=duration_sec, question=question,
                                        user_id=user_id, conversation_id=conversation_id)
            return tool_json(got)

        self.mcp.tool(
            name="check_shot_code", title="检查一镜代码",
            description="交代码**之前**自己查一遍：这段 TSX 过不过闸①（import 白名单、"
                        "逐帧纯函数、声明字段对账）。返回 verdict（green / red / "
                        "undetermined）与逐条错误；undetermined 表示渲染服务不可达、"
                        "**没判**，不是通过。")(check_shot_code)
        self.mcp.tool(
            name="check_film", title="检查整片分镜",
            description="把准备交出去的整份 shots 先过一遍整片判据，返回 errors"
                        "（打回）/ warnings（建议）/ undetermined（未判）与风格账。"
                        "与入库、出片时判的是同一份、不分档——这里看到的就是交上去"
                        "会写在计划卡与出片回执上的那一串读数。errors 里的条目"
                        "**不会**让入库失败（服务端只报不拦），但它们照样是真的。"
                        "undetermined 是「没量过」，不是通过。")(check_film)
        self.mcp.tool(
            name="preview_shot", title="看这一镜渲出来什么样",
            description="渲这一镜的几帧静帧，回一段**如实描述**：画面上有什么、"
                        "有没有哪一帧基本是空的。用来在交代码之前自己看一眼构图，"
                        "而不是等出片之后才发现。shot / spec 只借它们的几何与字段"
                        "（时长缺省 3 秒），注入参数由服务端按 /render-shot 同一份算出。"
                        "只描述，不改代码。")(preview_shot)
        self.handlers["check_shot_code"] = check_shot_code
        self.handlers["check_film"] = check_film
        self.handlers["preview_shot"] = preview_shot

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
