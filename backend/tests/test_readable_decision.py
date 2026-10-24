# -*- coding: utf-8 -*-
"""弹窗选项的「机器 key 不要露给用户」验证（不联网、不起服务）。

真机实测用户的原话：
    「之前的弹窗按钮点击后显示的那些 d180、key_only 等能不能不要在前端显示？」

根因：前端点选后回传的是选项的 ``key``（``d180``），服务端把它当 ``message``
写进消息链，用户就在对话里看到一串机器名。

这条用例钉两件事：
  ① ``message`` 与 ``answers[].answer`` 被换成人能读的 label；
  ② **``decision`` 保持原样不被换掉**——它是语义键，``decision_is_confirm``
     靠 ``confirm_render`` 判分支，换成中文标签渲染确认就失效了。

运行：  python tests/test_render_gate_readable.py
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import sys  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


def main() -> int:
    from agent_framework.server import readable_decision as f

    check(callable(f), "① 能从 server 模块直接 import readable_decision")

    row = {"approval": {"ask": {
        "title": "成片时长按哪种来？",
        "options": [
            {"key": "d90", "label": "90 秒（推荐）"},
            {"key": "d180", "label": "3 分钟"},
            {"key": "key_only", "label": "只在金句处出镜"},
        ],
    }}}

    print("\n=== ① 机器 key → 可读标签 ===")
    d, msg, ans = f(row, "d180", "d180", [{"key": "d180", "answer": "d180"}])
    check(msg == "3 分钟", f"message 换成标签：{msg!r}")
    check(ans[0]["answer"] == "3 分钟", f"answers[].answer 换成标签：{ans[0]['answer']!r}")
    check(d == "d180", "decision 保持原 key（语义键不能被换）")

    print("\n=== ② 渲染确认键不能被换掉（否则门失效）===")
    row2 = {"approval": {"ask": {"options": [
        {"key": "confirm_render", "label": "就这样，开始渲染"},
        {"key": "adjust_plan", "label": "我还要改"}]}}}
    d2, msg2, _ = f(row2, "confirm_render", "confirm_render", [])
    check(d2 == "confirm_render", f"confirm_render 原样传给 decision：{d2!r}")
    check(msg2 == "就这样，开始渲染", f"message 可读：{msg2!r}")

    print("\n=== ③ 用户自己写的优先，不编造 ===")
    d3, msg3, ans3 = f(row, "d90", "", [{"key": "d90", "custom": "我要 45 秒"}])
    check(ans3[0]["answer"] == "我要 45 秒", f"custom 优先：{ans3[0]['answer']!r}")
    check(msg3 == "我要 45 秒", f"message 用 custom：{msg3!r}")

    print("\n=== ④ 换不出来时保留原文（不编造）===")
    d4, msg4, ans4 = f(row, "whatever", "whatever", [{"key": "whatever",
                                                     "answer": "whatever"}])
    check(msg4 == "whatever", f"无对应 label 时保留原文：{msg4!r}")
    check(ans4[0]["answer"] == "whatever", "answers 也保留原文")

    print("\n=== ⑤ 普通 approve/reject 不受影响 ===")
    d5, msg5, _ = f(row, "approve", "approve", [])
    check(d5 == "approve" and msg5 == "approve", "approve 原样")

    print()
    print("全部通过" if not _fails else f"有 {_fails} 项未通过")
    print(f"用例 {_checks} 条")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
