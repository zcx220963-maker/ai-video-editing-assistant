"""节点事实：白名单、词表、参数枚举源、规划轮的过滤注册表。

全部**现取**（注册表与契约要到启动后才填齐，构造时只握引用），不做任何 LLM 调用。
计划门其余各层都从这里取事实，避免各写一套「什么算真实存在的节点/参数」。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..catalog import ToolCatalog, get_catalog
from ..editing_contract import ContractSlot, EditingContract
from ..tool import Tool, ToolRegistry
from .support import (MCP_PREFIX, OUTPUT_FIELD_NAMES, as_card_value, clean,
                      enum_of, num)

if False:  # TYPE_CHECKING：只为 planning_registry 的形参标注，避免与 tools 形成环
    from .tools import ConfirmPlanTool, SubmitPlanTool

# 这些工具会改变执行状态（分叉重跑 / 起子 Agent），规划轮同样不给
_MUTATING_TOOLS = frozenset({"rerun_from", "start_subagent"})


class PlanVocabulary:
    """剪辑节点与参数的事实清单（白名单 / 词表 / 枚举源 / 开关清单）。"""

    def __init__(self, *,
                 registry: ToolRegistry,
                 contract: ContractSlot | EditingContract | None = None,
                 extra_options: Callable[[str, str], Awaitable[Sequence[str]]] | None = None,
                 catalog: ToolCatalog | None = None) -> None:
        self._registry = registry
        self._contract = contract
        self._extra_options = extra_options
        self._catalog = catalog
        self._param_keys: set[str] = set()
        self._param_sig: tuple[str, ...] | None = None

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    # ---- 引用现取 ----

    @property
    def catalog(self) -> ToolCatalog:
        return self._catalog or get_catalog()

    @property
    def contract(self) -> EditingContract:
        if self._contract is None:
            return EditingContract()
        return self._contract.contract if isinstance(self._contract, ContractSlot) \
            else self._contract

    def resolve_tool(self, node: str) -> Tool | None:
        """计划卡上的节点名 → 注册表里的工具（兼容带/不带 MCP 前缀两种写法）。"""
        for cand in (node, f"{MCP_PREFIX}{node}"):
            tool = self._registry.get(cand)
            if tool is not None:
                return tool
        bare = self._registry.get(node.removeprefix(MCP_PREFIX)) if node else None
        return bare

    def whitelist(self) -> set[str]:
        """允许上卡的剪辑节点名 = Storyline 白名单 ∩ 已注册工具。

        契约未接入（Storyline 没连上）时退化成「注册表里带 DAG 契约的工具」：
        仍以真实存在的工具为界，模型无从臆造。
        """
        names = set(self.contract.names)
        if not names:
            names = {n for n in self._registry.tool_names
                     if getattr(self._registry.get(n), "contract", None) is not None}
        return {n for n in names if self.resolve_tool(n) is not None}

    def vocabulary(self) -> set[str]:
        return (set(self.catalog.names) | set(self._registry.tool_names)
                | set(self.contract.names))

    def param_keys(self) -> set[str]:
        """真实存在的工具入参键名 **与它们的合法枚举值**。

        文案提到它们不算臆造——那是参数/取值，不是节点名。

        枚举值必须一并收进来：计划一旦真把带 enum 的节点写进 steps，模型就会在
        why/expectation 里顺口提它的取值（如 ``script_template_rec`` 的
        ``tpl_vlog_3act``）。不收就会判成「引用了不存在的名字」——真机实测：
        模型按提示补上了 script_template_rec，紧接着就因为文案里写了模板 id
        被判臆造，卡连着两轮出不来卡。

        按「注册表名字清单 + 契约节点名清单」当签名缓存：MCP/Storyline 的工具要到
        启动后才注册进来、契约要到连上后才填上，缓存不刷新就会一直把晚到的参数名
        当假名字打回。
        """
        signature = (tuple(self._registry.tool_names), tuple(self.contract.names))
        if self._param_sig != signature:
            keys: set[str] = set()
            for name in self._registry.tool_names:
                keys.update(self.props_for(name))
            for name in signature[1]:
                keys.update(self.props_for(name))
            self._param_keys, self._param_sig = keys, signature
        return self._param_keys

    def enum_values(self) -> set[str]:
        """所有节点参数声明过的枚举取值（含 oneOf 风格的分支值）。"""
        out: set[str] = set()
        names = tuple(self._registry.tool_names)
        signature = (names, tuple(self.contract.names))
        if getattr(self, "_enum_sig", None) == signature:
            return self._enum_values
        for name in names:
            for spec in self.props_for(name).values():
                out.update(enum_of(spec))
        for name in signature[1]:
            for spec in self.props_for(name).values():
                out.update(enum_of(spec))
        self._enum_values, self._enum_sig = out, signature
        return out


    def props_for(self, node: str) -> dict[str, Mapping[str, Any]]:
        """一个节点的参数事实：MCP 声明给类型，契约给类型之外的那几样。

        为什么非要契约这一路：``_signature_for`` 把 input_schema 翻成函数签名时只带得出
        类型，description / enum / 上下界留在剪辑服务端——只看工具声明的话「带界数值」
        这类枚举源永远反查不到，计划卡上除了布尔开关就没有别的位可给。
        """
        tool = self.resolve_tool(node)
        props: dict[str, Mapping[str, Any]] = dict(
            ((tool.parameters if tool else {}) or {}).get("properties") or {})
        declared = (self.contract.get(node).params
                    if self.contract.get(node) is not None else {}) or {}
        for key, spec in declared.items():
            if not isinstance(spec, Mapping):
                continue
            base = props.get(key)
            merged = dict(base) if isinstance(base, Mapping) else {}
            merged.update(spec)
            props[key] = merged
        return props

    # ---- 规划轮注册表 ----

    def planning_registry(self, *, submit_tool: "SubmitPlanTool",
                          confirm_tool: "ConfirmPlanTool") -> ToolRegistry:
        """规划轮的工具集：**物理不含**剪辑执行节点，越权无工具可调。

        留下的是只读事实工具（素材、dag_contract、read_node_history、技能、
        检索/记忆等）+ ``submit_plan`` + ``confirm_plan``。与 ``build_subagent_registry`` 同一形状
        （独立注册表、共享工具实例），只是过滤方向相反：那边按白名单挑人，
        这里把剪辑执行节点整批挡在门外。
        """
        registry = ToolRegistry()
        for name in self._registry.tool_names:
            if name in _MUTATING_TOOLS or name == submit_tool.name:
                continue
            tool = self._registry.get(name)
            if tool is None or getattr(tool, "contract", None) is not None:
                continue               # 带 DAG 契约 = 剪辑执行节点
            registry.register(tool)
        registry.register(submit_tool)
        registry.register(confirm_tool)
        # 本轮按设计只给只读事实工具 + submit_plan。把这件事写进报错说明：
        # 原先模型调剪辑节点只得到一句 tool not found，看不出是「按设计不提供」，
        # 于是转去问用户「工具不可用怎么办」——白烧一轮（真机实测）。
        registry.unknown_tool_hint = (
            "这是**规划轮**，剪辑执行节点按设计不提供——它们要等用户在计划卡上确认后才可用。"
            "请调用 submit_plan 把 1~3 个候选计划交上来；"
            "只读事实工具（素材查询、节点产物、技能、dag_contract）本轮可用，可用来核实。")
        return registry

    async def enum_source(self, node: str, key: str,
                           spec: Mapping[str, Any]) -> tuple[str, Any] | None:
        """枚举源：enum → 值集合；boolean → true/false；带界的数值 → (下界, 上界)；
        否则问外部（曲库真实标签）。都没有就回 None（这参数不许上卡）。"""
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return ("enum", {as_card_value(v) for v in enum})
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "boolean" in types:
            return ("bool", {"true", "false"})
        if ("integer" in types or "number" in types) \
                and (spec.get("minimum") is not None or spec.get("maximum") is not None):
            return ("number", (num(spec.get("minimum")), num(spec.get("maximum"))))
        external = await self._external(node, key)
        if external:
            return ("label", set(external))
        return None

    async def knob_facts(self) -> list[dict[str, Any]]:
        """各节点**能上卡**的开关清单：判据就是 ``_check_params`` 那一份（同一个 ``_enum_source``）。

        为什么单独做这一份：规划轮的注册表里没有剪辑执行节点，节点参数事实只在
        服务端（契约 + 工具声明）——模型若被提示「自己去查参数」，唯一的路径就是拿
        ``read_node_history`` 猜键名，而执行前 Store 必空，真机实测这么连着错了六十次
        也没出卡。参数由这一层直接写进提示段，模型无从猜。
        """
        out: list[dict[str, Any]] = []
        for node in sorted(self.whitelist()):
            knobs: list[dict[str, str]] = []
            props = self.props_for(node)
            for key in sorted(props):
                spec = props[key]
                if not isinstance(spec, Mapping):
                    continue
                source = await self.enum_source(node, key, spec)
                if source is None:
                    continue
                kind, data = source
                if kind == "number":
                    low, high = data
                    unit = clean(spec.get("unit"))
                    span = "~".join("" if v is None else f"{v:g}" for v in (low, high))
                    values = f"数值 {span or '?'}{unit}"
                elif kind == "label":
                    names = sorted(str(v) for v in data)
                    values = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
                else:
                    values = "、".join(sorted(str(v) for v in data))
                knobs.append({
                    "key": key, "kind": kind, "values": values,
                    "default": as_card_value(spec.get("default")),
                    "note": clean(spec.get("description"))[:60],
                })
            out.append({"node": node, "knobs": knobs})
        return out

    async def _external(self, node: str, key: str) -> list[str]:
        if self._extra_options is None:
            return []
        try:
            return [t for t in (await self._extra_options(node, key)) if clean(t)]
        except Exception:  # noqa: BLE001 - 曲库读不到就当没有这个枚举源
            return []

    def explicit_call_nodes(self) -> set[str]:
        """既在 DAG 里、又标了「必须 LLM 显式调用」的节点集合。

        标记来自 ``dag_contract``（``NodeContract.explicit``）——与执行期拦截器
        （``orchestration.Interceptor._ensure_deps``）读的是服务端同一个
        ``require_explicit_call``，所以两边不会各说一套。
        """
        return {n for n in self.contract.names
                if (self.contract.get(n) is not None and self.contract.get(n).explicit)}

    def output_keys(self) -> set[str]:
        """节点**产出**的字段名（不是入参）。

        来源只有一处：剪辑服务端各节点 ``process`` 的返回键。主服务取不到那些声明，
        所以这里按「模型会写进文案的产出字段」维护一份显式清单——比让它撞墙再猜可靠。
        清单只用于放行文案里的名字，不参与任何执行判定，写多了也不会放宽别的东西。
        """
        return set(OUTPUT_FIELD_NAMES)
