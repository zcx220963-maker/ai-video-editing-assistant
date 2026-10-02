"""计划门（块 B）：规划轮的工具、服务端四重校验、执行帧编译与事后对账。

对应交接文档《剪辑 Agent「Plan-and-Execute 确认门 + 中文显示单源」》§3/§5/§7。
这一层全是**纯代码**，一次 LLM 都不叫：

* **规划轮物理过滤**：剪辑执行节点根本不在那一轮的注册表里——模型想越权也无工具可调
  （见 ``PlanGate.planning_registry``），比提示词里写「请勿执行」强。
* **枚举是承诺，自定义是诉求**：``param_options`` 的每个值都要能反查到枚举源
  （节点 input_schema 的 enum / 布尔开关 / 带上下界的数值 / 外部注入的曲库真实标签），
  反查不到就不许上卡；自由文本永不进节点参数，只进 ``<user_custom_requests>`` 段。
* **软约束**：批准的计划是强提示，不是硬调度。``reconcile`` 只在事后算偏差
  （计划外步骤 / 声明了没调），不拦截任何一次调用。

中文名不在这层拼：这层的产物里留机器名，出门时由 ``catalog.ToolCatalog`` 换轨
（块 A 的出口替换器对 ``node``/``skills_hint`` 这类键走 ``*_display`` 旁路）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .catalog import ToolCatalog, get_catalog
from .connection_manager import OUTBOUND_TOPIC
from .editing_contract import ContractSlot, EditingContract
from .hooks import AgentHook, AgentHookContext, _current_hook_ctx
from .mq import MessageQueue
from .plan_replay import (drain_plan_cards, drain_plan_round,
                          record_plan_card, reset_plan_cards)
from .tool import Tool, ToolError, ToolRegistry, UnknownToolError

__all__ = [
    "PLAN_MAX_CANDIDATES", "CUSTOM_MAX_ITEMS", "CUSTOM_MAX_CHARS",
    "PlanGate", "SubmitPlanTool", "ConfirmPlanTool", "PlanIssues", "neutralize",
    "render_injections", "reconcile", "preload_skills", "planning_section",
    "pending_continuation", "claims_plan_card", "claims_step_executed",
    "PlanCardHook", "PlanReconcileHook",
    "record_plan_card", "drain_plan_cards", "drain_plan_round", "reset_plan_cards",
]

PLAN_MAX_CANDIDATES = 3        # 一次规划最多几张卡
PLAN_STEPS_MAX = 24            # 单计划步数上限（防卡面失控）
CUSTOM_MAX_ITEMS = 5           # 自定义诉求条数
CUSTOM_MAX_CHARS = 200         # 单条自定义诉求长度

# 词表外别名的形状：snake_case 标识符（至少一个下划线）。
# 展示字段里出现**英文原名**不打回——块 A 的出口替换器会换轨成中文；
# 只有引用了词表里根本没有的名字才属于正确性问题（臆造），替换器无能为力。
_ALIAS = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

# 节点**产出**的字段名：模型在 why/expectation 里引用上游产物字段是正常表达，
# 不是「臆造工具名」。真机实测它连写三次「引用了不存在的名字「asr_segments」」，
# 卡连着两轮出不来。这些名字逐条对着 storyline_server 各节点的返回键抄；
# 只用于放行文案，不参与执行判定（写多了也不会放宽别的东西）。
_OUTPUT_FIELD_NAMES = frozenset({
    "media", "clips", "asr_segments", "clip_captions", "rough_clips", "groups",
    "templates", "templates_info", "group_scripts", "voiceover", "bgm",
    "timeline", "events", "audio_events", "subtitles", "warnings",
    "shot_count", "structure", "raw_text", "group_id", "start", "end", "duration",
})

# 这些工具会改变执行状态（分叉重跑 / 起子 Agent），规划轮同样不给
_MUTATING_TOOLS = frozenset({"rerun_from", "start_subagent"})

# 只读的记账查询不算「创作步骤」：渲染未达终态时 hint 明写「请调用 render_status 追」，
# 再把它标成「计划外一步」就是框架自己打自己的脸（真机踩到：角标写计划外 1 步 ·
# 渲染进度查询，用户读到的是「模型偷偷多做了一件事」）。
# ask_user 同一条路：弹窗提问是**交互动作**，不是多做出来的一步创作；
# 执行轮只要问过一个问题，角标就会挂一条「计划外 · 询问用户」，那是噪声不是偏差。
NON_STEP_TOOLS = frozenset({"render_status", "read_node_history", "ask_user"})

_MCP_PREFIX = "storyline_"


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _enum_of(spec: Any) -> set[str]:
    """一份参数声明里所有可能的取值（enum / const / oneOf 分支 / 默认值）。

    用于词表：计划文案里提到某个真实枚举取值（如模板 id ``tpl_vlog_3act``）
    不该被判成「臆造的名字」。只在声明里明确列出的值才算，不做任何猜测。
    """
    out: set[str] = set()
    if not isinstance(spec, Mapping):
        return out
    for key in ("enum", "examples"):
        raw = spec.get(key)
        if isinstance(raw, (list, tuple)):
            out.update(str(v) for v in raw if isinstance(v, (str, int, float, bool)))
    if "const" in spec:
        out.add(str(spec["const"]))
    for branch in spec.get("oneOf") or spec.get("anyOf") or ():
        out |= _enum_of(branch)
    if spec.get("type") == "boolean":
        out.update({"true", "false"})
    return out


def _as_card_value(value: Any) -> str:
    """卡面上的枚举值统一成字符串：布尔写成 true/false，数字去掉多余 .0。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = _norm(value)
    if re.fullmatch(r"-?\d+\.0", text):
        return text[:-2]
    return text


def neutralize(text: str) -> str:
    """中和自定义文本里的标签：``<``/``>`` 换成全角，用户写不出闭合标签。

    这是 prompt 注入面，不只是 XSS 面：诉求原样进 system 段，若允许
    ``</user_custom_requests>`` 出现，用户就能自己开一段 system 指令。
    """
    return text.replace("<", "＜").replace(">", "＞")


@dataclass
class PlanIssues:
    """一次校验的结论：errors 非空即打回，warnings 只在卡上挂角标。"""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


