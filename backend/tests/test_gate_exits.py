# -*- coding: utf-8 -*-
"""闸的出口静态检查（T0.4）：**故意删掉一个消费点，这里必须红**。

为什么有这个文件（2026-10-24 立的账）：

`whole_film_gate_results` 返回 `(打回, 提醒, 未判)`。2026-10-16 并档那天，
闸⑦ 的提醒与闸⑩ 的未判档**跟着元组一起被裁掉了**——`warns.extend(check_draw_floor(...)[1])`
那两行不在了。后果不是报错，是**回执永远显示「每一档都真量过」**：

· `style_warnings` 恒为空数组（「文案具体项低于 40%」这条线没人看得见）；
· `style_unjudged` 恒为空数组（「本片这一档压根没量过」被写成了「量过且通过」）。

**藏了至少三个月，147 个测试文件里没有一个会红。** 因为那些测试问的是「闸判得对不对」，
没有人问「闸判完之后，那个答案有没有人读」。

这个文件问的就是后者。判据落在**源码**上（`code_spec.audit_gate_exits`）：
闸的返回值只有两条出路——被下标取走（`gate(...)[1]`）或被解包（`a, b = gate(...)`）。
一处都没有 = 那个出口丢了。

四类断言：

1. 今天的代码是干净的（`audit_gate_exits() == []`）；
2. **删掉任一消费点 → 报出来**（三种删法各一条：直接下标 / 赋值后下标 / 元组通用循环）；
3. 新加一道多出口闸却不登记 → 报出来（不许「悄悄多一道闸、悄悄少一个出口」）；
4. **行为层**：那两条提醒/未判真的走到了 `whole_film_gate_results` 的返回里
   ——静态检查证明「有人读」，这一条证明「读到的东西没被中途吞掉」。

跑法：`python tests/test_gate_exits.py`（selfcheck 逐文件跑的就是这一条）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from open_storyline.motion import code_spec as cs   # noqa: E402

_checks = 0
_fails = 0

SOURCE_PATH = Path(cs.__file__).resolve()
SOURCE = SOURCE_PATH.read_text("utf-8")

#: 故意「删掉一行消费点」用的三处原文。写成常量是为了**先断言它真的存在**——
#: 哪天有人把这几行改个写法，测试要在这里红，而不是在下面的 `replace` 里静默失效。
DROP_DRAW_FLOOR = "    warns.extend(check_draw_floor(shots)[1])\n"
DROP_INK_UNDET = "        undetermined.extend(ink[2])\n"
DROP_LOOP_UNDET = "            undetermined.extend(got[2])\n"


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def _drop(source: str, needle: str, *, replace_with: str = "") -> str:
    """从源码里删掉一处消费点；**先确认它存在**，否则这次「删除」是个空操作。"""
    if needle not in source:
        raise AssertionError(
            f"夹具失效：源码里找不到这段消费点\n{needle!r}\n"
            f"——它被改写了。请同步更新 DROP_* 常量，否则这个测试会假装在测。")
    return source.replace(needle, replace_with, 1)


def _hollow_shots(n: int = 4) -> list[dict]:
    """一组「画了东西、但文案一个具体项都没有」的镜 → 闸⑦ 一定出提醒。

    `check_draw_floor` 的提醒判据是「带具体项的镜占比 < 40%」，而具体项只认数字、
    引号术语、拉丁缩写。所以这几镜既画了东西（不触发打回），又全部落在提醒里。
    """
    code = '<svg viewBox="0 0 100 100"><path d="M0 0 L100 100" /></svg>'
    return [{"id": f"s{i + 1:02d}", "text": "一样东西在动", "code": code}
            for i in range(n)]


def main() -> int:
    print("== ① 今天的代码是干净的 ==")
    problems = cs.audit_gate_exits()
    check(problems == [], f"audit_gate_exits() 无问题（实得 {problems}）")
    check(SOURCE == SOURCE_PATH.read_text("utf-8"),
          "审计读的是本文件的真实源码（不是另一份副本）")

    print("\n== ② 删掉一个消费点 → 必须报出来 ==")
    # ⑦ 的提醒：直接下标写法
    gone = _drop(SOURCE, DROP_DRAW_FLOOR)
    got = cs.audit_gate_exits(source=gone)
    check(any("check_draw_floor" in p and "warns" in p for p in got),
          f"删掉 `warns.extend(check_draw_floor(shots)[1])` → 报 ⑦ 的 warns 没人读（实得 {len(got)} 条）")
    # ⑩ 的未判：赋值后下标写法
    gone = _drop(SOURCE, DROP_INK_UNDET)
    got = cs.audit_gate_exits(source=gone)
    check(any("check_ink_contrast" in p and "undetermined" in p for p in got),
          "删掉 `undetermined.extend(ink[2])` → 报 ⑩ 的 undetermined 没人读")
    # 元组通用循环：影响元组里每一道三态闸
    gone = _drop(SOURCE, DROP_LOOP_UNDET)
    got = cs.audit_gate_exits(source=gone)
    check(any("check_caption_ownership" in p and "undetermined" in p for p in got),
          "删掉循环里的 `undetermined.extend(got[2])` → 报 ㉜ 的 undetermined 没人读")

    print("\n== ③ 新加一道多出口闸不登记 → 必须报出来 ==")

    def check_fake_gate(shots):
        """临时闸：两出口、不进任何注册表。"""
        return [], []

    # 注解要显式给：`gate_arity` 读的是返回注解（注解是契约，实测会随输入变）。
    check_fake_gate.__annotations__ = {"return": "tuple[list[str], list[str]]"}
    check_fake_gate.__module__ = cs.__name__
    cs.__dict__["check_fake_gate"] = check_fake_gate
    try:
        got = cs.audit_gate_exits()
    finally:
        cs.__dict__.pop("check_fake_gate", None)
    check(any("check_fake_gate" in p for p in got),
          f"未登记的 check_fake_gate 被点名（实得 {got[:1]}）")
    check(not any("check_fake_gate" in p for p in cs.audit_gate_exits()),
          "摘掉之后又干净了（这条检查不是靠常驻副作用通过的）")

    print("\n== ④ 出口数量与声明对得上 ==")
    arities = {g.__name__: cs.gate_arity(g) for g in cs.WHOLE_FILM_GATES}
    check(arities == {"check_subject_named": 2, "check_caption_ownership": 3},
          f"元组里两道闸的出口数（实得 {arities}）")
    check(cs.gate_arity(cs.check_draw_floor) == 2, "⑦ 是两态闸（打回/提醒）")
    check(cs.gate_arity(cs.check_ink_contrast) == 3, "⑩ 是三态闸（打回/提醒/未判）")
    check(all(0 <= cs._exit_index(cs.check_draw_floor, x) < 3
              for x in cs.GATE_EXIT_CONSUMERS[cs.check_draw_floor]),
          "⑦ 声明的出口名都能定位到位置")

    print("\n== ⑤ 行为层：那两条读数真的走到了返回值里 ==")
    shots = _hollow_shots()
    errors, warns, undetermined = cs.whole_film_gate_results(shots)
    check(any("具体项" in w for w in warns),
          f"闸⑦ 的「文案具体项」提醒出现在 warns 里（warns={len(warns)} 条）")
    check(any("未判" in u and "对比度" in u for u in undetermined),
          f"闸⑩ 的「对比度未判」出现在 undetermined 里（undetermined={len(undetermined)} 条）")
    check(isinstance(errors, list) and isinstance(warns, list)
          and isinstance(undetermined, list),
          "三个出口都是 list（形状是契约的一部分）")

    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
