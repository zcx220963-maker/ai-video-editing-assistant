"""启动一致性检查的离线用例。

为什么单独建这份：这个检查原先**一条测试都没有**，于是它的假阳性没人发现——
我给技能正文补了一段提到 `param_options`（那是 submit_plan 的嵌套字段名，
不是工具名）的说明后，启动就挂了一条
「技能正文点名了不存在的工具：param_options」。
假告警比不报更坏：它会教人忽略这个检查本身。所以这里把两个方向都钉住——
真问题要报出来，字段名要放行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_framework import consistency as C  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        FAIL.append(label)


class _FakeTool:
    """最小工具替身：这个检查只读 name 与 parameters 两样。"""

    def __init__(self, name: str, parameters: dict[str, Any]) -> None:
        self.name = name
        self.parameters = parameters


# 与真实 submit_plan 同形的嵌套 schema：
# plans[] → items → steps[] → items → param_options
SUBMIT_PLAN_LIKE = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "string"},
                    "label": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "seq": {"type": "integer"},
                                "node": {"type": "string"},
                                "why": {"type": "string"},
                                "skippable": {"type": "boolean"},
                                "param_options": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "key": {"type": "string"},
                                            "options": {"type": "array"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    },
}


def case_schema_property_names() -> None:
    print("\n=== ① 字段名收集（含嵌套层）===")
    names = C.schema_property_names([_FakeTool("submit_plan", SUBMIT_PLAN_LIKE)])
    for want in ("plans", "plan_id", "label", "steps", "seq", "node", "why",
                 "skippable", "param_options", "key", "options"):
        check(want in names, f"收出嵌套字段「{want}」")
    print(f"    共收出 {len(names)} 个字段名")

    # oneOf/anyOf 与 additionalProperties 分支也要进去：
    # 节点的 anyOf 参数（custom_script 那类）就长这样
    branched = {"type": "object", "properties": {"custom_script": {
        "anyOf": [{"type": "string"},
                  {"type": "array", "items": {"type": "string"}},
                  {"type": "object", "additionalProperties": True}]}}}
    got = C.schema_property_names([_FakeTool("generate_script", branched)])
    check("custom_script" in got, "anyOf 分支的参数名被收出")

    check(C.schema_property_names([]) == set(), "没有工具时返回空集（不崩）")
    check(C.schema_property_names([_FakeTool("x", None)]) == set(),
          "schema 为 None 时返回空集（不崩）")


def case_skills_hygiene() -> None:
    print("\n=== ② 技能正文：字段名放行、臆造工具仍要报 ===")
    param_keys = C.schema_property_names([_FakeTool("submit_plan", SUBMIT_PLAN_LIKE)])
    known = {"load_media", "filter_clips", "submit_plan"}

    # 这就是真机那条假告警的原文形状
    body_ok = ("在规划轮的 `param_options` 里把它变成开关；"
               "先调 load_media，再用 filter_clips 筛片段。")
    bad = C.unknown_tools_in_skills({"s": body_ok}, known, param_keys=param_keys)
    check(bad == [], f"提到 param_options 不再误报：{bad}")

    # 但真正的臆造工具必须报出来——放宽不能把检查放没了
    body_bad = "请先调 analyze_shots 再调 load_media。"
    bad2 = C.unknown_tools_in_skills({"s": body_bad}, known, param_keys=param_keys)
    check("analyze_shots" in bad2, f"臆造工具 analyze_shots 仍被报出：{bad2}")

    # 嵌套字段名之外，顶层字段名同样放行
    body3 = "把 keep_clips 传全。"      # 未在 param_keys 里 → 属于会报的
    bad3 = C.unknown_tools_in_skills({"s": body3}, known,
                                     param_keys=param_keys | {"keep_clips"})
    check(bad3 == [], f"顶层参数名放行：{bad3}")


def case_watched() -> None:
    print("\n=== ③ 方向 A：点名了不存在的工具 ===")
    known = {"load_media", "filter_clips"}
    bad = C.missing_watched(["请调 read_node_artifact 拿产物"], known)
    check(bool(bad), f"点名不存在的工具会被报出：{bad[:1]}")
    skip = C.missing_watched(["不存在的名字不在 WATCHED 里"], known)
    check(skip == [], "不在 WATCHED 里的名字不报（只查已知清单）")


def case_report_text() -> None:
    print("\n=== ④ 报告文案 ===")
    check(C.format_report([], [], ["a"]) == "", "无问题时返回空串（启动不打印告警）")
    txt = C.format_report([], ["analyze_shots"], ["load_media"])
    check("analyze_shots" in txt and "点名了不存在的工具" in txt,
          "有技能问题时给出可读告警并点名")


def main() -> None:
    case_schema_property_names()
    case_skills_hygiene()
    case_watched()
    case_report_text()
    print()
    if FAIL:
        print(f"有 {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
