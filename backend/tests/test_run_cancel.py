# -*- coding: utf-8 -*-
"""「停止 / 继续」的判据：停在最近一致点、状态单列一档、崩溃对账不认它。

    docker exec creation-app python tests/test_run_cancel.py

三个层次：
  ① `cancel.guard` 的赛跑语义（撤销工具协程、超时仍是超时）；
  ② 一条真跑的 `_drive`：工具跑到一半点停止 → 收尾是「已停下」、状态 cancelled、
     在途的那次调用**没有**回执（续跑会重放它）、崩溃对账看不见它；
  ③ 静态断言：路由、回投帧、前端那颗按钮与「继续」的接法都在。

不联网、不调模型（脚本化 LLM）、不渲染。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
ROOT = BACKEND.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import cancel as cancel_mod
from agent_framework.agent import AgentConfig, AgentOnceRun, _stop_reason
from agent_framework.checkpoint import (STATUS_CANCELLED, STATUS_COMPLETED,
                                        Checkpoint, CheckpointManager, _UNFINISHED)
from agent_framework.context import ContextBuilder
from agent_framework.llm import LLMResponse
from agent_framework.messages import ToolCall
from agent_framework.run_state import RunState
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


def src(rel: str) -> str | None:
    """读仓库里的文件；找不到回 None。

    容器里只挂了后端（agent_framework / open_storyline / prompts / examples），
    `frontend/` 不在镜像里——那几条要在仓库根跑，不在容器里假装通过。
    """
    for base in (ROOT, BACKEND):
        p = base / rel
        if p.exists():
            return p.read_text(encoding="utf-8")
    p = BACKEND / rel.replace("backend/", "", 1)
    return p.read_text(encoding="utf-8") if p.exists() else None


class SlowTool(Tool):
    """一个「要跑很久」的工具：被撤销时会走到 finally，据此判断协程真被撤了。"""

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.started = False
        self.cancelled = False
        self.finished = False

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "睡一会儿再返回（模拟一次长任务）"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs) -> str:
        self.started = True
        try:
            await asyncio.sleep(self.delay)
            self.finished = True
            return "跑完了"
        finally:
            if not self.finished:
                self.cancelled = True


class ScriptLLM:
    """按脚本返回响应；用完了就一直重复最后一条。"""

    def __init__(self, steps: list[LLMResponse]) -> None:
        self._steps = list(steps)
        self._i = 0
        self.calls = 0

    async def complete(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        step = self._steps[min(self._i, len(self._steps) - 1)]
        self._i += 1
        return step


class SlowStreamLLM:
    """吐得很慢的一段流式回答：模拟「正在生成」时用户点停止。

    这正是最常见的那一幕——用户点的不是工具、是模型在打字。若只在工具执行处看
    停止标记，这一整段（这里 5 秒）点了也停不下来。
    """

    def __init__(self, chunks: int = 50, interval: float = 0.1) -> None:
        self.chunks = chunks
        self.interval = interval
        self.sent = 0

    async def complete(self, messages, tools=None) -> LLMResponse:
        raise AssertionError("走的是流式路径，不该调 complete")

    async def complete_stream(self, messages, tools=None):
        from agent_framework.llm import StreamChunk
        for i in range(self.chunks):
            await asyncio.sleep(self.interval)
            self.sent += 1
            yield StreamChunk(delta=f"{i} ")
        yield StreamChunk(tool_calls=[])


async def _cancel_later(run_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    cancel_mod.request(run_id)


async def main() -> None:
    # ---- ① guard 的赛跑语义 ----
    cancel_mod._reset_for_tests()

    async def quick() -> str:
        return "好了"

    check(await cancel_mod.guard("no-such-run", quick()) == "好了",
          "没点过停止：guard 与普通 await 同形")

    flag = {"cancelled": False}

    async def long_body() -> str:
        try:
            await asyncio.sleep(5)
            return "跑完了"
        finally:
            flag["cancelled"] = True

    cancel_mod.request("r-guard")
    t0 = time.monotonic()
    raised = False
    try:
        await cancel_mod.guard("r-guard", long_body())
    except cancel_mod.RunCancelled:
        raised = True
    took = time.monotonic() - t0
    check(raised, "停止请求一到就抛 RunCancelled（不是等工具跑完）")
    check(took < 2.0, f"立刻返回（实测 {took:.2f}s，工具本身 5s）")
    check(flag["cancelled"], "工具协程被真的撤销（走到了 finally）")

    timed_out = False
    try:
        await cancel_mod.guard("another-run", asyncio.sleep(5), timeout=0.05)
    except asyncio.TimeoutError:
        timed_out = True
    check(timed_out, "超时仍然抛 TimeoutError（guard 不改变原来的超时语义）")

    cancel_mod.clear("r-guard")
    check(not cancel_mod.is_cancelled("r-guard"), "clear 之后不再算「已停止」")

    # 循环开头那条判据（检查点①）与花费/打转共用 `_stop_reason`——用户叫停排最前，
    # 免得被「再让位一轮」改写；不传 run_id 的老调用点行为不变。
    st0 = RunState.restore(None)
    cancel_mod.request("r-loop")
    check(_stop_reason(st0, [], "r-loop") == "cancelled",
          "每轮开头那条判据也认「已停止」")
    check(_stop_reason(st0, []) == "", "不传 run_id 时判据照旧（老调用点不受影响）")
    cancel_mod.clear("r-loop")

    # ---- ② 状态机：cancelled 单列一档 ----
    check(STATUS_CANCELLED not in _UNFINISHED,
          "cancelled 不在 _UNFINISHED（重启不会替用户重按「继续」）")

    storage = build_storage("memory")
    await storage.start()
    mgr = CheckpointManager(storage)
    cp = Checkpoint(run_id="r-state", session_id="u:c", message="hi", iteration=1,
                    messages=[{"role": "user", "content": "hi"}])
    await mgr.save(cp)
    await mgr.cancel(cp, iteration=2,
                     messages=[{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "停下"}])
    back = await mgr.load("r-state")
    check(back is not None and back.status == STATUS_CANCELLED, "cancel() 把状态落成 cancelled")
    check(back is not None and back.iteration == 2, "cancel() 记下停在第几轮")
    check("r-state" not in {c.run_id for c in await mgr.pending()},
          "cancelled 不出现在待恢复清单里")
    check(await mgr.claim_recoverable("inst-1") == [], "多副本认领也不会捞起 cancelled")
    check(await mgr.resume("r-state") is not None,
          "resume() 接得住 cancelled（只有 completed 才拒）")

    # ---- ③ 一条真跑的循环：工具跑到一半点停止 ----
    cancel_mod._reset_for_tests()
    tool = SlowTool(delay=5.0)
    reg = ToolRegistry()
    reg.register(tool)
    llm = ScriptLLM([LLMResponse(tool_calls=[ToolCall(id="c1", name="slow", arguments={})]),
                     LLMResponse(content="收尾")])
    runner = AgentOnceRun(llm, reg, ContextBuilder("S"),
                          config=AgentConfig(max_iterations=5), checkpoint=mgr)
    sess = Session(user_id="u", conversation_id="stop")
    ticker = asyncio.create_task(_cancel_later("run-stop", 0.3))
    t0 = time.monotonic()
    answer = await runner.run(sess, "开工", run_id="run-stop")
    took = time.monotonic() - t0
    await ticker

    check(tool.started, "工具确实开始跑了（不是还没轮到就停）")
    check(tool.cancelled and not tool.finished, "工具是被撤销的，不是跑完的")
    check(took < 4.0, f"整轮在几秒内收尾（实测 {took:.2f}s）")
    check("已经按你的要求停下" in answer, f"终答说的是「已停下」：{answer[:40]}")
    check("继续" in answer, "终答告诉用户可以从这里继续")

    stopped = await mgr.load("run-stop")
    check(stopped is not None and stopped.status == STATUS_CANCELLED,
          f"这条 run 落成 cancelled（实际 {stopped.status if stopped else '?'}）")
    n_tool = sum(1 for m in (stopped.messages if stopped else []) if m.get("role") == "tool")
    check(n_tool == 0, f"在途的那次调用没有回执（续跑会重放它，现有 {n_tool} 条工具消息）")
    check("run-stop" not in {c.run_id for c in await mgr.pending()},
          "停止后的 run 不在崩溃恢复清单里")
    check(not cancel_mod.is_cancelled("run-stop"),
          "run 收尾时清掉了停止标记（续跑不会一进循环就被叫停）")
    check(sess.messages and sess.messages[-1]["content"] == answer,
          "终答照常落回会话历史（刷新后看得见）")

    # ---- ③b 最常被点的那一幕：模型正在吐字时点停止 ----
    cancel_mod._reset_for_tests()
    slow_llm = SlowStreamLLM(chunks=50, interval=0.1)      # 整整 5 秒
    runner_s = AgentOnceRun(slow_llm, ToolRegistry(), ContextBuilder("S"),
                            config=AgentConfig(max_iterations=5), checkpoint=mgr)
    sess_s = Session(user_id="u", conversation_id="stop-stream")
    ticker = asyncio.create_task(_cancel_later("run-stream", 0.4))
    t0 = time.monotonic()
    answer_s = await runner_s.run(sess_s, "随便说点什么", run_id="run-stream", stream=True)
    took = time.monotonic() - t0
    await ticker

    check("已经按你的要求停下" in answer_s, f"流式路也能停在半路：{answer_s[:40]}")
    check(took < 2.5, f"没等那 5 秒吐完（实测 {took:.2f}s）")
    check(slow_llm.sent < slow_llm.chunks,
          f"流真的被打断了（只发出 {slow_llm.sent}/{slow_llm.chunks} 块）")
    st = await mgr.load("run-stream")
    check(st is not None and st.status == STATUS_CANCELLED, "流式半路停下也落成 cancelled")
    half = " ".join(str(m.get("content") or "") for m in (st.messages if st else []))
    check("0 1 2 3" not in half, "半截回答没被当成终答写进历史（续跑会重新生成）")

    # ---- ④ 停止之后「继续」：从最近一致点接着跑完 ----
    resumed = await mgr.resume("run-stop")
    llm2 = ScriptLLM([LLMResponse(content="接着做完了")])
    runner2 = AgentOnceRun(llm2, reg, ContextBuilder("S"),
                           config=AgentConfig(max_iterations=5), checkpoint=mgr)
    answer2 = await runner2.resume(resumed, sess)
    check(answer2 == "接着做完了", f"续跑跑到了终点：{answer2}")
    check((await mgr.load("run-stop")).status == STATUS_COMPLETED, "续跑后标记 completed")

    # ---- ⑤ 静态：路由 / 回投帧 / 前端那颗按钮 ----
    server = src("backend/agent_framework/server.py") or src("agent_framework/server.py") or ""
    check('@app.post("/runs/{run_id}/cancel")' in server, "服务端有停止端点")
    check('"type": "cancelled"' in server, "停止端点回投 cancelled 帧（按钮立刻变「正在停」）")
    check("cancel_mod.request(run_id)" in server, "停止端点是带外立标记，不投 MQ")

    agent = src("backend/agent_framework/agent.py") or src("agent_framework/agent.py") or ""
    check(agent.count("cancel_mod.is_cancelled") >= 3,
          "停止有三个检查点（每轮开头 / 模型流 / 渲染代查）")
    check("cancel_mod.guard(" in agent, "工具调用本身也盯着停止请求（长任务能及时停下）")
    check('r.stop_reason = "cancelled"' in agent, "停止走 stop_reason=cancelled 的收尾分支")
    check("self.checkpoint.cancel(" in agent, "收尾把状态落成 cancelled")

    vue = src("frontend/src/App.vue")
    if vue is None:
        # 容器里没有前端源码：这几条不在这里判，回仓库根再跑一次。
        print("  · 前端源码不在这个环境里（容器只挂了后端）：「停止按钮」那几条请在仓库根重跑")
    else:
        check('class="send stop"' in vue, "任务在跑时那颗按钮是「停止」")
        check("async function stopRun()" in vue, "前端有 stopRun()")
        check('p.type === "cancelled"' in vue, "前端认得 cancelled 帧")
        check("offerResumeIfInterrupted" in vue and '"cancelled"' in vue,
              "停完能直接续跑（续跑气泡认 cancelled）")
        check("wasStopping" in vue, "答复落地后自动补上「继续」入口")

    print(f"\n{_checks} 通过 / {_fails} 失败")
    if _fails:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
