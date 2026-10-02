"""⑤ execute 帧编译：选中版本 + 参数终值 + 跳过勾选 + 自定义诉求 → 可执行计划。

卡面上的枚举值在这里变成**承诺**（反查过的那一份才会出现在 steps 里），
自定义文本变成 ``custom`` 段的**诉求**（只受条数/长度上限与注入转义）。
"""

from __future__ import annotations

from typing import Any, Mapping

from .support import (CUSTOM_MAX_CHARS, CUSTOM_MAX_ITEMS, PlanIssues, as_card_value,
                      neutralize, clean, num)


class PlanCompiler:
    """确认帧 → 编译后的执行计划（无状态；规划轮与确认接口共用同一份判据）。"""

    # ---- ⑤ execute 帧：点击编译结果 ----

    def validate_execute(self, plan: Mapping[str, Any],
                         frame: Mapping[str, Any]
                         ) -> tuple[dict[str, Any], PlanIssues]:
        """选中版本 + 枚举终值 + 跳过勾选 + 自定义诉求。

        枚举值仍要反查（这些是**承诺**，会直接拼进节点调用参数）；
        ``custom_text`` 不做枚举校验，只受条数/长度上限与注入转义（这些是**诉求**）。
        """
        issues = PlanIssues()
        steps = {int(s["seq"]): s for s in plan.get("steps") or []}
        if not steps:
            issues.error("这张计划卡没有可执行的步骤。")
            return {}, issues
        selected = clean(frame.get("selected_plan"))
        if selected and selected != clean(plan.get("plan_id")):
            issues.error(f"选中的计划「{selected}」与卡面（{plan.get('plan_id')}）对不上。")
        param_finals: dict[tuple[Any, str], str] = {}
        for item in frame.get("param_finals") or []:
            if not isinstance(item, Mapping):
                issues.error("参数终值形状不对。")
                continue
            step = self._step_at(steps, item.get("step_seq"), issues, "参数终值")
            if step is None:
                continue
            key = clean(item.get("key"))
            opt = next((o for o in step["param_options"] if o["key"] == key), None)
            if opt is None:
                issues.error(f"第 {step['seq']} 步（{step['node']}）卡上没有参数「{key}」。")
                continue
            value = as_card_value(item.get("value"))
            if value not in {o["value"] for o in opt["options"]}:
                issues.error(f"第 {step['seq']} 步「{key}」的值「{value}」不在选项里"
                             f"——承诺过的枚举不许在确认时临时改。")
                continue
            param_finals[(step["seq"], key)] = value
        skips: list[int] = []
        for item in frame.get("skips") or []:
            raw_seq = item.get("step_seq") if isinstance(item, Mapping) else item
            step = self._step_at(steps, raw_seq, issues, "跳过项")
            if step is None:
                continue
            if not step["skippable"]:
                issues.error(f"第 {step['seq']} 步（{step['node']}）不可跳过"
                             f"（{step['skip_reason'] or '它被下游依赖踩着'}）。")
                continue
            if step["seq"] not in skips:
                skips.append(step["seq"])
        overrides: list[dict[str, Any]] = []
        for item in frame.get("overrides") or []:
            if not isinstance(item, Mapping):
                issues.error("自定义诉求形状不对。")
                continue
            text = clean(item.get("value"))
            if not text:
                continue               # 「其他」点开又留空 → 回落同组枚举，不产生空诉求
            if clean(item.get("kind")) not in ("", "custom_text"):
                issues.error(f"自定义诉求的 kind 只能是 custom_text（收到 {clean(item.get('kind'))}）。")
                continue
            if len(text) > CUSTOM_MAX_CHARS:
                issues.error(f"自定义诉求过长（{len(text)} > {CUSTOM_MAX_CHARS} 字）。")
                continue
            step = self._step_at(steps, item.get("step_seq"), issues, "自定义诉求",
                                 allow_none=True)
            if step is None and item.get("step_seq") is not None:
                continue
            overrides.append({"step_seq": None if step is None else step["seq"],
                              "key": clean(item.get("key")) or "_general",
                              "value": text})
        if len(overrides) > CUSTOM_MAX_ITEMS:
            issues.error(f"自定义诉求最多 {CUSTOM_MAX_ITEMS} 条，收到 {len(overrides)} 条。")
            return {}, issues
        # 同组枚举与「其他」都给了值：以 custom 为准并记警告（doc §8）
        superseded: set[tuple[int, str]] = set()
        for item in overrides:
            pair = (item["step_seq"], item["key"])
            if pair in param_finals:
                issues.warn(f"第 {item['step_seq']} 步「{item['key']}」同时给了枚举值"
                            f"（{param_finals[pair]}）与自定义诉求，以自定义为准。")
                param_finals.pop(pair)
                superseded.add(pair)      # 也不回落卡面默认值：那是用户刚放弃的承诺
        resolved: list[dict[str, Any]] = []
        for seq in sorted(steps):
            if seq in skips:
                continue
            step = steps[seq]
            resolved.append({
                "seq": seq,
                "node": step["node"],
                "params": {o["key"]: param_finals.get((seq, o["key"]), o["default"])
                           for o in step["param_options"]
                           if (seq, o["key"]) not in superseded},
                "skills_hint": list(step["skills_hint"]),
                "why": step["why"],
                "expectation": step["expectation"],
            })
        return {
            "plan_id": clean(plan.get("plan_id")),
            "label": clean(plan.get("label")),
            "goal": clean(plan.get("goal")),
            "steps": resolved,
            "skipped": skips,
            "custom": [dict(o, value=neutralize(o["value"])) for o in overrides],
            "custom_raw": list(overrides),
            "skills_hint": sorted({n for s in steps.values() for n in s["skills_hint"]}),
        }, issues

    @staticmethod
    def _step_at(steps: Mapping[int, Mapping[str, Any]], raw_seq: Any,
                 issues: PlanIssues, what: str, *,
                 allow_none: bool = False) -> dict[str, Any] | None:
        if raw_seq is None or clean(raw_seq) == "":
            if allow_none:
                return None
            issues.error(f"{what}缺少 step_seq。")
            return None
        value = num(raw_seq)
        step = steps.get(int(value)) if value is not None else None
        if step is None:
            issues.error(f"{what}指向不存在的第 {clean(raw_seq)} 步。")
            return None
        return dict(step)
