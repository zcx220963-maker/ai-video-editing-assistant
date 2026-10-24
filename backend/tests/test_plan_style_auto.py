# -*- coding: utf-8 -*-
"""自动入口：规划轮自己挑风格（用户只写自然语言）。

用户的原话是「导入别人的 skill，只保留一个自动入口，就能调用我们内部所有的组件库」。
自动入口的最后一环在**规划轮**：模型拿 ``find_skill`` 搜到对得上的风格包/导入技能，
把名字挂到候选计划上；执行轮 ``plan_motion`` 据此把整份技能注入写手。这一环静默断掉
的表现与「用户没选风格」完全一样——出片是默认视觉，没有任何报错——所以逐条钉住：

① 合法的风格名（系统风格包 / 用户导入的技能）原样留在候选上；
② 查不到的名字、把流程技能当风格用：**降级成警告并清空**，不打死整张卡；
③ 没接技能库（读不到）：放行 + 警告，与 ``skills_hint`` 同一种处境；
④ 用户这一次明确选的压过模型自己挑的；用户没选时模型挑的不能被覆盖。

运行：  python tests/test_plan_style_auto.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from test_plan_gate import make_gate, one_plan  # noqa: E402
from test_plan_style import _Gate, _Issues  # noqa: E402

from agent_framework.hooks import _current_hook_ctx  # noqa: E402
from agent_framework.identity import use_identity  # noqa: E402
from agent_framework.plan import SubmitPlanTool  # noqa: E402
from agent_framework.run_state import RunState  # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _skill(name: str, kind: str = "style", owner: str | None = None,
           available: bool = True, display: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, display=display or name, kind=kind,
                           owner_user_id=owner, available=available,
                           unavailable_reason="")


def skills(*items) -> object:
    table = {s.name: s for s in items}

    async def _load():
        return table
    return _load


def _with_style(name: str, **over) -> dict:
    plan = one_plan(**over)
    plan["style_skill"] = name
    return plan


async def case_valid_style() -> None:
    print("① 合法的风格名原样留在候选上（系统风格包 / 用户导入的技能）")
    gate = make_gate(skills=skills(
        _skill("highlight_extraction", kind="capability"),   # one_plan 的 skills_hint
        _skill("style_silhouette_history"),
        _skill("my_imported_skill", kind="workflow", owner="u1"),
    ))
    plans, issues = await gate.validate({"plans": [
        _with_style("style_silhouette_history"),
        _with_style("my_imported_skill", plan_id="p2", label="导入技能那版"),
    ]})
    check(issues.ok and len(plans) == 2, f"两张卡都通过（errors={issues.errors[:2]}）")
    check([p.get("style_skill") for p in plans]
          == ["style_silhouette_history", "my_imported_skill"],
          f"两份候选各留自己的风格：{[p.get('style_skill') for p in plans]}")
    check(not issues.warnings, f"没有多余告警：{issues.warnings}")


async def case_display_name() -> None:
    print("\n①′ 模型填了中文名（用户就是这么说的）：按显示名认领，换成机器名")
    gate = make_gate(skills=skills(
        _skill("highlight_extraction", kind="capability"),
        _skill("style_silhouette_history", display="剪影年轮"),
    ))
    plans, issues = await gate.validate({"plans": [_with_style("剪影年轮")]})
    check(issues.ok and plans and plans[0].get("style_skill") == "style_silhouette_history",
          f"显示名认领成机器名：{plans[0].get('style_skill') if plans else '无'}")
    check(not issues.warnings, f"认领成功不告警：{issues.warnings}")


async def case_bad_names() -> None:
    print("\n② 查不到的名字 / 流程技能当风格：清空并告警，但卡照样过")
    gate = make_gate(skills=skills(
        _skill("highlight_extraction", kind="capability"),
        _skill("style_demo"),
        _skill("motion_explainer", kind="workflow", owner=None),
        _skill("other_users_pack", kind="style", owner="someone-else"),
    ))
    plans, issues = await gate.validate({"plans": [
        _with_style("style_stylesheet", label="我自己编的"),
        _with_style("motion_explainer", plan_id="p2", label="流程技能"),
    ]})
    check(issues.ok and len(plans) == 2,
          f"两张卡照样通过（风格不对不该打死计划）：errors={issues.errors[:2]}")
    check(all("style_skill" not in p for p in plans),
          f"两份候选上的风格都被清空：{[p.get('style_skill') for p in plans]}")
    check(any("不存在" in w for w in issues.warnings),
          f"编出来的名字报「不存在」：{issues.warnings}")
    check(any("不是风格包" in w for w in issues.warnings),
          f"流程技能当风格报「不是风格包」：{issues.warnings}")
    # 归属不在这一层判（门里没有 user_id）：执行期 `_attach_style_pack` 会拒并回执。
    plans2, issues2 = await gate.validate({"plans": [_with_style("other_users_pack")]})
    check(issues2.ok and plans2[0].get("style_skill") == "other_users_pack",
          "别人的包在这一层放行（归属由执行期按 user_id 判，宁可少拦不可错拦）")


async def case_unreadable() -> None:
    print("\n③ 没接技能库：放行 + 告警（与 skills_hint 同一种处境）")
    gate = make_gate()          # skills=None → 校验器拿不到技能表
    plans, issues = await gate.validate({"plans": [_with_style("style_demo")]})
    check(issues.ok and plans and plans[0].get("style_skill") == "style_demo",
          "名字保留（读不到不等于用户选错了）")
    check(any("未能核验" in w for w in issues.warnings),
          f"卡面告警如实说没核验：{issues.warnings}")


def case_tool_precedence() -> None:
    print("\n④ 优先级：用户明确选的压过模型挑的；用户没选时模型挑的留着")
    tool = SubmitPlanTool(_Gate())
    ctx = SimpleNamespace(state=RunState(), extras={"checkpoint": SimpleNamespace(plan={})})
    token = _current_hook_ctx.set(ctx)
    try:
        # 用户没选（身份里是空串）：模型在候选上自己挑的那份必须留下
        with use_identity("u", "c"):
            asyncio.run(tool.execute([_with_style("style_auto_pick")]))
        cands = ctx.extras["checkpoint"].plan.get("candidates") or []
        check(cands and cands[0].get("style_skill") == "style_auto_pick",
              f"用户没选 → 模型挑的留下：{cands[0].get('style_skill') if cands else '无'}")

        # 用户在下拉里选了一套：所有候选都换成用户那套
        ctx.extras["checkpoint"].plan.clear()
        with use_identity("u", "c", style_skill="style_user_pick"):
            asyncio.run(tool.execute([_with_style("style_auto_pick"),
                                      _with_style("style_other", plan_id="p2")]))
        cands = ctx.extras["checkpoint"].plan.get("candidates") or []
        check([c.get("style_skill") for c in cands] == ["style_user_pick"] * 2,
              f"用户选了 → 两份候选都听用户的：{[c.get('style_skill') for c in cands]}")
    finally:
        _current_hook_ctx.reset(token)


def main() -> None:
    asyncio.run(case_valid_style())
    asyncio.run(case_display_name())
    asyncio.run(case_bad_names())
    asyncio.run(case_unreadable())
    case_tool_precedence()
    print(f"\n{'全部通过' if not _fails else f'有 {_fails} 项未通过'}")
    print(f"用例 {_checks} 条")
    raise SystemExit(0 if not _fails else 1)


if __name__ == "__main__":
    main()
