"""时长对账的离线验证：真机 run 79dcdc0ec0a8 那份分镜（14 镜、无旁白、目标 240s）。

那条 run 的病：估算不看 `min_duration_sec`，把合计 164s 的片子报成 73~112s，在用户点了
确认之后才在渲染口打回，还建议「加到 46 镜、放慢语速」（无旁白哪来的语速）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storyline_server.motion import code_spec as cs            # noqa: E402
from storyline_server.motion import render as rpipe            # noqa: E402

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    if not cond:
        _fails += 1
    print(f"  {'✓' if cond else '✗'} {label}")


DWELL = [9, 10, 12, 12, 13, 12, 13, 13, 13, 12, 12, 12, 12, 9]
CHARS = [19, 24, 29, 23, 38, 27, 30, 34, 31, 28, 28, 29, 26, 18]


def _spec(narration: bool = False, **over) -> dict:
    shots = [{"id": f"s{i + 1:02d}", "text": "字" * n, "min_duration_sec": m}
             for i, (m, n) in enumerate(zip(DWELL, CHARS))]
    return {"narration": narration, "transition": "none", "shots": shots, **over}


def main() -> int:
    print("\n〔单源〕无旁白算式与渲染层同一组常量")
    check(cs.NO_NARRATION_BASE_SEC == rpipe.DEFAULT_SHOT_SEC,
          f"驻留下限与 render.DEFAULT_SHOT_SEC 同值（{cs.NO_NARRATION_BASE_SEC}）")

    print("\n〔估算〕无旁白时尊重 min_duration_sec，是确定值")
    low, high = cs.estimate_sec(_spec())
    check(low == high == float(sum(DWELL)), f"估成 {low}~{high}s（应为驻留之和 {sum(DWELL)}s）")
    check(cs.duration_block(_spec(), 240) is None, "164s 对 240s 目标在闸值内，不拦")

    print("\n〔对齐〕无旁白由系统把驻留按比例调到目标")
    fitted, notes = cs.fit_duration(_spec(), 240)
    low, high = cs.estimate_sec(fitted)
    # 拉长有 MAX_STRETCH_RATIO 封顶（再拉后半截就是定格，见那个常量的注释）：
    # 目标超出封顶值时，宁可片子短一点、回执里说「要更长就加镜」。
    cap_total = float(sum(DWELL)) * cs.MAX_STRETCH_RATIO
    check(abs(low - min(240.0, cap_total)) < 1.0,
          f"对齐后 {low}s（目标 240s，封顶 {cap_total:.0f}s）")
    secs = [s["min_duration_sec"] for s in fitted["shots"]]
    check(max(secs) <= cs.MAX_DWELL_SEC, f"每镜不超过驻留上限：最长 {max(secs)}s")
    check(bool(notes) and "对齐目标" in notes[0], f"回执里交代了调了什么：{notes[:1]}")
    check(cs.fit_duration(_spec(narration=True), 240)[1] == [], "有旁白时不动驻留（时长归声音）")

    print("\n〔拒因〕改法按有无旁白分开给，不再对无旁白说「放慢语速」")
    no_nar = cs.duration_block(_spec(), 600) or ""
    check("加到约" in no_nar and "语速" not in no_nar, f"无旁白：{no_nar}")
    nar = cs.duration_block(_spec(narration=True), 600) or ""
    check("语速" in nar, f"有旁白：{nar}")
    # 真机 run 7dcaa7246e92：报错只说「加镜 / 放慢语速」，13 个时代被拆成 37 镜。
    # 慢节奏风格的正解是先把画面留长。
    check("min_duration_sec" in nar and nar.index("min_duration_sec") < nar.index("加镜"),
          f"有旁白：先教「写 min_duration_sec 把画面留长」，加镜排在最后：{nar}")

    print("\n〔code_from〕复用别镜代码：展开后与手写一份没有区别")
    raw = {"narration": False, "shots": [
        {"id": "s01", "code": "CODE", "card": "data/a", "derived_from": ["a.tsx"], "text": "一"},
        {"id": "s02", "code_from": "s01", "text": "二"},
        {"id": "s03", "code_from": "s09", "text": "三"}]}
    got = cs.expand_code_refs(raw)["shots"]
    check(got[1]["code"] == "CODE" and got[1]["card"] == "data/a"
          and "code_from" not in got[1], "s02 拿到 s01 的代码与卡名，code_from 不再留着")
    check(not got[2].get("code"), "引用不存在的镜不瞎补，交给校验报「没有代码」")
    check(raw["shots"][1].get("code") is None, "展开不改调用方那一份")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
