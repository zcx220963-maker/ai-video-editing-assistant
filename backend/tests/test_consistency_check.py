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


def case_prompt_named_tools_reachable() -> None:
    """③c 提示词叫模型去调的工具，**必须在白名单里**。

    这一条补的是一个**检查自己看不见的洞**：2026-10-16 真机核对时发现，
    规划轮提示词那句「要看整张图——有哪些节点、各依赖谁、参数什么形状——调
    `dag_contract`」教模型调的工具，主服务侧**从来没注册过**——服务端一直注册着它
    （editor 的 `[storyline] tools=33` 里那一份），但主服务按 `available_nodes`
    筛入册（`register_mcp_tools` 的 `_matches(name, enabled_tools)`），表里没写它。
    于是模型照着提示词调、拿到 UnknownTool，而启动日志那句「一致性检查通过」照打——
    因为 `missing_watched` 只扫 `WATCHED` 里的名字，`dag_contract` 当时不在其中。
    **「检查通过」与「模型调不到」可以同时成立**，这一条钉的就是这个缝。

    判据取三处真来源相乘：真提示词文件 × 真 `WATCHED` × 两份真白名单。
    三处缺一即红——只判白名单会放过「提示词没提它」，只判提示词会放过「提了却调不到」。
    """
    print("\n=== ③c 提示词点名的工具必须在白名单里（dag_contract 那一课） ===")
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "planning_round.md").read_text(encoding="utf-8")
    named = {n for n in C.WATCHED if n in prompt}
    check("dag_contract" in named,
          f"真规划轮提示词里点名了 dag_contract（这一轮实测取到：{sorted(named)}）")

    from agent_framework.video_editing import (load_storyline_config,
                                               storyline_available_nodes)
    for cfg_name in ("config.toml", "config.docker.toml"):
        cfg = load_storyline_config(root / "examples" / "storyline" / cfg_name)
        allowed = set(storyline_available_nodes(cfg))
        missing = sorted(n for n in named
                         # 只判**服务端**那几个口：本地工具的注册不归这张表白名单管
                         if n in _STORYLINE_ONLY and n not in allowed)
        check(not missing,
              f"{cfg_name} 的 available_nodes 里点名的服务端口都在"
              f"（缺：{missing}——缺一个就是「提示词教模型调一个不存在的工具」）")
        check("dag_contract" in allowed,
              f"{cfg_name} 的 available_nodes 含 dag_contract")


#: 归 storyline 服务端注册的那几个口（本地工具如 load_skill / ask_user / submit_plan
#: 由 agent_framework 自己装，不走这张白名单）。写在这里当判据而不是写进上一条断言：
#: 新加一个服务端口时，名字进这一份、白名单进两处，三处齐了才算接线完成。
_STORYLINE_ONLY = {
    "read_node_history", "render_status", "preview_voice", "preview_caption",
    "check_shot_code", "check_film", "preview_shot", "dag_contract",
    "load_media", "split_shots", "understand_clips", "filter_clips", "group_clips",
    "asr", "correct_transcript", "speech_rough_cut", "script_template_rec",
    "generate_script", "generate_ai_transition", "transition_rec", "text_rec",
    "generate_voiceover", "select_BGM", "plan_timeline", "plan_timeline_pro",
    "plan_timeline_ai_transition", "render_video", "render_web", "plan_motion",
    "render_motion_code", "patch_motion_code", "patch_video",
}


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

    from agent_framework.plan.support import OUTPUT_FIELD_NAMES

    check(C.skill_field_names() == set(OUTPUT_FIELD_NAMES),
          "都不传时只余「节点产出字段」这一份恒定豁免（不崩）")

    # 真机第二条假告警的形状：技能正文解释渲染回执字段（不是可调用名）。
    # （dry_run 是 render_video 的**入参**，走 node_param_keys 那一层豁免。）
    body = ("dry_run 的回执里有 not_checked 与 will_render，"
            "出镜段取 src_start/src_end，修字闸看 unchanged_suspects。")
    produced = C.skill_field_names(node_param_keys={"dry_run"}, tools=[main_batch])
    check(C.unknown_tools_in_skills({"s": body}, {"load_media"},
                                    param_keys=produced) == [],
          "技能正文解释产出字段（not_checked 等）→ 不报假告警")
    check(C.unknown_tools_in_skills({"s": body}, {"load_media"},
                                    param_keys=set()) != [],
          "不收产出字段这一份 → 正是那条假告警（用例守得住）")


