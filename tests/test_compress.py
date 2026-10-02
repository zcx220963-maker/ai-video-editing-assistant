"""上下文压缩验证（不联网）：预算内不动 / 第一级折叠 / 第二三级丢弃+摘要。

运行：  python tests/test_compress.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.compress import ContextCompressor, default_token_counter
from agent_framework.context import ContextBuilder
from agent_framework.messages import ToolCall, assistant, system, tool_result, user
from agent_framework.session import Session

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


def big(n: int) -> str:
    return "x" * n


async def main() -> None:
    # ---- 预算内：原样返回 ----
    small = [system("S"), user("hi"), assistant("yo"), user("again")]
    c = ContextCompressor(max_tokens=10_000)
    out = await c(small, "again")
    check(out == small, "预算内不压缩（原样返回）")

    # ---- 第一级：折叠旧工具结果 ----
    # 多条超长工具结果；预算刚好容纳折叠后的量、但容不下全部原文
    msgs = [system("S")]
    for i in range(6):
        msgs.append(user(f"问{i}"))
        msgs.append(assistant(f"调用工具{i}", [ToolCall(id=f"c{i}", name="t", arguments={})]))
        msgs.append(tool_result(f"c{i}", "t", big(4000)))
    msgs.append(user("最后的问题"))
    # 折叠前 ~6207、折叠后 ~2287：取 2400 让第一级折叠即可满足预算、无需触发丢弃
    fold_only = ContextCompressor(max_tokens=2400, keep_recent_tool_results=2)
    out = await fold_only(msgs, "最后的问题")
    folded = [m for m in out if m.get("role") == "tool" and "已折叠工具结果" in str(m.get("content"))]
    full_tool = [m for m in out if m.get("role") == "tool" and "已折叠工具结果" not in str(m.get("content"))]
    check(len(folded) >= 4, f"第一级折叠了旧工具结果: {len(folded)} 条被折叠")
    check(len(full_tool) <= 2, f"最近 {len(full_tool)} 条工具结果保持完整(<=keep_recent)")
    check(fold_only.estimate(out) <= fold_only.max_tokens
          and not any("<history_summary>" in str(m.get("content")) for m in out),
          "第一级折叠即满足预算（未触发丢弃/摘要）")
    check(out[-1]["content"] == "最后的问题" and out[0]["role"] == "system", "保留 system 与当前输入")

    # ---- 第二/三级：折叠仍超预算 → 丢弃过旧并摘要 ----
    spy_calls: list[int] = []

    async def spy_summarizer(dropped):
        spy_calls.append(len(dropped))
        return f"摘要了{len(dropped)}条"

    long_hist = [system("S")]
    for i in range(10):
        long_hist.append(user(f"历史请求{i} " + big(600)))
        long_hist.append(assistant(f"历史回复{i} " + big(600)))
    long_hist.append(user("当前问题"))

    c2 = ContextCompressor(max_tokens=400, keep_recent_tool_results=2, summarizer=spy_summarizer)
    out2 = await c2(long_hist, "当前问题")
    check(c2.estimate(out2) <= c2.max_tokens or len(out2) < len(long_hist), "压缩后规模显著下降")
    check(spy_summarizer and len(spy_calls) == 1, f"摘要器被调用一次: {spy_calls}")
    check(any("<history_summary>" in str(m.get("content")) and "摘要了" in str(m.get("content")) for m in out2),
          "插入摘要消息")
    # 摘要应紧跟 system 之后
    check(out2[0]["role"] == "system" and "<history_summary>" in str(out2[1].get("content")),
          "摘要位于 system 之后、保留消息之前")
    check(out2[-1]["content"] == "当前问题", "当前输入始终保留")
    check(out2[-2]["role"] in ("user", "assistant"), "保留了近期的历史消息")

    # ---- 自定义 token 计数器接缝 ----
    c3 = ContextCompressor(max_tokens=3, token_counter=lambda m: 10, summarizer=spy_summarizer)
    out3 = await c3([system("S"), user("a"), user("b"), user("c")], "c")
    check(c3.estimate(out3) <= c3.estimate([system("S"), user("a"), user("b"), user("c")]),
          "自定义计数器生效并触发压缩")

    # ---- 集成到 ContextBuilder ----
    builder = ContextBuilder(
        "S", runtime_context=None,
        compressor=ContextCompressor(max_tokens=200, summarizer=spy_summarizer),
    )
    sess = Session(user_id="u", conversation_id="c")
    for i in range(8):
        sess.add(user(f"旧{i} " + big(500)))
        sess.add(assistant(f"答{i} " + big(500)))
    built = await builder.build(sess, "新提问")
    check(any("<history_summary>" in str(m.get("content")) for m in built), "ContextBuilder 经 compressor 产出摘要")
    check(built[-1]["content"] == "新提问", "集成后当前输入仍在末尾")

    # ---- 孤儿 tool_calls / tool 消息修复 ----
    from agent_framework.compress import _repair_orphans

    # 场景1：assistant 带 tool_calls 但对应 tool 消息被淘汰 → 移除 tool_calls
    orphan_assistant = [
        system("S"),
        user("问"),
        assistant("我来调用工具", [ToolCall(id="c0", name="t", arguments={})]),
        user("继续"),
    ]
    repaired = _repair_orphans(orphan_assistant)
    asst_msgs = [m for m in repaired if m.get("role") == "assistant"]
    check(len(asst_msgs) == 1 and not asst_msgs[0].get("tool_calls"),
          "孤儿 tool_calls 被移除，assistant content 保留")

    # 场景2：tool 消息但对应 assistant tool_calls 被淘汰 → 移除 tool 消息
    orphan_tool = [
        system("S"),
        user("问"),
        tool_result("c9", "t", "结果"),
        user("继续"),
    ]
    repaired2 = _repair_orphans(orphan_tool)
    check(not any(m.get("role") == "tool" for m in repaired2),
          "孤儿 tool 消息被移除")

    # 场景3：正常配对不受影响
    normal = [
        system("S"),
        user("问"),
        assistant("调用", [ToolCall(id="c1", name="t", arguments={})]),
        tool_result("c1", "t", "结果"),
        user("继续"),
    ]
    repaired3 = _repair_orphans(normal)
    check(repaired3 == normal, "正常 tool_calls/tool 配对不受影响")

    # 场景4：assistant 有两个 tool_call，一个有结果一个没有 → 只保留有结果的
    mixed = [
        system("S"),
        user("问"),
        assistant("调用两个", [
            ToolCall(id="c_keep", name="t", arguments={}),
            ToolCall(id="c_orphan", name="t", arguments={}),
        ]),
        tool_result("c_keep", "t", "结果"),
        user("继续"),
    ]
    repaired4 = _repair_orphans(mixed)
    asst4 = [m for m in repaired4 if m.get("role") == "assistant"]
    check(len(asst4) == 1 and len(asst4[0].get("tool_calls", [])) == 1
          and asst4[0]["tool_calls"][0]["id"] == "c_keep",
          "混合场景：只保留有对应结果的 tool_call")

    # 场景5：assistant 无 content 且 tool_calls 全孤儿 → 整条丢弃
    empty_assistant = [
        system("S"),
        user("问"),
        assistant(None, [ToolCall(id="c_x", name="t", arguments={})]),
        user("继续"),
    ]
    repaired5 = _repair_orphans(empty_assistant)
    check(not any(m.get("role") == "assistant" for m in repaired5),
          "无 content 且 tool_calls 全孤儿 → 整条丢弃")

    # 场景6：压缩器端到端——触发丢弃后不产生孤儿
    big_with_tools = [system("S")]
    for i in range(10):
        big_with_tools.append(user(f"问{i} " + big(500)))
        big_with_tools.append(assistant(f"答{i} " + big(500), [ToolCall(id=f"tc{i}", name="t", arguments={})]))
        big_with_tools.append(tool_result(f"tc{i}", "t", big(500)))
    big_with_tools.append(user("当前问题"))
    c6 = ContextCompressor(max_tokens=600, summarizer=spy_summarizer)
    out6 = await c6(big_with_tools, "当前问题")
    # 验证不产生孤儿：每个 assistant tool_calls 都有对应 tool 消息
    tool_ids_in_out = {m.get("tool_call_id") for m in out6 if m.get("role") == "tool"}
    for m in out6:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                check(tc["id"] in tool_ids_in_out, f"端到端无孤儿 tool_calls: {tc['id']}")
    # 验证每个 tool 消息都有对应 assistant
    asst_ids_in_out = set()
    for m in out6:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                asst_ids_in_out.add(tc["id"])
    for m in out6:
        if m.get("role") == "tool":
            check(m["tool_call_id"] in asst_ids_in_out, f"端到端无孤儿 tool 消息: {m['tool_call_id']}")

    # 场景7：system 消息夹在 assistant(tool_calls) 和 tool 结果之间 → 重排
    interleaved = [
        system("S"),
        user("问"),
        assistant("调用工具", [ToolCall(id="c_mid", name="t", arguments={})]),
        system("工具失败提示"),
        tool_result("c_mid", "t", "结果"),
        user("继续"),
    ]
    repaired7 = _repair_orphans(interleaved)
    # 找到 assistant 的位置，其后必须紧跟 tool 消息
    asst_idx = next(i for i, m in enumerate(repaired7) if m.get("role") == "assistant")
    check(repaired7[asst_idx + 1].get("role") == "tool"
          and repaired7[asst_idx + 1]["tool_call_id"] == "c_mid",
          "system 夹在中间 → 重排：tool 紧跟 assistant")
    check(repaired7[asst_idx + 2].get("role") == "system",
          "重排后 system 消息移到 tool 之后")

    # 场景8：多个非 tool 消息夹在中间 + 多个 tool_call
    multi_interleaved = [
        system("S"),
        user("问"),
        assistant("调用两个", [
            ToolCall(id="c_a", name="t", arguments={}),
            ToolCall(id="c_b", name="t", arguments={}),
        ]),
        system("提示1"),
        tool_result("c_a", "t", "结果A"),
        user("插话"),
        tool_result("c_b", "t", "结果B"),
        user("继续"),
    ]
    repaired8 = _repair_orphans(multi_interleaved)
    asst8_idx = next(i for i, m in enumerate(repaired8) if m.get("role") == "assistant")
    check(repaired8[asst8_idx + 1].get("role") == "tool"
          and repaired8[asst8_idx + 2].get("role") == "tool",
          "多 tool_call：重排后两个 tool 消息紧跟 assistant")
    check(repaired8[asst8_idx + 3].get("role") in ("system", "user"),
          "重排后非 tool 消息排在两个 tool 之后")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
