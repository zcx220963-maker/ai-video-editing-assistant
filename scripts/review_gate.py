"""R11 ㉖ 回看环巡检：三支合成回执照进 `review_loop`，看三个出口是不是各归各位。

**为什么要有这么个入口。** ㉖ 是全片唯一一条「不打回画面、只保证交付语不说谎」的闸，而它
判的四件事（三出口 + 缺席）里，有三件在**正常情况下根本不会同时出现**——一条片子要不是
全绿（谁都缺席），要不是红了但没开回看（全是 `not_run`）。真机跑一次只能看见其中一种，
剩下的两种就得靠**合成回执**照出来。T56 的红法原文要的就是这四支：

① **红样**：⑲-a 报红 → 起回看 → 模型改了代码 → 该镜账上**恰好**多一次切片（缓存键变了
   且第二次是现烧的）→ 必须 `reviewed`；
② **红样**：重烧把**别的镜**也带上了 → 违反「局部改只重烧受影响镜」→ 必须**阻断**；
③ **红样**：判据不可用（帧通道/裁决通道缺一半）→ 不许落 `not_run`、也不许落「不需要回看」
   → 必须 `undetermined` **且阻断**（Q113）；
④ **对照**：过了所有闸的镜**不进射程** → 回执里那一栏对它**缺席**（缺席 ≠ not_run）。

这四支跑的是**假渲染器**（不烧一帧像素、不调一次模型）：它验的是这一环自己的记账与三出口，
不是像素。真机那一半（真的取到 `frame.png`、真的改出代码、真的只重烧那一镜）随 §8.1 的
真机夹具轮，如实记为未跑。

用法：
    python scripts/review_gate.py            # 四支全跑，逐条打 ok/XX
    python scripts/review_gate.py --json     # 同上，机器可读

退出码：0 全绿 / 1 有不合预期的出口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _add_backend_to_path() -> None:
    """把 `storyline_server` 放上 import 路径（宿主 `<仓库根>/backend`，容器 `/app`）。"""
    here = Path(__file__).resolve()
    for cand in (here.parents[1] / "backend", Path("/app")):
        if (cand / "storyline_server").is_dir():
            sys.path.insert(0, str(cand))
            return


_add_backend_to_path()

from storyline_server.motion import review_loop as rl      # noqa: E402

FAILURES: list[str] = []


def say(ok: bool, msg: str) -> None:
    if not ok:
        FAILURES.append(msg)
    print(f"{'  ok ' if ok else '  XX '} {msg}")


# ---------------------------------------------------------------------------
# 合成读数：一个「卡片+文字」的镜（红）与一个「两部件场景」的镜（绿）
# ---------------------------------------------------------------------------

_CARD = {"subjects": 0, "subject_share": 0.02, "text_share": 0.60, "grid_text": 5,
         "grid_drawn": 0, "colors": ["rgb(20,20,24)"],
         "band": {"w": 936, "h": 1524}, "subject_boxes": []}
_PARTS = {"subjects": 2, "subject_share": 0.40, "text_share": 0.10, "grid_text": 3,
          "grid_drawn": 9, "colors": ["rgb(14,14,16)"],
          "band": {"w": 936, "h": 1524},
          "subject_boxes": [{"x": 40, "y": 60, "w": 220, "h": 180},
                            {"x": 420, "y": 600, "w": 260, "h": 160}]}


def _row(sid: str, key: str, scanned: dict, cached: bool) -> dict[str, Any]:
    return {"id": sid, "cache_key": key, "cached": cached, "status": "scanned",
            "layout": {"status": "scanned", "scanned": scanned}}


_SPEC = {"fps": 30, "shots": [{"id": "s01", "code": "A"}, {"id": "s02", "code": "B"}]}
_FIRST = {"shots": [_row("s01", "k1", _CARD, False), _row("s02", "k2", _PARTS, True)],
          "shot_files": {"s01": {}, "s02": {}}}


class _Cache:
    """假的切片缓存：`fetch` 一律给一帧（这一支验的是记账，不是像素）。"""

    async def fetch(self, key: str, dst: Any) -> dict[str, Any]:
        return {"frame.png": b"\x89PNG"}


async def _ask(mod: Any, **kw: Any) -> dict[str, Any]:
    """假回看：永远改出一份新代码（新代码落下去之后那一镜就绿了）。"""
    return {"ok": True, "code": "A2", "why": ""}


async def _ask_nothing(mod: Any, **kw: Any) -> dict[str, Any]:
    return {"ok": True, "code": None, "why": "这一版改不动"}


def _install_fake_channel(*, self_ok: bool = True) -> None:
    """装一个**假**帧通道模块 + 假能力探测（真通道是 T31 的交付件，未落地）。

    这一支刻意把「通道可用」这条路也走通：㉖ 的三出口里有两个（`reviewed` / `not_run`）
    只有通道可用时才可能到达，而 T31 没落地的话它们**永远到不了**——那样这一环就只被
    验了三分之一，剩下的两个出口在合并那天才会第一次执行。
    """
    mod = types.ModuleType("storyline_server.motion.judge_channel")
    mod.judge_frame = _ask
    if self_ok:
        async def _self_test(**kw: Any) -> dict[str, Any]:
            return {"ok": True, "reason": "两支夹具分得开"}
    else:
        async def _self_test(**kw: Any) -> dict[str, Any]:
            return {"ok": None, "reason": "没测过"}
    mod.self_test = _self_test
    sys.modules["storyline_server.motion.judge_channel"] = mod
    rl.frame_channel = lambda: (mod, "")            # noqa: E731

    async def _cap(*, available: bool = True) -> dict[str, Any]:
        return {"gate": "26", "available": available, "judge": available,
                "judge_why": "" if available else "判断模型没配（合成：这里假装没配）",
                "frame": available,
                "frame_why": "" if available else "T31 的帧通道没落地（合成）",
                "subject_terms_in_schema": False,
                "why": "" if available else "判断模型没配；T31 的帧通道没落地（合成）"}

    rl.capability = lambda: _cap(available=True)
    return mod


async def _run(*, enabled: bool, render_fn: Any, ask: Any = _ask,
               cache: Any = None) -> dict[str, Any]:
    return await rl.run(spec=_SPEC, first=_FIRST, render_fn=render_fn,
                        cache=cache if cache is not None else _Cache(),
                        enabled=enabled, ask=ask)


# ---------------------------------------------------------------------------
# 四支
# ---------------------------------------------------------------------------

async def _case_reviewed() -> None:
    """① 红 → 回看 → 只重烧该镜 → `reviewed`。"""
    async def render_fn(spec: dict[str, Any]) -> dict[str, Any]:
        # 断言「改的是哪一镜」：回看必须把新代码落到**那一镜**上（不是别处、不是整片）。
        say(spec["shots"][0].get("code") == "A2" and spec["shots"][1].get("code") == "B",
            "①回看把新代码落在了 s01，s02 一个字节没动")
        return {"shots": [_row("s01", "k1b", _PARTS, False), _row("s02", "k2", _PARTS, True)],
                "shot_files": {"s01": {}, "s02": {}}, "clips": [], "hitmaps": {},
                "frames_used": 0}

    out = await _run(enabled=True, render_fn=render_fn)
    rc = out["receipt"]
    s1 = rc["shots"]["s01"]
    say(s1["status"] == "reviewed",
        f"①该镜的出口是 reviewed（实得 {s1['status']}）——「回看过」这句话有产物撑着")
    say(s1["cache_key_before"] == "k1" and s1["cache_key_after"] == "k1b",
        f"①账上打得出**改动前后的两个缓存键**（{s1['cache_key_before']} → "
        f"{s1['cache_key_after']}）")
    say("s02" in rc["absent"] and "s02" not in rc["shots"],
        "①没红的镜不进射程（s02 在缺席名单里，三出口里根本没有它这一栏）")
    say(rc["collateral"] == [], f"①没有牵连别的镜（实得 {rc['collateral']}）")
    say(not out["block"], "①这一支不该阻断")


async def _case_no_second_slice() -> None:
    """①′ 同一支但模型没改出东西 → 账上没有第二次切片 → 必须 `not_run`。"""
    async def render_fn(spec: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("这一支不该重烧：模型没给出新代码")

    out = await _run(enabled=True, render_fn=render_fn, ask=_ask_nothing)
    s1 = out["receipt"]["shots"]["s01"]
    say(s1["status"] == "not_run",
        f"①′没有第二次切片时出口是 not_run（实得 {s1['status']}）——"
        "形容词不算实现（㉖ 的红法原话）")


async def _case_collateral() -> None:
    """② 重烧把别的镜也带上了 → 必须阻断（T56-②）。"""
    async def render_fn(spec: dict[str, Any]) -> dict[str, Any]:
        return {"shots": [_row("s01", "k1b", _PARTS, False), _row("s02", "k2b", _PARTS, False)],
                "shot_files": {"s01": {}, "s02": {}}, "clips": [], "hitmaps": {},
                "frames_used": 0}

    try:
        out = await _run(enabled=True, render_fn=render_fn)
    except rl.ReviewBlocked as exc:
        say("s02" in (exc.receipt or {}).get("collateral", []),
            f"②牵连到的镜在回执里点名（实得 {(exc.receipt or {}).get('collateral')}）")
        say(True, "②重烧牵连别的镜 → 已阻断（「局部改只重烧受影响镜」没被弄破）")
        return
    say(False, f"②重烧牵连别的镜却没有阻断（回执 {out['receipt']['collateral']}）")


async def _case_undetermined() -> None:
    """③ 判据不可用 → `undetermined` **且阻断**（不是 not_run、不是「不需要回看」）。"""
    async def _cap() -> dict[str, Any]:
        return {"gate": "26", "available": False, "judge": False,
                "judge_why": "判断模型没配（合成）", "frame": False,
                "frame_why": "T31 的帧通道没落地（合成）",
                "subject_terms_in_schema": False,
                "why": "判断模型没配（合成）；T31 的帧通道没落地（合成）"}
    rl.capability = _cap

    async def render_fn(spec: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("判据不可用时不许烧任何像素")

    try:
        await _run(enabled=True, render_fn=render_fn)
    except rl.ReviewBlocked as exc:
        rc = exc.receipt or {}
        st = (rc.get("shots") or {}).get("s01", {}).get("status")
        say(st == "undetermined", f"③出口是 undetermined（实得 {st}）——"
                                  "既不是 not_run，也不是「不需要回看」")
        say(rc.get("judge", {}).get("frame") is False and len(rc.get("judge", {})) > 0,
            "③回执里能读出是**哪一半**通道缺的（帧/裁决各一栏）")
        say("s02" in rc.get("absent", []), "③没红的镜照样在缺席名单里")
    else:
        say(False, "③判据不可用却没有阻断（Q113 原文：绝不静默放行）")


async def _case_off() -> None:
    """④ 开关关着 → 红镜记 `not_run`（不是缺席、不是通过）。"""
    async def render_fn(spec: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("开关关着不许烧任何像素")

    out = await _run(enabled=False, render_fn=render_fn)
    rc = out["receipt"]
    say(rc["shots"]["s01"]["status"] == "not_run",
        f"④没开回看时红镜记 not_run（实得 {rc['shots']['s01']['status']}）")
    say("s02" in rc["absent"] and rc["shots"]["s01"]["status"] != "absent",
        "④「该回看没回看」与「不需要回看」在回执里是两栏（not_run vs 缺席）")
    say(not out["block"], "④没开回看只是少一条修复的路，不阻断出片")


async def run(*, as_json: bool = False) -> int:
    print("=== R11 ㉖ 回看环巡检（四支合成回执）===")
    print("为什么是合成回执：三出口在一条真片子上不会同时出现（见本文件头）。")
    print("真机那一半（真取帧、真改代码、真只重烧那一镜）随 §8.1，**记为未跑**。\n")
    _install_fake_channel()
    for fn in (_case_reviewed, _case_no_second_slice, _case_collateral,
               _case_undetermined, _case_off):
        await fn()
        # 每支跑完把探针复原：`_case_undetermined` 会把能力探测换成「不可用」，
        # 不复原的话后一支会在一个假的前提上判绿。
        _install_fake_channel()
        print("")
    if as_json:
        print(json.dumps({"failures": FAILURES}, ensure_ascii=False, indent=2))
    print("结论：" + ("绿（四支的出口都与 T56 的验收同形）" if not FAILURES
                     else f"红（{len(FAILURES)} 条不合预期）"))
    return 1 if FAILURES else 0


def main() -> int:
    # 判词里带「㉖」这类字符：Windows 宿主控制台默认 GBK，直接 print 会 UnicodeEncodeError
    # ——巡检自己炸掉会把「有一条没跑」伪装成「这一条跑过了」（D-未判不绿：没跑过也算未判）。
    # 容器里本来就是 UTF-8，这一句在那里是空操作。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:       # noqa: BLE001 —— 老解释器没有 reconfigure 就算了
        pass
    parser = argparse.ArgumentParser(description="R11 ㉖ 回看环巡检")
    parser.add_argument("--json", action="store_true", help="末尾附机器可读的失败清单")
    args = parser.parse_args()
    return asyncio.run(run(as_json=args.json))


if __name__ == "__main__":
    sys.exit(main())
