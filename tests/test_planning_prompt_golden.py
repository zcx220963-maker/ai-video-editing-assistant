# -*- coding: utf-8 -*-
"""规划轮 system 段的**逐字**金样：文案搬进 prompts/planning_round.md 后不许改一个字。

钉的是这次迁移的唯一风险面：把几十行按条件拼装的文案换成「模板 + 块守卫」，
渲染结果哪怕差一个空格，模型的行为就可能漂。所以这里不比「意思差不多」，
比的是**字符串相等**。

金样在拆之前从旧代码逐字捕获（``tests/data/planning_round_golden.json``，
9 条用例覆盖：最小 / 待确认卡开关 / 换一版旧卡 / 反馈原话 / 待续跑 / 同会话历史 /
全开 / 无节点无开关 / 有节点无开关）。这份用例跑三遍同一批输入：

① 内联回落（``prompt_library=None``）——目录没拷过去时的行为；
② 仓库自带 prompts/（默认生效的那条路径）——生产实际读的文案；
③ 两份模板**互相**逐字相同（漂移守卫）：文件在磁盘、内联在代码，各写一份，
   靠人眼比对一定会漂，所以让用例去比。

另加一条接线守卫：``run_server.py`` 构造 ``PlanGate`` 时必须真的传 ``prompt_library``。
「写了但没人接」是本项目死过一次的失败模式（Bootstrap 的 bootstrap_dir 一直没传），
金样全绿也可能意味着磁盘那份从来没被读过。

运行：  python tests/test_planning_prompt_golden.py
"""

from __future__ import annotations

from pathlib import Path as _Path
import sys
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import ast
import asyncio
import json
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.plan.prompt import (  # noqa: E402
    PLANNING_PROMPT_FILE, _PLANNING_TEMPLATE, planning_section,
)
from agent_framework.prompts import (  # noqa: E402
    DEFAULT_PROMPTS_DIR, build_prompt_library, placeholders_in,
)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "tests" / "data" / PLANNING_PROMPT_FILE.replace(
    ".md", "_golden.json")).read_text(encoding="utf-8"))

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class _StubGate:
    """只给 planning_section 用到的三张口：白名单、开关事实、提示词库引用。"""

    def __init__(self, whitelist, knob_facts, library=None) -> None:
        self._wl, self._facts, self.prompt_library = whitelist, knob_facts, library

    def whitelist(self) -> list:
        return list(self._wl)

    async def knob_facts(self) -> list:
        return json.loads(json.dumps(self._facts))  # 防被渲染过程就地改


def _diff(a: str, b: str) -> str:
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return (f"\n      断点 @{at}\n      金样: …{a[max(0, at - 40):at + 40]!r}"
            f"\n      实得: …{b[max(0, at - 40):at + 40]!r}")


def _render(case, library):
    gate = _StubGate(case["whitelist"], case["knob_facts"], library)
    return asyncio.run(planning_section(gate, **case["kwargs"]))


def case_inline_fallback() -> None:
    print("\n① 内联回落（无提示词库）逐字等于金样")
    for case in GOLDEN["cases"]:
        got = _render(case, None)
        check(got == case["expect"],
              f"{case['name']}（{len(got)} 字符）" + ("" if got == case["expect"]
                                                    else _diff(case["expect"], got)))


def case_on_disk() -> None:
    print(f"② 仓库自带 prompts/{PLANNING_PROMPT_FILE} 逐字等于同一批金样")
    lib = build_prompt_library(None)
    check(PLANNING_PROMPT_FILE in lib.loaded_from_disk(),
          f"文件真的在磁盘上（读到的清单：{lib.loaded_from_disk()}）")
    for case in GOLDEN["cases"]:
        got = _render(case, lib)
        check(got == case["expect"],
              f"{case['name']}（{len(got)} 字符）" + ("" if got == case["expect"]
                                                    else _diff(case["expect"], got)))


