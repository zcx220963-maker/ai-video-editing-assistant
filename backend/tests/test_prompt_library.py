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
④ **动态选择层（manifest.json）**：清单说「谁在哪个相（phase）发」，`validate_manifest`
   拿它跟磁盘对账。**这一条取代了旧的「内联 vs 文件逐字一致」漂移守卫**——
   2026-10-14 起那层内联副本已经删掉、提示词只有盘上一份，旧守卫退化成了
   「拿文件跟它自己比」（必然绿，等于没守）。防漂移换了个方向：现在要防的是
   「清单说有、盘上没有」「加了文件没记账」「某一条悄悄长成一篇教程」。

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
    print("\n② 关键提示词取自文件（内联副本已删，不再逐字对账）")
    from_file = build_prompt_library(None).system_prompt("X")
    check(_norm(DEFAULT_SYSTEM_PROMPT) == _norm(from_file),
          "system_prompt.md 与 DEFAULT_SYSTEM_PROMPT 一致（唯一还带内联兜底的一份）")
    check("ask_user" in DEFAULT_SYSTEM_PROMPT,
          "系统提示词里写了「用 ask_user 提问」")
    check("ask_user" in from_file, "文件版提示词里写了「用 ask_user 提问」")
    # 剪辑轮：EDITING_SYSTEM_PROMPT 现在是 read_prompts_file("editing_round.md") 的返回值，
    # 所以这里是**同源**断言（拿文件跟读到的值比）。留着它不为防漂移，为防**改错来源**：
    # 哪天有人把常量换回字面量，文件与常量就会分叉，这一条立刻红。
    from agent_framework.editing_agent import EDITING_SYSTEM_PROMPT
    side = build_prompt_library(None).text("editing_round.md", "")
    check(_norm(side) == _norm(EDITING_SYSTEM_PROMPT),
          "editing_round.md 就是 EDITING_SYSTEM_PROMPT 的来源（同源）")
    check("select_BGM" in side and "select_BGM" in EDITING_SYSTEM_PROMPT,
          "「配乐只能从曲库选」两边都写着（这是那两条链最容易漂的一句）")


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
          "常量就是磁盘那份（同源：改回字面量就会红）")

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


def case_manifest() -> None:
    print("\n⑧ 动态选择层：清单登记 vs 磁盘（validate_manifest）")
    from agent_framework.prompts import (
        PHASES, load_manifest, prompt_text, read_prompts_file, validate_manifest,
    )

    lib = build_prompt_library(None)
    manifest = load_manifest()
    entries = manifest.get("prompts") or {}
    check(bool(entries), f"manifest.json 有 {len(entries)} 项登记")
    check(validate_manifest() == [], "仓库现状自检通过（清单与磁盘对得上）")

    # 每一相都能查到归属；「谁在什么时候发」的答案来自清单，不来自调用点。
    covered = {e["name"] for ph in PHASES for e in lib.prompts_for_phase(ph)}
    check(covered == set(entries), f"每项都落在某个 phase 里（{len(covered)} 项）")
    check(lib.entry("system_prompt").get("fixed") is True,
          "只剩 system_prompt 是 fixed（唯一还带内联兜底的一份）")
    check(all(not m.get("fixed") for n, m in entries.items() if n != "system_prompt"),
          "其余全部 fixed=false：内联副本已删干净")

    # 四条判据各自能单独判红（否则这个自检就只是「跑过了」）。
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "manifest.json").write_text(
            '{"version":1,"prompts":{"a":{"file":"a.md","phase":"nope","kind":"always",'
            '"max_bytes":999},"b":{"file":"missing.md","phase":"guard","kind":"always"},'
            '"c":{"file":"c.md","phase":"guard","kind":"always","max_bytes":3}}}',
            encoding="utf-8")
        (d / "a.md").write_text("内容", encoding="utf-8")
        (d / "c.md").write_text("这一份显然超过三个字节", encoding="utf-8")
        (d / "orphan.md").write_text("没记账", encoding="utf-8")
        probs = "\n".join(validate_manifest(d))
        check("nope" in probs, "① phase 拼错被判红")
        check("missing.md" in probs and "不存在" in probs, "② 清单说有、盘上没有 → 判红")
        check("超过清单上限" in probs, "③ 超出 max_bytes → 判红（拦「粘了一篇教程」）")
        check("orphan.md" in probs and "没登记" in probs, "④ 盘上有、清单没记 → 判红")

    # 没有内联兜底了：文件不在就抛，不给空串。
    try:
        read_prompts_file("definitely_not_a_prompt.md")
        check(False, "读不到的文件应当抛 FileNotFoundError")
    except FileNotFoundError:
        check(True, "read_prompts_file 缺文件即抛（不再静默回落）")
    check(prompt_text(None, "no_card_nudge.md").strip() != "",
          "prompt_text(库=None) 退回仓库目录读得到")

    # kind 词表在 prompts.py 自留一份（那一层不依赖包内任何模块），context.py 另有一份：
    # 两处都是「三个字符串」，没有这条断言，改一处漏一处不会有任何红灯。
    from agent_framework.context import LEDGER_KINDS
    from agent_framework.prompts import LEDGER_KINDS_LOCAL
    check(tuple(LEDGER_KINDS_LOCAL) == tuple(LEDGER_KINDS),
          f"kind 词表与 context.LEDGER_KINDS 同值（{LEDGER_KINDS_LOCAL}）")


def main() -> int:
    print("=== 提示词库（读盘 / 占位符 / 块守卫 / 回落 / 热替换 / 漂移守卫）===")
    case_default_dir_and_fallback()
    case_inline_matches_file()
    case_placeholder_semantics()
    case_hot_reload()
    case_all_nudges_loadable()
    case_block_rendering()
    case_subagent_prompt_on_disk()
    case_manifest()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
