# -*- coding: utf-8 -*-
"""`showrunner`（LLM 当导演）的单元测试。

守四件事：
1. **导演给的 plan 只做物理校验**，越界就重问，不替它改值
2. **镜长来自 plan**，不由公式算（这是这次改动的核心）
3. 读数是**证据**不是判据：只有全黑/全白算"没渲出来"
4. 前两镜摘要会进写手的上下文（替代「同一视觉手段连续 ≤2 镜」那条硬判据）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from storyline_server.simple import kernel, showrunner as sr  # noqa: E402

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _FAILS += 1


def shots(*secs: float) -> dict:
    return {"title": "T", "tone": "近黑底", "motif": "蓝点",
            "shots": [{"sec": s, "text": f"第{i + 1}句", "plan": f"第{i + 1}镜画什么"}
                      for i, s in enumerate(secs)]}


def main() -> int:
    print("\n① 物理校验：只判能不能画，不判好不好看")
    check(sr.plan_problems(None, 60) != [], "None 不合格")
    check(sr.plan_problems({"shots": []}, 60) != [], "空 shots 不合格")
    check(sr.plan_problems(shots(2, 2, 2), 6) == [], "2+2+2 秒 / 目标 6 秒 → 合格")
    bad_short = sr.plan_problems({"shots": [{"sec": 0.3, "text": "a", "plan": "p"}]}, 6)
    check(any("镜数" in x or "sec" in x for x in bad_short), f"0.3 秒的闪帧被拒（{bad_short}）")
    over = sr.plan_problems(shots(15, 15, 15), 20)
    check(any("总时长" in x for x in over), f"总时长偏太多被拒（{over}）")
    check(sr.plan_problems(shots(2, 2, 2, 2, 2, 2), 12) == [],
          "6 镜 × 2 秒 = 12 秒 → 合格（镜数由内容定，不套公式）")

    print("\n② 归一化：导演写的 `plan` 要变成写手认的 `plate`")
    got = sr.normalize(shots(4, 5))
    check(got["shots"][0]["plate"] == "第1镜画什么", f"plan → plate（{got['shots'][0]}）")
    check(got["tone"] == "近黑底" and got["motif"] == "蓝点", "tone / motif 保留")
    check(len(got["shots"]) == 2, "两镜都在")

    print("\n③ 外部给的分镜：list 与 dict 都收，且不丢 tone/motif/sources")
    a = sr.adopt([{"sec": 2, "text": "x", "plan": "y"}])
    check(len(a["shots"]) == 1, f"list 形状（{len(a['shots'])} 镜）")
    b = sr.adopt({"title": "旧", "tone": "冷蓝", "motif": "星",
                  "sources": [{"claim": "1", "source": "s"}],
                  "shots": [{"sec": 3, "text": "x", "plan": "y"}]})
    check(b["tone"] == "冷蓝" and b["motif"] == "星", "dict 形状保住画法基调与贯穿物")
    check(len(b["sources"]) == 1, "sources 不丢（出处账要跟着走）")

    print("\n④ 物理判据：只有全黑/全白算「没渲出来」")
    check(sr.physical({"readings": {"mean_lum": 1.0}}) == "black", "1/255 → black")
    check(sr.physical({"readings": {"mean_lum": 252.0}}) == "white", "252/255 → white")
    check(sr.physical({"readings": {"mean_lum": 27.0}}) == "ok",
          "27/255 → ok（偏暗是审美，不打回）")
    check(sr.physical({"readings": {}}) == "unknown", "没读数 → unknown（不当通过）")

    print("\n⑤ 前两镜摘要：写手要看到「前面画过什么」")
    three = sr.normalize(shots(3, 3, 3))["shots"]
    check("没有前作" in sr.prev_summary(three, 0), "第 1 镜：没有前作")
    check("第1镜" in sr.prev_summary(three, 1), "第 2 镜：看得到第 1 镜")
    s3 = sr.prev_summary(three, 2)
    check("第1镜" in s3 and "第2镜" in s3, "第 3 镜：看得到前两镜")
    check("第3镜" not in s3, "不会把自己写进去")

    print("\n⑥ 导演重问：格式不合格时带着问题再问一次")
    calls: list[str] = []
    orig = kernel._complete

    async def fake(messages, **kw):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return "这不是 JSON"
        return '{"title":"T","tone":"冷","shots":[' \
               '{"sec":4,"text":"a","plan":"p"},{"sec":4,"text":"b","plan":"q"},' \
               '{"sec":4,"text":"c","plan":"r"}]}'

    kernel._complete = fake                     # type: ignore[assignment]
    try:
        plan_obj, n = asyncio.run(sr.plan("测试", seconds=12, aspect="portrait"))
    finally:
        kernel._complete = orig                 # type: ignore[assignment]
    check(n == 1 and len(plan_obj["shots"]) == 3, f"第二次拿到合格 plan（{len(plan_obj['shots'])} 镜）")
    check(len(calls) == 2, f"一共问了 2 次（{len(calls)}）")
    check("不合格" in calls[1], "第二次把问题回喂进去了")

    print("\n⑦ 镜长真的来自 plan，不是除出来的")
    check([s["sec"] for s in plan_obj["shots"]] == [4.0, 4.0, 4.0],
          "12 秒 3 镜是导演给的 4/4/4，不是 12÷3 的巧合（值取自 plan）")
    check(sr.MIN_SHOT_SEC == 1.0 and sr.MAX_SHOT_SEC == 15.0,
          f"单镜区间 {sr.MIN_SHOT_SEC}~{sr.MAX_SHOT_SEC} 秒（护栏，不是审美）")

    print("\n⑧ 时长兜底：结构对、只是总长偏了 → 按比例缩，不整条失败")
    # 真机：导演连给三次 193 秒（目标 165），旧代码直接判"3 次都不合格"整条失败。
    # 用户要的是片子，不是一条判决——所以结构合格就按比例救回来，并如实标注。
    over = sr.normalize(shots(12, 10, 14, 11, 9, 8))       # 6 镜共 64 秒
    check(sr._total_sec(over) == 64.0, f"造一份超长的：{sr._total_sec(over)} 秒")
    check(sr.structural_ok(over, 50) is True,
          "结构合格（镜数/单镜时长/text 都对，只有总长不对）")
    scaled = sr.scale_to(over, 50)
    check(abs(sr._total_sec(scaled) - 50) < 0.5,
          f"缩到目标 50 秒（实际 {sr._total_sec(scaled)}）")
    check(scaled.get("scaled_from") == 64.0, f"记下原值 {scaled.get('scaled_from')}")
    # 相对节奏：最长的那一镜缩完还得是最长（第 3 镜，原 14 秒）
    check(max(range(len(scaled["shots"])),
              key=lambda i: scaled["shots"][i]["sec"]) == 2,
          "相对节奏保留（最长的那镜还是同一镜）")
    check(all(sr.MIN_SHOT_SEC <= x["sec"] <= sr.MAX_SHOT_SEC for x in scaled["shots"]),
          "缩完仍在单镜上下限内")
    check(sr.structural_ok({"shots": [{"sec": 30, "text": "a", "plan": "p"}] * 3}, 50) is False,
          "单镜 30 秒 → 结构不合格（救不了，只能重问）")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