class PlanGate:
    """计划的服务端四重校验 + execute 帧校验（纯代码，无 LLM）。

    依赖全部**现取**（注册表与契约要到启动后才填齐，构造时只握引用）：
    ``contract`` 给依赖边与下游集，``registry`` 给节点参数枚举源，
    ``skills`` 给技能可用性，``extra_options`` 给不在 schema 里的真实枚举
    （生产里是 BGM 曲库的实际标签），``catalog`` 给词表以判「臆造别名」。
    """

    def __init__(self, *,
                 registry: ToolRegistry,
                 contract: ContractSlot | EditingContract | None = None,
                 skills: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
                 extra_options: Callable[[str, str], Awaitable[Sequence[str]]] | None = None,
                 catalog: ToolCatalog | None = None) -> None:
        self._registry = registry
        self._contract = contract
        self._skills = skills
        self._extra_options = extra_options
        self._catalog = catalog
        self._param_keys: set[str] = set()
        self._param_sig: tuple[str, ...] | None = None

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
        for cand in (node, f"{_MCP_PREFIX}{node}"):
            tool = self._registry.get(cand)
            if tool is not None:
                return tool
        bare = self._registry.get(node.removeprefix(_MCP_PREFIX)) if node else None
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
                keys.update(self._props_for(name))
            for name in signature[1]:
                keys.update(self._props_for(name))
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
            for spec in self._props_for(name).values():
                out.update(_enum_of(spec))
        for name in signature[1]:
            for spec in self._props_for(name).values():
                out.update(_enum_of(spec))
        self._enum_values, self._enum_sig = out, signature
        return out


    def _props_for(self, node: str) -> dict[str, Mapping[str, Any]]:
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

    # ---- ①~④ 计划校验 ----

    async def validate(self, payload: Any) -> tuple[list[dict[str, Any]], PlanIssues]:
        raw = _coerce_plans(payload)
        issues = PlanIssues()
        if raw is None:
            issues.error('计划形状不对：应当是 {"plans": [{"plan_id","label","steps":[…]}] }。')
            return [], issues
        if not (1 <= len(raw) <= PLAN_MAX_CANDIDATES):
            issues.error(f"候选计划必须是 1~{PLAN_MAX_CANDIDATES} 个，收到 {len(raw)} 个。")
            return [], issues
        allowed = self.whitelist()
        skills = await self._skill_map()
        out: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, plan in enumerate(raw, start=1):
            norm = await self._validate_plan(plan, index, allowed, skills, issues, seen_ids)
            if norm is not None:
                out.append(norm)
        return out, issues

    async def _skill_map(self) -> dict[str, Any]:
        """技能表。``__unreadable__`` 是哨兵：没接技能库与读不到同一种处境——
        第三条（技能存在/可用/有中文名）无从核验，只能降级为警告。"""
        if self._skills is None:
            return {"__unreadable__": "未接入技能库"}
        try:
            return dict(await self._skills() or {})
        except Exception as exc:  # noqa: BLE001 - 技能读不到只影响第三条，如实降级
            return {"__unreadable__": f"技能库读取失败：{type(exc).__name__}"}

    async def _validate_plan(self, plan: Mapping[str, Any], index: int,
                             allowed: set[str], skills: Mapping[str, Any],
                             issues: PlanIssues, seen_ids: set[str]) -> dict[str, Any] | None:
        tag = _norm(plan.get("plan_id")) or f"p{index}"
        if tag in seen_ids:
            issues.error(f"plan_id「{tag}」重复。")
        seen_ids.add(tag)
        if not _norm(plan.get("label")):
            issues.error(f"计划 {tag} 缺少 label（卡面标题）。")
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            issues.error(f"计划 {tag} 没有步骤。")
            return None
        if len(steps) > PLAN_STEPS_MAX:
            issues.error(f"计划 {tag} 步骤过多（{len(steps)} > {PLAN_STEPS_MAX}）。")
            return None
        before = len(issues.errors)
        norm_steps: list[dict[str, Any]] = []
        for step_index, step in enumerate(steps, start=1):
            norm = await self._validate_step(step, tag, step_index, allowed, skills, issues)
            if norm is not None:
                norm_steps.append(norm)
        if len(issues.errors) > before:
            return None
        self._check_topology(tag, norm_steps, issues)
        self._check_explicit_deps(tag, norm_steps, issues)
        self._check_skip_rules(tag, norm_steps, issues)
        self._check_duplicates(tag, norm_steps, issues)
        self._check_hygiene(tag, [plan.get("label"), plan.get("goal"),
                                  *[s["why"] for s in norm_steps],
                                  *[s["expectation"] for s in norm_steps]], issues)
        return {
            "plan_id": tag,
            "label": _norm(plan.get("label")),
            "goal": _norm(plan.get("goal")),
            "steps": norm_steps,
        }

    async def _validate_step(self, step: Any, tag: str, index: int,
                             allowed: set[str], skills: Mapping[str, Any],
                             issues: PlanIssues) -> dict[str, Any] | None:
        if not isinstance(step, Mapping):
            issues.error(f"计划 {tag} 第 {index} 步不是对象。")
            return None
        node = _norm(step.get("node"))
        # ① node ∈ ToolRegistry ∩ Storyline 白名单
        if node not in allowed:
            issues.error(f"计划 {tag} 第 {index} 步的节点「{node or '(空)'}」不存在"
                         f"——只能使用真实存在的剪辑节点，不许臆造。")
            return None
        tool = self.resolve_tool(node) or self._registry.get(node)
        declared = step.get("seq")
        if declared is not None and _num(declared) is not None \
                and int(_num(declared)) != index:
            issues.error(f"计划 {tag} 第 {index} 步的 seq={_norm(declared)} 与位置不符："
                         f"步骤序必须与依赖序一致。")
            return None
        norm: dict[str, Any] = {
            "seq": index,
            "node": node,
            "why": _norm(step.get("why")),
            "expectation": _norm(step.get("expectation")),
            "tool_kind": "mcp" if getattr(tool, "contract", None) is not None else "tool",
            "skills_hint": [],
            "skippable": bool(step.get("skippable")),
            "skip_reason": "",
            "param_options": [],
            "requires": list(self.contract.get(node).requires)
            if self.contract.get(node) is not None else [],
        }
        if not norm["why"]:
            issues.error(f"计划 {tag} 第 {index} 步（{node}）缺少 why："
                         f"卡面必须说清为什么要做这步。")
        await self._check_skills(tag, index, step, skills, norm, issues)
        await self._check_params(tag, index, step, norm, issues)
        return norm

    # ③ skills_hint 存在、可用、且中文名在词表里

    async def _check_skills(self, tag: str, index: int, step: Mapping[str, Any],
                            skills: Mapping[str, Any], norm: dict[str, Any],
                            issues: PlanIssues) -> None:
        hints = step.get("skills_hint") or []
        if isinstance(hints, str):
            hints = [hints]
        if not isinstance(hints, list):
            issues.error(f"计划 {tag} 第 {index} 步的 skills_hint 必须是技能名列表。")
            return
        for name in hints:
            key = _norm(name)
            if not key:
                continue
            unreadable = skills.get("__unreadable__")
            if unreadable is not None:
                issues.warn(f"计划 {tag} 第 {index} 步的技能「{key}」未能核验"
                            f"（{unreadable}）。")
                norm["skills_hint"].append(key)
                continue
            skill = skills.get(key)
            if skill is None:
                issues.error(f"计划 {tag} 第 {index} 步引用了不存在的技能「{key}」。")
                continue
            if not getattr(skill, "available", True):
                issues.error(f"计划 {tag} 第 {index} 步的技能「{key}」当前不可用"
                             f"（{getattr(skill, 'unavailable_reason', '')}）。")
                continue
            if not (self.catalog.display(key) or getattr(skill, "display", "")):
                # 词表里没有这条技能：卡面与执行记录都会露出机器名（块 A 换不了轨）
                issues.error(f"计划 {tag} 第 {index} 步的技能「{key}」没有登记中文名，"
                             f"上卡会在界面露出机器名。")
                continue
            norm["skills_hint"].append(key)

    # ④ param_options 的每个值可反查枚举源；节点没有的参数不许上卡

    async def _check_params(self, tag: str, index: int, step: Mapping[str, Any],
                            norm: dict[str, Any], issues: PlanIssues) -> None:
        raw_opts = step.get("param_options") or []
        if not isinstance(raw_opts, list):
            issues.error(f"计划 {tag} 第 {index} 步的 param_options 必须是列表。")
            return
        props = self._props_for(norm["node"])
        for raw in raw_opts:
            if not isinstance(raw, Mapping):
                issues.error(f"计划 {tag} 第 {index} 步的参数项不是对象。")
                continue
            key = _norm(raw.get("key"))
            spec = props.get(key)
            if spec is None:
                issues.error(f"计划 {tag} 第 {index} 步（{norm['node']}）没有参数「{key}」，"
                             f"不许在卡上造这个开关；这类诉求请走「其他」。")
                continue
            options = raw.get("options")
            if not isinstance(options, list) or not options:
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」缺少 options。")
                continue
            source = await self._enum_source(norm["node"], key, spec)
            if source is None:
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」没有可反查的枚举源"
                             f"（既不是枚举/开关/带界数值，曲库也没有这组标签）。")
                continue
            values: list[dict[str, str]] = []
            picked: set[str] = set()
            for opt in options:
                value = opt.get("value") if isinstance(opt, Mapping) else opt
                text = _as_card_value(value)
                reason = self._value_rejected(source, text)
                if reason:
                    issues.error(f"计划 {tag} 第 {index} 步「{key}」的值「{text}」{reason}。")
                    continue
                if text in picked:
                    continue
                picked.add(text)
                values.append({
                    "value": text,
                    "display": _norm(opt.get("display")) if isinstance(opt, Mapping) else "",
                    "kind": source[0],
                })
            if not values:
                continue
            # 单值开关 = 没给用户选择权。用户的原话是「有需要的参数也弹窗式询问，
            # 永远不要瞎编」——一个只有一个值的开关，用户既看不出这是个可改的决定，
            # 也只能靠「其他」打字。所以**取值范围本来就存在**的那几类（数值区间、
            # 布尔、枚举）必须给至少两个真实候选，推荐值放 default。
            #
            # 但 `label`（曲库标签这类"指名道姓"的源）不在此列：用户说「用 Someday
            # I'll Fly 做配乐」时，那首歌就是唯一答案，硬凑第二个候选反而是瞎编。
            if len(values) < 2 and source[0] in ("number", "bool", "enum"):
                hint = ""
                if source[0] == "number":
                    low, high = source[1]
                    hint = (f"该参数是数值区间 "
                            f"{'?' if low is None else f'{low:g}'}~"
                            f"{'?' if high is None else f'{high:g}'}，"
                            f"请给 2~4 个有真实差异的候选（如 0.1/0.2/0.3/0.5）")
                elif source[0] == "bool":
                    hint = "给 true 与 false 两个候选，让用户自己选"
                else:
                    hint = "请把该参数可用的取值多列几个，让用户有得挑"
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」只给了 1 个候选值"
                             f"「{values[0]['value']}」，用户没有选择余地。{hint}；"
                             f"若确实不该由用户定，就不要把它做成开关。")
                continue
            default = _as_card_value(raw.get("default"))
            if default and default not in picked:
                issues.error(f"计划 {tag} 第 {index} 步「{key}」的 default「{default}」"
                             f"不在选项里。")
                continue
            spec_meta: dict[str, Any] = {
                "key": key,
                "display": _norm(spec.get("description")) or key,
                "options": values,
                "default": default or values[0]["value"],
            }
            if source[0] == "number":
                spec_meta["unit"] = _norm(spec.get("unit"))
            norm["param_options"].append(spec_meta)

    async def _enum_source(self, node: str, key: str,
                           spec: Mapping[str, Any]) -> tuple[str, Any] | None:
        """枚举源：enum → 值集合；boolean → true/false；带界的数值 → (下界, 上界)；
        否则问外部（曲库真实标签）。都没有就回 None（这参数不许上卡）。"""
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            return ("enum", {_as_card_value(v) for v in enum})
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "boolean" in types:
            return ("bool", {"true", "false"})
        if ("integer" in types or "number" in types) \
                and (spec.get("minimum") is not None or spec.get("maximum") is not None):
            return ("number", (_num(spec.get("minimum")), _num(spec.get("maximum"))))
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
            props = self._props_for(node)
            for key in sorted(props):
                spec = props[key]
                if not isinstance(spec, Mapping):
                    continue
                source = await self._enum_source(node, key, spec)
                if source is None:
                    continue
                kind, data = source
                if kind == "number":
                    low, high = data
                    unit = _norm(spec.get("unit"))
                    span = "~".join("" if v is None else f"{v:g}" for v in (low, high))
                    values = f"数值 {span or '?'}{unit}"
                elif kind == "label":
                    names = sorted(str(v) for v in data)
                    values = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
                else:
                    values = "、".join(sorted(str(v) for v in data))
                knobs.append({
                    "key": key, "kind": kind, "values": values,
                    "default": _as_card_value(spec.get("default")),
                    "note": _norm(spec.get("description"))[:60],
                })
            out.append({"node": node, "knobs": knobs})
        return out

    @staticmethod
    def _value_rejected(source: tuple[str, Any], text: str) -> str:
        kind, data = source
        if kind == "number":
            value = _num(text)
            if value is None:
                return "不是数值"
            low, high = data
            if low is not None and value < low:
                return f"低于节点下界 {low:g}"
            if high is not None and value > high:
                return f"超过节点上界 {high:g}"
            return ""
        if text in data:
            return ""
        joined = ", ".join(sorted(data))
        return f"反查不到枚举源（可用：{joined[:160] or '无'}）"

    async def _external(self, node: str, key: str) -> list[str]:
        if self._extra_options is None:
            return []
        try:
            return [t for t in (await self._extra_options(node, key)) if _norm(t)]
        except Exception:  # noqa: BLE001 - 曲库读不到就当没有这个枚举源
            return []

    # ---- ② 拓扑相容 / 跳过规则 / 文本卫生 ----

    def _check_topology(self, tag: str, steps: list[dict[str, Any]],
                        issues: PlanIssues) -> None:
        """步骤序与 dag_contract.requires 拓扑相容：前置排在后面就是错。

        前置**没上卡**是允许的（拦截器会补齐），界面上画成虚边——
        「乱序容忍但逼依赖显式化」里被禁的是顺序颠倒，不是省略。
        """
        seq_of = {s["node"]: s["seq"] for s in steps}
        for step in steps:
            for dep in step["requires"]:
                if dep in seq_of and seq_of[dep] > step["seq"]:
                    issues.error(f"计划 {tag} 第 {step['seq']} 步（{step['node']}）"
                                 f"排在它的前置「{dep}」之前，与 DAG 契约冲突。")

    def _explicit_call_nodes(self) -> set[str]:
        """既在 DAG 里、又标了「必须 LLM 显式调用」的节点集合。

        标记来自 ``dag_contract``（``NodeContract.explicit``）——与执行期拦截器
        （``orchestration.Interceptor._ensure_deps``）读的是服务端同一个
        ``require_explicit_call``，所以两边不会各说一套。
        """
        return {n for n in self.contract.names
                if (self.contract.get(n) is not None and self.contract.get(n).explicit)}

    def _check_explicit_deps(self, tag: str, steps: list[dict[str, Any]],
                             issues: PlanIssues) -> None:
        """卡上必须列出「不会自动补齐」的前置，否则执行轮跑到那一步必炸。

        执行期拦截器**只**自动补齐没标 ``require_explicit_call`` 的依赖；标了的那几个
        （``filter_clips`` / ``group_clips`` / ``script_template_rec`` / ``transition_rec``
        / ``text_rec``）一旦缺失就抛 ValueError：

            group_clips 需要你直接调用并传入创意决策参数，不能自动补齐。

        真机实测：p1 卡只列了 ``group_clips`` 却漏了 ``script_template_rec``，
        执行到 ``generate_script`` 被拦下——用户已经点过确认，却注定失败。

        ``_check_topology`` 写明「前置没上卡是允许的（拦截器会补齐）」，
        这对可自动补齐的依赖成立；对这几个不成立，所以补这一道。

        查的是**传递闭包**：漏掉的可能是间接前置（``generate_script ← script_template_rec``，
        而卡上只写了 ``generate_script``）。
        """
        explicit = self._explicit_call_nodes()
        if not explicit:
            return                  # 契约没接上／没有这类节点：退回原行为
        present = {s["node"] for s in steps}
        reported: set[tuple[str, str]] = set()
        for step in steps:
            queue: list[str] = list(step["requires"])
            seen: set[str] = set()
            while queue:
                dep = queue.pop(0)
                if dep in seen:
                    continue
                seen.add(dep)
                if dep not in present:
                    if dep in explicit and (dep, step["node"]) not in reported:
                        reported.add((dep, step["node"]))
                        issues.error(
                            f"计划 {tag} 的步骤「{step['node']}」依赖「{dep}」，"
                            f"但 {dep} 不会自动补齐（它需要你传入创意决策参数）。"
                            f"请把 {dep} 也写进这张计划的 steps，排在 {step['node']} 之前。")
                    continue            # 没上卡的中间节点：继续顺着它的前置查
                contract = self.contract.get(dep)
                if contract is not None:
                    queue.extend(contract.requires)

    def _check_skip_rules(self, tag: str, steps: list[dict[str, Any]],
                          issues: PlanIssues) -> None:
        """skippable 只在没有下游依赖踩着时成立；否则降级为不可跳并给角标原因。"""
        present = {s["node"] for s in steps}
        for step in steps:
            if not step["skippable"]:
                continue
            blocked = (self.contract.downstream(step["node"]) - {step["node"]}) & present
            if blocked:
                step["skippable"] = False
                step["skip_reason"] = f"下游「{'、'.join(sorted(blocked))}」依赖它的产物"
                issues.warn(f"计划 {tag} 第 {step['seq']} 步（{step['node']}）本可跳过，"
                            f"但{step['skip_reason']}，已置为不可跳。")

    def _check_duplicates(self, tag: str, steps: list[dict[str, Any]],
                          issues: PlanIssues) -> None:
        seen: set[str] = set()
        for step in steps:
            if step["node"] in seen:
                issues.error(f"计划 {tag} 里节点「{step['node']}」出现了多次。")
            seen.add(step["node"])

    def _check_hygiene(self, tag: str, texts: Sequence[Any], issues: PlanIssues) -> None:
        """只拦一种文本错误：词表里没有的别名/臆造工具名（英文原名由出口替换器处理）。

        词表要含真实参数键：``material_id`` 这类名字是入参不是节点，模型在 why/expectation
        里提它是正常表达，拦下来只会逼它换个说法重试一轮。

        同理要含**节点产出字段名**（``asr_segments``/``clip_captions``/``groups``…）：
        模型在为什么/预期里引用上游产物字段是最自然的写法，真机实测它连写三次
        「引用不存在的名字「asr_segments」」，卡连着两轮出不来。产出字段不是臆造的工具名，
        该放行。
        """
        vocab = (self.vocabulary() | self.param_keys() | self.enum_values()
                 | self.output_keys())
        for text in texts:
            for token in _ALIAS.findall(_norm(text or "")):
                if token in vocab or self.resolve_tool(token) is not None:
                    continue
                issues.error(f"计划 {tag} 的文案引用了不存在的名字「{token}」"
                             f"（词表里没有，界面换不成中文）。")

    def output_keys(self) -> set[str]:
        """节点**产出**的字段名（不是入参）。

        来源只有一处：剪辑服务端各节点 ``process`` 的返回键。主服务取不到那些声明，
        所以这里按「模型会写进文案的产出字段」维护一份显式清单——比让它撞墙再猜可靠。
        清单只用于放行文案里的名字，不参与任何执行判定，写多了也不会放宽别的东西。
        """
        return set(_OUTPUT_FIELD_NAMES)

    # ---- ⑤ execute 帧：点击编译结果 ----

    def validate_execute(self, plan: Mapping[str, Any],
                         frame: Mapping[str, Any]
                         ) -> tuple[dict[str, Any], PlanIssues]:
        """选中版本 + 枚举终值 + 跳过勾选 + 自定义诉求。

        枚举值仍要反查（这些是**承诺**，会直接拼进节点调用参数）；
        ``custom_text`` 不做枚举校验，只受条数/长度上限与注入转义（这些是**诉求**）。
        """
        issues = PlanIssues()
        steps = {int(s["seq"]): s for s in plan.get("steps") or []}
        if not steps:
            issues.error("这张计划卡没有可执行的步骤。")
            return {}, issues
        selected = _norm(frame.get("selected_plan"))
        if selected and selected != _norm(plan.get("plan_id")):
            issues.error(f"选中的计划「{selected}」与卡面（{plan.get('plan_id')}）对不上。")
        param_finals: dict[tuple[Any, str], str] = {}
        for item in frame.get("param_finals") or []:
            if not isinstance(item, Mapping):
                issues.error("参数终值形状不对。")
                continue
            step = self._step_at(steps, item.get("step_seq"), issues, "参数终值")
            if step is None:
                continue
            key = _norm(item.get("key"))
            opt = next((o for o in step["param_options"] if o["key"] == key), None)
            if opt is None:
                issues.error(f"第 {step['seq']} 步（{step['node']}）卡上没有参数「{key}」。")
                continue
            value = _as_card_value(item.get("value"))
            if value not in {o["value"] for o in opt["options"]}:
                issues.error(f"第 {step['seq']} 步「{key}」的值「{value}」不在选项里"
                             f"——承诺过的枚举不许在确认时临时改。")
                continue
            param_finals[(step["seq"], key)] = value
        skips: list[int] = []
        for item in frame.get("skips") or []:
            raw_seq = item.get("step_seq") if isinstance(item, Mapping) else item
            step = self._step_at(steps, raw_seq, issues, "跳过项")
            if step is None:
                continue
            if not step["skippable"]:
                issues.error(f"第 {step['seq']} 步（{step['node']}）不可跳过"
                             f"（{step['skip_reason'] or '它被下游依赖踩着'}）。")
                continue
            if step["seq"] not in skips:
                skips.append(step["seq"])
        overrides: list[dict[str, Any]] = []
        for item in frame.get("overrides") or []:
            if not isinstance(item, Mapping):
                issues.error("自定义诉求形状不对。")
                continue
            text = _norm(item.get("value"))
            if not text:
                continue               # 「其他」点开又留空 → 回落同组枚举，不产生空诉求
            if _norm(item.get("kind")) not in ("", "custom_text"):
                issues.error(f"自定义诉求的 kind 只能是 custom_text（收到 {_norm(item.get('kind'))}）。")
                continue
            if len(text) > CUSTOM_MAX_CHARS:
                issues.error(f"自定义诉求过长（{len(text)} > {CUSTOM_MAX_CHARS} 字）。")
                continue
            step = self._step_at(steps, item.get("step_seq"), issues, "自定义诉求",
                                 allow_none=True)
            if step is None and item.get("step_seq") is not None:
                continue
            overrides.append({"step_seq": None if step is None else step["seq"],
                              "key": _norm(item.get("key")) or "_general",
                              "value": text})
        if len(overrides) > CUSTOM_MAX_ITEMS:
            issues.error(f"自定义诉求最多 {CUSTOM_MAX_ITEMS} 条，收到 {len(overrides)} 条。")
            return {}, issues
        # 同组枚举与「其他」都给了值：以 custom 为准并记警告（doc §8）
        superseded: set[tuple[int, str]] = set()
        for item in overrides:
            pair = (item["step_seq"], item["key"])
            if pair in param_finals:
                issues.warn(f"第 {item['step_seq']} 步「{item['key']}」同时给了枚举值"
                            f"（{param_finals[pair]}）与自定义诉求，以自定义为准。")
                param_finals.pop(pair)
                superseded.add(pair)      # 也不回落卡面默认值：那是用户刚放弃的承诺
        resolved: list[dict[str, Any]] = []
        for seq in sorted(steps):
            if seq in skips:
                continue
            step = steps[seq]
            resolved.append({
                "seq": seq,
                "node": step["node"],
                "params": {o["key"]: param_finals.get((seq, o["key"]), o["default"])
                           for o in step["param_options"]
                           if (seq, o["key"]) not in superseded},
                "skills_hint": list(step["skills_hint"]),
                "why": step["why"],
                "expectation": step["expectation"],
            })
        return {
            "plan_id": _norm(plan.get("plan_id")),
            "label": _norm(plan.get("label")),
            "goal": _norm(plan.get("goal")),
            "steps": resolved,
            "skipped": skips,
            "custom": [dict(o, value=neutralize(o["value"])) for o in overrides],
            "custom_raw": list(overrides),
            "skills_hint": sorted({n for s in steps.values() for n in s["skills_hint"]}),
        }, issues

    @staticmethod
    def _step_at(steps: Mapping[int, Mapping[str, Any]], raw_seq: Any,
                 issues: PlanIssues, what: str, *,
                 allow_none: bool = False) -> dict[str, Any] | None:
        if raw_seq is None or _norm(raw_seq) == "":
            if allow_none:
                return None
            issues.error(f"{what}缺少 step_seq。")
            return None
        value = _num(raw_seq)
        step = steps.get(int(value)) if value is not None else None
        if step is None:
            issues.error(f"{what}指向不存在的第 {_norm(raw_seq)} 步。")
            return None
        return dict(step)


