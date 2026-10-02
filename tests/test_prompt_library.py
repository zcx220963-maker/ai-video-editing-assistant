# -*- coding: utf-8 -*-
"""提示词库验证（不联网、不起服务）。

钉的链子形状：
① 默认接仓库自带 prompts/，整段文本来自磁盘；文件缺失时逐段回落到内联默认值；
② 占位符只替换**明确给出**的键——提示词里天然有 {"plans": [...]} 这类 JSON 片段，
   按 str.format_map 会当占位符直接 KeyError 或吃字符；
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
    DEFAULT_PROMPTS_DIR, build_prompt_library, placeholders_in, render_template,
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
    check(len(files) >= 6, f"磁盘上至少 6 份提示词（实得 {len(files)}：{files}）")
    check(DEFAULT_PROMPTS_DIR.is_dir(), "仓库自带 prompts/ 存在（默认就会生效）")
    sp = lib.system_prompt("内联兜底")
    check(sp.strip() != "内联兜底" and "智能创作助手" in sp,
          "系统提示词取自文件而不是兜底值")

    empty = build_prompt_library(Path(tempfile.mkdtemp()) / "nothere")
    check(empty.loaded_from_disk() == [], "目录不存在 → 0 份文件")
    check(empty.system_prompt("内联系统提示") == "内联系统提示", "系统提示词回落内联")
    check(empty.text("no_card_nudge.md", "内联nudge") == "内联nudge", "nudge 也回落内联")
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


def main() -> int:
    print("=== 提示词库（读盘 / 占位符 / 回落 / 热替换 / 漂移守卫）===")
    case_default_dir_and_fallback()
    case_inline_matches_file()
    case_placeholder_semantics()
    case_hot_reload()
    case_all_nudges_loadable()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
