"""Session 历史验证（不联网）：QA 结构进 messages 表、重启加载、History QA 重建。

对应 Context 构建流程图左侧：PG ``messages`` → Session Manager → Session → History QA
（spec §3.3 取代 ``.runtime/sessions/{user}/{conv}.jsonl``）。表里 assistant 行的 ``qa``
列存 ``{"parts": [{type:"think"|"tool call"|"answer", ...}]}``，读回时与前面的 user 行
配成一条 QA。

运行：  python tests/test_session_history.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.llm import ScriptedLLM
from agent_framework.session import SessionManager
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


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显文本"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def execute(self, text: str) -> str:
        return f"echo:{text}"


async def main() -> None:
    storage = build_storage("memory")
    reg = ToolRegistry()
    reg.register(EchoTool())

    # ---- 一轮含工具调用的执行：QA 分段进 messages 的 assistant 行 ----
    session = await SessionManager(storage).get_or_create("u1", "c1")
    llm = ScriptedLLM([
        ("tool", "echo", {"text": "hi"}, "先查一下"),
        ("answer", "完成"),
    ])
    agent = AgentOnceRun(llm, reg, config=AgentConfig(max_iterations=5), storage=storage)
    out = await agent.run(session, "说你好")
    check(out == "完成", "Agent 产出最终答复")

    rows = await storage.messages.history("u1", "c1")
    check([r["role"] for r in rows] == ["user", "assistant"],
          f"一轮执行落两行（user + assistant）：{[r['role'] for r in rows]}")
    check(rows[0]["content"] == "说你好", "user 行存原话")
    rec = {"question": rows[0]["content"], "answer": rows[1]["qa"]["parts"]}
    types = [p["type"] for p in rec["answer"]]
    check(types == ["prompt_fingerprint", "think", "tool call", "answer"],
          f"qa.parts 为 prompt_fingerprint/think/tool call/answer 分段: {types}")
    tc = rec["answer"][2]
    check(tc["name"] == "echo" and tc["arguments"] == {"text": "hi"},
          "tool call 段记录工具名与参数")
    check(rec["answer"][3]["content"] == "完成", "answer 段记录最终答复")
    check(rows[1]["seq"] == 2 and rows[0]["seq"] == 1,
          f"seq 在会话内单调（取代 .jsonl 行序）：{[r['seq'] for r in rows]}")

    # ---- 重启后：新 SessionManager 从 messages 表 load 回 History QA ----
    s2 = await SessionManager(storage).get_or_create("u1", "c1")
    check(len(s2.qa_log) == 1 and s2.qa_log[0] == rec, "qa_log 从 messages 表完整重载")
    roles = [m["role"] for m in s2.messages]
    check(roles == ["user", "assistant"], f"messages 重建为 user/assistant 历史: {roles}")
    check(s2.messages[1]["content"] == "完成", "重建的 assistant 消息取 answer 段内容")

    # ---- 第二轮继续追加，同一会话按 seq 排序 ----
    llm3 = ScriptedLLM([("answer", "第二轮答复")])
    out3 = await AgentOnceRun(llm3, reg, storage=storage).run(s2, "再来一轮")
    check(out3 == "第二轮答复", "第二轮执行成功")
    rows2 = await storage.messages.history("u1", "c1")
    check([r["seq"] for r in rows2] == [1, 2, 3, 4],
          f"同一会话的多轮按 seq 顺序追加：{[r['seq'] for r in rows2]}")
    s3 = await SessionManager(storage).get_or_create("u1", "c1")
    check([q["question"] for q in s3.qa_log] == ["说你好", "再来一轮"],
          "重载后的两条 QA 顺序与提问原文一致")

    # ---- 会话隔离：不同 owner 读不到别人的历史 ----
    other = await SessionManager(storage).get_or_create("u2", "c1")
    check(other.qa_log == [] and other.messages == [],
          "换用户读不到他人会话历史（messages 按 owner 过滤）")

    # ---- 未配置存储：纯内存同样记录 qa_log ----
    mem = SessionManager()
    s4 = await mem.get_or_create("u", "c")
    await AgentOnceRun(ScriptedLLM([("answer", "x")]), reg).run(s4, "q")
    check(len(s4.qa_log) == 1 and s4.qa_log[0]["question"] == "q",
          "无存储句柄时 qa_log 仍在内存中维护")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