class SubmitPlanTool(Tool):
    """规划轮唯一的产出通道：把候选计划交给服务端四重校验。

    校验不过返回 ``ToolError``——失败文本原样回喂，模型据此重提一次；
    第二次仍不过就不再磨它：明确要求它向用户如实说明，而不是无限打回。
    """

    def __init__(self, gate: PlanGate, *, max_retries: int = 1) -> None:
        self._gate = gate
        self._max_retries = max_retries

    @property
    def name(self) -> str:
        return "submit_plan"

    @property
    def display_name(self) -> str:
        return "提交候选计划"

    @property
    def description(self) -> str:
        return (
            "提交 1~3 个候选剪辑计划供用户在计划卡上确认（规划轮的唯一出口）。"
            "每个计划是一列步骤：每步给 node（必须是真实存在的剪辑节点名）、"
            "why（为什么做这一步）、expectation（预期产出），可选 skills_hint"
            "（这一步要用的技能名）、skippable（只有没有下游依赖时才给跳过开关）、"
            "param_options（节点真实参数的候选值，每个值必须能在节点 schema 或"
            "曲库标签里反查到；节点没有的参数不许写，用户另有诉求就走卡上的「其他」）。"
            "多个候选之间必须在思路上有真实差异（label 与 steps 至少一处不同），"
            "不许同一方案改个名字充数。服务端会逐条校验，不通过会打回重写。\n"
            "**你可以提方案，但不能替用户偷偷定事。** 凡是会影响成片效果的取值"
            "（时长、出镜比例、保留哪些片段、用哪个模板、要不要配音、BGM 音量…），"
            "二选一：\n"
            "① 写进该步的 param_options 当开关——用户会在计划卡上看到并自己勾选，"
            "这就是「让用户知道并做主」；\n"
            "② 判断不了用户要哪种，就先调用 ask_user 弹窗问清楚（给 2~6 个有真实"
            "差异的选项），拿到答复再提交计划。\n"
            "**绝不允许**：自己挑一个值填进去、不告诉用户；或者在 why/expectation 里"
            "用「60~120 秒可选」这种模糊说法把决定权含混过去。"
            "你选了默认值也必须在卡上显示成一个开关，让用户有机会改。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "plans": {
                    "type": "array",
                    "maxItems": PLAN_MAX_CANDIDATES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "plan_id": {"type": "string", "description": "p1/p2/p3"},
                            "label": {"type": "string", "description": "卡面标题，一句话思路"},
                            "goal": {"type": "string", "description": "这版要达成什么"},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "seq": {"type": "integer"},
                                        "node": {"type": "string"},
                                        "why": {"type": "string"},
                                        "expectation": {"type": "string"},
                                        "skippable": {"type": "boolean"},
                                        "skills_hint": {"type": "array",
                                                        "items": {"type": "string"}},
                                        "param_options": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "key": {"type": "string"},
                                                    "default": {},
                                                    "options": {
                                                        "type": "array",
                                                        "items": {
                                                            "type": "object",
                                                            "properties": {
                                                                "value": {},
                                                                "display": {"type": "string"},
                                                            },
                                                        },
                                                    },
                                                },
                                                "required": ["key", "options"],
                                            },
                                        },
                                    },
                                    "required": ["node", "why"],
                                },
                            },
                        },
                        "required": ["plan_id", "label", "steps"],
                    },
                },
            },
            "required": ["plans"],
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, plans: Any) -> str:
        normalized, issues = await self._gate.validate({"plans": plans})
        if issues.errors:
            return ToolError(self.name, "计划校验未通过，修正后重新提交一次：\n- "
                             + "\n- ".join(issues.errors[:8]))
        ctx = _current_hook_ctx.get()
        if ctx is not None:
            ctx.state.plan_candidates = normalized
            ctx.state.plan_warnings.extend(issues.warnings)
            # 候选计划同时落到本 run 的指针行：确认接口据此取回**服务端自己校验过的那一份**，
            # 浏览器只负责回传「选了哪张卡、点了哪些开关」，不回传计划本体。
            cp = ctx.extras.get("checkpoint")
            if cp is not None:
                cp.plan["candidates"] = normalized
                # GET /plans/{id} 重放时读的就是这一位：不落则刷新后告警全丢。
                cp.plan["warnings"] = list(issues.warnings)
        # 当轮落库不在这里登记：工具跑在 gather 起的子 Task 里，那里 set 的 contextvar
        # 回不到主 Task，assistant 行 drain 不到——统一交给 PlanCardHook.after_execute_tools。
        return json.dumps({
            "accepted": [p["plan_id"] for p in normalized],
            "warnings": issues.warnings,
            "note": "计划卡已回投给用户等待确认。本轮只需简短说明各版本的思路差异，"
                    "不要声称已经开始剪辑。",
        }, ensure_ascii=False)


