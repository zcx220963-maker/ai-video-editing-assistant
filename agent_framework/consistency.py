"""装配期一致性检查：**已知的改名/失效风险名字**必须真的存在。

**这一层在防什么。** 这个项目的 LLM 面向层有四份独立清单：

1. 生产提示词里手写的工具名（``plan_gate`` 的规划段与历史段、``agent`` 的纠错 nudge、
   ``editing_agent`` 的角色提示）；
2. 真实注册的工具名（本地 Tool 子类 + MCP ``tools/list``）；
3. 计划门白名单（剪辑节点 ∩ 已注册）；
4. 技能正文（``SKILL.md``）里让模型调用的工具名。

四份互不校验，漂移了也没人知道。真机就是被这个坑住的：

    提示词写 ``read_node_artifact``，真实工具叫 ``read_node_history``
    → 模型照提示词调 → UnknownToolError → 调用从未发生、Storyline 没收到请求
    → 每轮空烧迭代预算，表现成「流程走到一半莫名其妙断了」。

**为什么不做「扫出所有像工具名的 token 再求差集」。** 试过，不可用：提示段里同时有
工具名（``load_media``）、参数键（``material_ids``）、字段名（``object_key``）、
枚举值（``tpl_free``）、以及**代码标识符**（``read_node_history`` 自己是函数名、
``system_prompt`` 是变量名）。想靠黑名单把它们分开，就会把「真名」一起豁免掉——
正好放过要抓的那一类。判据无法从形态上分辨语义。

**改成两个确定能做的方向：**

* 方向 A（精确）：维护一份**曾经出现过或高风险**的工具名清单（``WATCHED``），
  每个名字要么在真实工具集合里，要么就是漂移。它的价值随事故累积，
  且完全无假阳性——扫到就是真错。
* 方向 B（跨来源）：把**技能正文里**出现的工具名，与真实工具集合求差集。
  技能正文是给人看的自然语言、不是代码，所以那里的 ``[_a-z]+`` 形式
  基本都是工具名，误报率可接受。

两者都在启动时跑，非空就显式告警。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# 「像工具名」的标识符：小写字母开头、至少含一个下划线。
_TOOLISH = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# 方向 A：重点盯防的工具名。加进来的名字必须是「曾经写错过 / 极易写错」的那几个，
# 不是所有工具——这份清单的意义是「无条件检查」，所以不能塞会误报的东西。
#   read_node_artifact  ← 真机事故：提示词写了这个名字，真实工具叫 read_node_history
WATCHED = (
    "read_node_artifact",   # 历史误名（应为 read_node_history）
    "read_node_history",    # 当前真名：确认它没被哪次改动静默删掉
    "render_status",
    "load_media", "split_shots", "understand_clips", "filter_clips", "group_clips",
    "generate_script", "generate_voiceover", "select_BGM", "plan_timeline",
    "render_video", "submit_plan", "load_skill",
)

# 方向 B 用：技能正文里这些词不是工具名（是栏目名/字段名/通用短语）。
# 只放**确定不是工具**的，宁漏不误——误报会让人不再看告警。
_SKILL_NOT_TOOLS = frozenset({
    "workflow", "skill", "role", "objective", "constraints", "raw_text",
    "group_id", "group_scripts", "style_reference_text", "no_see_say",
    "chain_of_thought",
    # 技能里被当作**产物字段**描述的名字（不是可调用的工具，也不是入参键）：
    #   asr_segments —— asr 节点输出的段落数组
    #   material_id  —— search_media 结果里的素材 id（load_media 的入参叫 material_ids）
    "asr_segments", "material_id", "clip_captions", "shot_count", "kept_sec",
    "rough_clips", "group_scripts", "transitions", "text_effects", "voiceover",
})

# 明确不是工具名的**形态**（技能正文里大量出现，属正常内容）：
#   · 技能自己的名字：default_editing_workflow_skill / subtitle_imitation_skill
#   · 脚本模板 id：tpl_vlog_3act
#   · 样例素材/片段 id：m0_s0 / group_0001 / mat-a957c6
#   · 参数键：keep_clips / custom_groups / speaker_ratio（由调用方按真实 schema 传入）
_SKILL_NOT_TOOL_PATTERNS = (
    re.compile(r".*_skill$"),
    re.compile(r"^tpl_"),
    re.compile(r"^m\d+_s\d+$"),
    re.compile(r"^group_\d+$"),
    re.compile(r"^mat_[0-9a-f]+$"),
)


def schema_property_names(tools: Iterable[Any]) -> set[str]:
    """把所有工具 JSON Schema 里出现过的 **properties 键名**全收出来（含嵌套层）。

    为什么必须递归：判据要能认出「技能正文里提到的字段名不是工具名」。
    ``plan_gate.param_keys()`` 只覆盖计划门那批节点的参数，而模型在技能正文里
    顺口提到的字段可能来自任何工具的嵌套结构——例如 ``submit_plan`` 的
    ``plans[].steps[].param_options``。只取顶层就会把 ``param_options`` 判成
    「臆造工具」，启动时挂一条假告警；假告警比不报更坏，它教人忽略这个检查。

    只放宽文案检查，不影响任何执行判定。
    """
    out: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or not isinstance(node, Mapping):
            return
        props = node.get("properties")
        if isinstance(props, Mapping):
            for key, sub in props.items():
                out.add(str(key))
                walk(sub, depth + 1)
        for key in ("items", "additionalProperties", "oneOf", "anyOf", "allOf"):
            sub = node.get(key)
            if isinstance(sub, Mapping):
                walk(sub, depth + 1)
            elif isinstance(sub, (list, tuple)):
                for item in sub:
                    walk(item, depth + 1)

    for tool in tools or ():
        schema = getattr(tool, "parameters", None)
        walk(schema if isinstance(schema, Mapping) else {})
    return out


def skill_field_names(*, node_param_keys: Iterable[str] = (),
                      tools: Iterable[Iterable[Any]] = ()) -> set[str]:
    """技能正文里可能出现的**字段名**总集（这些都不是工具名，不能报成臆造工具）。

    为什么要一个函数而不是让调用方自己拼：装配处曾经只扫了主注册表，漏掉「submit_plan
    只活在规划注册表里」这一事实，于是启动期挂出一条假告警
    （「引用了不存在的工具：param_options」）；而离线测试当时用的是手写的假 schema，
    照样全绿——**测试与生产各算各的来源**就是这次漏判的形状。现在两边都走这里，
    来源少一个就会在测试里红。

    ``tools`` 是「一批批工具对象」（每批一个可迭代的 Tool），不是工具名的可迭代——
    所以调用方传列表，别传生成器。
    """
    out = {str(k) for k in node_param_keys}
    for batch in tools:
        out |= schema_property_names(batch)
    return out


def _looks_like_non_tool(token: str, param_keys: Iterable[str]) -> bool:
    if token in _SKILL_NOT_TOOLS:
        return True
    if token in {str(k) for k in param_keys}:
        return True
    return any(p.match(token) for p in _SKILL_NOT_TOOL_PATTERNS)


def _iter_strings(value: Any, *, depth: int = 0) -> Iterable[str]:
    """递归取出结构里的所有字符串（提示段可能是 str / list / dict 混排）。"""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _iter_strings(v, depth=depth + 1)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _iter_strings(v, depth=depth + 1)


def missing_watched(texts: Iterable[str], known: Iterable[str]) -> list[str]:
    """方向 A：``WATCHED`` 里有哪些名字没被注册，**却在文本里被点名**。

    这就是 ``read_node_artifact`` 那类事故的判据：文本让模型去调一个不存在的工具，
    启动期不报错、运行期 UnknownToolError、白烧一轮迭代。完全无假阳性——扫到就是真错。

    注意只查「不在 known 但在文本里」这一向。「在 known 但文本没提」不报：
    工具存在而提示词没提它是正常的（不是每个工具都要在提示词里被点名）。
    """
    known_set = {str(k) for k in known}
    joined = "\n".join(str(t or "") for t in texts)
    bad: list[str] = []
    for name in WATCHED:
        if name in known_set:
            continue
        if name in joined:
            bad.append(f"{name}（文本里点名了，但工具集里没有——模型会调它然后报 UnknownTool）")
    return bad


def unknown_tools_in_skills(skill_bodies: Mapping[str, str],
                            known: Iterable[str],
                            *, param_keys: Iterable[str] = ()) -> list[str]:
    """方向 B：技能正文里出现、但真实工具集里没有的「像工具名」的标识符。

    技能正文是给模型看的**自然语言文档**（不是代码），所以里面 ``xxx_yyy`` 形式的
    标识符基本上就是工具名——这让判据可以做得比提示词那侧激进：
    不认识就报，再排掉参数键、技能名、模板 id、样例 id 这些明确不是工具的东西。

    为什么不能按「动词前缀」过滤：那只能覆盖 load_/plan_/render_ 这些我们想得到的动词，
    而恰恰是没想到的动词（``analyze_shots``）才是臆造工具。判据要跟动词无关。
    """
    known_set = {str(k) for k in known}
    bad: set[str] = set()
    for _name, body in (skill_bodies or {}).items():
        for token in _TOOLISH.findall(str(body or "")):
            if token in known_set or _looks_like_non_tool(token, param_keys):
                continue
            bad.add(token)
    return sorted(bad)


def format_report(watched_bad: Iterable[str], skill_bad: Iterable[str],
                  known: Iterable[str], *, sample: int = 30) -> str:
    """把检查结果整理成一段可读的启动告警；没问题就回空串。"""
    w = list(watched_bad)
    s = list(skill_bad)
    if not w and not s:
        return ""
    kn = sorted(str(k) for k in known)
    lines = ["启动一致性检查发现问题（不影响启动，但模型会在运行期撞上）："]
    if w:
        lines.append("  [提示词] " + "; ".join(w))
    if s:
        lines.append(f"  [技能正文] 点名了不存在的工具：{', '.join(s)}")
    lines.append(f"  真实工具共 {len(kn)} 个：{', '.join(kn[:sample])}"
                 + ("…" if len(kn) > sample else ""))
    lines.append("  这类名字在启动期不报错，在运行期变成 UnknownToolError——"
                 "模型照提示词调、调用从未发生、白烧一轮迭代预算。请对齐拼写。")
    return "\n".join(lines)
