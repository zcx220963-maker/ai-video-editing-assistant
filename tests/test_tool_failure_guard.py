# -*- coding: utf-8 -*-
"""工具连续失败守卫的**消息序**离线验证（不联网、不起服务）。

浏览器实测炸过一次：守卫的 system 提示当时拼在 ``assistant(tool_calls=…)`` 与它的
逐条 ``tool`` 回执**之间**，DeepSeek 按 OpenAI 口径直接 400
「An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'」，整条 run 置 failed。守卫本身是有用的
（别无限重试同一个坏工具），坏的是它插错了位置。

这里钉的是**链子形状**而不是守卫措辞：
① 任何带 tool_calls 的 assistant 之后，紧跟逐条回执、数量与 id 一一对应；
② 守卫提示排在本批全部回执之后，且同一批里同一个工具只念一次；
③ 提示里写的是判定那一刻的失败次数（同批后续成功把计数清零也不影响它）；
④ 失败一次就成功的工具不被念（计数按工具各算各的）。

运行：  PYTHONPATH=. python tests/test_tool_failure_guard.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun        # noqa: E402
from agent_framework.context import ContextBuilder                  # noqa: E402
from agent_framework.hooks import CompositeHook                     # noqa: E402
from agent_framework.llm import ScriptedLLM                         # noqa: E402
from agent_framework.messages import Message                        # noqa: E402
from agent_framework.session import Session                         # noqa: E402
from agent_framework.tool import Tool, ToolError, ToolRegistry      # noqa: E402

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


class FlakyTool(Tool):
    """按脚本依次成败的替身：True=成功，False=抛 ToolError（registry 的回喂路径）。"""

    def __init__(self, name: str, outcomes: list[bool]) -> None:
        self._name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "按脚本成败的替身工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        i = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        if not self.outcomes[i]:
            raise ToolError(self._name, f"boom #{self.calls}")
        return f"ok #{self.calls}"


class BoomTool(Tool):
    """execute 里抛未定性异常：走 Registry 之外那层兜底，同样回喂成 ToolError。"""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "总是崩的替身工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        raise RuntimeError("素材文件不存在")


class ScriptedFailureTool(Tool):
    """按脚本决定这次失败算不算「失败」：None=成功，True=真失败，False=反馈类错误。

    ``counts_as_failure=False`` 是 ``ToolError`` 的另一半语义：这条错误本身就是可执行
    的反馈（如 ``submit_plan`` 的校验打回），模型读着清单改输入再交一次就是正路。
    """

    def __init__(self, name: str, script: list[bool | None]) -> None:
        self._name = name
        self.script = list(script)
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "按脚本区分失败性质的替身工具"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        outcome = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if outcome is None:
            return f"ok #{self.calls}"
        if outcome:
            raise ToolError(self._name, f"真跑坏了 #{self.calls}")
        raise ToolError(self._name, f"清单在此，改好再交 #{self.calls}",
                        counts_as_failure=False)


def build_agent(steps: list[Any], *, tools: list[Tool]) -> AgentOnceRun:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return AgentOnceRun(
        ScriptedLLM(steps), reg,
        context_builder=ContextBuilder("BASE"),
        hooks=CompositeHook([]),
        config=AgentConfig(max_iterations=8),
    )


def chain_violations(messages: list[Message]) -> list[str]:
    """按 OpenAI 口径走一遍链子：返回所有「tool_calls 没有紧跟逐条回执」的位置描述。"""
    bad: list[str] = []
    for i, m in enumerate(messages):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        want = [c["id"] for c in m["tool_calls"]]
        for k, cid in enumerate(want):
            j = i + 1 + k
            nxt = messages[j] if j < len(messages) else None
            if nxt is None:
                bad.append(f"#{i}：回执缺 {cid}（链子到底）")
            elif nxt.get("role") != "tool" or nxt.get("tool_call_id") != cid:
                bad.append(f"#{i}：第 {k + 1} 条回执应是 {cid}，实为 "
                           f"{nxt.get('role')}/{nxt.get('tool_call_id')}")
    return bad


def guards(messages: list[Message]) -> list[tuple[int, str]]:
    return [(i, str(m.get("content") or "")) for i, m in enumerate(messages)
            if m.get("role") == "system" and "连续失败" in str(m.get("content") or "")]


async def case_a_order_after_failure() -> None:
    print("\n=== ①② 守卫排在整批回执之后，链子按 OpenAI 口径收口 ===")
    boom = BoomTool("load_media")
    agent = build_agent(
        [("tool", "load_media", {}), ("tool", "load_media", {}), ("answer", "说明失败")],
        tools=[boom])
    out = await agent.run(Session(user_id="u", conversation_id="c_guard_a"), "跑一下")
    last: list[Message] = agent.llm.calls[-1]

    check(out == "说明失败", f"① run 照常收尾（不因守卫炸链子）：{out}")
    check(boom.calls == 2, f"① 模型确实重试了一次：{boom.calls} 次调用")
    check(chain_violations(last) == [],
          f"① 每条 assistant(tool_calls) 都紧跟逐条回执：{chain_violations(last)}")
    g = guards(last)
    check(len(g) == 1 and "load_media" in g[0][1],
          f"② 第二次失败后确实念了守卫：{len(g)} 条")
    first_call = next(i for i, m in enumerate(last) if m.get("tool_calls"))
    check(last[first_call + 1].get("role") == "tool",
          f"② assistant(tool_calls) 之后第一位就是回执："
          f"{last[first_call + 1].get('role')}")
    if g:
        tool_before = [i for i, m in enumerate(last)
                       if m.get("role") == "tool" and i < g[0][0]]
        check(len(tool_before) == 2,
              f"② 守卫之前两条回执都在（{tool_before}），提示排在整批之后")


async def case_b_same_batch_two_calls() -> None:
    print("\n=== ②③ 同一批里同名多次：守卫只念一次、次数取判定那一刻的值 ===")
    flaky = FlakyTool("split_shots", [False, False, True])

    class _Two(ScriptedLLM):
        async def complete(self, messages, tools=None):
            r = await super().complete(messages, tools)
            if r.tool_calls and len(r.tool_calls) == 1:
                from agent_framework.messages import ToolCall
                r = type(r)(content=r.content, tool_calls=[
                    ToolCall(id="call_a", name="split_shots", arguments={}),
                    ToolCall(id="call_b", name="split_shots", arguments={})])
            return r

    reg = ToolRegistry()
    reg.register(flaky)
    agent = AgentOnceRun(
        _Two([("tool", "split_shots", {}), ("answer", "好了")]), reg,
        context_builder=ContextBuilder("BASE"), hooks=CompositeHook([]),
        config=AgentConfig(max_iterations=6))
    await agent.run(Session(user_id="u", conversation_id="c_guard_b"), "跑一下")
    last = agent.llm.calls[-1]

    check(chain_violations(last) == [],
          f"② 同批两次失败也没有打断回执链：{chain_violations(last)}")
    ids = [m.get("tool_call_id") for m in last if m.get("role") == "tool"]
    check(ids == ["call_a", "call_b"], f"③ 两条回执按声明 id 逐一对应：{ids}")
    g = guards(last)
    check(len(g) == 1, f"② 同一个工具一批只念一次：{len(g)} 条守卫")
    check(bool(g) and "连续失败 2 次" in g[0][1],
          f"③ 次数取判定那一刻（后续成功不清零它）：{(g[0][1] if g else '')[:40]}")


async def case_c_no_guard_before_two() -> None:
    print("\n=== ④ 失败一次就成功：不念守卫；计数按工具各算各的 ===")
    ok_after = FlakyTool("asr", [False, True])
    other = FlakyTool("render_video", [False, True])
    agent = build_agent(
        [("tool", "asr", {}), ("tool", "asr", {}),
         ("tool", "render_video", {}), ("tool", "render_video", {}),
         ("answer", "完成")],
        tools=[ok_after, other])
    await agent.run(Session(user_id="u", conversation_id="c_guard_c"), "跑一下")
    last = agent.llm.calls[-1]
    check(guards(last) == [],
          f"④ 各失败一次（不同工具）不该念守卫：{guards(last)}")
    check(chain_violations(last) == [],
          f"④ 链子始终收口：{chain_violations(last)}")


async def case_d_hard_stop() -> None:
    print("\n=== ⑤ 连续失败到上限：不再真打工具（猜键名式死循环的兜底） ===")
    boom = BoomTool("read_node_artifact")
    agent = build_agent(
        [("tool", "read_node_artifact", {})] * 6 + [("answer", "说明卡在哪")],
        tools=[boom])
    agent.config.max_iterations = 10
    out = await agent.run(Session(user_id="u", conversation_id="c_guard_d"), "跑一下")
    last: list[Message] = agent.llm.calls[-1]

    from agent_framework.agent import _MAX_CONSECUTIVE_FAILURES
    check(boom.calls == _MAX_CONSECUTIVE_FAILURES,
          f"⑤ 只真打了 {boom.calls} 次，第 {boom.calls + 1} 次起被拦在门外"
          f"（阈值 {_MAX_CONSECUTIVE_FAILURES}）")
    check(out == "说明卡在哪", f"⑤ run 仍正常收尾：{out}")
    check(chain_violations(last) == [],
          f"⑤ 拒绝回执也按 tool_call_id 逐条对上：{chain_violations(last)}")
    refused = [str(m.get("content")) for m in last
               if m.get("role") == "tool" and "没有再执行" in str(m.get("content") or "")]
    check(len(refused) == 6 - _MAX_CONSECUTIVE_FAILURES,
          f"⑤ 后续每轮都拿到「这次没有再执行」而不是又一次真失败：{len(refused)} 条")
    check(bool(refused) and "submit_plan" in refused[0],
          "⑤ 拒绝话里给出路（换参数 / 说明卡点 / 规划轮直接交卡）")


async def case_e_feedback_is_not_a_failure() -> None:
    print("\n=== ⑥ 反馈类错误不计入失败：模型照着清单改，不会被守卫打断 ===")
    tool = ScriptedFailureTool("submit_plan", [False])   # 每次都只是「打回重写」
    agent = build_agent(
        [("tool", "submit_plan", {})] * 6 + [("answer", "按清单改好再交一次")],
        tools=[tool])
    out = await agent.run(Session(user_id="u", conversation_id="c_guard_e"), "来一版计划")
    last = agent.llm.calls[-1]

    check(tool.calls == 6,
          f"⑥ 六次都真打到工具上（一次没被守卫拦在门外）：{tool.calls}")
    check(guards(last) == [], f"⑥ 一条守卫提示都没有：{guards(last)}")
    check(chain_violations(last) == [], f"⑥ 链子照常收口：{chain_violations(last)}")
    check(out == "按清单改好再交一次", f"⑥ run 正常收尾：{out}")


async def case_f_feedback_neither_adds_nor_clears() -> None:
    print("\n=== ⑦ 反馈类错误既不计数也不清零：夹在中间的真失败照数 ===")
    # 第一次真失败、第二次反馈、随后三次真失败。反馈那次既不该被数成一次失败
    # （否则念的是 3/4/5），也不该把计数清零（否则第一次真失败作废、只念 2/3）。
    tool = ScriptedFailureTool("asr", [True, False, True, True, True])
    agent = build_agent([("tool", "asr", {})] * 5 + [("answer", "说明卡在哪")],
                        tools=[tool])
    await agent.run(Session(user_id="u", conversation_id="c_guard_f"), "跑一下")
    last = agent.llm.calls[-1]

    check(tool.calls == 5, f"⑦ 阈值 4 之前都不拦：真打了 {tool.calls} 次")
    g = guards(last)
    counted = [int(re.search(r"连续失败 (\d+) 次", text).group(1)) for _, text in g]
    check(counted == [2, 3, 4],
          f"⑦ 真失败的计数如实累加（2→3→4），反馈那次不在其中：{counted}")


async def main() -> int:
    await case_a_order_after_failure()
    await case_b_same_batch_two_calls()
    await case_c_no_guard_before_two()
    await case_d_hard_stop()
    await case_e_feedback_is_not_a_failure()
    await case_f_feedback_neither_adds_nor_clears()
    print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
    print(f"用例 {CHECKS} 条", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
