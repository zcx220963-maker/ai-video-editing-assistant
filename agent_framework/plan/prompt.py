"""计划门的中文长文案：执行轮的两段分离注入 + 规划轮的 system 段 + 同会话历史段。

段内一律留机器名，界面侧由块 A 的出口替换器换轨成 ``*_display``。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .support import clean, neutralize

if False:  # TYPE_CHECKING
    from .gate import PlanGate

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
            bits.append("参数 " + ", ".join(f"{k}={clean(v)}" for k, v in params.items()))
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
    note = clean(feedback)
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
        goal = clean(plan.get("goal"))
        if goal:
            head += f"（目标：{goal}）"
        out.append(head)
        for step in plan.get("steps") or []:
            if not isinstance(step, Mapping):
                continue
            opts = "；".join(
                f"{clean(o.get('key'))}={clean(o.get('default'))}"
                f"（候选 {'/'.join(clean(c.get('value')) for c in (o.get('options') or []) if isinstance(c, Mapping))}）"
                for o in (step.get("param_options") or [])
                if isinstance(o, Mapping) and o.get("options"))
            tail = f"（{opts}）" if opts else ""
            out.append(f"    第 {step.get('seq')} 步 {step.get('node')} "
                       f"{clean(step.get('why'))[:40]}{tail}")
    return out
