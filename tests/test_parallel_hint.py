# -*- coding: utf-8 -*-
"""「互不依赖的步骤要同轮发出」——执行轮承诺段必须带上并批提示。

用户原话：「为什么不并行了这有的不是可以并行吗」

背景：asr 与 split_shots 都只依赖 load_media、读写集互不冲突，
执行器（tool.plan_batches）会把它们并成一批 gather——**但前提是模型
把它们放在同一次回复里**。真机实测模型分了两轮：先单独调 asr、拿到结果才调
split_shots，于是本可并行的链路退化成一前一后，白等一整段 ASR（227 秒）。

根因：执行轮是另一次 run，规划轮加载的技能不会继承；执行轮能否看到并批说明，
取决于计划卡的 skills_hint 恰好带了那份技能——不可靠。所以把提示钉进
`<approved_plan>` 承诺段（每次执行都带着），并由计划自身的依赖关系算出该并谁。

这条用例钉住：
  ① 真机那份计划只报 asr + split_shots，不把有依赖的算进去；
  ② 提示真的写进了 render_injections 的产物里；
  ③ 单步 / 无依赖信息时不乱报（宁可退化成串行，也不要错报顺序）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.plan.prompt import _parallel_pairs, render_injections  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


REAL_PLAN = {
    "plan_id": "p1", "label": "原声混剪",
    "steps": [
        {"seq": 1, "node": "load_media"},
        {"seq": 2, "node": "asr", "requires": ["load_media"]},
        {"seq": 3, "node": "split_shots", "requires": ["load_media"]},
        {"seq": 4, "node": "understand_clips", "requires": ["split_shots"]},
        {"seq": 5, "node": "filter_clips", "requires": ["understand_clips"]},
        {"seq": 6, "node": "speech_rough_cut", "requires": ["asr"]},
    ],
}


def main() -> int:
    print("=== ① 真机那份计划：只报同层且互不依赖的 ===")
    pairs = _parallel_pairs(REAL_PLAN)
    print(f"    算出：{pairs}")
    check(any("asr" in p and "split_shots" in p for p in pairs),
          "报出了 asr + split_shots")
    check(not any("understand_clips" in p for p in pairs),
          "没有把 understand_clips 算进去（它依赖 split_shots）")
    check(not any("speech_rough_cut" in p for p in pairs),
          "没有把 speech_rough_cut 算进去（它依赖 asr）")

    print("\n=== ② 提示真的写进了 <approved_plan> ===")
    segs = render_injections(REAL_PLAN)
    joined = "\n".join(segs)
    check("可并行" in joined, "承诺段里有「可并行」提示")
    check("同一次回复" in joined, "明确要求放进同一次回复")
    check("asr + split_shots" in joined, "点名了具体哪两步可以同轮")

    print("\n=== ③ 不该报的时候不乱报 ===")
    check(_parallel_pairs({"steps": [{"seq": 1, "node": "load_media"}]}) == [],
          "只有一步 -> 不报")
    check(_parallel_pairs({"steps": []}) == [], "空计划 -> 不报")
    # 一条纯串行链：每一步都依赖前一步
    chain = {"steps": [
        {"seq": 1, "node": "a"},
        {"seq": 2, "node": "b", "requires": ["a"]},
        {"seq": 3, "node": "c", "requires": ["b"]},
    ]}
    check(_parallel_pairs(chain) == [], "纯串行链 -> 不报（不错报顺序）")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
