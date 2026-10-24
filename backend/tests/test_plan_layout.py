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
                "gate", "prompt", "reconcile", "resume", "skills", "tools", "structure",
                "knobs", "switches"}
    present = {p.stem for p in PKG.glob("*.py")}
    check(expected <= present,
          f"计划门包齐 {len(expected)} 个模块（`knobs` 是 T40 的判据层、"
          f"`switches` 是单取值那一层）：{sorted(present)}")
    big = {p.name: len(p.read_text(encoding="utf-8").splitlines()) for p in PKG.glob("*.py")}
    check(all(n <= 400 for n in big.values()),
          f"没有任何一个模块长回 god file（最大 {max(big.values())} 行：{big}）")


def case_dependency_direction() -> None:
    print("\n=== ② 依赖方向：底层不认识上层，装配层不被任何一层引用 ===")
    deps = {p.stem: module_deps(p) for p in PKG.glob("*.py")}
    check(deps["support"][0] == set(), "support 不 import 包内任何模块（谁都能用它）")
    for mod in ("wording", "vocab", "compile", "resume"):
        check(deps[mod][0] <= {"support"}, f"{mod} 只依赖 support：{sorted(deps[mod][0])}")
    # `knobs`（T40 的判据层）与 `switches`（单取值那层）都是只依赖 support 的叶子层，
    # 所以上层读它们不破坏方向；允许集合**逐模块点名**，不是把判据放宽成一个全局阈值。
    check(deps["switches"][0] <= {"support"},
          f"switches 只依赖 support：{sorted(deps['switches'][0])}")
    # skills 与 knobs 共用同一条节点名口径（`bare_node`），这是 T40 有意留的一处复用：
    # 两处各写一套「怎么从计划里的字符串认出一个节点」会漂成两个答案。
    check(deps["skills"][0] <= {"support", "knobs"},
          f"skills 只依赖 support+knobs：{sorted(deps['skills'][0])}")
    check(deps["validate"][0] <= {"support", "vocab", "structure", "knobs", "switches"},
          f"validate 只依赖 support+vocab+structure+knobs+switches："
          f"{sorted(deps['validate'][0])}")
    check(deps["structure"][0] <= {"support", "vocab"},
          f"structure 只依赖 support+vocab：{sorted(deps['structure'][0])}")
    check(deps["reconcile"][0] <= {"support"},
          f"reconcile 只依赖 support：{sorted(deps['reconcile'][0])}")
    # `knobs`（T40）是只依赖 support 的叶子层：判「这一段发不发」的判据住在那里，
    # 文案拼装（prompt）读它、它不认识 prompt——方向仍然是单向的。
    check(deps["knobs"][0] <= {"support"},
          f"knobs 只依赖 support：{sorted(deps['knobs'][0])}")
    check(deps["prompt"][0] <= {"support", "knobs"},
          f"prompt 只依赖 support 与 knobs（判据层）：{sorted(deps['prompt'][0])}")
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
    # `context_gate`：每轮从 `tool_gate.shrink(messages)` 现算的瘦身读数，不落盘
    # （落盘反而会让续跑拿旧窗口当新窗口）。`_shot_card_refusals`：一次 run 内
    # 「已经拒过几张 shot 卡」的计数，只在本轮工具回执里读（counts_as_failure），
    # 没有跨挂起的读者。`_skill_body_refusals` 同性质——它是「同一份正文的窗口闸
    # 已经告过几次」的升级计数器，挂起后不该带着旧计数续跑（带着只会更早停用工具）。
    # `_skill_attach_refusals` 同理，是「同一份附件的窗口闸」的升级计数器（真机
    # 2026-10-17：模型对同一份 demo 连打 80 枪 limit=1，见 agent.py 的
    # `_MAX_ATTACH_PAGES`）。三个都是本轮/本 run 的注入，不是跨挂起的事实。
    # `_skill_budget_refusals` / `_skill_read_bytes` 同族：规划轮材料预算的升级
    # 计数器与累计回喂字节（run d63501fbe337：103 枪 load_skill、≈1.29MB、
    # 零张计划卡）——挂起后带着旧累计续跑只会更早停用工具，落盘也失真。
    allowed = {"run_id", "checkpoint", "checkpoint_manager", "handover",
               "confirm_plan_requested", "_fail_counts", "_media_urls_pushed",
               "context_gate", "_shot_card_refusals", "_skill_body_refusals",
               "_skill_attach_refusals", "_attach_refusals",
               "_skill_budget_refusals",
               "_skill_read_bytes",
               # 整 run 技能阅读总量（2026-10-19，`_MAX_SKILL_READS`）：`_skill_reads`
               # 是这条 run 已经放行的 load_skill 枪数，`_skill_read_refusals` 是硬线
               # 之后的升级计数器。与上面几本账同族——**本 run 的注入**，挂起后重来
               # 是对的（续跑那半截是另一段工作，带着旧总量只会更早停用工具）。
               "_skill_reads", "_skill_read_refusals",
               # 迭代预算感知（2026-10-17，`_ITER_SOFT_RATIO`）：三个都是**本轮注入**，
               # 落盘反而失真。`_iter_limit` 是本轮的有效上限（续跑会往后延，见
               # `_iteration_limit`），`_iter_soft_noticed` 是「软线那句已经说过」的一位，
               # `_deadline_refusals` 是见底闸的升级计数器（告两次后记真失败）。
               # 挂起后续跑时它们重来一遍是对的：续跑那半截本来就换了一条预算。
               "_iter_limit", "_iter_soft_noticed", "_deadline_refusals",
               # 同族（2026-10-17）：`_plan_demo_refusals` 是规划轮 demo 份数闸的
               # 升级计数器（`_MAX_PLAN_DEMOS`），`_web_fetches` / `_web_fetch_refusals`
               # 是全 run 联网枪数与升级计数器（`_MAX_WEB_FETCHES`）。三者都只在本
               # run 的工具回执里读，没有跨挂起的读者。
               "_plan_demo_refusals", "_web_fetches", "_web_fetch_refusals",
               # 无进展检测（2026-10-17，`_NO_PROGRESS_ROUNDS`）：`_seen_call_keys`
               # 是「这条 run 已经拿到过哪些执行结果」的累计集，`_no_progress_rounds`
               # 是连续零新信息的轮数，`_no_progress_trips` 是触发过几次，
               # `_force_tool_whitelist` 是「哪一轮只给 submit_plan/ask_user」的轮次号。
               # 四个都是**本 run 的注入**，不是跨挂起的事实：续跑那半截是另一段工作，
               # 继承「你刚才在打转」的判定只会更早把工具收走。轮次号那一位尤其——
               # 续跑后轮次号是新的，旧号自然失效，落盘反而更容易撞上同一号。
               "_seen_call_keys", "_no_progress_rounds", "_no_progress_trips",
               "_force_tool_whitelist",
               # `_progress_keys` 是无进展检测的第二本账：按**精确参数**记「这一枪
               # 是不是同一个动作」（`_repeat_key` 按文件记，判的是「是不是同一份」）。
               # 同族：本 run 注入，挂起后重来。
               "_progress_keys"}
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
