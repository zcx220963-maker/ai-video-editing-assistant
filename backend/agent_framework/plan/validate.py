"""①~④ 四重校验器的**步骤级**那一半：node 白名单 / skills_hint / param_options，
外加逐步骤的 why/expectation 与 seq。纯代码，一次 LLM 都不叫。

跨步骤的结构判据（拓扑、跳过规则、重复步骤、重复开关、文案卫生）在
``structure.StructureChecks``——它拿的是一张完整的卡，不是单步。
**单个取值**那一级的判据（候选值能不能反查回来、一个开关上的几处毛病怎么合成一句）
在 ``switches``——它不认识步骤、节点与 tag，这一层只管逐步归一化并把意见汇总。

事实一律问 ``PlanVocabulary``（什么节点存在、参数有哪些枚举源），这一层只管判。
"""

from __future__ import annotations

from typing import Any, Mapping

from .knobs import knob_hint
from .support import (PLAN_MAX_CANDIDATES, PLAN_STEPS_MAX, PlanIssues,
                      add_implicit_skills, as_card_value, clean, coerce_plans,
                      num, topology_view)
from .structure import StructureChecks
from .switches import USER_SAYS_MAX, switch_problems, value_rejected
from .vocab import SYSTEM_INJECTED_PARAMS, PlanVocabulary


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
            norm = await self._validate_plan(plan, index, allowed, skills, issues,
                                             seen_ids)
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
                             issues: PlanIssues, seen_ids: set[str]
                             ) -> dict[str, Any] | None:
        tag = clean(plan.get("plan_id")) or f"p{index}"
        # 本份计划之后出的每条意见都记上来源：三份候选的毛病往往一模一样，
        # 打回清单要按它把「同一件事在第几份上」合成一条（``PlanIssues.groups``）。
        issues.current = tag
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
        # 原先还有一条 `explicit_deps`（「标了 require_explicit_call 的前置必须写进
        # steps」）夹在这里。它守的是执行期那个「必须先显式调用」的报错，而那个报错
        # 已撤（见 `structure.StructureChecks` 上方那段注释与 `orchestration.Interceptor`）：
        # 那几步没写进卡**不是错误**，只是写法之一——执行期会按默认参数替你补齐，
        # 并在回执里标注是哪几步。建议归提示段（`prompt._explicit_nodes`）。
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
        norm = {
            "plan_id": tag,
            "label": clean(plan.get("label")),
            "goal": clean(plan.get("goal")),
            "steps": norm_steps,
        }
        style = self._check_style(tag, plan.get("style_skill"), skills, issues)
        if style:
            norm["style_skill"] = style
        return norm

    def _check_style(self, tag: str, raw: Any, skills: Mapping[str, Any],
                     issues: PlanIssues) -> str:
        """计划级 ``style_skill``：这一片用哪套视觉风格（风格包 / 用户导入的技能）。

        判据只有两条：名字**真实存在**，且它是**画面契约那一类**——``kind=style``
        的系统/个人风格包，或用户自己导入的技能（导入件由 ``plan_motion`` 注入成
        ``imported`` 契约）。流程技能与能力技能当风格用没有意义：它们讲的是怎么做，
        不是长什么样。

        不通过一律**降级成警告并清空**，不让整张卡作废：风格只是可选的画面选择，
        为了它把一份步骤全对、参数全对的计划打回，代价是白烧一轮迭代；而打回清单
        里那条「风格名不对」模型也未必改得对——它本来就在猜名字。清空之后写手按
        默认视觉画，用户看到卡上没有风格，随时能自己点一个。
        """
        name = clean(raw)
        if not name:
            return ""
        unreadable = skills.get("__unreadable__")
        if unreadable is not None:
            # 与 ``_check_skills`` 同一种处境：没接技能库 ≠ 用户选错了。放行、如实记账。
            issues.warn(f"计划 {tag} 选的风格「{name}」未能核验（{unreadable}），仍按它执行。")
            return name
        skill = skills.get(name)
        if skill is None:
            # 模型常把中文名（display）当机器名填进来——用户说的是「剪影年轮」，
            # 它照抄也能懂。先按显示名认一遍，认到了就换成机器名继续走；
            # 认不到再清空（这时候才真的是编的名字）。
            by_display = [s for s in skills.values()
                          if getattr(s, "display", "") == name
                          and self._usable_style(s)]
            if len(by_display) == 1:
                return str(getattr(by_display[0], "name", "") or "")
            style_names = [str(getattr(s, "name", "")) for s in skills.values()
                           if self._usable_style(s)][:8]
            hint = ("可用的风格：" + "、".join(style_names)) if style_names else \
                "现在没有任何风格包，这一项留空即可"
            issues.warn(f"计划 {tag} 选的风格「{name}」不存在，已忽略，按默认视觉执行"
                        f"（{hint}）。")
            return ""
        kind = str(getattr(skill, "kind", "workflow") or "workflow")
        if kind != "style" and not getattr(skill, "owner_user_id", None):
            issues.warn(f"计划 {tag} 选的「{name}」不是风格包（它是流程/能力技能），"
                        f"已忽略，按默认视觉执行。")
            return ""
        if not getattr(skill, "available", True):
            # 不可用也照放：风格正文注入不依赖它声明的外部依赖（依赖是给 load_skill
            # 判「能不能照着做」用的），拦下来只会让一份本来能画的计划少了风格。
            issues.warn(f"计划 {tag} 选的风格「{name}」标着不可用"
                        f"（{getattr(skill, 'unavailable_reason', '')}），仍按它执行。")
        return name

    @staticmethod
    def _usable_style(skill: Any) -> bool:
        """能不能当「这一片长什么样」的契约用：系统风格包，或用户自己导入的技能。

        与 ``_check_style`` 的主判据同口径，抽出来是因为显示名认领那一路也要用它。
        """
        kind = str(getattr(skill, "kind", "workflow") or "workflow")
        return kind == "style" or bool(getattr(skill, "owner_user_id", None))

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
        unknown: list[str] = []        # 本步造出来的、节点上不存在的开关（合批上报，见下）
        for raw in raw_opts:
            if not isinstance(raw, Mapping):
                issues.error(f"计划 {tag} 第 {index} 步的参数项不是对象。")
                continue
            key = clean(raw.get("key"))
            spec = props.get(key)
            # 系统注入项既不进卡面清单，也不许模型在卡上造：
            # 造出来就等于替用户决定「跳过预渲静帧 / 跳过出片前确认」。
            if key in SYSTEM_INJECTED_PARAMS:
                issues.error(f"计划 {tag} 第 {index} 步的参数「{key}」"
                             f"由系统注入，不要做成开关。")
                continue
            if spec is None:
                # 打回时把**该节点真实的开关键**一并交出（T40 起这条比过去更必要）：
                # 开关清单不再每个规划轮常发，只发本轮计划命中的那几个节点；冷启动
                # （本会话还没有计划上下文）那一轮模型没有清单可抄，凭印象造键名时
                # 唯一的出路不该是「再猜一次」。判据与规划段那份清单同一份
                # （`vocab.knob_facts_for`），所以这里不会告诉它一个上不了卡的键。
                unknown.append(key)
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
            bad: list[tuple[str, str]] = []     # 同一个开关上没过反查的值（合批上报）
            for opt in options:
                value = opt.get("value") if isinstance(opt, Mapping) else opt
                text = as_card_value(value)
                reason = value_rejected(source, text)
                if reason:
                    bad.append((text, reason))
                    continue
                if text in picked:
                    continue
                picked.add(text)
                own = clean(opt.get("display")) if isinstance(opt, Mapping) else ""
                values.append({"value": text, "kind": source[0],
                               "display": own or str(labels.get(text) or "")})
            # 这一项是不是**用户自己在消息里指名要的**（``user_says`` 是他那句原话）。
            # 真机那条 14 镜的 run：用户在消息里写死了「横屏」，模型给 ``aspect`` 只列了
            # landscape 一个候选——它没错，用户确实没给别的余地——却被「只给了 1 个候选值
            # 「landscape」，用户没有选择余地」打回；重写一轮还是一条同样的意见，到顶之后
            # 整条 run 停在一个「画幅要横屏还是竖屏？」的弹窗上，14 镜的片子一镜未出。
            # 旧措辞给的出路是「若确实不该由用户定，就不要把它做成开关」，而
            # ``compile.validate_execute`` 只把**卡上列出的键**传给节点：把它从卡上删掉，
            # 用户要的那个值就传不到执行里（``aspect`` 会按节点默认 portrait 跑）——
            # 模型当时提的三个选项里，第一项正是这条错路（「把 aspect 从卡面开关里去掉，
            # 直接按横屏 16:9 执行」）。所以「用户已定」必须是**能上卡的合法形态**。
            user_says = clean(raw.get("user_says"))
            over_long = len(user_says) > USER_SAYS_MAX
            if over_long:
                user_says = ""           # 不按「已定」算，退回普通开关那套判据
            # 这一个开关上的毛病**合成一条**：值反查不过 / 改完之后只剩一个候选 /
            # default 不在选项里——三者的修法是同一次编辑（重挑这几个候选值），分几行
            # 报就是把几行挤进了打回清单的窗口。真机第二轮打回列出的 8 条「只给了 1 个
            # 候选值」里，有两条正是被同一步骤的「值超过节点上界」引出来的：一个毛病
            # 报了两遍。措辞在有几种毛病时用顿分号串起来，每种都把具体取值点出来。
            problems = switch_problems(bad, values, source,
                                       user_says=user_says)
            if over_long:
                problems.insert(0, f"user_says 太长（{len(clean(raw.get('user_says')))} 字）"
                                   f"：这里只写用户点名那一两个词的原话"
                                   f"（如「横屏」「15 帧」），不是整句")
            default = as_card_value(raw.get("default"))
            if default and default not in picked:
                problems.append(f"default「{default}」不在选项里")
            if problems:
                head = f"计划 {tag} 第 {index} 步的参数「{key}」"
                if len(problems) == 1:
                    # 只有一处毛病时保持原先逐字的措辞（「…的参数「x」只给了 1 个候选值…」），
                    # 免得读回执的人以为判据换了。「值…」「default…」是接着前半句说的
                    # （要有「的」），「只给了…」「有 N 个值…」自己就是一句完整的话。
                    one = problems[0]
                    glue = "的" if one.startswith(("值「", "default")) else ""
                    issues.error(f"{head}{glue}{one}。")
                else:
                    # 几处毛病串成一条：修法是同一次编辑，分几行报就是几行挤窗口。
                    issues.error(f"{head}：" + "；".join(problems) + "。")
                continue
            spec_meta: dict[str, Any] = {
                "key": key,
                "display": clean(spec.get("description")) or key,
                "options": values,
                "default": default or values[0]["value"],
            }
            if user_says:
                # 带进卡面：卡上把这一项显示成「已定 + 用户原话」（前端 plan-panel /
                # 计划弹窗），用户可以核对自己是不是这么说的、也能改。
                spec_meta["user_says"] = user_says
            if source[0] == "number":
                spec_meta["unit"] = clean(spec.get("unit"))
            norm["param_options"].append(spec_meta)
        # 造出来的开关**合批上报**：一个节点上写错六个键名，原先就是六条几乎一样的话
        # （每条还各带一遍该节点的真实开关清单），一步就把清单窗口吃掉一半。判据只有
        # 一条——「键不在这个节点上」——所以只报一条，把六个键名全列出来：模型一次就
        # 知道这一步要删/改哪几个，而不会以为「只错了一个」。真机实测（14 镜那一条 run）
        # 56 条意见里 12 条是造开关，乘 3 份候选 = 36 条，其中大半从没被列出来过。
        if unknown:
            knobs = knob_hint(await self.vocab.knob_facts_for(norm["node"]))
            if len(unknown) == 1:
                issues.error(f"计划 {tag} 第 {index} 步（{norm['node']}）没有参数"
                             f"「{unknown[0]}」，不许在卡上造这个开关；{knobs}"
                             f"这类诉求请走「其他」。")
            else:
                names = "".join(f"「{k}」" for k in unknown)
                issues.error(f"计划 {tag} 第 {index} 步（{norm['node']}）没有参数{names}"
                             f"（这一步一共造了 {len(unknown)} 个不存在的开关）："
                             f"不许在卡上造这些开关；{knobs}这类诉求请走「其他」。")
