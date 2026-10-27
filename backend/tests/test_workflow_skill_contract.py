# -*- coding: utf-8 -*-
"""R17：两类 Skill 的契约——步骤标记、能力四步、终止判据。

设计 §22 把 Skill 分成两类：
  * WORKFLOW SKILL：写清**整体流程**，每一步标注「可跳过 / 固定 / 应默认运行」；
  * CAPABILITY SKILL：写清**单点能力**的四步（校验 / 取数 / 创作 / 格式化），
    取数入口是 ``read_node_history``。

本用例只读 Skill 正文（不联网、不起服务），钉住这三类内容：
  1. step_markers：流程 Skill 的每个工具步骤都带标记；固定步骤集合与 DAG 一致；
  2. capability_four_steps：能力 Skill 四步齐全、取数走 read_node_history；
  3. 终止判据：流程 Skill 写明「停在哪一步」，且不强制渲染。

运行：  python tests/test_workflow_skill_contract.py
"""

from __future__ import annotations

import pathlib
import re
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.video_editing import build_node_registry

SKILLS = BACKEND / "examples" / "skills"
WORKFLOW = SKILLS / "default_editing_workflow" / "SKILL.md"
CAPABILITY = SKILLS / "subtitle_imitation" / "SKILL.md"
EDITING_ROUND = BACKEND / "prompts" / "editing_round.md"

STEP_TOOLS = [
    "search_media", "load_media", "split_shots", "understand_clips", "filter_clips",
    "group_clips", "generate_script", "generate_voiceover", "select_BGM",
    "plan_timeline", "render_video",
]
FIXED = {"load_media", "plan_timeline", "render_video"}

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out: dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def step_marks(text: str) -> dict[str, str]:
    """抓形如 `- 文案生成 "generate_script"（可跳过）` 的步骤行。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^-\s+.*?"([A-Za-z0-9_]+)"（([^）]*)）', line.strip())
        if m and m.group(1) in STEP_TOOLS:
            out[m.group(1)] = m.group(2)
    return out


def step_markers() -> None:
    print("\n[1] step_markers：流程 Skill 每步都带标记")
    text = WORKFLOW.read_text(encoding="utf-8")
    fm = frontmatter(text)
    check("【WORKFLOW SKILL】" in fm.get("description", ""),
          "frontmatter 标明这是 WORKFLOW SKILL")
    check("always" not in fm, "未声明 always → 走 load_skill 渐进加载，不进常驻正文")

    marks = step_marks(text)
    missing = [s for s in STEP_TOOLS if s not in marks]
    check(not missing, f"11 个流程步骤都列出并带标记（缺 {missing}）")
    unmarked = [s for s, m in marks.items()
                if not any(k in m for k in ("可跳过", "固定", "默认运行"))]
    check(not unmarked, f"每个步骤的标记都取自三种口径（漏 {unmarked}）")

    fixed = {s for s, m in marks.items() if "固定" in m}
    check(fixed == FIXED, f"固定步骤恰为 {sorted(FIXED)}（实际 {sorted(fixed)}）")
    check("默认运行" in marks.get("group_clips", ""),
          f"group_clips 标为「可跳过但应默认运行」：{marks.get('group_clips')}")
    check("固定与可跳过约定" in text, "正文有固定/可跳过的汇总约定")

    nodes = set(build_node_registry().names())
    unknown = [s for s in STEP_TOOLS if s not in nodes]
    check(not unknown, f"步骤名与剪辑服务端的节点工具名一致（不存在 {unknown}）")


def capability_four_steps() -> None:
    print("\n[2] capability_four_steps：能力 Skill 四步齐全")
    text = CAPABILITY.read_text(encoding="utf-8")
    fm = frontmatter(text)
    check("【CAPABILITY SKILL】" in fm.get("description", ""),
          "frontmatter 标明这是 CAPABILITY SKILL")
    check("always" not in fm, "能力 Skill 同样按需加载（未声明 always）")

    heads = re.findall(r"^##\s*第([一二三四])步[：:]\s*(.+)$", text, re.M)
    check([n for n, _ in heads] == ["一", "二", "三", "四"],
          f"四步标题按序出现（实际 {[n for n, _ in heads]}）")
    for idx, keyword in enumerate(("校验", "素材", "创作", "格式化")):
        ok = idx < len(heads) and keyword in heads[idx][1]
        check(ok, f"第{['一', '二', '三', '四'][idx]}步讲{keyword}："
                  f"{heads[idx][1] if idx < len(heads) else '（缺）'}")

    check("`read_node_history`" in text,
          "取数入口写明 read_node_history（不是直接读 Store 的私货）")
    check("`understand_clips`" in text, "取数目标写明 understand_clips")
    check("custom_script" in text and "group_scripts" in text,
          "输出对齐 generate_script 的 custom_script / group_scripts 字段")
    check("不渲染" in text, "能力 Skill 明确不渲染（只产出脚本）")


def termination() -> None:
    print("\n[3] 终止判据：停在哪一步写死，且不强制渲染")
    text = WORKFLOW.read_text(encoding="utf-8")
    check("终止判据" in text, "流程 Skill 有「终止判据」小节")
    for phrase in ("看看有哪些片段", "做成片", "先出个方案给我看"):
        check(phrase in text, f"终止表覆盖「{phrase}」")
    check("没有交付物就停" in text and "有交付物还继续跑" in text,
          "终止判据写明两端：空停与空烧都算失败")
    check("不必渲染成片" in text, "流程 Skill 明确「不必走到 render_video」")

    prompt = EDITING_ROUND.read_text(encoding="utf-8")
    check("终止点不固定" in prompt and "不必渲染成片" in prompt,
          "编辑轮提示词同样声明终止点不固定（Skill 与提示词不打架）")


def main() -> None:
    step_markers()
    capability_four_steps()
    termination()
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if _fails == 0 else f"，失败 {_fails}"))
    if _fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
