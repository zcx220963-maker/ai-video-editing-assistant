# -*- coding: utf-8 -*-
"""「答案必须属于当前这道题」的守口验证（不联网、不起服务）。

真机事故（模型自己都发现了，原话「我这边一直只收到「治愈系慢节奏」这一句，
时长选项始终没被选中」）——实际操作序列是：

    seq=4  提问：这条 10 秒的兔子素材，你想剪成哪种风格？   options: healing_* / ...
    seq=5  用户答：治愈系慢节奏                            ← 对
    seq=6  提问：成片时长想控制在多少？                    options: full_10s / short_6s / ...
    seq=7  用户答：治愈系慢节奏                            ← 错！风格题的 key 答了时长题
    seq=8  提问：成片时长想控制在多少？      ← 同一道题被反复弹
    seq=9  用户答：治愈系慢节奏               ← 还是错
    ...
    模型：我这边一直只收到「治愈系慢节奏」这一句

服务端原先照单全收 → run 原地打转、用户看到「没选完就继续」「做完没反应」。

这条用例钉住：陈旧 key 一律 409，正当答案/自由文本/通用审批放行。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from fastapi import HTTPException  # noqa: E402

from agent_framework.server import reject_stale_answer  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def rejected(row, decision, answers=()) -> bool:
    try:
        reject_stale_answer(row, decision, list(answers))
        return False
    except HTTPException:
        return True


# 时长题的挂起态（真机 seq=6 的形状）
ROW_DURATION = {"approval": {"ask": {
    "title": "成片时长想控制在多少？（素材总共只有 10 秒）",
    "options": [
        {"key": "full_10s", "label": "就用完整 10 秒"},
        {"key": "short_6s", "label": "剪到 6 秒左右"},
        {"key": "short_4s", "label": "剪到 4 秒左右"},
    ]}}}


def main() -> int:
    print("=== ① 真机事故：拿风格题的 key 答时长题 → 必须拒 ===")
    check(rejected(ROW_DURATION, "healing_voiceover"),
          "陈旧 key「healing_voiceover」被拒（这正是原地打转的成因）")
    check(rejected(ROW_DURATION, "healing_voiceover",
                   [{"key": "healing_voiceover", "answer": "治愈系慢节奏"}]),
          "陈旧 key 出现在 answers 里也被拒")

    print("\n=== ② 正当答案放行 ===")
    check(not rejected(ROW_DURATION, "full_10s"),
          "本页选项 key「full_10s」放行")
    check(not rejected(ROW_DURATION, "full_10s",
                       [{"key": "full_10s", "answer": "就用完整 10 秒"}]),
          "带 answers 的正当答案放行")

    print("\n=== ③ 用户自己写的自由文本永远放行 ===")
    check(not rejected(ROW_DURATION, "剪成 8 秒吧",
                       [{"key": "other", "answer": "剪成 8 秒吧", "custom": "剪成 8 秒吧"}]),
          "custom 有值时放行（那是人打的字，不可能是陈旧 key）")

    print("\n=== ④ 通用审批门（approve/reject）不受影响 ===")
    check(not rejected(ROW_DURATION, "approve"), "approve 放行")
    check(not rejected(ROW_DURATION, "reject"), "reject 放行")
    check(not rejected(ROW_DURATION, ""), "空 decision 放行（走原逻辑）")

    print("\n=== ⑤ 没有 ask / 没有 options 时不猜、不拦 ===")
    check(not rejected({}, "whatever"), "无 approval 时放行")
    check(not rejected({"approval": {}}, "whatever"), "无 ask 时放行")
    check(not rejected({"approval": {"ask": {"title": "t", "options": []}}}, "whatever"),
          "options 为空时放行")
    # 渲染确认门：options 是 confirm_render / adjust_plan
    row_render = {"approval": {"ask": {"options": [
        {"key": "confirm_render", "label": "就这样，开始渲染"},
        {"key": "adjust_plan", "label": "我还要改"}]}}}
    check(not rejected(row_render, "confirm_render"), "渲染确认的 key 放行")
    check(rejected(row_render, "full_10s"), "时长题的 key 答渲染门 → 被拒")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