class ConfirmPlanTool(Tool):
    """用户表达了确认执行计划的意图时，LLM 调此工具弹出计划确认弹窗。

    与 ``submit_plan`` 的区别：``submit_plan`` 提交新候选计划，``confirm_plan``
    把同会话已有的待确认计划卡重新推给前端弹窗——用户关了弹窗后在输入框打字
    「就按这版执行」时，LLM 调此工具让弹窗再弹一次，用户点「按此执行」进执行轮。
    """

    @property
    def name(self) -> str:
        return "confirm_plan"

    @property
    def display_name(self) -> str:
        return "确认计划"

    @property
    def description(self) -> str:
        return (
            "同会话已有待确认的计划卡时，调用此工具将其重新推给用户确认。"
            "由 LLM 根据用户消息的意图判断是否调用：用户想确认或执行已有计划时调用，"
            "提新需求或改需求时不调用。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self) -> str:
        ctx = _current_hook_ctx.get()
        if ctx is not None:
            ctx.extras["confirm_plan_requested"] = True
        return json.dumps({
            "note": "已请求弹出计划确认弹窗。请简短告诉用户在弹窗里点「按此执行」确认。",
        }, ensure_ascii=False)


# ---- 计划卡当轮落库（与 media_replay 同一条 contextvar 通道）----
#
# record_plan_card / drain_plan_cards / reset_plan_cards 住在 ``plan_replay``（文件顶部
# 导入并转出）：``MessagesRepo`` 也要用它们，而存储层不该反向 import 本模块。


# ---- 执行轮注入 ----

_APPROVED_OPEN = (
    "<approved_plan>\n"
    "用户已在计划卡上确认下面这份计划。卡上的参数值是校验过的**承诺**，照单执行；"
    "计划本身是强提示不是硬调度（依赖由服务端拦截器兜底），"
    "但要改动参数或跳过步骤必须在答复里向用户说明理由：\n")

_CUSTOM_OPEN = (
    "<user_custom_requests>\n"
    "用户在计划卡的「其他 / 整体补充」里另外提了诉求，这些**没有**经过枚举校验。"
    "逐条处理：能力内能实现就实现；做不到必须明确说哪一步做不到并给替代方案，"
    "不得静默吞掉，不得臆造资源或参数：\n")


def render_injections(compiled: Mapping[str, Any]) -> list[str]:
    """两段分离注入：``<approved_plan>``（承诺）+ ``<user_custom_requests>``（诉求）。

    段内保留机器名（模型与校验层都用机器名），界面侧由块 A 的出口替换器换轨。
    """
    lines = [_APPROVED_OPEN,
             f"计划 {compiled.get('plan_id') or ''}：{compiled.get('label') or ''}"]
    if compiled.get("goal"):
        lines.append(f"目标：{compiled['goal']}")
    for step in compiled.get("steps") or []:
        bits = [f"{step['seq']}. {step['node']}"]
        params = step.get("params") or {}
        if params:
            bits.append("参数 " + ", ".join(f"{k}={_norm(v)}" for k, v in params.items()))
        if step.get("skills_hint"):
            bits.append("技能 " + ", ".join(step["skills_hint"]))
        if step.get("why"):
            bits.append(f"理由：{step['why']}")
        if step.get("expectation"):
            bits.append(f"预期：{step['expectation']}")
        lines.append("；".join(bits))
    if compiled.get("skipped"):
        lines.append("用户要求跳过的步骤序号：" + "、".join(str(s) for s in compiled["skipped"]))
    lines.append("</approved_plan>")
    out = ["\n".join(lines)]
    custom = compiled.get("custom") or []
    if custom:
        rows = [_CUSTOM_OPEN]
        for item in custom:
            where = "整体" if item.get("step_seq") is None else f"第 {item['step_seq']} 步"
            rows.append(f"- {where}（{item.get('key') or '_general'}）：{item['value']}")
        rows.append("</user_custom_requests>")
        out.append("\n".join(rows))
    return out


async def preload_skills(loader: Any, names: Sequence[str], *,
                         resident: Iterable[str] = ()) -> list[str]:
    """执行轮起步预注入：各步声明的技能正文直接拼进上下文（不赌模型点 load_skill）。

    ``always=true`` 的技能已由 ``SkillManifestContextSource`` 常驻注入，这里不重复给。
    """
    if loader is None or not names:
        return []
    skip = set(resident)
    sections: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = _norm(name)
        if not key or key in seen or key in skip:
            continue
        seen.add(key)
        try:
            skill = await loader.get(key)
        except Exception:  # noqa: BLE001 - 预注入是增益，读不到就照常跑
            continue
        if skill is None or not getattr(skill, "available", True) or not skill.body:
            continue
        sections.append(f'<skill_instructions name="{skill.name}">\n{skill.body}\n'
                        f'</skill_instructions>')
    return sections


def reconcile(plan_steps: Sequence[Mapping[str, Any]],
              actual_calls: Sequence[str]) -> dict[str, list[str]]:
    """⑦ 对账：实际 − 计划 = 计划外步骤；计划 − 实际（跑完仍未调）= 未履行。

    **只观测不拦截**：返回的只是给执行记录面板的两列角标数据（机器名，出口换中文），
    不改任何一次调用。
    """
    planned = [_norm(s.get("node")) for s in plan_steps]
    called = [_norm(n) for n in actual_calls if _norm(n)
              and _norm(n).removeprefix(_MCP_PREFIX) not in NON_STEP_TOOLS]
    extra = [n for i, n in enumerate(called) if n not in planned and n not in called[:i]]
    return {"extra": extra,
            "unfulfilled": [n for n in planned if n not in called]}


def _coerce_plans(payload: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(payload, Mapping):
        payload = payload.get("plans", payload)
    if isinstance(payload, Mapping):        # {"p1": {...}} 这种写法也接住
        payload = list(payload.values())
    if not isinstance(payload, list):
        return None
    return [p for p in payload if isinstance(p, Mapping)]


# ---- 规划轮收尾的事实核对 ------------------------------------------------


# 规划轮里模型写一句「续跑卡已提交，等你在计划卡上确认」而本轮**没有**任何 submit_plan
# 成功记录时，界面上不会多出那张卡：用户无从点确认，这一轮就死在这里（真机实测撞到过）。
# 名词与「已完成」两个条件都要命中，才把「要不要我出计划卡？」这类正常回答当成假称交付。
# 完成态的写法比第一次预想的野：真机又漏过一句「这次提交成功了，计划卡已回投给你，请确认」
# ——既不是「已提交」也不是「等你确认」，所以动词按同一意图的几种说法都认。
_CARD_NOUN = re.compile(r"(计划卡|续跑卡|候选计划)")
_CARD_DONE = re.compile(
    r"(已(?:经)?(提交|生成|出卡|发出|回投|给出|送到|挂上)|提交成功"
    r"|等你确认|等你在|请确认)")


def claims_plan_card(text: Any) -> bool:
    """这句答复是否在声称「计划卡已经交到界面上了」。"""
    body = _norm(text)
    return bool(body) and bool(_CARD_NOUN.search(body)) and bool(_CARD_DONE.search(body))


# 执行轮里同样的假称换成另一副面孔：一句「时间线重排完成，时长 8.64 秒」而本轮一次步骤
# 都没调用（真机实测：确认帧落地 2 秒收尾、iteration 0、Storyline 没收到任何请求），
# 那串数字是会话历史里上一轮的旧产物，被复述成本轮产出。
# 完成态措辞命中即算（真机补漏：「渲染完成，成片返件如下」没写节点名，按节点名匹配就漏了）；
# 「要 A 还是 B？」这类正当反问不含完成态词，仍不会被当成假称执行。
_STEP_DONE = re.compile(
    r"(已完成|完成|已生成|生成了|已渲染|渲染完成|已产出|产出|重排完|排好|跑完|做好了|已落地"
    r"|返件|播放直链|可播放成片)")


def claims_step_executed(text: Any) -> bool:
    """这句答复是否在声称「本轮已经跑出了产物」。

    调用方（``AgentOnceRun._drive``）的前提是**批准计划的步骤一个都没调用**，所以这里
    只看措辞，不再要求答复点名节点：真机上假称偏偏爱说「渲染完成，成片返件如下」，
    一句都不提节点名，按节点名匹配就漏了。
    """
    return bool(_STEP_DONE.search(_norm(text)))


# ---- 规划轮提示段（Run A）-------------------------------------------------

async def pending_continuation(checkpoint: Any, session_id: str) -> Mapping[str, Any] | None:
    """本会话最近一条「确认过、但计划没跑完」的执行轮；没有就回 None。

    真机踩到的死胡同：执行轮跑到一半问了一句「A 还是 B？」就收尾（对账如实记
    「未履行 时间线编排、成片渲染」），用户对 A/B 的回答按默认入口落进**新一轮规划轮**，
    而那张注册表物理不含剪辑节点——模型只能回「我这边没有调用它们的执行入口」，
    一张确认过的计划就此续不上。这里把那份未履行状态查回来，交给规划轮当续跑依据。

    只看最近一条执行轮：它跑完了就没有待续，不会把更早的旧账翻出来拦今天的新诉求。
    """
    if checkpoint is None:
        return None
    for row in await checkpoint.list_for_session(session_id):
        plan_run_id = str(row.get("plan_run_id") or "")
        if not plan_run_id:
            continue                     # 规划轮与普通轮：它们不是「确认后的执行」
        audit = (row.get("plan") or {}).get("audit") or {}
        unfulfilled = [str(n) for n in (audit.get("unfulfilled") or []) if n]
        if not unfulfilled:
            return None                  # 最近一条执行轮把计划跑完了
        candidates = ((await checkpoint.row(plan_run_id) or {}).get("plan") or {}) \
            .get("candidates") or []
        wanted = str(audit.get("plan_id") or "")
        label = next((str(c.get("label") or "") for c in candidates
                      if str(c.get("plan_id") or "") == wanted), "")
        return {"run_id": str(row.get("run_id") or ""), "plan_run_id": plan_run_id,
                "plan_id": wanted, "label": label, "unfulfilled": unfulfilled,
                "asked": _norm(audit.get("reason"))[:300]}
    return None


def _session_history_lines(history: Sequence[Mapping[str, Any]],
                           artifacts: Sequence[str] = ()) -> list[str]:
    """同会话之前的 checkpoint 摘要 → 给模型看的历史段。

    用户在同一个会话窗口里发的每一条消息，后端都按 ``session_id`` 关联了 checkpoint。
    但新轮的 LLM 只看得到 messages 表里的对话文字，看不到 checkpoint 里的计划卡内容和
    执行状态——于是误判「计划没落到服务端」「素材没到位」。这里把同会话最近几条
    checkpoint 的摘要注入 system 段，让 LLM 知道之前做过什么。

    ``artifacts`` 是同会话已产出的节点名列表，让 LLM 知道可以用 read_node_history
    读哪些产物（例如 plan_timeline 里存着 LLM 修正后的时间线）。
    """
    lines = ["<同会话历史>",
             "本会话之前有过以下执行（最近的在前），供你判断用户这条消息的性质："]
    for h in history:
        run_id = h.get("run_id", "")
        status = h.get("status", "")
        msg = neutralize((h.get("message") or "")[:80])
        iter_n = h.get("iteration", 0)
        if h.get("is_planning"):
            cands = h.get("candidates") or []
            if cands:
                parts = []
                for c in cands:
                    steps = ", ".join(c.get("steps") or [])
                    parts.append(f"{c.get('plan_id', '')}「{c.get('label', '')}」(步骤: {steps})")
                cand_brief = "; ".join(parts)
            else:
                cand_brief = "（未出卡）"
            lines.append(f"· 规划轮 {run_id} [{status}] iter={iter_n} 消息「{msg}」→ {cand_brief}")
        elif h.get("is_execution"):
            plan_rid = h.get("plan_run_id", "")
            audit_pid = h.get("audit_plan_id", "")
            unfulfilled = h.get("audit_unfulfilled") or []
            unful = "、".join(unfulfilled) if unfulfilled else "无"
            lines.append(f"· 执行轮 {run_id} [{status}] iter={iter_n} 源自规划 {plan_rid} "
                         f"执行计划 {audit_pid} 未履行: {unful}")
        else:
            lines.append(f"· 普通轮 {run_id} [{status}] iter={iter_n} 消息「{msg}」")
    if artifacts:
        _hints = {
            "understand_clips": "每个镜头的视觉描述(谁出镜/什么场景)",
            "split_shots": "镜头切分(时间码/分辨率,无画面内容)",
            "asr": "语音转文字",
            "plan_timeline": "时间线编排",
            "filter_clips": "镜头筛选",
            "group_clips": "镜头分组",
            "select_BGM": "配乐选取",
            "render_video": "渲染成片",
            "speech_rough_cut": "原声粗剪",
            "generate_script": "文案生成",
        }
        parts = [f"{n}({_hints.get(n, '产物')})" for n in artifacts]
        lines.append(f"已产出节点（可用 read_node_history 读取）：{', '.join(parts)}")
    lines.append(
        "如果用户在续跑或询问之前的任务，上面的历史就是上下文——"
        "不要说「计划没落到服务端」或「素材没到位」，它们都在服务端，只是不在本轮的执行上下文里。")
    lines.append("</同会话历史>")
    return lines


async def planning_section(gate: PlanGate, *, feedback: str = "",
                           prior: Sequence[Mapping[str, Any]] = (),
                           pending: Mapping[str, Any] | None = None,
                           session_history: Sequence[Mapping[str, Any]] = (),
                           session_artifacts: Sequence[str] = (),
                           has_pending_plan: bool = False) -> str:
    """规划轮的 system 段：本轮**没有**剪辑执行工具，出口只有 ``submit_plan`` 或直接回答。

    可用节点白名单在这里给模型（机器名）：它只能从这份名单里挑，写名单外的名字
    会被四重校验打回。界面给人看的是块 A 换轨后的 ``*_display``，不在这层拼。

    节点参数事实也在这段里一次给全（``gate.knob_facts()``，与卡面校验同一套判据）：
    规划轮唯一的查参路径是猜 Store 键名，而执行前 Store 必空——真机实测模型为此
    连着错了六十次。不写清「不用再查」，这段提示就成了烧迭代预算的指令。

    ``prior`` 是「换一版」那张旧卡的候选：卡面步骤不在会话历史里（历史只有那一轮的
    文字摘要），不把它写进提示，模型既无从做出可辨别的差异、也常常干脆不再出卡。

    ``has_pending_plan`` 为真时，提示词里加一段 ``confirm_plan`` 工具的使用说明：
    用户在输入框打字确认已有计划（而非点按钮）时，LLM 调 ``confirm_plan`` 弹窗，
    不要反复说「本轮是规划轮」把用户困住。
    """
    nodes = sorted(gate.whitelist())
    lines = [
        "<planning_round>",
        "本轮是**规划轮**：工具集里没有剪辑执行节点，任何剪辑动作都不会发生。",
    ]
    if has_pending_plan:
        lines += [
            "同会话已有一张待确认的计划卡。confirm_plan 工具可将其重新推给用户确认。",
            "请根据用户这条消息的真实意图判断：用户是否想确认/执行那张已有计划。",
            "是 → 调用 confirm_plan；用户在提新需求或改需求 → 走下面的分支。",
        ]
    lines += [
        "判断用户这条消息的性质：",
        "- 纯咨询（问现状、问时长、问用了什么素材，不要求任何改动） → 直接正常回答，"
        "不要调用 submit_plan，不要为了走流程编一份计划。",
        "- 用户要求改成片或提出新诉求（换音乐、改音轨、调时长、改字幕、换画面、"
        "调比例、重新编排、修改已有视频的任何方面） → 这是剪辑任务，必须调用 "
        "submit_plan 提交 1~3 个有真实差异的候选计划。"
        "不要先读产物再口头描述方案让用户确认——查完直接出卡，"
        "把修改方案写进计划的 steps 和 why 里。",
    ]
    lines += [
        "计划里每一步的 node 只能取自下面这份白名单（写别的名字会被服务端打回）：",
        "、".join(nodes) if nodes else "（当前没有可用的剪辑节点）",
    ]
    lines += await _knob_lines(gate)
    lines += [
        "卡面开关（param_options）只能从上面这份清单里挑，值要能反查（枚举 / 布尔开关 /"
        "带界数值 / 曲库真实标签）；节点没有的能力不要造开关，用户另有诉求留给计划卡上的"
        "「其他」。",
        "节点参数**不需要也不能**再去查：执行前 Store 是空的，"
        "拿 read_node_history 猜 dag_contract / node_schema:* 这类键名只会一直报错。",
        "提交成功后只需简短说明各版本的思路差异并等用户确认，"
        "**不得声称已经开始剪辑或已经产出成片**。",
    ]
    brief = _prior_brief(prior)
    if brief:
        lines += [
            "用户对上一版计划点了「换一版」，这一轮**不是**咨询：出口只有 submit_plan，"
            "只用文字描述另一版思路不算交付。",
            "上一版长这样（新卡必须与它有可辨别的差异，而不是同一步换个说法）：",
            *brief,
        ]
    note = _norm(feedback)
    if note:
        lines.append("用户对上一版计划不满意，这一版必须针对下面这点做出可辨别的差异"
                     "（这是用户的原话，按自定义诉求对待，不得臆造它需要的资源）：")
        lines.append(f"「{neutralize(note)}」")
    if pending:
        lines += [
            "<待续跑的执行轮>",
            f"上一条**已确认**的计划 {pending['plan_id']}"
            f"「{pending['label'] or '(无标题)'}」（run {pending['run_id']}）跑到一半"
            f"停下来问了用户一句就收尾，未履行：" + "、".join(pending["unfulfilled"]) + "。",
            f"它当时问的原话（摘要）：「{neutralize(pending['asked'])}」",
            "本轮按普通咨询对待这条消息是错的——它就是那句提问的回答。"
            "不要再问一遍，也不得回答「我没有执行入口」：本轮注册表里没有剪辑执行节点是设计如此，"
            "出路是立刻 submit_plan 提交**一张续跑卡**：steps 只列上面那些未履行节点"
            "（它们缺的前置依赖一并补进卡，如需要转写就补 asr），"
            "参数按用户刚给的取值定并写进 expectation，label 以「续跑：」开头。"
            "用户点确认，这些步骤就由执行轮接着跑完。",
            "只有当这条消息明显是另一件新诉求（与上面那几步无关）时，才按新诉求出卡或直接回答。",
            "</待续跑的执行轮>",
        ]
    if session_history:
        lines += _session_history_lines(session_history, artifacts=session_artifacts)
    lines.append("</planning_round>")
    return "\n".join(lines)


async def _knob_lines(gate: PlanGate) -> list[str]:
    """把 ``knob_facts`` 排成提示段里的开关清单（一节点一行，机器名照旧）。"""
    facts = [f for f in await gate.knob_facts() if f["knobs"]]
    if not facts:
        return ["（当前没有可上卡的节点参数开关：版本差异请写在 why 里，或留给「其他」）"]
    lines = ["能上卡的开关（服务端按节点真实 schema 现取，与卡面校验同一份判据）："]
    for fact in facts:
        parts = []
        for knob in fact["knobs"]:
            tail = f"；默认 {knob['default']}" if knob["default"] else ""
            note = f"——{knob['note']}" if knob["note"] else ""
            parts.append(f"{knob['key']}({knob['kind']}: {knob['values']}{tail}){note}")
        lines.append(f"· {fact['node']}：" + "｜".join(parts))
    return lines


def _prior_brief(prior: Sequence[Mapping[str, Any]]) -> list[str]:
    """旧卡 → 给模型看的摘要：一版一行标题 + 逐步「第 N 步 节点名（已定的参数值）」。"""
    out: list[str] = []
    for plan in prior or ():
        if not isinstance(plan, Mapping):
            continue
        head = f"· 版本 {plan.get('plan_id') or '?'}：{plan.get('label') or '（无标题）'}"
        goal = _norm(plan.get("goal"))
        if goal:
            head += f"（目标：{goal}）"
        out.append(head)
        for step in plan.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            opts = "；".join(
                f"{_norm(o.get('key'))}={_norm(o.get('default'))}"
                f"（候选 {'/'.join(_norm(c.get('value')) for c in (o.get('options') or []) if isinstance(c, Mapping))}）"
                for o in (step.get("param_options") or [])
                if isinstance(o, Mapping) and o.get("options"))
            tail = f"（{opts}）" if opts else ""
            out.append(f"    第 {step.get('seq')} 步 {step.get('node')} "
                       f"{_norm(step.get('why'))[:40]}{tail}")
    return out


# ---- 计划卡回投 + 当轮落库（与 MediaCardHook 同一条通道）--------------------

class PlanCardHook(AgentHook):
    """把本轮 ``submit_plan`` 通过的候选计划回投成计划卡帧，并交给当轮落库通道。

    两件事都在 ``after_execute_tools`` 做，而不是在工具里做：工具跑在 ``gather`` 起的
    子 Task 里，那里 set 的 contextvar 不会回到主 Task，assistant 行落库时就 drain 不到。
    本钩子与 ``MessagesRepo.append`` 在同一条 await 链上（``_drive`` 内），所以传得过去。

    ``cp_mgr`` 让 ``confirm_plan`` 工具能找到同会话已有的待确认计划卡并重新推帧弹窗。
    """

    def __init__(self, mq: MessageQueue, cp_mgr: Any = None, *,
                 topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._cp_mgr = cp_mgr
        self._topic = topic

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        candidates = context.state.plan_candidates
        if candidates and not context.state.plan_card_pushed:
            # 这一位落在可恢复状态上：挂起→续跑之后不会又推一张一模一样的卡。
            context.state.plan_card_pushed = True
            session_id = context.session.session_id
            run_id = context.extras.get("run_id")
            # 落库那份多带一个 plan_run_id：历史重放时前端要靠它定位确认帧发往哪条 run
            # （实时那帧本身带 run_id，不必重复）。
            warnings = list(context.state.plan_warnings)
            record_plan_card([dict(p, plan_run_id=run_id) for p in candidates], warnings)
            await self._mq.publish(
                self._topic, session_id,
                {"type": "plan", "session_id": session_id,
                 "run_id": run_id,
                 "plans": [dict(p) for p in candidates],
                 "warnings": warnings},
            )
        # confirm_plan 工具被调用时，把同会话已有的待确认计划卡重新推给前端弹窗。
        if context.extras.get("confirm_plan_requested") and self._cp_mgr is not None:
            context.extras["confirm_plan_requested"] = False
            session_id = context.session.session_id
            try:
                pending = await self._cp_mgr.pending_plan_for_session(session_id)
            except Exception:
                pending = None
            if pending is not None:
                await self._mq.publish(
                    self._topic, session_id,
                    {"type": "plan", "session_id": session_id,
                     "run_id": pending["plan_run_id"],
                     "plans": [dict(p) for p in pending["candidates"]],
                     "warnings": []},
                )


class PlanReconcileHook(AgentHook):
    """事后对账：批准计划的步骤 vs 本轮实际调用，偏差落 run B 记录——只观测不拦截。

    三个节点各司其职：``after_tool_call`` 累积实际调用清单（含轮询渲染这类非模型发起的
    调用，它们同样真实发生过；注册表里没有的工具不计——那一步压根没跑）；
    ``after_execute_tools`` 每批工具后算一次当前偏差，
    只在变化时发一帧（前端按 run_id 覆盖角标，不堆重复帧）；``finalize_content`` 是
    唯一同时拿得到**终答原文**（偏离理由）与 qa_parts 的位置，完整结论在那里落库。
    """

    def __init__(self, mq: MessageQueue | None = None, *,
                 topic: str = OUTBOUND_TOPIC) -> None:
        self._mq = mq
        self._topic = topic

    @staticmethod
    def _audit(context: AgentHookContext) -> dict[str, Any] | None:
        plan = context.state.approved_plan
        if not isinstance(plan, Mapping):
            return None                 # 普通轮 / 规划轮：没有批准计划可对账
        diff = reconcile(plan.get("steps") or [], context.state.calls_executed)
        diff["plan_id"] = plan.get("plan_id") or ""
        return diff

    async def after_tool_call(self, context: AgentHookContext, tool_name: str,
                              arguments: dict, result: Any, elapsed: float, *,
                              error: Exception | None = None,
                              call_id: str = "") -> None:
        if isinstance(error, UnknownToolError):
            return          # 调用从未发生：记进去等于把没跑的步当跑过
        context.state.note_executed(tool_name)

    async def after_execute_tools(self, context: AgentHookContext) -> None:
        audit = self._audit(context)
        if audit is None or self._mq is None:
            return
        if audit == context.state.audit_pushed:
            return                      # 这批工具没改变偏差，不再发重复帧
        # 去重位也在可恢复状态上：挂起前推过的那一版偏差，续跑后不会又推一次。
        context.state.audit_pushed = dict(audit)
        session_id = context.session.session_id
        await self._mq.publish(
            self._topic, session_id,
            {"type": "plan reconciliation", "session_id": session_id,
             "run_id": context.extras.get("run_id"), **audit},
        )

    def finalize_content(self, context: AgentHookContext,
                         content: str | None) -> str | None:
        audit = self._audit(context)
        if audit is None:
            return content
        # 偏离理由同屏：终答原文就是对账那一句的交代，抓这一条胜过另起一套机制。
        audit["reason"] = _norm(content)[:400]
        cp = context.extras.get("checkpoint")
        if cp is not None:
            cp.plan["audit"] = audit        # 指针行：执行记录面板按 run 取
        context.state.plan_audit = audit
        if isinstance(context.state.qa_parts, list):
            context.state.qa_parts.append({"type": "plan reconciliation", **audit})
        return content
