# -*- coding: utf-8 -*-
"""配额闸的离线验证（不联网）：UsageHook 落库 + QuotaHook 超限拦截 + 按身份热读的三层优先级。

八条：① 用量落账 ② 未超限放行 ③ 超限拦截 ④ limit=0 只记账
⑤ 页面值压过环境变量与启动参数（拦截能由用户自己打开）
⑥ 页面填 0 时环境变量里的正数盖不回来
⑦ 超限时的「回落时刻」数学（滑动窗口里最早被挤出去的那一笔 + window）
⑧ 拦截文案自带出路（明说刷新没用 + 指路页面设置）

运行：  PYTHONPATH=. python tests/test_quota_gate.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import secrets  # noqa: E402
from agent_framework.agent import AgentConfig, AgentOnceRun  # noqa: E402
from agent_framework.context import ContextBuilder  # noqa: E402
from agent_framework.hooks import (CompositeHook, QuotaExceeded, UsageHook,  # noqa: E402
                                   QuotaHook, quota_status)
from agent_framework.llm import LLMResponse, ScriptedLLM  # noqa: E402
from agent_framework.session import Session  # noqa: E402
from agent_framework.storage import build_storage  # noqa: E402
from agent_framework.tool import ToolRegistry  # noqa: E402

CHECKS = 0
FAILS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    print(("PASS" if cond else "FAIL") + f"  {label}")
    if not cond:
        FAILS += 1


class clear_env_quota:
    """摘掉环境变量层的配额两项（结束时还原）。

    热读优先级是「页面 → 环境变量 → 启动参数」，而容器里 .env 带着 QUOTA_LIMIT=8000000。
    不 pop 干净的话，`QuotaHook(db, limit=400)` 传的数会被环境层盖掉，判据就成了看机器脸色。
    """

    def __enter__(self) -> "clear_env_quota":
        self.saved = {n: os.environ.pop(n, None)
                      for n in (secrets.ENV_QUOTA_LIMIT, secrets.ENV_QUOTA_WINDOW)}
        return self

    def __exit__(self, *exc: Any) -> bool:
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        secrets.invalidate()
        return False


class UsageLLM(ScriptedLLM):
    """ScriptedLLM 但在响应里带 usage。"""

    def __init__(self, steps: list[Any], usage: dict[str, int] | None = None) -> None:
        super().__init__(steps)
        self._usage = usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    async def complete(self, messages, tools=None) -> LLMResponse:
        r = await super().complete(messages, tools)
        r.usage = self._usage
        return r


async def _basic_cases(storage) -> None:
    print("\n=== ① UsageHook 把 token 用量落 token_usage 表 ===")
    usage_hook = UsageHook(storage.db, model="test-model")
    hooks = CompositeHook([usage_hook])
    llm = UsageLLM([("answer", "你好")],
                   usage={"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150})
    runner = AgentOnceRun(llm, ToolRegistry(), ContextBuilder("S"),
                          hooks=hooks, config=AgentConfig(max_iterations=3))
    sess = Session(user_id="u_quota", conversation_id="c1")
    out = await runner.run(sess, "hi", run_id="run-q1")
    check(out == "你好", f"① 正常返回：{out}")
    rows = await storage.db.select("token_usage", where={"user_id": "u_quota"})
    check(len(rows) == 1, f"① token_usage 落了一行：{len(rows)} 行")
    if rows:
        r = rows[0]
        check(r["prompt_tokens"] == 120 and r["completion_tokens"] == 30,
              f"① token 数对得上：pt={r['prompt_tokens']} ct={r['completion_tokens']}")
        check(r["run_id"] == "run-q1", f"① run_id 记对：{r['run_id']}")

    print("\n=== ② QuotaHook 未超限时不拦截 ===")
    quota_hook = QuotaHook(storage.db, limit=10000, window=86400)
    hooks2 = CompositeHook([UsageHook(storage.db), quota_hook])
    llm2 = UsageLLM([("answer", "继续")],
                    usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
    runner2 = AgentOnceRun(llm2, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks2, config=AgentConfig(max_iterations=3))
    out2 = await runner2.run(Session(user_id="u_quota", conversation_id="c2"), "hi", run_id="run-q2")
    check(out2 == "继续", f"② 未超限正常返回：{out2}")

    print("\n=== ③ QuotaHook 超限时抛 QuotaExceeded ===")
    quota_hook3 = QuotaHook(storage.db, limit=400, window=86400)
    hooks3 = CompositeHook([UsageHook(storage.db), quota_hook3])
    llm3 = UsageLLM([("answer", "不该到这")],
                    usage={"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300})
    runner3 = AgentOnceRun(llm3, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks3, config=AgentConfig(max_iterations=3))
    raised = False
    try:
        await runner3.run(Session(user_id="u_quota", conversation_id="c3"), "hi", run_id="run-q3")
    except QuotaExceeded as e:
        raised = True
        check(e.used >= 400, f"③ 报的已用量 ≥400：{e.used}")
        check(e.limit == 400, f"③ 报的上限=400：{e.limit}")
    check(raised, "③ 超限时抛 QuotaExceeded")

    print("\n=== ④ limit=0 不拦截（默认关闭）===")
    quota_hook4 = QuotaHook(storage.db, limit=0, window=86400)
    hooks4 = CompositeHook([UsageHook(storage.db), quota_hook4])
    llm4 = UsageLLM([("answer", "ok")])
    runner4 = AgentOnceRun(llm4, ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks4, config=AgentConfig(max_iterations=3))
    out4 = await runner4.run(Session(user_id="u_quota", conversation_id="c4"), "hi", run_id="run-q4")
    check(out4 == "ok", f"④ limit=0 不拦截：{out4}")


async def _case_page_opens_the_gate(storage) -> QuotaExceeded | None:
    """⑤ 启动参数说不拦（limit=0），页面填一个正数就把拦截打开——这条曾经落不了地。"""
    print("\n=== ⑤ 页面值压过启动参数：拦截能由用户自己打开 ===")
    uid = "u_page"
    await secrets.put_user_key(uid, secrets.QUOTA_LIMIT_KEY, "400")
    limit, window, source = await secrets.resolve_quota(uid, default_limit=0,
                                                        default_window=86400)
    check(limit == 400 and source == secrets.SOURCE_PG,
          f"⑤ 生效上限来自页面：limit={limit} source={source}")
    check(window == 86400, f"⑤ 页面没填窗口时用启动默认：{window}")

    quota_hook = QuotaHook(storage.db, limit=0, window=86400)   # 启动参数层 = 不拦
    hooks = CompositeHook([UsageHook(storage.db), quota_hook])
    llm = UsageLLM([("answer", "继续")] * 3,
                   usage={"prompt_tokens": 200, "completion_tokens": 100,
                          "total_tokens": 300})
    runner = AgentOnceRun(llm, ToolRegistry(), ContextBuilder("S"),
                          hooks=hooks, config=AgentConfig(max_iterations=3))
    caught: QuotaExceeded | None = None
    ok_runs = 0
    for i in range(3):
        try:
            await runner.run(Session(user_id=uid, conversation_id=f"p{i}"), "hi",
                             run_id=f"run-p{i}")
            ok_runs += 1
        except QuotaExceeded as e:
            caught = e
            break
    check(ok_runs == 2, f"⑤ 前两轮放行（累计 0→300→600 才越线）：{ok_runs} 轮")
    check(caught is not None and caught.limit == 400,
          f"⑤ 第三轮按页面上限拦住：{caught.limit if caught else '没拦'}")
    check(caught is not None and caught.source == secrets.SOURCE_PG,
          f"⑤ 报错说清上限来源是页面：{caught.source if caught else ''}")
    return caught


async def _case_zero_beats_env(storage) -> None:
    """⑥ 页面填 0 = 明确关拦截；环境变量里的正数不许把这个决定盖回去。"""
    print("\n=== ⑥ 页面填 0 时，env 的正数不复活拦截 ===")
    uid = "u_zero"
    for total in (2000, 3000):
        await storage.db.insert("token_usage", {
            "user_id": uid, "session_id": "s", "run_id": "r", "model": "m",
            "prompt_tokens": total, "completion_tokens": 0, "total_tokens": total})
    os.environ[secrets.ENV_QUOTA_LIMIT] = "1000"
    await secrets.put_user_key(uid, secrets.QUOTA_LIMIT_KEY, "0")
    limit, _, source = await secrets.resolve_quota(uid, default_limit=999)
    check(limit == 0 and source == secrets.SOURCE_PG,
          f"⑥ 页面 0 压过 env：limit={limit} source={source}")
    hooks = CompositeHook([UsageHook(storage.db), QuotaHook(storage.db, limit=999)])
    llm = UsageLLM([("answer", "照跑")])
    runner = AgentOnceRun(llm, ToolRegistry(), ContextBuilder("S"),
                          hooks=hooks, config=AgentConfig(max_iterations=3))
    out = await runner.run(Session(user_id=uid, conversation_id="z1"), "hi", run_id="run-z1")
    check(out == "照跑", f"⑥ 页面填 0 → 已用 5000 也放行：{out}")

    # 反向对照：把页面覆盖清掉，env 的 1000 立刻恢复拦截（证明上面那条真是页面在说话）
    await secrets.drop_user_key(uid, secrets.QUOTA_LIMIT_KEY)
    limit2, _, source2 = await secrets.resolve_quota(uid, default_limit=999)
    check(limit2 == 1000 and source2.startswith("环境变量"),
          f"⑥ 清除页面覆盖后回落 env：limit={limit2} source={source2}")
    hooks2 = CompositeHook([UsageHook(storage.db), QuotaHook(storage.db, limit=999)])
    runner2 = AgentOnceRun(UsageLLM([("answer", "不该到")]), ToolRegistry(), ContextBuilder("S"),
                           hooks=hooks2, config=AgentConfig(max_iterations=3))
    raised = False
    try:
        await runner2.run(Session(user_id=uid, conversation_id="z2"), "hi", run_id="run-z2")
    except QuotaExceeded:
        raised = True
    check(raised, "⑥ 反向对照：env 层的 1000 在没有页面覆盖时确实拦")
    os.environ.pop(secrets.ENV_QUOTA_LIMIT, None)


async def _case_release_math(storage) -> None:
    """⑦ 回落时刻：滑动窗口里「刚好把超出部分挤出去」的那一笔 + window。"""
    print("\n=== ⑦ frees_at 数学：最早那笔什么时候滚出窗口 ===")
    uid = "u_math"
    now = datetime.now(timezone.utc)
    for back, tokens in ((3000, 300), (2000, 300), (600, 400)):
        await storage.db.insert("token_usage", {
            "user_id": uid, "session_id": "s", "run_id": "r", "model": "m",
            "prompt_tokens": tokens, "completion_tokens": 0, "total_tokens": tokens,
            "created_at": now - timedelta(seconds=back)})
    st = await quota_status(storage.db, uid, limit=600, window=3600)
    check(st["used"] == 1000, f"⑦ 窗口内合计：{st['used']}")
    check(st["blocking"] is True and st["remaining"] == 0, "⑦ 超限：blocking=True，剩余 0")
    # need = 1000-600+1 = 401 → 升序累加 300(<401) → +300=600(≥401) → 那笔在 now-2000
    expect = now - timedelta(seconds=2000) + timedelta(seconds=3600)
    got = datetime.fromisoformat(st["frees_at"])
    check(abs((got - expect).total_seconds()) < 5,
          f"⑦ 回落时刻 = 第二笔 + 窗口：{got.isoformat()} vs {expect.isoformat()}")
    check(25 <= st["frees_in_minutes"] <= 27,
          f"⑦ 倒计时分钟数对得上：{st['frees_in_minutes']}")
    st2 = await quota_status(storage.db, uid, limit=2000, window=3600)
    check(st2["frees_at"] is None and st2["remaining"] == 1000,
          f"⑦ 未超限时不给回落时刻，只报剩余：{st2['frees_at']}/{st2['remaining']}")
    st3 = await quota_status(storage.db, uid, limit=0, window=3600)
    check(st3["blocking"] is False and st3["remaining"] is None and st3["used"] == 1000,
          "⑦ limit=0 时仍出账（不拦也看得见用了多少）")


async def _case_exit_wording(storage) -> None:
    """⑧ 文案自带出路：刷新没用要说破，改上限的路要指到。"""
    print("\n=== ⑧ 拦截文案：说破「刷新不退额」并指路页面设置 ===")
    saved = os.environ.pop(secrets.ENV_QUOTA_LIMIT, None)
    await secrets.put_user_key("u_word", secrets.QUOTA_LIMIT_KEY, "100")
    await storage.db.insert("token_usage", {
        "user_id": "u_word", "session_id": "s", "run_id": "r", "model": "m",
        "prompt_tokens": 500, "completion_tokens": 0, "total_tokens": 500})
    hooks = CompositeHook([QuotaHook(storage.db, limit=100)])
    runner = AgentOnceRun(UsageLLM([("answer", "不该到")]), ToolRegistry(),
                          ContextBuilder("S"), hooks=hooks,
                          config=AgentConfig(max_iterations=3))
    msg = ""
    try:
        await runner.run(Session(user_id="u_word", conversation_id="w1"), "hi", run_id="run-w1")
    except QuotaExceeded as e:
        msg = str(e)
    for frag in ("刷新页面、换会话、重开标签页都不会让额度回来",
                 "设置 → 配额", "滚出窗口", "上限来源：前端配置"):
        check(frag in msg, f"⑧ 文案含「{frag}」")
    await secrets.drop_user_key("u_word", secrets.QUOTA_LIMIT_KEY)
    if saved is not None:
        os.environ[secrets.ENV_QUOTA_LIMIT] = saved


async def main() -> int:
    storage = build_storage("memory")
    await storage.start()
    secrets.bind_storage(storage)
    try:
        with clear_env_quota():
            await _basic_cases(storage)
            err = await _case_page_opens_the_gate(storage)
            if err is not None:
                check(err.frees_at is not None,
                      f"⑤⑦ 拦下来的那一刻就算出了回落时刻：{err.frees_at}")
            await _case_zero_beats_env(storage)
            await _case_release_math(storage)
            await _case_exit_wording(storage)
    finally:
        secrets.unbind_storage()
        await storage.close()
    print("\n" + ("SMOKE PASSED" if not FAILS else f"SMOKE FAILED：{FAILS}"), flush=True)
    print(f"用例 {CHECKS} 条", flush=True)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))