"""`knobs` 段的条件判据与排版（T40，§5.7 第③条）。

这一层只干一件别处都不干的事：**决定这一轮该不该发开关清单、发哪几个节点的**。
文案拼装住在 ``prompt.py``，节点事实住在 ``vocab.py``；判据住在单独一个模块，
是因为它自己有两条要能单独被测的输入源（计划上下文 + 白名单），
既要能被规划段的用例直接喂，也要能被反证（改回「整份发」当场红）。

它只依赖 ``support``（去前缀与 ``clean``）——不认识装配层、不认识提示词层。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .support import MCP_PREFIX, clean

__all__ = ["bare_node", "knob_facts_for_round", "knob_lines", "plan_node_hits"]

# 节点名在自然语言里的形态：ASCII 标识符（`plan_motion` / `storyline_asr`）。
# 只用来从用户原话里把节点名捞出来；捞不到的后果只是这一段不发（见 `plan_node_hits`）。
_NODE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def bare_node(node: Any) -> str:
    """节点名的**裸名**：去掉 ``storyline_`` 前缀（``vocab.implicit_skills`` / ``resolve_tool`` 同一口径）。

    T40 的命中判据只认这一种写法：白名单、卡上、旧卡里都可能带前缀，去掉前缀再比——
    这与 ``resolve_tool`` 同时接受两种写法是同一件事，**不新增第二套判据**。
    """
    return clean(node).removeprefix(MCP_PREFIX)


def plan_node_hits(nodes: Sequence[str], *, prior, pending, session_history,
                   feedback: str = "") -> set[str]:
    """**本轮计划上下文**里出现过的节点裸名集合——`knobs` 段发不发的唯一判据。

    规划轮还没出卡，「这一步会用到哪个节点」只能从这一轮手里的计划上下文读出来：

    1. ``prior``：「换一版」的那张旧卡，新卡要覆盖的就是这些步骤；
    2. ``pending``：待续跑计划里**未履行**的那几步（续跑卡只列它们）；
    3. ``session_history``：同会话里规划轮候选的 steps、执行轮的未履行清单。

    外加两段**锚在计划上的用户原话**（``feedback`` 与 ``pending.asked``）里的节点名：
    用户会说「render_video 那步别要了」。这里只做词面捞取，不猜意图；捞到的名字
    必须在该轮白名单里（去前缀后逐字相等，与 ``resolve_tool`` 同一口径）。

    为什么**不**把「本会话已产出的节点」也算进来：那是**产物**不是计划——一个会话跑过
    一次 ``plan_motion``，后面每一轮（哪怕只是问一句时长）都会因此常发整份开关教程，
    那正是 T40 要治的「参数教程常发」，只是换了个入口。
    为什么**不**把本轮用户消息整段算进来：模型在消息里看到 ``plan_motion`` 四个字多半
    是在**问它是什么**，不是要排它。这两条边界如实登记在 §9-Q103。
    """
    known = {bare_node(n) for n in nodes if clean(n)}
    if not known:
        return set()
    found: set[str] = set()

    def take_node(value: Any) -> None:
        """结构化字段里的节点名（候选步骤可能是裸串，也可能是带 ``node`` 的对象）。"""
        if isinstance(value, Mapping):
            value = value.get("node")
        bare = bare_node(value)
        if bare in known:
            found.add(bare)

    def take_text(value: Any) -> None:
        for token in _NODE_TOKEN_RE.findall(str(value or "")):
            bare = token.removeprefix(MCP_PREFIX)
            if bare in known:
                found.add(bare)

    for plan in prior or ():
        if isinstance(plan, Mapping):
            for step in plan.get("steps") or ():
                take_node(step)
    for row in session_history or ():
        if not isinstance(row, Mapping):
            continue
        for cand in row.get("candidates") or ():
            if isinstance(cand, Mapping):
                for step in cand.get("steps") or ():
                    take_node(step)
        for node in row.get("audit_unfulfilled") or ():
            take_node(node)
    pend = pending or {}
    for node in pend.get("unfulfilled") or ():
        take_node(node)
    take_text(feedback)
    take_text(pend.get("asked"))
    return found


async def knob_facts_for_round(gate: Any, nodes: Sequence[str], *, prior, pending,
                               session_history,
                               feedback: str = "") -> list[Mapping[str, Any]]:
    """本轮该发的开关事实：**只发命中节点的那几条，一条都没命中就一条都不发**。

    这是 §5.7 第③条的落地。此前只要白名单非空就把整份开关清单每个规划轮发一遍
    （「参数教程常发」的最后一条残留）；现在与 ``_IMPLICIT_SKILLS`` 同构——由本轮
    **计划步骤命中的节点**决定发哪几条。命中为空时连 ``knob_facts_for`` 都不问
    （曲库标签那一路是真去取标签的），账面因此如实记 0 字节。

    代价与补偿（冷启动那一轮）：本会话还没有任何计划上下文时，这份清单一条不发，
    模型若凭印象造 ``param_options``，会被校验器 ④ 打回——**而打回消息里此时会带上
    该节点真实的开关键**（``validate._check_params``）。也就是「谁真要用，谁才拿到
    清单」：常驻的教程没了，用错的那一刻照样有人告诉它。
    """
    hits = plan_node_hits(nodes, prior=prior, pending=pending,
                          session_history=session_history, feedback=feedback)
    if not hits:
        return []
    out: list[Mapping[str, Any]] = []
    for node in nodes:
        if bare_node(node) not in hits:
            continue
        fact = await gate.knob_facts_for(node)
        if fact.get("knobs"):
            out.append(fact)
    return out


def knob_lines(facts: Sequence[Mapping[str, Any]]) -> list[str]:
    """把开关事实排成提示段里的那份清单（一节点一行，机器名照旧）。

    **一条都没有就回空表**（不是回一行「没有开关」）：这一段存在与否完全等价于
    「本轮有没有真开关要发」——本节点没有开关这件事，由校验器在模型真造了这个开关时
    当场告诉它（``validate._check_params``），不必每个规划轮都声明一次。
    这样 §5.4.1 那条判据（非 motion 规划轮 `knobs` 字节 = 0）也就没有歧义可钻。
    """
    if not facts:
        return []
    lines = ["能上卡的开关（服务端按节点真实 schema 现取，与卡面校验同一份判据）："]
    for fact in facts:
        parts = []
        for knob in fact["knobs"]:
            tail = f"；默认 {knob['default']}" if knob["default"] else ""
            note = f"——{knob['note']}" if knob["note"] else ""
            parts.append(f"{knob['key']}({knob['kind']}: {knob['values']}{tail}){note}")
        lines.append(f"· {fact['node']}：" + "｜".join(parts))
    return lines