def case_no_leftover_placeholder() -> None:
    print("③ 渲染结果里不留守卫行与未替换的数据占位符")
    everything = next(c for c in GOLDEN["cases"] if c["name"] == "everything")
    data_keys = ("nodes", "knobs", "prior_cards", "feedback_quote",
                 "session_history_rows", "pending_plan_id", "pending_label",
                 "pending_run_id", "pending_unfulfilled", "pending_asked")
    for label, library in (("内联", None), ("磁盘", build_prompt_library(None))):
        got = _render(everything, library)
        check("{?" not in got, f"{label}版：块守卫行不会漏进上下文")
        left = [f"{{{k}}}" for k in data_keys if f"{{{k}}}" in got]
        check(not left, f"{label}版：数据键全被替换，无残留 {left}")


def case_guard_key_contract() -> None:
    print("④ 模板要的键 == 代码给的键（少一个键就整段静默消失）")
    keys = placeholders_in(_PLANNING_TEMPLATE)
    wanted = {"has_pending_plan", "prior_cards", "feedback_quote", "has_pending",
              "session_history_rows", "nodes", "knobs", "pending_plan_id",
              "pending_label", "pending_run_id", "pending_unfulfilled", "pending_asked"}
    check(keys == wanted, f"内联模板的键名/守卫名固定：{sorted(keys)}")
    file_keys = placeholders_in(
        (DEFAULT_PROMPTS_DIR / PLANNING_PROMPT_FILE).read_text(encoding="utf-8"))
    check(file_keys == wanted, f"磁盘模板的键名与内联一致：{sorted(file_keys)}")

    given = {"nodes", "knobs", "has_pending_plan", "prior_cards", "feedback_quote",
             "session_history_rows", "has_pending", "pending_plan_id", "pending_label",
             "pending_run_id", "pending_unfulfilled", "pending_asked"}
    check(wanted <= given, "planning_section 的 values 覆盖了模板要的每个键")


def case_templates_identical() -> None:
    print("⑤ 内联回落与磁盘模板逐字相同（漂移守卫）")
    on_disk = (DEFAULT_PROMPTS_DIR / PLANNING_PROMPT_FILE).read_text(encoding="utf-8")

    def norm(t: str) -> str:
        return "\n".join(ln.rstrip() for ln in t.replace("\r\n", "\n").split("\n")).rstrip("\n")

    check(norm(_PLANNING_TEMPLATE) == norm(on_disk),
          "两份文案只差行尾空白；有实质差异就是漂了" + ("" if norm(_PLANNING_TEMPLATE)
                                                       == norm(on_disk)
                                                       else _diff(norm(on_disk),
                                                                  norm(_PLANNING_TEMPLATE))))


def _plangate_call_kwargs(path: Path) -> set[str]:
    """run_server 里 ``PlanGate(...)`` 的形参名（AST 取，不靠正则认注释里的字样）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "PlanGate":
                found.update(kw.arg for kw in node.keywords if kw.arg)
    return found


def case_wired_in_production() -> None:
    print("⑥ 生产装配真的传了 prompt_library（不是写了模板没人接）")
    kw = _plangate_call_kwargs(ROOT / "run_server.py")
    check("prompt_library" in kw, f"run_server 的 PlanGate(...) 带上 prompt_library：{sorted(kw)}")
    src = (ROOT / "run_server.py").read_text(encoding="utf-8")
    check(src.index("prompt_library = build_prompt_library")
          < src.index("plan_gate = PlanGate("),
          "提示词库在计划门之前构造（传的是实例，不是 None）")


def main() -> int:
    print("=== 规划轮提示词迁移：金样逐字 / 块守卫 / 漂移 / 接线 ===")
    case_inline_fallback()
    case_on_disk()
    case_no_leftover_placeholder()
    case_guard_key_contract()
    case_templates_identical()
    case_wired_in_production()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
