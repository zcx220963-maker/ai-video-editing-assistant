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
        out |= enum_of(branch)
    if spec.get("type") == "boolean":
        out.update({"true", "false"})
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
