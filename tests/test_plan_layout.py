"""计划门拆包的结构性护栏：依赖方向、公开出口、以及 `_drive` 不再是 god method。

这一套钉的不是剪辑逻辑（那些在 test_plan_gate / test_plan_flow 里），而是**形状**：
`agent_framework/plan_gate.py` 原先 1526 行混 6 种职责、`_drive` 325 行靠
`ctx.extras` 的约定键互相旁路通信，两条都已经在 2026-10-02 拆掉。这份用例的作用是让
它们**不能悄悄长回去**——重新往一个文件里堆职责、或者重新用内存字典旁路 checkpoint，
这里会先红。

运行：  python tests/test_plan_layout.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "agent_framework" / "plan"

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def module_deps(path: Path) -> tuple[set[str], set[str]]:
    """(运行期的包内依赖, 只在类型标注里出现的包内依赖)。

    ``if False:  # TYPE_CHECKING`` 里的那几条不算运行期依赖——装配层被各层引用是环，
    但只为写个形参标注而引用不是。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    runtime: set[str] = set()
    typed: set[str] = set()

    def walk(node: ast.AST, type_only: bool) -> None:
        if isinstance(node, ast.If):
            # 常量 False 或名字 TYPE_CHECKING 的分支：里面的 import 不做判据
            test = node.test
            guard = (isinstance(test, ast.Constant) and test.value is False) or \
                    (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            for child in ast.iter_child_nodes(node):
                walk(child, type_only or guard)
            return
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            if path.name == "__init__.py":
                return
            names = [a.name for a in node.names if a.name != "annotations"]
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                target = node.module or ""
                # from . import support  →  support；from .support import x  →  support
                deps = {target} if target else set(names)
                (typed if type_only else runtime).update(
                    d.split(".")[0] for d in deps if d)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, type_only)

    walk(tree, False)
    return runtime, typed


def case_files_exist() -> None:
    print("\n=== ① 一文件一职责：包在、老 god file 不在 ===")
    check(not (ROOT / "agent_framework" / "plan_gate.py").exists(),
          "agent_framework/plan_gate.py 已经不在（拆成包，不是加一份副本）")
    expected = {"__init__", "support", "wording", "vocab", "validate", "compile",
                "gate", "prompt", "reconcile", "resume", "skills", "tools"}
    present = {p.stem for p in PKG.glob("*.py")}
    check(expected <= present, f"计划门包齐 12 个模块：{sorted(present)}")
    big = {p.name: len(p.read_text(encoding="utf-8").splitlines()) for p in PKG.glob("*.py")}
    check(all(n <= 400 for n in big.values()),
          f"没有任何一个模块长回 god file（最大 {max(big.values())} 行：{big}）")


def case_dependency_direction() -> None:
    print("\n=== ② 依赖方向：底层不认识上层，装配层不被任何一层引用 ===")
    deps = {p.stem: module_deps(p) for p in PKG.glob("*.py")}
    check(deps["support"][0] == set(), "support 不 import 包内任何模块（谁都能用它）")
    for mod in ("wording", "vocab", "compile", "skills", "resume"):
        check(deps[mod][0] <= {"support"}, f"{mod} 只依赖 support：{sorted(deps[mod][0])}")
    check(deps["validate"][0] <= {"support", "vocab"},
          f"validate 只依赖 support+vocab：{sorted(deps['validate'][0])}")
    check(deps["reconcile"][0] <= {"support"},
          f"reconcile 只依赖 support：{sorted(deps['reconcile'][0])}")
    check(deps["prompt"][0] <= {"support"},
          f"prompt 只依赖 support：{sorted(deps['prompt'][0])}")
    check(deps["tools"][0] <= {"support"},
          f"tools 只依赖 support：{sorted(deps['tools'][0])}")
    runtime_users = {m for m, (rt, _) in deps.items() if "gate" in rt}
    check(not runtime_users, f"运行期没有任何模块引用装配层：{sorted(runtime_users)}")
    check("gate" in deps["tools"][1] or "gate" in deps["prompt"][1],
          "只为形参标注才引用 PlanGate（类型层，运行期不成环）")
    check(deps["gate"][0] >= {"vocab", "validate", "compile"},
          f"装配层把三层拼起来：{sorted(deps['gate'][0])}")


