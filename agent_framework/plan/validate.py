"""①~④ 四重校验器：node 白名单 / 拓扑与依赖 / skills_hint / param_options，
外加文本卫生与跳过规则。纯代码，一次 LLM 都不叫。

事实一律问 ``PlanVocabulary``（什么节点存在、参数有哪些枚举源），这一层只管判。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .support import (PLAN_MAX_CANDIDATES, PLAN_STEPS_MAX,
                      PlanIssues, as_card_value, clean, coerce_plans, num)
from .vocab import PlanVocabulary

# 词表外别名的形状：snake_case 标识符（至少一个下划线）。
# 展示字段里出现**英文原名**不打回——块 A 的出口替换器会换轨成中文；
# 只有引用了词表里根本没有的名字才属于正确性问题（臆造），替换器无能为力。
_ALIAS = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


class PlanValidator:
    """候选计划的服务端校验：errors 非空即打回，warnings 只在卡上挂角标。"""

    def __init__(self, vocab: PlanVocabulary, *,
                 skills: Any = None) -> None:
        self.vocab = vocab
        self._skills = skills

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
                values.append({
                    "value": text,
                    "display": clean(opt.get("display")) if isinstance(opt, Mapping) else "",
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
        explicit = self.vocab.explicit_call_nodes()
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
                contract = self.vocab.contract.get(dep)
                if contract is not None:
                    queue.extend(contract.requires)

    def _check_skip_rules(self, tag: str, steps: list[dict[str, Any]],
                          issues: PlanIssues) -> None:
        """skippable 只在没有下游依赖踩着时成立；否则降级为不可跳并给角标原因。"""
        present = {s["node"] for s in steps}
        for step in steps:
            if not step["skippable"]:
                continue
            blocked = (self.vocab.contract.downstream(step["node"]) - {step["node"]}) & present
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
        vocab = (self.vocab.vocabulary() | self.vocab.param_keys() | self.vocab.enum_values()
                 | self.vocab.output_keys())
        for text in texts:
            for token in _ALIAS.findall(clean(text or "")):
                if token in vocab or self.vocab.resolve_tool(token) is not None:
                    continue
                issues.error(f"计划 {tag} 的文案引用了不存在的名字「{token}」"
                             f"（词表里没有，界面换不成中文）。")
