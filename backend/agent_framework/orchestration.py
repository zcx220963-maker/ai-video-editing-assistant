"""基于 DAG 的编排引擎：BaseNode + Store 数据总线 + 拦截器递归补齐依赖。

对应设计文档「基于 DAG 的编排引擎设计 / BaseNode 与 Store 机制」：

* DAG 由节点的 required_nodes（文档里的 require_node）声明式定义：执行某节点前，
  必须先执行过它依赖的节点，据此构建有向无环图。
* 拦截器（Interceptor）：LLM 选定某个 Tool 后、真正执行前经过它；若该 Tool 的某个
  前置依赖尚未执行，就先递归补齐依赖 —— 这是约束 LLM 按剪辑流程行动的安全网。
* BaseNode 统一「取数 → 解析入参 → process → 打包输出 → 写入 Store」链路，新节点只需
  重写 process。
* Store 是**程序控制**的节点间数据总线（框架管），与 Agent Memory（Agent 自主读写）严格区分。
  各节点输出以节点名为 key 存于 Store，供下游 _parse_input 读取，构成 DAG 的数据流。
* 终止点不固定：不强制走到 render_video，走到哪一步取决于模型判断与用户要求，故拦截器
  只在“被选中的节点”上做依赖补齐，绝不主动渲染。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Store：按会话隔离的节点输出总线
# --------------------------------------------------------------------------


def _reduce_last_write_wins(old: Any, new: Any) -> Any:
    return new


def _reduce_append(old: Any, new: Any) -> Any:
    """列表才追加；旧值是列表而新值不是则把新值当一个元素；其余退回覆盖（不猜语义）。"""
    if old is None:
        return list(new) if isinstance(new, list) else [new]
    if not isinstance(old, list):
        return new
    return [*old, *new] if isinstance(new, list) else [*old, new]


def _reduce_merge(old: Any, new: Any) -> Any:
    """两边都是 dict 才浅合并；否则退回覆盖。"""
    if isinstance(old, dict) and isinstance(new, dict):
        return {**old, **new}
    return new


REDUCERS: dict[str, Any] = {
    "last_write_wins": _reduce_last_write_wins,
    "append": _reduce_append,
    "merge": _reduce_merge,
}


class Store:
    """单个剪辑会话的数据总线：node_name -> payload。由框架写入、节点读取。

    接口是 async 的：落点可以是进程内存，也可以是 `artifacts` 表的一行（见
    ``ArtifactStore``）。总线语义（谁执行过、产物是什么）两种落点完全一致。

    并发写同一个 key 必须有确定的结果，所以写入带 **reducer**：缺省后写覆盖，
    列表类产物声明 ``append``、字典类声明 ``merge``。Agent 循环按工具的读写集
    （见 ``tool.Tool.writes``）决定两个调用能否同批，同键的写会被拆开按序执行；
    reducer 兜的是「两个 run 共用同一份产物作用域」这种跨进程情形——那一行的
    读-改-写不在同一把锁下，极端交错时 append 可能少算一次。要严格串行就给每次
    执行换一个作用域：``CheckpointManager.fork`` 正是给分叉重跑新开一份产物集。
    """

    def __init__(self, session_id: str = "", artifact_id: str = "") -> None:
        self.session_id = session_id
        self.artifact_id = artifact_id or "_default"
        self._data: dict[str, Any] = {}

    async def put(self, key: str, payload: Any, *, reducer: str = "last_write_wins") -> None:
        if reducer not in REDUCERS:
            raise ValueError(f"未知 reducer {reducer!r}，可选：{sorted(REDUCERS)}")
        self._data[key] = REDUCERS[reducer](self._data.get(key), payload)

    async def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    async def has(self, key: str) -> bool:
        return key in self._data

    async def executed(self) -> list[str]:
        return list(self._data)

    async def snapshot(self) -> dict[str, Any]:
        return dict(self._data)

    async def persist(self, key: str) -> None:
        """响应拦截器 save_media_content_after 的落盘钩子；内存版无需落盘。"""

    async def meta(self, node: str) -> dict[str, Any] | None:
        """某节点产物的 artifact_meta（存在性 + 归属），供请求拦截器校验上游。"""
        if node not in self._data:
            return None
        return {"node": node, "session_id": self.session_id,
                "artifact_id": self.artifact_id}


_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str) -> str:
    """把 session_id/node/artifact_id 变成各平台合法的路径片段（如 "u:c" → "u_c"）。"""
    return _ILLEGAL_FS.sub("_", name) or "_"


class ArtifactStore(Store):
    """产物总线的持久化实现：``artifacts`` 表就是它的全部真相（取代 FileStore 目录树）。

    作用域 = ``(session_id, artifact_id)`` —— 一次渲染一份产物集，不同会话/不同版本互
    不干扰。``open`` 从表里回填内存镜像，因此进程重启、甚至换一台实例接着同一会话调用，
    上游产物照样读得回来；写只在响应拦截器 ``persist`` 时落表。
    """

    def __init__(self, repo: Any, session_id: str, artifact_id: str = "") -> None:
        super().__init__(session_id, artifact_id)
        self.repo = repo

    @classmethod
    async def open(cls, repo: Any, session_id: str,
                   artifact_id: str = "") -> "ArtifactStore":
        store = cls(repo, session_id, artifact_id)
        store._data.update(await repo.snapshot())
        return store

    async def persist(self, key: str) -> None:
        if key not in self._data:
            return
        await self.repo.put(key, self._data[key])

    async def meta(self, node: str) -> dict[str, Any] | None:
        return await self.repo.meta(node)


class StoreManager:
    """按 session_id 管理 Store，隔离不同剪辑任务的数据。"""

    def __init__(self) -> None:
        self._stores: dict[str, Store] = {}

    def get(self, session_id: str) -> Store:
        return self._stores.setdefault(session_id, Store())

    def reset(self, session_id: str) -> None:
        self._stores.pop(session_id, None)


# --------------------------------------------------------------------------
# NodeState：一次节点执行共享的上下文容器
# --------------------------------------------------------------------------


@dataclass
class NodeSummary:
    """节点执行摘要（耗时、产出规模等），此处保持轻量。"""

    calls: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class NodeState:
    """随节点执行流转的共享状态：会话、产物、语言与 Store 引用。"""

    session_id: str
    artifact_id: str = ""
    lang: str = "zh"
    mode: str = "auto"
    user_request: str = ""
    # 发起方身份：素材按 owner 过滤的查询条件（空身份 = 什么都看不见，而不是放行全部）
    user_id: str = ""
    conversation_id: str = ""
    store: Store = field(default_factory=Store)
    summary: NodeSummary = field(default_factory=NodeSummary)
    # 任一节点从客户端见过的参数在此留痕：拦截器补齐的依赖以默认入参执行，
    # 跨节点的显式开关（如 keep_original_audio）靠 flags 传达。
    flags: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 节点注册表 + BaseNode
# --------------------------------------------------------------------------


class NodeRegistry:
    """节点注册表（对应文档 NODE_REGISTRY）：按 name 存取节点实例。"""

    def __init__(self) -> None:
        self._nodes: dict[str, "BaseNode"] = {}

    def register(self, node: "BaseNode") -> "BaseNode":
        self._nodes[node.name] = node
        return node

    def get(self, name: str) -> "BaseNode | None":
        return self._nodes.get(name)

    def names(self) -> list[str]:
        return list(self._nodes)

    def all(self) -> list["BaseNode"]:
        return list(self._nodes.values())

    def __contains__(self, name: str) -> bool:
        return name in self._nodes


class BaseNode(ABC):
    """所有剪辑节点的基类。子类只需声明元信息并重写 process。

    元信息（可用类属性或构造参数覆盖）：
      name               节点/工具名（同时作为写入 Store 的 key）
      display_name       面向用户的中文名（机器名不出现在界面上，见 editing_contract 契约）
      description        供 LLM 判断何时调用
      input_schema       入参 JSON Schema
      required_nodes     前置依赖节点名列表 —— 定义 DAG 边
      require_prior_kind 本节点运行前必须能在校验 Store 中拿到 artifact_meta 的上游种类
                         （流程图①「读取 require_prior_kind，从 store 获取上游 artifact_meta」）
    """

    name: str = ""
    display_name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    required_nodes: list[str] = []
    require_prior_kind: list[str] = []
    require_explicit_call: bool = False
    # 本节点产物写总线时的合并语义：默认独占一份（后写覆盖），
    # 累积型产物（如逐镜理解结果）声明 "append"。
    reducer: str = "last_write_wins"
    # 只回给调用方、不落进共享库的产物键（会过期的 presigned 直链）。
    # 与「payload 不带工作区绝对路径」是同一类约束：会失效的东西不当持久引用存。
    ephemeral: tuple[str, ...] = ()

    # ---- 并发契约：状态键读写集（Agent 循环据此拆执行批次）----

    @property
    def upstream(self) -> list[str]:
        """DAG 依赖 + 必须校验 artifact_meta 的上游种类（去重、保序）。"""
        return list(dict.fromkeys([*self.required_nodes, *self.require_prior_kind]))

    @property
    def write_keys(self) -> frozenset[str]:
        return frozenset({f"store:{self.name}"})

    @property
    def read_keys(self) -> frozenset[str]:
        return frozenset(f"store:{dep}" for dep in self.upstream)

    def __init__(
        self,
        *,
        name: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        input_schema: dict[str, Any] | None = None,
        required_nodes: Iterable[str] | None = None,
        require_prior_kind: Iterable[str] | None = None,
        registry: NodeRegistry | None = None,
    ) -> None:
        # 实例级覆盖允许同一实现以不同 name/依赖复用；默认回落到类属性。
        if name is not None:
            self.name = name
        if display_name is not None:
            self.display_name = display_name
        if description is not None:
            self.description = description
        if input_schema is not None:
            self.input_schema = input_schema
        if required_nodes is not None:
            self.required_nodes = list(required_nodes)
        if require_prior_kind is not None:
            self.require_prior_kind = list(require_prior_kind)
        if registry is not None:
            registry.register(self)

    @abstractmethod
    async def process(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        """节点核心逻辑（音视频处理等），返回要写入 Store 的 payload。"""

    # ---- 统一的执行链路：取数 → 解析 → 处理 → 打包 → 入库 ----

    async def __call__(self, state: NodeState, **params: Any) -> dict[str, Any]:
        inputs = self.load_inputs_from_client(state, dict(params))
        parsed = await self._parse_input(state, inputs)
        outputs = await self.process(state, parsed)
        # 显式不落库的返回（render_video 的 dry_run：那是「将要渲成什么样」的答复，
        # 不是一次渲染产物；落进 artifacts 就成了「这步已经有产出」的假象）。
        no_store = bool(outputs.pop("__no_store__", None))
        packed = self.pack_outputs_to_client(state, outputs)
        if not no_store:
            # 入库的那份剔掉 ephemeral：会过期的直链写进共享库，就是一条注定失效的引用
            stored = ({k: v for k, v in outputs.items() if k not in self.ephemeral}
                      if self.ephemeral else outputs)
            await state.store.put(self.name, stored, reducer=self.reducer)  # 入库，供下游读取
        state.summary.calls += 1
        return packed

    def load_inputs_from_client(self, state: NodeState, params: dict[str, Any]) -> dict[str, Any]:
        """客户端入参 + 会话上下文的合并（lang/mode/user_request/artifact_id）。"""
        merged = dict(params)
        merged.setdefault("lang", state.lang)
        merged.setdefault("mode", state.mode)
        merged.setdefault("user_request", state.user_request)
        merged["artifact_id"] = state.artifact_id
        return merged

    async def _parse_input(self, state: NodeState, inputs: dict[str, Any]) -> dict[str, Any]:
        """从 Store 读出前驱节点输出，拼进本节点入参（按节点名取，缺省不报错）。"""
        parsed: dict[str, Any] = {}
        for dep in self.required_nodes:
            if await state.store.has(dep):
                parsed[dep] = await state.store.get(dep)
        parsed.update(inputs)
        return parsed

    def pack_outputs_to_client(self, state: NodeState, outputs: dict[str, Any]) -> dict[str, Any]:
        """打包成返回给客户端的结构（默认带上节点名与产物 id）。"""
        return {"node": self.name, "artifact_id": state.artifact_id, "output": outputs}


# --------------------------------------------------------------------------
# DAG 工具：拓扑排序 / 环检测（由 required_nodes 建图）
# --------------------------------------------------------------------------


def build_edges(registry: NodeRegistry) -> dict[str, list[str]]:
    """返回邻接表 dep -> [该依赖支撑的后继节点]。"""
    edges: dict[str, list[str]] = {n.name: [] for n in registry.all()}
    for node in registry.all():
        for dep in node.required_nodes:
            if dep not in edges:
                raise ValueError(f"节点 {node.name!r} 依赖了未注册的 {dep!r}")
            edges[dep].append(node.name)
    return edges


def detect_cycle(registry: NodeRegistry) -> list[str] | None:
    """Kahn 拓扑排序；若无法排完则说明有环，返回参与环的节点名列表。"""
    indeg: dict[str, int] = {n.name: 0 for n in registry.all()}
    edges = build_edges(registry)
    for node in registry.all():
        indeg[node.name] = len(node.required_nodes)

    queue = deque(name for name, d in indeg.items() if d == 0)
    seen = 0
    while queue:
        cur = queue.popleft()
        seen += 1
        for nxt in edges.get(cur, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen == len(indeg):
        return None
    return [name for name, d in indeg.items() if d > 0]


def topological_order(registry: NodeRegistry) -> list[str]:
    cycle = detect_cycle(registry)
    if cycle:
        raise ValueError(f"DAG 存在环，涉及节点: {cycle}")
    return _kahn(registry)


def _kahn(registry: NodeRegistry) -> list[str]:
    indeg = {n.name: len(n.required_nodes) for n in registry.all()}
    edges = build_edges(registry)
    queue = deque(sorted(k for k, d in indeg.items() if d == 0))
    order: list[str] = []
    while queue:
        cur = queue.popleft()
        order.append(cur)
        for nxt in sorted(edges.get(cur, [])):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return order


# --------------------------------------------------------------------------
# 拦截器：执行前递归补齐未满足的前置依赖
# --------------------------------------------------------------------------


class MissingNode(KeyError):
    pass


class Interceptor:
    """两阶段拦截器（对应流程图「MCP 请求拦截器 → Server → 响应拦截器」）。

    请求拦截器 ``inject_media_content_before``：
      ① 读取 required_nodes + require_prior_kind，校验 Store 中已有上游 artifact_meta；
      ② 把上游产物 payload 注入 request_args；
      ③ 合并 artifact_id / lang 进请求参数；
      缺少依赖 → 先递归执行上游节点再回到本节点。
    响应拦截器 ``save_media_content_after``：
      把本节点产出持久化写入 Store（``ArtifactStore`` 落 ``artifacts`` 表一行，内存版为 no-op）。

    绝不主动执行未被选中的下游（如 render_video）——终止点由上层决定。
    """

    def __init__(self, registry: NodeRegistry) -> None:
        self.registry = registry
        self.order_trace: list[str] = []  # 实际执行顺序，便于断言与观测

    def _require(self, name: str) -> BaseNode:
        node = self.registry.get(name)
        if node is None:
            raise MissingNode(f"未注册的节点: {name}")
        return node

    def _upstream(self, node: BaseNode) -> list[str]:
        """DAG 依赖 + 必须校验 artifact_meta 的上游种类（契约在 BaseNode.upstream）。"""
        return node.upstream

    async def invoke(self, name: str, state: NodeState, **params: Any) -> dict[str, Any]:
        # 客户端参数先留痕：依赖补齐以默认入参执行，跨节点开关靠 state.flags 传达
        state.flags.update(params)
        args = await self.inject_media_content_before(name, state, dict(params))
        node = self._require(name)
        result = await node(state, **args)
        await self.save_media_content_after(name, state)
        self.order_trace.append(name)
        return result

    async def inject_media_content_before(
        self, name: str, state: NodeState, request_args: dict[str, Any]
    ) -> dict[str, Any]:
        """请求拦截：补齐依赖 → 注入上游 payload → 合并 artifact_id/lang。"""
        await self._ensure_deps(name, state, path=[])
        node = self._require(name)
        for dep in self._upstream(node):  # ② 上游产物注入请求参数
            if await state.store.has(dep):
                request_args.setdefault(dep, await state.store.get(dep))
        request_args.setdefault("artifact_id", state.artifact_id)  # ③ 合并请求参数
        request_args.setdefault("lang", state.lang)
        return request_args

    async def save_media_content_after(self, name: str, state: NodeState) -> None:
        """响应拦截：把节点产出持久化进 Store（内存版为 no-op，表版写 artifacts 行）。"""
        await state.store.persist(name)

    async def _ensure_deps(self, name: str, state: NodeState, path: list[str]) -> None:
        if name in path:
            raise ValueError(f"依赖成环: {' -> '.join([*path, name])}")
        node = self._require(name)
        for dep in self._upstream(node):  # ① 检查上游依赖
            if await state.store.has(dep):
                continue  # 依赖已执行，无需重复
            dep_node = self._require(dep)
            if getattr(dep_node, 'require_explicit_call', False):
                raise ValueError(
                    f"{dep} 需要你直接调用并传入创意决策参数，不能自动补齐。"
                    f"请先调用 {dep}，再调用 {name}。"
                )
            await self._ensure_deps(dep, state, path=[*path, name])  # 递归执行上游节点
            await dep_node(state)  # 以默认入参补齐前置
            self.order_trace.append(dep)
            await self.save_media_content_after(dep, state)
