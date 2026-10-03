# -*- coding: utf-8 -*-
"""提问卡是单例，但它**不能**把用户正填着的那张吃掉（前端接线守卫）。

真机事故（用户原话「怎么我第一个弹窗的问题还没选完，就继续了然后跳第二次弹窗，
第一次都没提交啊」）：规划轮交出计划卡 → 自动弹计划确认卡（origin=plan_confirm）；
另一条 ask 帧同时到达 → `openQuestionCard` 直接整体重建 `questionCard`，
用户选到一半的答案（answers / customAnswers）连同那张卡一起没了。
用户接着点「确认」，续跑的那条审批根本不是计划卡那条，流程对不上。

前端没有 JS 测试跑器，所以这里按仓库既有做法做**源码级接线守卫**
（同 tests/test_display_outlets.py 的「前端接线」段）：钉住三件事——
① 被顶掉的卡整份进 `parkedCards` 队列，而不是丢弃；
② 当前卡提交/关闭之后真的把队首还回来（三个出口都要还）；
③ 同一道题被再问一次不重建（否则队列也救不回被洗掉的答案）。

运行：  python tests/test_question_card_queue.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "frontend" / "src" / "App.vue"

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


TEXT = APP.read_text(encoding="utf-8")


def body(name: str) -> str:
    """取一个顶层函数的函数体（靠列 0 的收尾 ``}`` 收口）。"""
    start = TEXT.index(f"function {name}(")
    return TEXT[start:TEXT.index("\n}\n", start)]


def case_park_on_displace() -> None:
    print("\n=== ① 新卡上场时，旧卡整份进队列（不是丢掉） ===")
    open_card = body("openQuestionCard")
    push = open_card.find("parkedCards.value.push(cur)")
    assign = open_card.find("questionCard.value = {")
    check(push >= 0, "openQuestionCard 里确实把被顶掉的卡放进 parkedCards")
    check(assign >= 0 and 0 <= push < assign,
          "先让位（push）再换新卡，顺序不能反")
    check("if (cur && !cur.submitting) parkedCards.value.push(cur)" in open_card,
          "正在提交的那张不动（它的回调靠对象身份认自己，换掉就关不上了）")
    check("const parkedCards = ref([])" in TEXT, "队列本身是响应式的 ref([])")


def case_same_question_not_rebuilt() -> None:
    print("\n=== ② 同一道题被再问一次不重建（重建 = 答案洗掉） ===")
    open_card = body("openQuestionCard")
    check("sig(cur) === mine" in open_card and "return cur;" in open_card,
          "同 run + 同题面 → 直接把现有那张还回去，不重建")
    check("!(meta.card && cur.card === meta.card)" in open_card,
          "计划卡例外：同一张卡重开是用户要刷新题面（版本换过就得重算）")


def case_restore_points() -> None:
    print("\n=== ③ 当前卡走完（提交/关闭）→ 队首还回来 ===")
    close = body("qClose")
    check("questionCard.value = null" in close
          and "restoreParkedCard()" in close,
          "「稍后再说」关掉当前卡后把让位的还回来")

    for name, marker in (("decideApproval", "questionCard.value.approval === card"),
                         ("confirmPlan", "questionCard.value.card === card")):
        fn = body(name)
        at = fn.find(marker)
        seg = fn[at:at + 260] if at >= 0 else ""
        check(at >= 0 and "questionCard.value = null" in seg
              and "restoreParkedCard()" in seg,
              f"{name}：确认「就是我刚交的那张」之后清空并让位卡归位")

    restore = body("restoreParkedCard")
    check("if (questionCard.value) return;" in restore
          and "parkedCards.value.shift()" in restore,
          "restoreParkedCard：还开着别的题就等它，不抢屏幕")


def case_queue_is_per_conversation() -> None:
    print("\n=== ④ 队列不跨会话：切走就清（服务端挂起态会补弹回来） ===")
    conv = body("openConv")
    check("parkedCards.value = []" in conv,
          "openConv 开头清空队列（挂起态在服务端，checkActiveRun 会补弹）")


def case_signature_uses_frame_run() -> None:
    print("\n=== ⑤ 签名用的是帧上那条 run（挂起的就是它） ===")
    ask = body("askQuestionCard")
    check("runId || frameRunId || src.plan_run_id || src.run_id" in ask,
          "askQuestionCard：帧 run 优先，审批卡记录兜底")
    bubble = body("planBubble")
    check("planAskCard" not in bubble, "planBubble 仍只负责推气泡（弹卡走 openPlanModal）")
    plan = body("planAskCard")
    check("card.frame_run_id || card.plan_run_id" in plan,
          "计划确认卡也按帧 run 对齐签名")
    check("frame_run_id: src.run_id || \"\"" in TEXT,
          "planCardOf 把帧 run 记在卡上（签名/补弹都读它）")


def main() -> int:
    case_park_on_displace()
    case_same_question_not_rebuilt()
    case_restore_points()
    case_queue_is_per_conversation()
    case_signature_uses_frame_run()
    print("\n" + ("全部通过" if not _fails else f"有 {_fails} 项未通过"), flush=True)
    print(f"用例 {_checks} 条", flush=True)
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
