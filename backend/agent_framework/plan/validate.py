"""①~④ 四重校验器的**步骤级**那一半：node 白名单 / skills_hint / param_options，
外加逐步骤的 why/expectation 与 seq。纯代码，一次 LLM 都不叫。

跨步骤的结构判据（拓扑、显式依赖、跳过规则、重复步骤、重复开关、文案卫生）在
``structure.StructureChecks``——它拿的是一张完整的卡，不是单步。

事实一律问 ``PlanVocabulary``（什么节点存在、参数有哪些枚举源），这一层只管判。
"""

from __future__ import annotations

from typing import Any, Mapping

from .support import (PLAN_MAX_CANDIDATES, PLAN_STEPS_MAX, PlanIssues,
                      add_implicit_skills, as_card_value, clean, coerce_plans,
                      num, topology_view)
from .structure import StructureChecks
from .vocab import PlanVocabulary


class PlanValidator:
    """候选计划的服务端校验：errors 非空即打回，warnings 只在卡上挂角标。"""

    def __init__(self, vocab: PlanVocabulary, *,
                 skills: Any = None) -> None:
        self.vocab = vocab
        self._skills = skills
        # 跨步骤的结构判据在 structure 那一半：这里只管一步一步地归一化并判它
        self.structure = StructureChecks(vocab)

    # ---- ①~④ 计划校验 ----

    async def validate(self, payload: Any) -> tuple[list[dict[str, Any]], PlanIssues]:
        raw = coerce_plans(payload)
        issues = PlanIssues()
        if raw is None:
            issues.error('计划形状不对：应当是 {"plans": [{"plan_id","label","steps":[…]}] }。')
            return [], issues
        if not (1 <= len(raw) <= PLAN_MAX_CANDIDATES):
            issues.error(f"候选计划必须是 1~{PLAN_MAX_CANDIDATES} 个，收到 {len(raw)} 个。")
            return [], issues
        allowed = self.vocab.whitelist()
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
        tag = clean(plan.get("plan_id")) or f"p{index}"
        if tag in seen_ids:
            issues.error(f"plan_id「{tag}」重复。")
        seen_ids.add(tag)
        if not clean(plan.get("label")):
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
        ok = len(issues.errors) == before
        # 步骤级错误（参数不合规、缺 why…）不再**短路**掉下面这几条：拓扑与依赖检查
        # 只看节点名与 requires，拿原始步骤也判得出来。原先一有步骤级错误就 return，
        # 一次校验只暴露一层规则——真机实测模型交了两回才把「布尔开关要给两个候选」
        # 与「group_clips 必须显式上卡」先后试出来：白烧一轮迭代，还半路撞上失败守卫
        # 被推去问用户「这轮没有剪辑工具怎么办」（那一轮就是死路）。
        view = norm_steps if ok else topology_view(self.vocab.contract, steps)
        self.structure.topology(tag, view, issues)
        self.structure.explicit_deps(tag, view, issues)
        if ok:
            # 跳过规则要写回步骤字段（skippable / skip_reason），只在归一化过的步骤上跑。
            self.structure.skip_rules(tag, view, issues)
        self.structure.duplicates(tag, view, issues)
        if ok:
            self.structure.dup_param_options(tag, norm_steps, issues)
        # 文案卫生扫的是**原始** why/expectation：归一化只是 clean()，两处同文；
        # 用原始的还能覆盖到没归一化成功的步骤（它们同样会被替换器扫）。
        self.structure.hygiene(tag, [plan.get("label"), plan.get("goal"),
                                    *[text for step in steps
                                      if isinstance(step, Mapping)
                                      for text in (step.get("why"),
                                                   step.get("expectation"))]], issues)
        if not ok:
            return None
        return {
            "plan_id": tag,
            "label": clean(plan.get("label")),
            "goal": clean(plan.get("goal")),
            "steps": norm_steps,
        }

    async def _validate_step(self, step: Any, tag: str, index: int,
                             allowed: set[str], skills: Mapping[str, Any],
                             issues: PlanIssues) -> dict[str, Any] | None:
        if not isinstance(step, Mapping):
            issues.error(f"计划 {tag} 第 {index} 步不是对象。")
            return None
        node = clean(step.get("node"))
        # ① node ∈ ToolRegistry ∩ Storyline 白名单
        if node not in allowed:
            issues.error(f"计划 {tag} 第 {index} 步的节点「{node or '(空)'}」不存在"
                         f"——只能使用真实存在的剪辑节点，不许臆造。")
            return None
        tool = self.vocab.resolve_tool(node) or self.vocab.registry.get(node)
        declared = step.get("seq")
        if declared is not None and num(declared) is not None \
                and int(num(declared)) != index:
            issues.error(f"计划 {tag} 第 {index} 步的 seq={clean(declared)} 与位置不符："
                         f"步骤序必须与依赖序一致。")
            return None
        norm: dict[str, Any] = {
            "seq": index,
            "node": node,
            "why": clean(step.get("why")),
            "expectation": clean(step.get("expectation")),
            "tool_kind": "mcp" if getattr(tool, "contract", None) is not None else "tool",
            "skills_hint": [],
            "skippable": bool(step.get("skippable")),
            "skip_reason": "",
            "param_options": [],
            "requires": list(self.vocab.contract.get(node).requires)
            if self.vocab.contract.get(node) is not None else [],
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
            key = clean(name)
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
            if not (self.vocab.catalog.display(key) or getattr(skill, "display", "")):
                # 词表里没有这条技能：卡面与执行记录都会露出机器名（块 A 换不了轨）
                issues.error(f"计划 {tag} 第 {index} 步的技能「{key}」没有登记中文名，"
                             f"上卡会在界面露出机器名。")
                continue
            norm["skills_hint"].append(key)
        add_implicit_skills(norm, self.vocab.implicit_skills(norm["node"]), skills)

    # ④ param_options 的每个值可反查枚举源；节点没有的参数不许上卡

    async def _check_params(self, tag: str, index: int, step: Mapping[str, Any],
                            norm: dict[str, Any], issues: PlanIssues) -> None:
        raw_opts = step.get("param_options") or []
        if not isinstance(raw_opts, list):
            issues.error(f"计划 {tag} 第 {index} 步的 param_options 必须是列表。")
            return
        props = self.vocab.props_for(norm["node"])
        for raw in raw_opts:
            if not isinstance(raw, Mapping):
                issues.error(f"计划 {tag} 第 {index} 步的参数项不是对象。")
                continue
            key = clean(raw.get("key"))
            spec = props.get(key)
            if spec is None:
                issues.error(f"计划 {tag} 第 {index} 步（{norm['node']}）没有参数「{key}」，"
                             f"不许在卡上造这个开关；这类诉求请走「其他」。")
                continue
            options = raw.get("options")
            if not isinstance(options, list) or not options:
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」缺少 options。")
                continue
            source = await self.vocab.enum_source(norm["node"], key, spec)
            if source is None:
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」没有可反查的枚举源"
                             f"（既不是枚举/开关/带界数值，曲库也没有这组标签）。")
                continue
            # 卡面文字优先模型写的 display；没写就回落到**参数声明里的标签**（``value_labels``
            # 随契约从剪辑服务端带回来）——否则用户看到的是 portrait 这种机器名。
            labels = spec.get("value_labels") or {}
            values: list[dict[str, str]] = []
            picked: set[str] = set()
            for opt in options:
                value = opt.get("value") if isinstance(opt, Mapping) else opt
                text = as_card_value(value)
                reason = self._value_rejected(source, text)
                if reason:
                    issues.error(f"计划 {tag} 第 {index} 步「{key}」的值「{text}」{reason}。")
                    continue
                if text in picked:
                    continue
                picked.add(text)
                own = clean(opt.get("display")) if isinstance(opt, Mapping) else ""
                values.append({"value": text, "kind": source[0],
                               "display": own or str(labels.get(text) or "")})
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
            default = as_card_value(raw.get("default"))
            if default and default not in picked:
                issues.error(f"计划 {tag} 第 {index} 步「{key}」的 default「{default}」"
                             f"不在选项里。")
                continue
            spec_meta: dict[str, Any] = {
                "key": key,
                "display": clean(spec.get("description")) or key,
                "options": values,
                "default": default or values[0]["value"],
            }
            if source[0] == "number":
                spec_meta["unit"] = clean(spec.get("unit"))
            norm["param_options"].append(spec_meta)

    @staticmethod
    def _value_rejected(source: tuple[str, Any], text: str) -> str:
        kind, data = source
        if kind == "number":
            value = num(text)
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
