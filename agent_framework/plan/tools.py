"""模型侧的两个计划门工具：submit_plan（提交候选计划）与 confirm_plan（重弹确认）。

两者都是只读工具：它们不碰剪辑执行节点，只把候选计划交服务端校验、
或请求把已有的待确认卡重新推给用户。
"""

from __future__ import annotations

import json
from typing import Any

from ..hooks import _current_hook_ctx
from ..tool import Tool, ToolError
from .support import PLAN_MAX_CANDIDATES

if False:  # TYPE_CHECKING：构造参数只做标注，运行期不需要（避免与 gate 形成环）
    from .gate import PlanGate


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
