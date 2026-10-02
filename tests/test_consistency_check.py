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

# ✓/✗ 这些符号在 GBK 控制台上直接 print 会 UnicodeEncodeError（pytest 里被捕获才没暴露）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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


def case_real_sources() -> None:
    """②b 字段名来源要读**真实的 submit_plan schema**，不是手写替身。

    为什么专门钉：这个用例的上一版喂的是手写的 SUBMIT_PLAN_LIKE 替身 schema，
    而真机装配处只扫主注册表（submit_plan 只在规划注册表里），
    于是启动日志挂着「引用了不存在的工具：param_options」，测试却依然全绿。
    现在这里直接取真实工具的 schema：字段改名、或来源没并进 skill_field_names，都会红。

    「真实技能正文 × 真实工具集 → 零误报」这半条**不放在离线用例里**：
    工具集要等装配时从 MCP ``tools/list`` 灌进 ToolCatalog，离线那份 ``catalog.names``
    近乎空表，拿它当 known 会把技能正文里每一个真名都报成假工具。
    这一半由带服务跑的标定脚本核（``.runtime/audit/calibrate_consistency.py``）。
    """
    print("\n=== ②b 真实 submit_plan schema → 字段名来源 ===")
    from agent_framework.catalog import get_catalog
    from agent_framework.plan import ConfirmPlanTool, SubmitPlanTool

    # SubmitPlanTool 的 schema 是静态的，只有 execute 才用 gate，这里不调它
    planning_tools = [SubmitPlanTool(None), ConfirmPlanTool()]
    fields = C.skill_field_names(node_param_keys=get_catalog().params_display().keys(),
                                 tools=[planning_tools])
    check("param_options" in fields,
          "真实 submit_plan schema 里能反查出 param_options（不是靠手写替身）")
    for want in ("plans", "plan_id", "steps", "seq", "node", "why", "skippable"):
        check(want in fields, f"真实 schema 的嵌套字段「{want}」也在来源里")


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


def case_skill_field_names() -> None:
    """skill_field_names 是装配处真正调用的那个入口。

    它比 schema_property_names 多一层：把「多批工具」合并起来。
    为什么这个函数非测不可：真机那次假告警的根因正是**只扫了主注册表**，
    漏掉「submit_plan / confirm_plan 只活在规划注册表里」这一事实；
    而当时的离线用例用的是手写假 schema，生产与测试各算各的来源，照样全绿。
    所以这里专门钉住「来源必须都传进来」这件事。
    """
    print("\n=== ⑤ skill_field_names：多批来源必须都算上 ===")
    main_batch = [_FakeTool("load_media", {"type": "object", "properties": {
        "material_ids": {"type": "array"}}})]
    # 规划注册表那批：只在这里才有的 submit_plan
    plan_batch = [_FakeTool("submit_plan", SUBMIT_PLAN_LIKE)]

    names = C.skill_field_names(node_param_keys={"keep_clips"},
                               tools=[main_batch, plan_batch])
    for want in ("keep_clips", "material_ids", "param_options", "steps"):
        check(want in names, f"合并后含「{want}」")

    # 少传规划注册表那批 → param_options 就收不到。这正是真机 bug 的形状，
    # 用例必须能复现它（否则测试就失去意义）。
    missing = C.skill_field_names(node_param_keys=set(), tools=[main_batch])
    check("param_options" not in missing,
          "只扫主注册表时收不到 param_options（复现真机 bug 的形状）")

    # 而且这种缺失会真的产生假告警——证明这个用例测的是有意义的东西
    body = "在规划轮用 param_options 做成开关。"
    ok_names = C.skill_field_names(node_param_keys=set(),
                                  tools=[main_batch, plan_batch])
    bad_names = C.skill_field_names(node_param_keys=set(), tools=[main_batch])
    check(C.unknown_tools_in_skills({"s": body}, {"load_media"},
                                    param_keys=ok_names) == [],
          "来源传全 → 不报假告警")
    check(C.unknown_tools_in_skills({"s": body}, {"load_media"},
                                    param_keys=bad_names) == ["param_options"],
          "来源漏传 → 正是那条假告警（用例确实守得住）")

    check(C.skill_field_names() == set(), "都不传时返回空集（不崩）")


def main() -> None:
    case_schema_property_names()
    case_skills_hygiene()
    case_real_sources()
    case_watched()
    case_report_text()
    case_skill_field_names()
    print()
    if FAIL:
        print(f"有 {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