def case_public_surface() -> None:
    print("\n=== ③ 对外出口：调用方只认 agent_framework.plan ===")
    from agent_framework import plan

    required = ["PlanGate", "SubmitPlanTool", "ConfirmPlanTool", "PlanIssues",
                "reconcile", "render_injections", "planning_section", "preload_skills",
                "pending_continuation", "claims_plan_card", "claims_step_executed",
                "PlanCardHook", "PlanReconcileHook", "neutralize",
                "drain_plan_cards", "record_plan_card", "CUSTOM_MAX_CHARS",
                "CUSTOM_MAX_ITEMS", "PLAN_MAX_CANDIDATES"]
    missing = [n for n in required if not hasattr(plan, n)]
    check(not missing, f"计划门对外的名字一个都没漏：{missing}")
    check(set(plan.__all__) >= set(required), "__all__ 覆盖了这些对外名字")

    from agent_framework.plan.compile import PlanCompiler
    from agent_framework.plan.gate import PlanGate as GateCls
    from agent_framework.plan.validate import PlanValidator
    from agent_framework.plan.vocab import PlanVocabulary
    from agent_framework.tool import ToolRegistry

    gate = GateCls(registry=ToolRegistry())
    check(isinstance(gate.vocab, PlanVocabulary) and isinstance(gate.validator, PlanValidator)
          and isinstance(gate.compiler, PlanCompiler),
          "PlanGate 只做装配：自己不持任何判据")
    check(gate.whitelist() == set(), "契约未接入时白名单为空（没有可规划的节点）")


def case_drive_is_not_a_god_method() -> None:
    print("\n=== ④ _drive 不再是 god method ===")
    src = (ROOT / "agent_framework" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "AgentOnceRun")
    # _open_run 是同步的（它只组装一轮的上下文，不发请求），所以两种 def 都算方法
    methods = {n.name: n for n in cls.body
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
    drive = methods.get("_drive")
    check(drive is not None, "_drive 还在（没被拆散到看不见）")
    if drive is None:
        return
    span = drive.end_lineno - drive.lineno + 1
    check(span <= 90, f"_drive 只剩骨架（{span} 行；拆之前 325 行）")
    for name in ("_open_run", "_tool_round", "_suppress_repeat_render", "_pause",
                 "_nudge_back", "_deliver"):
        check(name in methods, f"切出去的那一块有名字：{name}")
    check("_adopt_fork" in methods and "_settle_pending" in methods
          and "_execute_tool_calls" in methods and "_follow_inflight_renders" in methods,
          "原有的四块（分叉交接/审批收口/工具执行/渲染代查）仍在，没被就地塞回循环")


def case_no_extras_bypass() -> None:
    print("\n=== ⑤ 不许再用 ctx.extras 旁路 checkpoint ===")
    banned = ["approved_plan", "tool_calls_seen", "calls_attempted", "calls_executed",
              "plan_candidates", "plan_warnings", "plan_card_pushed", "audit_pushed",
              "plan_audit", "qa_parts"]
    files = [ROOT / "agent_framework" / "agent.py", *sorted(PKG.glob("*.py"))]
    hits = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        for key in banned:
            if f'extras["{key}"]' in text:
                hits.append(f"{f.name}:{key}")
    check(not hits, f"跨挂起的事实只走 RunState（不再挂在 extras 约定键上）：{hits}")
    # 仍然允许的本轮注入：这几个键写在 extras 里是对的，落盘反而失真
    allowed = {"run_id", "checkpoint", "checkpoint_manager", "handover",
               "confirm_plan_requested", "_fail_counts", "_media_urls_pushed"}
    import re
    keys = set(re.findall(r'extras\["([a-zA-Z_]+)"\]',
                          (ROOT / "agent_framework" / "agent.py").read_text(encoding="utf-8")))
    check(keys <= allowed, f"agent.py 里留在 extras 的键都在白名单内：{sorted(keys)}")


def main() -> int:
    case_files_exist()
    case_dependency_direction()
    case_public_surface()
    case_drive_is_not_a_god_method()
    case_no_extras_bypass()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
