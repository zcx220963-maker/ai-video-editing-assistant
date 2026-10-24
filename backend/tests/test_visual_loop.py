# -*- coding: utf-8 -*-
"""看片闭环：看 → 按意见改 → 重渲那几镜 → **再看**，直到没有红镜或轮数用完。

用户 2026-10-22 的说法是「出静帧 → 看图评审 → 带着图重写，N 轮」。原先只有
「看一轮、改一轮」：改完的镜不再看第二眼，于是「改了却更差」「改完还是红」两种
情况都只能落到主对话，主对话再走一整轮 `plan_motion`（全片重写、重渲、重评）。

这一支钉住四件事：
① 第二轮只看改过的那几镜，红的接着改，绿了就收；
② 同一句意见改过一次还红 → **停手**（再改只是同一个来回），镜号留下来落账；
③ 重写一镜都没成 → 不再渲、不再看；
④ 轮数按 `review_rounds` 封顶，spec 可覆盖。

运行：  python tests/test_visual_loop.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.motion import design_review as dr  # noqa: E402
from storyline_server.nodes.motion_plan import PlanMotionNode  # noqa: E402
from agent_framework.orchestration import NodeState  # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def _fail(sid: str, fix: str) -> dict:
    return {"shot": sid, "why": f"{sid} 没画好", "fix": fix,
            "scores": {k: 2 for k in dr.RUBRIC}}


def _review(*, passed=(), failed=(), unjudged=(), cost=0.01) -> dict:
    return {"checked": len(passed) + len(failed), "passed": list(passed),
            "failed": list(failed), "unjudged": list(unjudged), "scores": {},
            "cost_yuan": cost}


class _Rewriter:
    """按镜号回不同结果；记录每次调的镜号与交来的静帧。"""

    def __init__(self, plan: dict[str, list[dict]]) -> None:
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, *, spec, failed, stills=None, **kw) -> dict:
        out = {"rewritten": {}, "skipped": [], "cost_yuan": 0.02, "calls": 1}
        for item in failed:
            sid = str(item["shot"])
            self.calls.append((sid, (stills or {}).get(sid)))
            seq = self.plan.get(sid) or []
            got = seq.pop(0) if seq else {"ok": False, "note": "没计划了"}
            out["rewritten"][sid] = got
            if not got.get("ok"):
                out["skipped"].append(sid)
        return out


def _node(renders: dict[str, dict]) -> tuple[PlanMotionNode, list[set]]:
    """一个不带 storage 的节点：`_publish_previews` 换成记账的假口。"""
    node = PlanMotionNode.__new__(PlanMotionNode)
    seen: list[set] = []

    async def _publish(state, spec, *, only=None):
        seen.append(set(only or ()))
        files = {sid: f"/tmp/{sid}.jpg" for sid in (only or ())}
        return ({sid: f"previews/{sid}.jpg" for sid in files}, [],
                {}, files, dict(renders.get("bad") or {}))

    node._publish_previews = _publish            # type: ignore[method-assign]
    return node, seen


def _run(node, *, rounds, reviews, rewriter) -> dict:
    spec = {**SPECK, "visual_review_rounds": rounds,
            "shots": [{"id": sid, "text": f"{sid} 的句子"} for sid in ("s01", "s02", "s03")]}
    state = NodeState(session_id="u:v:c:v")
    reviews_seq = list(reviews)
    files = {"s01": "/tmp/s01.jpg", "s02": "/tmp/s02.jpg", "s03": "/tmp/s03.jpg"}

    async def fake_review(**kw):
        return reviews_seq.pop(0)

    with mock.patch.object(dr, "review_stills", fake_review), \
         mock.patch.object(dr, "rewrite_failed", rewriter):
        return asyncio.run(node._visual_loop(
            state, spec, refs={}, files=files, notes=[], failures={}, art={}))


SPECK = {"aspect": "landscape", "fps": 30, "recurring_prop": "",
         "style": "", "style_grammar": "", "style_pack": None}


def case_two_rounds() -> None:
    print("① 第一轮红两镜 → 改 → 第二轮只看这两镜：一镜转绿、一镜换了意见接着改")
    node, seen = _node({})
    rewriter = _Rewriter({"s01": [{"ok": True}], "s02": [{"ok": True}, {"ok": True}]})
    out = _run(node, rounds=3, reviews=[
        _review(passed=["s03"], failed=[_fail("s01", "主体太小"), _fail("s02", "描边太细")]),
        _review(passed=["s01"], failed=[_fail("s02", "换一处光源")]),
        _review(passed=["s02"]),
    ], rewriter=rewriter)
    check(len(out["rounds"]) == 3, f"走了三轮：{len(out['rounds'])}")
    check(seen == [{"s01", "s02"}, {"s02"}],
          f"重渲只渲改过的镜，而且逐轮收窄：{seen}")
    check(out["rounds"][1]["checked"] == 2,
          f"第二轮只看了改过的那两镜：{out['rounds'][1]['checked']}")
    check(out["rounds"][2]["checked"] == 1, "第三轮只看还红的那一镜")
    check(out["still_red"] == [], f"最后没有红镜：{out['still_red']}")
    check(sorted(out["rewritten"]) == ["s01", "s02"], "两镜都改过（s02 改了两回）")
    check(len([c for c in rewriter.calls if c[0] == "s02"]) == 2, "s02 换意见后确实又改了一回")
    check([c[1] for c in rewriter.calls if c[0] == "s02"][1] == "/tmp/s02.jpg",
          "第二回改 s02 时交的是它上一版的静帧（不是最早那张）")
    check(out["first"]["checked"] == 3, "回执里的「第一轮」是整片那一轮")
    check(out["last"]["passed"] == ["s02"], "「最近一轮」是改完以后的那一次")
    check(abs(out["cost_yuan"] - 0.01 * 3 - 0.02 * 2) < 1e-9,
          f"钱按轮累加：{out['cost_yuan']}")
    check(out["skipped"] == [], "改成了的镜不留在「没改成」里（哪怕中间那一轮失手过）")


def case_same_fix_stops() -> None:
    print("\n② 同一句意见改过一次还红 → 停手，镜号留在账上")
    node, seen = _node({})
    rewriter = _Rewriter({"s01": [{"ok": True}]})
    out = _run(node, rounds=3, reviews=[
        _review(passed=[], failed=[_fail("s01", "主体太小")]),
        _review(passed=[], failed=[_fail("s01", "主体太小")]),
    ], rewriter=rewriter)
    check(len(out["rounds"]) == 2, f"第二轮之后停手（没有第三轮）：{len(out['rounds'])} 轮")
    check(out["stale"] == ["s01"], f"记下「同一句」的那一镜：{out['stale']}")
    check(len(rewriter.calls) == 1, f"只改了一次，没有来回烧：{rewriter.calls}")
    check(out["still_red"] == ["s01"], f"仍红如实落账：{out['still_red']}")
    check(seen == [{"s01"}], f"只重渲过一轮：{seen}")


def case_nothing_fixed() -> None:
    print("\n③ 一镜都没改成就停：不再重渲、不再看第二眼")
    node, seen = _node({})
    rewriter = _Rewriter({"s01": [{"ok": False, "note": "闸① 未判"}]})
    out = _run(node, rounds=3, reviews=[
        _review(passed=[], failed=[_fail("s01", "主体太小")]),
    ], rewriter=rewriter)
    check(len(out["rounds"]) == 1, f"只看了一轮：{len(out['rounds'])}")
    check(seen == [], f"没有重渲：{seen}")
    check(out["skipped"] == ["s01"], f"没改成的镜进了 skipped：{out['skipped']}")
    check(out["still_red"] == ["s01"], "仍红照记")


def case_round_cap() -> None:
    print("\n④ 轮数封顶：spec 写 1 就只看一轮")
    node, _ = _node({})
    rewriter = _Rewriter({"s01": [{"ok": True}]})
    out = _run(node, rounds=1, reviews=[
        _review(passed=[], failed=[_fail("s01", "主体太小")]),
    ], rewriter=rewriter)
    check(len(out["rounds"]) == 1, f"只走一轮：{len(out['rounds'])}")
    check(out["rewritten"]["s01"]["ok"] is True, "这一轮照样按意见改（改不改与看几轮无关）")
    check(out["still_red"] == [],
          "改过但没再看的镜**不算仍红**——不知道它现在什么样子，不替它下结论")


def main() -> None:
    case_two_rounds()
    case_same_fix_stops()
    case_nothing_fixed()
    case_round_cap()
    print(f"\n通过 {_checks - _fails}/{_checks}")
    raise SystemExit(1 if _fails else 0)


if __name__ == "__main__":
    main()
