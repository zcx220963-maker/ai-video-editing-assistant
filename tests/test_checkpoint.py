"""Checkpoint 崩溃恢复验证（不联网）：PG 快照读写 + 一致点落表 + 崩溃恢复不重放工具。

对应 Context 构建流程图的 Checkpoint 分支：一致点快照存 ``checkpoints`` 表
（spec §8，取代 ``.runtime/checkpoints/{run_id}.json`` 原子写文件）。

运行：  python tests/test_checkpoint.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.checkpoint import (
    Checkpoint,
    CheckpointManager,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from agent_framework.context import ContextBuilder
from agent_framework.llm import LLMResponse
from agent_framework.messages import ToolCall
from agent_framework.session import Session
from agent_framework.storage import build_storage
from agent_framework.tool import Tool, ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class Boom(Exception):
    """模拟进程在执行途中崩溃。"""


class CountingTool(Tool):
    """无副作用之外只累加计数器的工具，用来验证工具是否被重放。"""

    def __init__(self, counter: list[int]) -> None:
        self._counter = counter

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "累加计数器并返回当前值"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> str:
        self._counter[0] += 1
        return f"result{self._counter[0]}"


class CrashLLM:
    """按脚本返回响应；在第 crash_on_call 次调用时抛 Boom 模拟崩溃。"""

    def __init__(self, steps: list[LLMResponse], crash_on_call: int | None = None) -> None:
        self._steps = list(steps)
        self._i = 0
        self.crash_on_call = crash_on_call
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        self._i += 1
        if self.crash_on_call is not None and self._i == self.crash_on_call:
            raise Boom("simulated crash")
        step = self._steps[min(self._i - 1, len(self._steps) - 1)]
        return step


def _tool_call(step: int) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"c{step}", name="count", arguments={})])


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        # ---- 存储层：一行一快照 + 往返 + 未完成扫描 ----
        storage = build_storage("memory")
        await storage.start()
        store = CheckpointManager(storage)
        cp = Checkpoint(
            run_id="r1", session_id="u:c", message="hi", iteration=2,
            messages=[{"role": "user", "content": "hi"}],
        )
        await store.save(cp)
        loaded = await store.load("r1")
        check(loaded is not None and loaded.iteration == 2 and loaded.status == STATUS_RUNNING,
              "保存后可读回，进度/状态一致")
        row = await storage.db.get_by_pk("checkpoints", {"run_id": "r1"})
        check(row is not None and row["session_id"] == "u:c", "快照是 checkpoints 表的一行")
        check("messages" not in row, "指针行不装正文（每轮写放大是一个小行）")
        entries = await storage.checkpoints.load_entries("r1")
        check(entries[0]["kind"] == "full" and entries[0]["payload"][0]["role"] == "user",
              "首个一致点是 full 基准，消息列表原样进 jsonb")
        check(row["head_seq"] == entries[0]["seq"], "指针行指向链尾")
        check(list(Path(td).iterdir()) == [], "整程没在本地目录留任何文件（快照只进表）")

        await store.save(Checkpoint(run_id="r2", session_id="u:c", message="x", iteration=0,
                                    messages=[], status=STATUS_COMPLETED))
        unfinished = {c.run_id for c in await store.pending()}
        check(unfinished == {"r1"}, f"pending 只含未结束项: {sorted(unfinished)}")
        await store.save(Checkpoint(run_id="r1", session_id="u:c", message="hi",
                                    iteration=3, messages=[], status=STATUS_RUNNING))
        again = await storage.db.get_by_pk("checkpoints", {"run_id": "r1"})
        check(again["iteration"] == 3 and again["created_at_ms"] == row["created_at_ms"],
              "同一 run 复跑是 UPDATE 而非插第二条，且保留首次创建时间")
        await store.delete("r1")
        check(await store.load("r1") is None, "delete 后不可读")

        # ---- 正常执行：跑到结束标记 completed ----
        counter = [0]
        registry = ToolRegistry()
        registry.register(CountingTool(counter))
        mgr = CheckpointManager(storage)
        llm = CrashLLM([_tool_call(1), LLMResponse(content="完成")])
        runner = AgentOnceRun(llm, registry, ContextBuilder("S"), config=AgentConfig(max_iterations=5),
                              checkpoint=mgr)
        sess = Session(user_id="u", conversation_id="ok")
        answer = await runner.run(sess, "开工", run_id="run-ok")
        done = await mgr.load("run-ok")
        check(answer == "完成", "无崩溃时正常返回最终答复")
        check(done is not None and done.status == STATUS_COMPLETED, "正常结束标记 completed")
        check(await mgr.pending() == [], "completed 的不属于待恢复项")
        check(sess.messages[-1]["content"] == "完成", "最终答复落回会话历史")

        # ---- 崩溃 + 恢复：不重放已提交工具 ----
        counter2 = [0]
        reg2 = ToolRegistry()
        reg2.register(CountingTool(counter2))
        mgr2 = CheckpointManager(storage)
        # 迭代0 → 工具；迭代1（第2次调用）崩溃
        crash_llm = CrashLLM([_tool_call(1), _tool_call(2)], crash_on_call=2)
        r2 = AgentOnceRun(crash_llm, reg2, ContextBuilder("S"),
                          config=AgentConfig(max_iterations=5), checkpoint=mgr2)
        sess2 = Session(user_id="u", conversation_id="crash")
        try:
            await r2.run(sess2, "查询", run_id="run-crash")
            crashed = False
        except Boom:
            crashed = True
        check(crashed, "执行途中模拟崩溃并向上抛出")
        check(counter2[0] == 1, f"崩溃前已提交 1 次工具调用: {counter2[0]}")

        crashed_cp = await mgr2.load("run-crash")
        n_tool = sum(1 for m in crashed_cp.messages if m.get("role") == "tool")
        check(crashed_cp.status in (STATUS_FAILED, STATUS_RUNNING)
              and crashed_cp.iteration == 1 and n_tool == 1,
              f"崩溃快照停在一致点(iter=1, 含 1 条工具结果) status={crashed_cp.status}")
        check([c.run_id for c in await mgr2.pending()] == ["run-crash"], "崩溃项出现在待恢复清单")

        # 恢复：新 LLM 继续脚本（迭代1 再调一次工具，迭代2 给答案）
        resume_llm = CrashLLM([_tool_call(2), LLMResponse(content="恢复完成")])
        r3 = AgentOnceRun(resume_llm, reg2, ContextBuilder("S"),
                          config=AgentConfig(max_iterations=5), checkpoint=mgr2)
        ans = await r3.resume(await mgr2.resume("run-crash"), sess2)
        check(ans == "恢复完成", "resume 续跑并返回最终答复")
        check(counter2[0] == 2, f"恢复只执行本轮新工具、未重放已提交工具: {counter2[0]}")
        # 恢复时载入的一致点（首次续跑的入参）只含崩溃前那一条工具结果，未被复制
        n_tool_at_load = sum(1 for m in resume_llm.calls[0] if m.get("role") == "tool")
        check(n_tool_at_load == 1, "恢复入参里已提交工具结果只有一条（未复制）")
        final_cp = await mgr2.load("run-crash")
        check(final_cp.status == STATUS_COMPLETED, "恢复完成后标记 completed")
        check(await mgr2.pending() == [], "恢复后待恢复清单清空")

        # ---- 完成的后无法再恢复 ----
        check(await mgr2.resume("run-crash") is None,
              "completed 的 run 恢复入口取不到快照（不重放已完成的执行）")

        # ---- 旧快照按量截断（文件时代只增不减）----
        bulk = CheckpointManager(storage, prune_keep=2)
        for i in range(5):
            await bulk.save(Checkpoint(run_id=f"old-{i}", session_id="u:c", message="x",
                                       iteration=0, messages=[], status=STATUS_COMPLETED))
        check(await bulk.prune() >= 2, "prune 截断已收尾的旧快照")
        check(await storage.db.count("checkpoints") <= 2 + 2, "截断后表里只留最近的收尾行")

        # ---- 未配置 checkpoint 时行为不变 ----
        plain = AgentOnceRun(CrashLLM([LLMResponse(content="无 ckpt")]), reg2, ContextBuilder("S"))
        s = Session(user_id="u", conversation_id="plain")
        check(await plain.run(s, "hi") == "无 ckpt", "未配置 checkpoint 时正常执行")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
