# -*- coding: utf-8 -*-
"""渲染确认门：用户选「要改」时，挂起的 render_video 绝不能冒充已执行。

真机事故（用户日志原文）：
    seq=18 [assistant] calls=['render_video id=call_00_5y9…']   ← 模型要渲染
    seq=19 [tool] tool_call_id=call_00_5y9…
           和音乐长度一致或者比音乐短一点,保证句子不被从中间截断      ← 用户的原话！
    seq=20 [assistant] 渲染已提交，正在跑。                        ← 模型被骗了
    seq=20 [tool] {"render": {"status": "none"},
                   "hint": "这个作用域还没有 render_video 任务"}
    seq=22 [tool] 和音乐长度一致或者比音乐短一点…                   ← 又一次
    模型：「render_video 连续两次返回的都是那句用户补充要求」

根因：`_settle_pending` 的非确认分支把 `note`（用户自由文本）直接当成 ``render_video``
的**工具结果**写回，模型看到「工具返回了这句话」就以为受理了。
实际服务端从未创建渲染任务 → 整条链路断在最后一步。

这条用例钉住：那种 tool 结果必须是**失败**形态（is_tool_error 为真），
且文本要说明「未执行 / 没有产物」，不能让模型误以为成功。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio  # noqa: E402
import sys as _s  # noqa: E402

if hasattr(_s.stdout, "reconfigure"):
    _s.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.checkpoint import CheckpointManager  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import CompositeHook  # noqa: E402
from agent_framework.llm import ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolError, ToolRegistry, is_tool_error  # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class RenderTool(Tool):
    """最简 render_video：只被记录，不真渲染。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "render_video"

    @property
    def description(self) -> str:
        return "成片渲染"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, **kw):
        self.calls += 1
        return '{"node": "render_video", "render": {"status": "running"}}'


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()
    try:
        tool = RenderTool()
        reg = ToolRegistry()
        reg.register(tool)          # type: ignore[arg-type]
        mgr = CheckpointManager(storage)
        # 模型先要渲染（会被确认门拦下），用户再给自由文本要求
        llm = ScriptedLLM([("tool", "render_video", {}), ("answer", "收到")])
        runner = AgentOnceRun(
            llm, reg, ContextBuilder("BASE"), hooks=CompositeHook([]),
            config=AgentConfig(max_iterations=6), checkpoint=mgr, storage=storage,
        )
        sess = Session(user_id="u", conversation_id="c_render_note")
        await runner.run(sess, "渲染成片", run_id="run-rn")

        cp = await mgr.load("run-rn")
        check(cp is not None and cp.status == "awaiting_approval",
              f"渲染门把 render_video 拦下并挂起（status={getattr(cp, 'status', '?')}）")
        check(tool.calls == 0, f"挂起时渲染**没有**被执行（实际 {tool.calls} 次）")

        # 用户选「要改」并写自由文本（真机那句原话）
        note = "和音乐长度一致或者比音乐短一点,保证句子不被从中间截断"
        await runner.approve(cp, sess, decision="adjust_plan", note=note)
        check(tool.calls == 0, f"给要求之后渲染**仍然没执行**（实际 {tool.calls} 次）")

        cp2 = await mgr.load("run-rn")
        msgs = list(getattr(cp2, "messages", None) or [])
        results = [m for m in msgs
                   if isinstance(m, dict) and m.get("role") == "tool"
                   and m.get("name") == "render_video"]
        check(bool(results), f"补了 render_video 的 tool 结果（{len(results)} 条）")
        if results:
            body = str(results[-1].get("content") or "")
            print(f"    实际写回的文本：{body[:120]}")
            # 关键：必须是「失败/未执行」形态，不能冒充成功回执
            check("Error" in body or "未执行" in body,
                  "写回的是失败形态（不会让模型以为成功）")
            check("没有" in body and ("产物" in body or "运行" in body),
                  "明确说明「没有运行 / 没有产物」")
            check(note in body, "带上了用户的要求原文（模型据此重做）")
            # 反向：旧行为是纯用户文本、不带任何失败标记
            check(not body.strip().startswith("和音乐"),
                  "不再是「纯用户文本」那种冒充回执的写法")

        print()
        # 关键回归：**同一道门题只能问一次**。
        #
        # 真机事故：`decision_is_confirm` 只认 `confirm_render`，用户选「保内容完整」
        # 不是它 → 门认为"没确认" → 模型重渲 → 再拦 → 再问同一道题，问了几十遍。
        # 用户的原则：「答了就是答了」。所以第二次渲染必须**放行**（不再拦、不再问）。
        print("=== ② 同一道门题不能问第二次 ===")
        # 模拟模型「没改参数就重渲」（真机就是这样）
        llm2 = ScriptedLLM([("tool", "render_video", {}),
                            ("tool", "render_video", {}),
                            ("answer", "完成")])
        storage2 = build_storage("memory")
        await storage2.start()
        try:
            tool2 = RenderTool()
            reg2 = ToolRegistry()
            reg2.register(tool2)      # type: ignore[arg-type]
            mgr2 = CheckpointManager(storage2)
            runner2 = AgentOnceRun(
                llm2, reg2, ContextBuilder("BASE"), hooks=CompositeHook([]),
                config=AgentConfig(max_iterations=6), checkpoint=mgr2,
                storage=storage2,
            )
            sess2 = Session(user_id="u", conversation_id="c_gate_once")
            await runner2.run(sess2, "渲染成片", run_id="run-go")
            cp_a = await mgr2.load("run-go")
            check(cp_a is not None and cp_a.status == "awaiting_approval",
                  "第一次渲染被拦下（该问就问）")
            check(tool2.calls == 0, f"第一次没渲染（实际 {tool2.calls}）")

            # 用户选了「保完整」（非 confirm_render）
            await runner2.approve(cp_a, sess2, decision="keep_full_sentence",
                                 note="按音乐时长内,不要截断句子即可")
            # 关键断言：第二次渲染**不该再被拦**
            cp_b = await mgr2.load("run-go")
            status_b = getattr(cp_b, "status", "?")
            check(tool2.calls >= 1,
                  f"第二次渲染**放行了**（渲染真的执行了 {tool2.calls} 次）")
            check(status_b != "awaiting_approval",
                  f"没有再次停在等待确认上（status={status_b}）——"
                  f"这就是「问几十遍」的反面")
        finally:
            await storage2.close()

        print()
        print("全部通过" if not _fails else f"有 {_fails} 项未通过")
        print(f"用例 {_checks} 条")
        return 0 if not _fails else 1
    finally:
        await storage.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
