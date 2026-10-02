# -*- coding: utf-8 -*-
"""提示词库验证（不联网、不起服务）。

钉的链子形状：
① 默认接仓库自带 prompts/，整段文本来自磁盘；文件缺失时逐段回落到内联默认值；
② 占位符只替换**明确给出**的键——提示词里天然有 {"plans": [...]} 这类 JSON 片段，
   按 str.format_map 会当占位符直接 KeyError 或吃字符；
②b 块守卫（render_blocks）只有两条规则：块首独占一行的 {?key} 没值时**整块不出现**、
   块内渲染后为空的行不留空行。规划轮那种「按条件出现的小段」靠它表达，
   而不是往仓库里塞一门模板语言；
③ 改磁盘文件后 reload_prompts 生效，且指纹随之变化（eval 归因要靠它）；
④ **内联默认值与文件内容必须一致**：文件在就用文件、缺了回落内联，
   两处一旦漂移，同一份代码在「有没有 prompts/ 目录」两种环境里行为不同。
   这条守卫是必需的——文案在两个地方各写一份，靠人眼比对一定会漂。

运行：  python tests/test_prompt_library.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder  # noqa: E402
from agent_framework.prompts import (  # noqa: E402
    DEFAULT_PROMPTS_DIR, build_prompt_library, placeholders_in, render_blocks,
    render_template,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _norm(text: str) -> str:
    """比较提示词时把空白归一：段落换行不该算成内容差异。"""
    return " ".join((text or "").split())


def case_default_dir_and_fallback() -> None:
    print("\n① 默认接仓库自带 prompts/；缺目录时逐段回落")
    lib = build_prompt_library(None)
    files = lib.loaded_from_disk()
    check(len(files) >= 8, f"磁盘上至少 8 份提示词（实得 {len(files)}：{files}）")
    check("planning_round.md" in files, "规划轮整段也在库里（结构性改造 4）")
    check("subagent_system.md" in files, "子 Agent 的 system 提示词也在库里")
    check(DEFAULT_PROMPTS_DIR.is_dir(), "仓库自带 prompts/ 存在（默认就会生效）")
    sp = lib.system_prompt("内联兜底")
    check(sp.strip() != "内联兜底" and "智能创作助手" in sp,
          "系统提示词取自文件而不是兜底值")

    empty = build_prompt_library(Path(tempfile.mkdtemp()) / "nothere")
    check(empty.loaded_from_disk() == [], "目录不存在 → 0 份文件")
    check(empty.system_prompt("内联系统提示") == "内联系统提示", "系统提示词回落内联")
    check(empty.text("no_card_nudge.md", "内联nudge") == "内联nudge", "nudge 也回落内联")
    tpl = "{?has}\n出现\n\n{nodes}"
    check(empty.blocks("planning_round.md", tpl, nodes="A") == "A",
          "blocks() 同样回落内联模板；守卫键没值 → 整块不出现")
    check(empty.blocks("planning_round.md", tpl, has=True, nodes="A") == "出现\nA",
          "守卫有值时去掉守卫行、留下文案（未守卫的块照常渲染）")
    check(build_prompt_library(False).loaded_from_disk() == [],
          "显式 False = 关闭提示词库（全走内联）")


def case_inline_matches_file() -> None:
    print("\n② 内联默认值与文件内容一致（漂移守卫）")
    from_file = build_prompt_library(None).system_prompt("X")
    same = _norm(DEFAULT_SYSTEM_PROMPT) == _norm(from_file)
    if not same:
        a, b = _norm(DEFAULT_SYSTEM_PROMPT), _norm(from_file)
        at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        print(f"      内联: …{a[max(0, at - 60):at + 60]}")
        print(f"      文件: …{b[max(0, at - 60):at + 60]}")
    check(same, "system_prompt.md 与 DEFAULT_SYSTEM_PROMPT 内容一致（只差空白）")
    check("ask_user" in DEFAULT_SYSTEM_PROMPT,
          "内联提示词里也写了「用 ask_user 提问」（两边都得有）")
    check("ask_user" in from_file, "文件版提示词里写了「用 ask_user 提问」")


def case_placeholder_semantics() -> None:
    print("\n③ 占位符只换明确给出的键，JSON 示例不被误吃")
    t = '批准计划第 1 步是 `{first}`；示例 {"plans": [1]}；未知键 {unknown} 原样'
    got = render_template(t, {"first": "load_media"})
    check("`load_media`" in got, "{first} 被正确替换")
    check('{"plans": [1]}' in got, "JSON 示例原样保留（不会被当占位符）")
    check("{unknown}" in got, "未提供的占位符原样保留（不 KeyError）")
    check(render_template("{a}-{b}", {}) == "{a}-{b}", "一个键都不给时原样返回")
    check(render_template("{{not_a_key}}", {"a": "1"}) == "{{not_a_key}}",
          "双花括号不被当成占位符")
    check(placeholders_in("x {first} y {second}") == {"first", "second"},
          "能列出文本里的占位符名")
    check(placeholders_in(build_prompt_library(None).text("step_nudge.md", "")) == {"first"},
          "step_nudge 需要一个 {first} 占位符")
    check("{first}" not in build_prompt_library(None).text(
        "step_nudge.md", "", first="load_media"),
        "step_nudge 经替换后不再留占位符")


def case_hot_reload() -> None:
    print("\n④ 改磁盘文件即生效，指纹随之变化")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "system_prompt.md").write_text("第一版提示词", encoding="utf-8")
        lib = build_prompt_library(d)
        cb = ContextBuilder(None, prompt_library=lib)
        fp1 = cb.fingerprint()
        check(cb.system_prompt == "第一版提示词", "读到第一版")
        time.sleep(0.01)
        (d / "system_prompt.md").write_text("第二版提示词", encoding="utf-8")
        fp2 = cb.reload_prompts()
        check(cb.system_prompt == "第二版提示词", "改动磁盘文件后 reload 生效")
        check(fp2 != fp1, f"指纹随内容变化（{fp1} → {fp2}）")
        check(cb.fingerprint() == fp2, "fingerprint() 与 reload 返回值一致")


def case_all_nudges_loadable() -> None:
    print("\n⑤ 四类纠错说明都能从磁盘读到，且都不是空壳")
    lib = build_prompt_library(None)
    for name in ("no_card_nudge.md", "no_card_note.md",
                 "no_card_structural_nudge.md", "step_nudge.md", "step_note.md"):
        text = lib._read(name)
        check(bool(text and text.strip()), f"{name} 非空")
    check(len(lib.describe()) > 0, f"describe() 能说明来源：{lib.describe()[:60]}…")


def case_block_rendering() -> None:
    print("\n⑥ 块守卫渲染（render_blocks）：只有两条规则")
    # 规则 1：块首单独一行的 {?key} 是守卫
    check(render_blocks("{?has}\n出现", {}) == "", "守卫键没值 → 整块消失")
    check(render_blocks("{?has}\n出现", {"has": "   "}) == "",
          "只含空白的值算没值（空串不能留下半句话）")
    check(render_blocks("{?has}\n出现", {"has": True}) == "出现", "有值 → 去掉守卫行")
    check(render_blocks("{?has}\n第一行\n第二行", {"has": 1}) == "第一行\n第二行",
          "守卫只管整块，块内多行照常")
    check(render_blocks("写在行中 {?has} 不算守卫", {"has": False})
          == "写在行中 {?has} 不算守卫", "守卫必须独占一行（否则当普通文本）")
    # 规则 2：块内某行渲染后为空 → 丢掉这一行
    check(render_blocks("头\n{body}\n尾", {"body": ""}) == "头\n尾",
          "空数据的行不留空行")
    check(render_blocks("头\n\n{body}", {"body": "x\n\ny"}) == "头\nx\n\ny",
          "先按空行切块再替换：值自带空行不会被重新解释成块边界")
    check(render_blocks('a\n\n示例 {"plans": [1]}', {}) == 'a\n示例 {"plans": [1]}',
          "JSON 示例不被误吃；块之间固定收成一个换行（金样就是这个形状）")
    check(placeholders_in("{?guard}\n{data}") == {"guard", "data"},
          "placeholders_in 也列守卫键（漏传只会静默少一段，更要能查）")


def case_subagent_prompt_on_disk() -> None:
    print("\n⑦ 子 Agent 的 system 提示词也读盘（构造时取的是刷新后的那份）")
    from agent_framework import subagent
    from agent_framework.subagent import (
        SUBAGENT_PROMPT_FILE, SubAgentRunner, refresh_subagent_prompt,
    )

    check(SUBAGENT_PROMPT_FILE in build_prompt_library(None).loaded_from_disk(),
          "prompts/subagent_system.md 在库里")
    on_disk = (DEFAULT_PROMPTS_DIR / SUBAGENT_PROMPT_FILE).read_text(encoding="utf-8")
    check(_norm(on_disk) == _norm(subagent._CHILD_SYSTEM_PROMPT_DEFAULT),
          "磁盘版与内联回落逐字一致（只差空白）")

    saved = subagent.CHILD_SYSTEM_PROMPT
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / SUBAGENT_PROMPT_FILE).write_text("磁盘版子 Agent 提示词", encoding="utf-8")
            refresh_subagent_prompt(build_prompt_library(d))
            check(SubAgentRunner(None, []).child_system_prompt == "磁盘版子 Agent 提示词",
                  "刷新后新构造的 runner 用的是磁盘那份")
            refresh_subagent_prompt(None)
            check(SubAgentRunner(None, []).child_system_prompt == "磁盘版子 Agent 提示词",
                  "refresh(None) 不改动：库关掉时照用当前值")
        check(SubAgentRunner(None, [], child_system_prompt="显式覆盖").child_system_prompt
              == "显式覆盖", "显式传参仍然优先")
    finally:
        subagent.CHILD_SYSTEM_PROMPT = saved
    check(subagent.CHILD_SYSTEM_PROMPT == saved, "用例跑完不留脏全局")

    # 接线：runner 在构造时就把提示词固化进去了，所以刷新必须排在 make_spawn_tool 之前
    src = (Path(__file__).resolve().parent.parent / "run_server.py").read_text(encoding="utf-8")
    at = src.index("make_spawn_tool(")
    check(src.index("prompt_library = build_prompt_library") < at,
          "提示词库在 make_spawn_tool 之前构造")
    check(src.index("refresh_subagent_prompt(prompt_library)") < at,
          "刷新也在 make_spawn_tool 之前（否则子 Agent 永远读内联那份）")


def main() -> int:
    print("=== 提示词库（读盘 / 占位符 / 块守卫 / 回落 / 热替换 / 漂移守卫）===")
    case_default_dir_and_fallback()
    case_inline_matches_file()
    case_placeholder_semantics()
    case_hot_reload()
    case_all_nudges_loadable()
    case_block_rendering()
    case_subagent_prompt_on_disk()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