def case_real_skill_bodies() -> None:
    """③b **真实技能正文**里那些「像工具名但不是工具」的读数名必须被放行。

    为什么加这一条：2026-10-14 真机启动日志整批挂出九个名字（R11 部件档那两节
    `asset-parts` / `scale-safe`，T57）——`scale_safe`、`scale_note`、`subject_boxes`、
    `no_match`、`source_not_available`、`text_share`、`subject_terms`、`code_spec`、
    `contrast_ratio`。逐条对着 SKILL.md 读，**没有一个是让模型调的工具**：全是回执栏位名、
    栏位取值、或一个带点的常量路径（`code_spec.contrast_ratio`）。而**离线用例全绿**——
    因为上面那些用例喂的是手写的 `{"s": "…"}` 正文，从来没读过盘上真那一份。
    这正是本仓反复吃亏的「测试与生产各算各的来源」（§9-Q91）：判据读真文件，
    用例读替身，于是真机的假告警与全绿的用例可以同时成立。

    本条把**正文**那一半接回真实来源（`examples/skills/*/SKILL.md` 是盘上的文件，
    离线读得到）。**工具集那一半仍不在这里**：真实 known 要等装配时从 MCP
    ``tools/list`` 灌进 ToolCatalog，离线那份近乎空表，拿它当 known 会把正文里每一个
    真工具名都报成假工具（`case_real_sources` 的 docstring 已写明这件事）。
    所以这里**只钉「读数名被放行」这一向**，不假装钉住了「零误报」——
    后者由带服务跑的那一趟出账（启动日志）。
    """
    print("\n=== ③b 真实技能正文里的读数名放行（R11 部件档那次假告警） ===")
    root = Path(__file__).resolve().parents[1]
    bodies: dict[str, str] = {}
    for p in sorted((root / "examples" / "skills").glob("*/SKILL.md")):
        bodies[p.parent.name] = p.read_text(encoding="utf-8")
    check(bool(bodies), f"盘上的技能正文读到了（{len(bodies)} 份：{list(bodies)}）")

    # 真机日志报出的那九个，逐个走**判据真正用的那道口**（不是直接查集合——
    # 查集合证明不了「调用链上会经过它」，而 `_looks_like_non_tool` 才是那个分岔点）。
    # 2026-10-16 缩到两个：`scale_safe`/`scale_note`/`no_match`/`source_not_available`/
    # `text_share`/`code_spec`/`contrast_ratio` 只活在取材那几节正文里，那几节整块删了，
    # 豁免跟着下架（下面那条「真的在正文里」正是逼着这一步的那根钉子）。
    nine = ["subject_boxes", "subject_terms"]
    for tok in nine:
        check(C._looks_like_non_tool(tok, ()), f"「{tok}」被放行（不是工具名）")

    # 而且这些名字**真的在正文里**：判据的输入不是空气，这条用例才不会随着正文改动
    # 悄悄变成空转（一个只查常量集合的断言，删掉正文也照样绿）。
    joined = "\n".join(bodies.values())
    for tok in nine:
        check(tok in joined, f"「{tok}」确实出现在正文里（判据的输入不是空气）")
    # 反面：豁免集里**已经没有**只在被删正文里活过的名字。漏摘一个不会报错（它照样
    # 放行），只会在将来吃掉一条真告警——所以这一向得有一条判据盯着，否则「跟着正文
    # 一起缩」只是句注释。
    gone = ["scale_safe", "scale_note", "no_match", "source_not_available",
            "text_share", "code_spec", "contrast_ratio", "supply_order", "stage_asset"]
    for tok in gone:
        check(tok not in joined,
              f"「{tok}」已随取材正文一起下架（今天正文里不该再有它）")
        check(not C._looks_like_non_tool(tok, ()),
              f"「{tok}」不再被豁免——留在集合里就是一条将来会吃掉真告警的旧账")

    # 反向：正文里**真工具名**不能被误放行（放行表加宽到把工具也罩住就等于检查失效）。
    # 2026-10-16：取样里原来那两个取材工具（search_illustration / select_illustration）
    # 已经不存在了，换成今天仍在册的两条——这一向要证的是「豁免口径没放宽到罩住工具」，
    # 拿一个**已经不是工具**的名字来证，证不到任何东西。
    for real in ("load_media", "filter_clips", "search_media",
                 "render_motion_code", "patch_motion_code"):
        check(not C._looks_like_non_tool(real, ()), f"真工具名「{real}」仍会被检查")


def main() -> None:
    case_schema_property_names()
    case_skills_hygiene()
    case_real_sources()
    case_watched()
    case_prompt_named_tools_reachable()
    case_report_text()
    case_skill_field_names()
    case_real_skill_bodies()
    print()
    if FAIL:
        print(f"有 {len(FAIL)} 项未通过：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
