"""计划门的公共件：上限常量、纯文本工具、校验结论容器、入参形状归一。

这一层谁都用得到，所以它不依赖任何其他计划门模块（除标准库与 typing）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

PLAN_MAX_CANDIDATES = 3        # 一次规划最多几张卡
PLAN_STEPS_MAX = 24            # 单计划步数上限（防卡面失控）
CUSTOM_MAX_ITEMS = 5           # 自定义诉求条数
CUSTOM_MAX_CHARS = 200         # 单条自定义诉求长度

# 节点**产出**的字段名：模型在 why/expectation 里引用上游产物字段是正常表达，
# 不是「臆造工具名」。真机实测它连写三次「引用了不存在的名字「asr_segments」」，
# 卡连着两轮出不来。这些名字逐条对着 storyline_server 各节点的返回键抄；
# 只用于放行文案，不参与执行判定（写多了也不会放宽别的东西）。
OUTPUT_FIELD_NAMES = frozenset({
    "media", "clips", "asr_segments", "clip_captions", "rough_clips", "groups",
    "templates", "templates_info", "group_scripts", "voiceover", "bgm",
    "timeline", "events", "audio_events", "overlay_events", "subtitles", "warnings",
    "ids", "corrections", "unchanged_suspects", "corrected",
    "shot_count", "structure", "raw_text", "group_id", "start", "end", "duration",
    # 渲染产物的账本与 dry_run 回执（技能正文按字段解释给模型看，不是可调用名）：
    "evidence", "evidence_rule", "render_plan", "blocking", "not_checked",
    "will_render", "timeline_digest", "src_start", "src_end",
    # 零素材图形科普片那一路：plan_motion 的分镜账 + render_motion_video 的帧数回执
    # （技能正文里「读 estimated_sec 与 target_warnings 对账」就是这些键）：
    "motion_spec", "estimated_sec", "target_warnings", "target_duration_sec",
    "plan_table", "char_count", "frames_total",
    # 局部改那一路（patch_motion_video）：min_duration_sec 是归一后的镜头字段名
    # （duration_sec 归一就成了它），before_frames 是补丁账里的改前代表帧。
    # 两者都不是入参键，schema 扫不出来，不收就是假告警——技能正文教模型「拖镜头长度
    # 改的是 min_duration_sec」，被拦下来模型只能换个说法，读者反而看不到真字段名。
    "min_duration_sec", "before_frames",
    # 口播 / 素材片那一路（patch_video）：segment_cache 是产物里的逐窗账
    # （windows/rebuilt/reused/ledger 那一整块的顶层键名），segment_id 是补丁条目里
    # 与指针交叉核对的段号。两者都不是入参键也不是工具名，不收就是假告警——
    # 技能正文教模型「照 segment_cache 那份账说话」，被拦下来读者反而看不到真字段名。
    "segment_cache", "segment_id",
})

MCP_PREFIX = "storyline_"

def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def enum_of(spec: Any) -> set[str]:
    """一份参数声明里所有可能的取值（enum / const / oneOf 分支 + 嵌套子字段里声明的取值）。

    用于词表：计划一旦真把带 enum 的节点写进 steps，模型就会在 why/expectation 里
    顺口提它的取值（如 ``script_template_rec`` 的 ``tpl_vlog_3act``）。不收就会判成
    「引用了不存在的名字」——真机实测：模型按提示补上了节点，紧接着因为文案里写了
    模板 id 被判臆造，卡连着两轮出不来。

    为什么要往 ``properties`` / ``items`` 里递归：像分镜这种「一个参数装整棵结构」的
    节点，取值清单挂在子字段上（每镜的版式组件名在 ``shots[].card`` 的 enum 里）。
    只看顶层就收不到它，而模型在计划文案里写「版式选 dict_entry」是完全正常的表达。

    只在声明里明确列出的值才算，不做任何猜测。
    """
    out: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or not isinstance(node, Mapping):
            return
        for key in ("enum", "examples"):
            raw = node.get(key)
            if isinstance(raw, (list, tuple)):
                out.update(str(v) for v in raw if isinstance(v, (str, int, float, bool)))
        if "const" in node:
            out.add(str(node["const"]))
        if node.get("type") == "boolean":
            out.update({"true", "false"})
        for branch in node.get("oneOf") or node.get("anyOf") or ():
            walk(branch, depth + 1)
        props = node.get("properties")
        if isinstance(props, Mapping):
            for sub in props.values():
                walk(sub, depth + 1)
        for key in ("items", "additionalProperties"):
            sub = node.get(key)
            if isinstance(sub, Mapping):
                walk(sub, depth + 1)
            elif isinstance(sub, (list, tuple)):
                for item in sub:
                    walk(item, depth + 1)

    walk(spec)
    return out


def as_card_value(value: Any) -> str:
    """卡面上的枚举值统一成字符串：布尔写成 true/false，数字去掉多余 .0。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = clean(value)
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

def topology_view(contract: Any, steps: Any) -> list[dict[str, Any]]:
    """原始步骤 → 够拓扑检查用的最小形状（位置、节点名、契约里现成的 requires）。

    给「已经出现步骤级错误」的那条路用：拓扑与依赖两道检查只读这几个字段，而
    ``requires`` 按节点名问契约就有，不依赖这一步归一化成功。于是「参数不合规」
    与「group_clips 没上卡」能在同一次打回里一起交给模型，不必交两回卡、吃两回打回。

    ``skippable`` 一律 False：跳过规则的判据（下游踩没踩着它）要归一化后的完整
    步骤集才成立，这条路上不跑它。
    """
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps or (), start=1):
        if not isinstance(step, Mapping):
            continue
        node = clean(step.get("node"))
        if not node:
            continue
        node_contract = contract.get(node) if contract is not None else None
        out.append({"seq": index, "node": node, "skippable": False, "skip_reason": "",
                    "requires": list(node_contract.requires)
                    if node_contract is not None else []})
    return out


def add_implicit_skills(step: dict[str, Any], implicit: Sequence[str],
                        skills: Mapping[str, Any]) -> None:
    """把「这个节点离不开、模型却没写上卡」的技能补进步骤的 skills_hint（不重复、保序）。

    只补清单里查得到且当前可用的：卡上挂一个点开是空的技能，比不补更坏。
    """
    hints = step["skills_hint"]
    for name in implicit:
        skill = skills.get(name)
        if name in hints or skill is None or not getattr(skill, "available", True):
            continue
        hints.append(name)


def coerce_plans(payload: Any) -> list[Mapping[str, Any]] | None:
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
