# -*- coding: utf-8 -*-
"""R10：逐调用 checkpoint——payload 是对象时 rebuild 跳过；completed_calls 按 call_id 汇总。

前半段用 StubRepo 钉条目语义；后半段起真 storage + 真 AgentOnceRun 走一次
「进程崩溃 → 从待恢复清单续跑」：已提交的工具不重放，最终落 completed。
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.checkpoint import (  # noqa: E402
    STATUS_COMPLETED,
    CheckpointManager,
    rebuild,
)
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.llm import LLMResponse  # noqa: E402
from agent_framework.messages import ToolCall  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import Tool, ToolRegistry  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


class StubRepo:
    """只实现 load_entries / append_entry。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    async def load_entries(self, run_id, upto_seq=None):
        return [e for e in self.entries if e.get("run_id") == run_id]

    async def append_entry(self, run_id, *, kind, payload, iteration, parent_seq):
        seq = len(self.entries) + 1
        self.entries.append({"run_id": run_id, "seq": seq, "kind": kind, "payload": payload})
        return {"seq": seq}


def _manager(repo: StubRepo) -> CheckpointManager:
    mgr = CheckpointManager.__new__(CheckpointManager)
    mgr._repo = repo
    return mgr


class Boom(Exception):
    pass


class CountingTool(Tool):
    def __init__(self, counter: list[int]) -> None:
        self._counter = counter

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "累加计数器"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kw) -> str:
        self._counter[0] += 1
        return f"result{self._counter[0]}"


class CrashLLM:
    def __init__(self, steps: list[LLMResponse], crash_on_call: int | None = None) -> None:
        self._steps = list(steps)
        self._i = 0
        self.crash_on_call = crash_on_call
        self.calls: list[list] = []

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append(messages)
        self._i += 1
        if self.crash_on_call is not None and self._i == self.crash_on_call:
            raise Boom("simulated crash")
        return self._steps[min(self._i - 1, len(self._steps) - 1)]


def _tool_call(call_id: str) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=call_id, name="count", arguments={})])


async def real_crash_restore() -> None:
    storage = build_storage("memory")
    await storage.start()
    counter: list[int] = [0]
    reg = ToolRegistry()
    reg.register(CountingTool(counter))
    mgr = CheckpointManager(storage)
    llm = CrashLLM([_tool_call("c1"), _tool_call("c2")], crash_on_call=2)
    runner = AgentOnceRun(llm, reg, ContextBuilder("S"),
                          config=AgentConfig(max_iterations=5), checkpoint=mgr)
    sess = Session(user_id="u1", conversation_id="crash")

    crashed = False
    try:
        await runner.run(sess, "开工", run_id="run-pc")
    except Boom:
        crashed = True
    check(crashed, "执行途中真的抛出（模拟进程崩溃）")
    check(counter[0] == 1, f"崩溃前已提交 1 次工具（实际 {counter[0]}）")

    done = await mgr.completed_calls("run-pc")
    check(done == {"c1"}, f"completed_calls 按 call_id 认出已完成项（实际 {done}）")
    entries = await storage.checkpoints.load_entries("run-pc")
    kinds = [e["kind"] for e in entries]
    check("tool_call" in kinds and "tool_result" in kinds,
          f"逐调用条目已落表（{kinds}）")
    pending = await mgr.pending()
    check([c.run_id for c in pending] == ["run-pc"], "崩溃项进入待恢复清单")

    # 新进程续跑：第 1 次调用直接给答案（已提交的 count 不该再跑）
    resume_llm = CrashLLM([LLMResponse(content="恢复完成")])
    resumed = AgentOnceRun(resume_llm, reg, ContextBuilder("S"),
                           config=AgentConfig(max_iterations=5), checkpoint=mgr)
    answer = await resumed.resume(await mgr.resume("run-pc"), sess)
    check(answer == "恢复完成", "续跑返回最终答复")
    check(counter[0] == 1, f"已提交工具没有重放（实际仍 {counter[0]} 次）")
    final = await mgr.load("run-pc")
    check(final is not None and final.status == STATUS_COMPLETED, "恢复完成后落 completed")
    check(await mgr.pending() == [], "待恢复清单清空")


async def main() -> None:
    repo = StubRepo()
    mgr = _manager(repo)

    # ---- 写 tool_call / tool_result ----
    await mgr.record_tool_call("r1", call_id="c1", name="load_media", arguments={"a": 1})
    await mgr.record_tool_result("r1", call_id="c1", name="load_media", status="ok")
    kinds = [e["kind"] for e in repo.entries]
    check(kinds == ["tool_call", "tool_result"], f"两类条目都写了（{kinds}）")

    # ---- completed_calls 只看 tool_result ----
    await mgr.record_tool_call("r1", call_id="c2", name="split_shots")
    done = await mgr.completed_calls("r1")
    check(done == {"c1"}, f"只有带结果的算完成（{done}）")
    await mgr.record_tool_result("r1", call_id="c2", name="split_shots", status="error")
    check(await mgr.completed_calls("r1") == {"c1", "c2"}, "失败的也算「跑过了」，不再重放")

    # ---- rebuild 不会被对象 payload 带坏 ----
    entries = [
        {"kind": "full", "payload": [{"role": "user", "content": "hi"}]},
        {"kind": "tool_call", "payload": {"call_id": "c1"}},
        {"kind": "tool_result", "payload": {"call_id": "c1"}},
        {"kind": "delta", "payload": [{"role": "assistant", "content": "ok"}]},
    ]
    msgs = rebuild(entries)
    check(len(msgs) == 2, f"rebuild 跳过对象 payload（得到 {len(msgs)} 条）")
    check(msgs[-1]["content"] == "ok", "delta 正常追加")

    # ---- 空 run ----
    check(await mgr.completed_calls("nope") == set(), "没有条目时返回空集合")

    # ---- 真崩溃再续跑（真 storage + 真 AgentOnceRun）----
    await real_crash_restore()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
