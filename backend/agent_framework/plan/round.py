"""执行轮工具面的**按计划装配**（T41，§5.7 第⑥条）。

规划轮已经有一层物理过滤：``vocab.planning_registry`` 把剪辑执行节点整批挡在门外
（越权无工具可调）。执行轮此前相反——**整批**发（53~57 份 schema，真机 2026-10-13
实测 54,140 B）。这一层是那件事的另一半：按这张计划卡的步骤取「计划里用得到的剪辑
节点 + 通用件」，其余剪辑节点不批发 schema。

**裁的是 schema 字节，不是可发现性**（Q56）。这两件事必须成对出现，所以本模块导出
的是**两样东西**：一张注册表（``registry_for_round``）与一段说明（``scope_lines``，
被裁掉的节点名逐个写在里面）。任何只给表、不给说明的装配都是这条判据的反面——
模型会把「没给 schema」读成「功能不存在」，那是 §5.5 名单漂移的同一种失败。

「是不是剪辑执行节点」的判据**只有一条**：``tool.contract is not None``（与
``planning_registry`` 逐字同一口径）。不带契约的工具一律算通用件、一律不裁——
``load_skill``/``ask_user`` 这类发现性工具因此**物理上**不会被裁（Q56 的硬要求），
不需要再挂一张「永不裁」的名单（那是第二份真相）。

**为什么 team 那几件不做「按是否多 Agent 计划」的条件裁剪**（§5.7 第⑥条括号里提过）：
「这条计划会不会派子代理」在执行轮**跑到中途**才知道，计划里没有这个信号。按计划裁它
只会让本来能派的计划先撞一次「本轮不提供」，然后再走一遍扩面——多一轮的代价换不到
可预期的字节数（团队工具合计不到 2 KB）。这条取舍如实登记在 §9-Q105。

依赖：``support``（``clean``/``MCP_PREFIX``）与 ``knobs``（``bare_node``，节点名口径的
唯一出处）。不认识装配层、不认识提示词层——排段与包裹标签由 agent 侧做。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from ..tool import ToolRegistry
from .knobs import bare_node
from .support import clean

__all__ = [
    "ROUND_FILTER", "SCOPE_CONDITION_KEY", "SCOPE_SECTION", "plan_nodes",
    "registry_for_round", "scope_lines", "trimmed_nodes", "truncated_nodes",
    "unbacked_truncations",
]

# 账面 ``tools.filter`` 那一栏的取值（T37 的 ``tools_box`` 里那个 filter_name）。
# 写成常量而不是各写一遍字符串：账面上这三个字是「这一轮按什么裁的」的全部证据。
ROUND_FILTER = "registry_for_round"

# 「如何请求扩面」小节的段名与条件键（进 ``context_ledger`` 的 sections 栏）。
SCOPE_SECTION = "tool_scope"
SCOPE_CONDITION_KEY = "plan_trim"

# 说明里逐列被裁节点名时用的连接符：顿号与中文串里的一致（逗号会让名字看起来像参数）。
_SEP = "、"


def plan_nodes(plan_steps: Iterable[Any]) -> set[str]:
    """计划步骤 → 节点**裸名**集合（去 ``storyline_`` 前缀；与 ``bare_node`` 同口径）。

    步骤可能是对象（``{"node": ...}``）也可能是裸串（真机上两种都出现过）：这里两种都收，
    收不到就跳过。空集合是合法输入——那表示「这张卡没有剪辑步骤」，此时执行轮只剩通用件，
    说明小节会把**全部**剪辑节点列成可请求的。
    """
    out: set[str] = set()
    for step in plan_steps or ():
        if isinstance(step, Mapping):
            step = step.get("node")
        bare = bare_node(step)
        if bare:
            out.add(bare)
    return out


def _kept(registry: ToolRegistry, wanted: set[str]) -> ToolRegistry:
    """按「通用件 + 计划命中的契约节点」重建一张注册表（工具实例共享，不复制）。

    重建而不是就地删：主注册表是**全进程共享**的（规划轮的续跑、定时任务、子代理都从
    它出发），就地裁一次，别的轮次就少一批工具——那正是「按轮裁剪」最容易出的事故。
    """
    out = ToolRegistry()
    for name in registry.tool_names:
        tool = registry.get(name)
        if tool is None:
            continue
        if getattr(tool, "contract", None) is None or bare_node(name) in wanted:
            out.register(tool)
    return out


def trimmed_nodes(registry: ToolRegistry, plan_steps: Iterable[Any]) -> list[str]:
    """会被裁掉的剪辑节点名（升序）——「谁不在这一轮的工具面里」的唯一出处。

    说明小节与承载闸都读它，所以它必须是**纯函数**：同一张注册表、同一份计划，
    两次调用给出同一份清单（假命中红样就是拿这一点当判据的）。
    """
    wanted = plan_nodes(plan_steps)
    out = [name for name in registry.tool_names
           if (tool := registry.get(name)) is not None
           and getattr(tool, "contract", None) is not None
           and bare_node(name) not in wanted]
    return sorted(out)


def registry_for_round(registry: ToolRegistry, plan_steps: Iterable[Any]) -> ToolRegistry:
    """执行轮的工具面：计划命中的剪辑节点 + 全部通用件（其余剪辑节点不批 schema）。

    报错说明也跟着换：被裁掉的节点被调用时回的是「本轮没提供 + 怎么请求扩面」，
    而不是一句 ``tool not found``——后者会被模型读成「这个功能不存在」，
    于是它转去问用户或换个做法，而不是来要权限（规划轮踩过同一个坑，见
    ``planning_registry`` 里那段注释）。
    """
    wanted = plan_nodes(plan_steps)
    out = _kept(registry, wanted)
    cut = trimmed_nodes(registry, plan_steps)
    if cut:
        out.unknown_tool_hint = (
            "本轮的工具面按这张计划卡裁过：只装载了计划里用得到的剪辑节点与通用件。"
            f"没装载的剪辑节点（真实存在、说得出名字就能请求）：{_SEP.join(cut)}。"
            "真需要它们时**先来要权限**：调 ask_user 说明「这一步需要哪个节点、为什么」，"
            "或直接在答复里说清。别把它当成功能不存在，也别去找替代做法。")
    return out


def scope_lines(trimmed: Sequence[str]) -> list[str]:
    """「如何请求扩面」的正文行；**没有被裁的节点就回空表**（整节不发）。

    与 ``knobs.knob_lines`` 同一形状：判据层给行，装配层排段。空表意味着这一轮的工具面
    是完整的（计划覆盖了全部剪辑节点，或者这张注册表里根本没有剪辑节点）——那时候
    写一段「你可以请求扩面」只会让模型以为工具被裁了。
    """
    if not trimmed:
        return []
    return [
        f"没装载但**真实存在**的剪辑节点：{_SEP.join(trimmed)}。",
        "要扩面：这一轮真需要上面某个节点时，先调 `ask_user` 说明「这一步要它、为什么」"
        "（或直接在答复里说清），拿到允许再往下走。**不要**因为清单里没有就换成别的做法，"
        "更不要对用户说这个功能不存在。",
        "直接按名字调一个没装载的节点，拿到的是「本轮没提供」——那是裁剪，不是缺失；"
        "它不会替你跑，也不会自动补上。",
    ]


def truncated_nodes(registry: ToolRegistry) -> list[str]:
    """描述被截到 ≤600 B 的那些 MCP 节点名（升序）——溢出内容的**承载闸**读它。

    「截了」这件事必须有人看得见（D-未判不绿）：只留一个更短的说辞而不留证据，
    下一个人会把「被截的那半句」当成从没写过。
    """
    out = [name for name in registry.tool_names
           if (tool := registry.get(name)) is not None
           and getattr(tool, "desc_truncated", False)]
    return sorted(out)


def unbacked_truncations(registry: ToolRegistry,
                         carrier_for: Callable[[str], Sequence[str]]) -> list[str]:
    """截了描述、却**没有技能承载**的节点名（升序）＝空表才允许上线。

    §5.7 第⑥条把这件事写成一句话：「溢出内容移进 ``load_skill`` 附件，**移出即须有闸
    读到**」。前两件是「移出」（截断 + 追加指向技能正文的那一行），这一条才是闸——
    它问的是「被移走的那半句，真有一个随身的载体吗」。没有载体的截断是**静默丢内容**：
    模型看不到、人也不知道少了什么（那正是 ``ContextBudgetError`` 拒绝就地截断的理由）。

    只在**真带契约的剪辑节点**上问：MCP 侧的非剪辑工具（比如某个搜索服务）描述超长时
    没有随身技能可言，硬塞一份等于给不相关的技能挂名。它们在真机上不存在（探针实测
    2026-10-13：超 600 B 的四件全是剪辑节点，见 §9-Q105），所以这里如实只盖住剪辑节点。
    """
    out: list[str] = []
    for name in truncated_nodes(registry):
        tool = registry.get(name)
        if tool is None or getattr(tool, "contract", None) is None:
            continue
        if not [str(s).strip() for s in carrier_for(bare_node(name)) if str(s).strip()]:
            out.append(name)
    return sorted(out)
