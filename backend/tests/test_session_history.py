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
    check(types == ["prompt_fingerprint", "context_ledger", "think", "tool call", "answer"],
          f"qa.parts 为 指纹/上下文账面/think/tool call/answer 分段: {types}")
    # R9-T37：账面随这条既有通道留档（§5.7 第①条），每支夹具的逐镜记录里都带它。
    ledger = rec["answer"][1]
    check({"sections", "tools", "over_budget", "orphan_sections",
           "over_target"} <= set(ledger),
          f"账面键齐（多出来也照实报）：{sorted(ledger)}")
    check(any(r["name"] == "system_prompt" and r["bytes"] > 0 for r in ledger["sections"]),
          "账里有系统提示词那一行，且字节是真的（不是 0 占位）")
    check(ledger["over_budget"] == [], "这一轮没有块顶穿断值")
    check(ledger["tools"]["sent"] is not None and ledger["tools"]["total_registered"],
          f"工具面由 agent 侧补上了：sent={ledger['tools']['sent']} / "
          f"total={ledger['tools']['total_registered']}")
    tc = rec["answer"][3]
    check(tc["name"] == "echo" and tc["arguments"] == {"text": "hi"},
          "tool call 段记录工具名与参数")
    check(rec["answer"][4]["content"] == "完成", "answer 段记录最终答复")
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

    # ---- R10-T48：历史改窗口拉取，且 `<history>` 第一次进账 ----
    # 病：原先无 LIMIT 全量拉，长会话下这段既进 token 账也进 ㉓ 的字节账，
    # 却从未在任何预算表里出现过——「没人记账」不等于「没花钱」。
    from agent_framework.context import ContextBuilder
    from agent_framework.session import render_history
    from agent_framework.summary_store import SummaryStore

    # 造一段 60 条的历史（c9），且**先落一份摘要**——被截掉的头部要有替代物，
    # 否则「丢头部」就是静默丢内容（同 P24 的纪律）。
    await storage.conversations.ensure("u9", "c9")
    for i in range(30):
        await storage.messages.append("u9", "c9", "user", f"第{i}问 内容")
        await storage.messages.append("u9", "c9", "assistant", f"第{i}答 内容")
    await SummaryStore(storage).put("u9:c9", "seed", "更早那 30 条的摘要正文")

    full = await SessionManager(storage, history_window=0).get_or_create("u9", "c9")
    check(len(full.messages) == 60 and full.history_dropped == 0,
          f"window=0 = 旧口径：全量拉（{len(full.messages)} 条，没截）")

    win = await SessionManager(storage, history_window=10).get_or_create("u9", "c9")
    check(win.history_window == 10 and len(win.messages) == 11,
          f"窗口=10 条：装进 10 条历史 + 1 条前缀摘要（{len(win.messages)}）")
    check(win.history_dropped == 50, f"回执报出截了多少条（{win.history_dropped}）")
    check(str(win.messages[0].get("content")).startswith("<history_summary>")
          and "更早那 30 条的摘要正文" in str(win.messages[0].get("content")),
          "被截的头部换成持久化的那条摘要，且正文写明「更早的 N 条已压缩」")
    check(render_history(win.messages) != render_history(full.messages)
          and len(render_history(win.messages)) < len(render_history(full.messages)),
          f"同一会话的 `<history>` 字节确实降了"
          f"（{len(render_history(win.messages))} < {len(render_history(full.messages))}）")

    # 进账：`context_ledger.sections` 里第一次有了 `history` 这一行。
    b = ContextBuilder("SYS")
    await b.build(win, "现在的问题")
    led = b.last_ledger
    names = [r["name"] for r in led["sections"]]
    check("history" in names, f"`<history>` 出现在账面里：{names}")
    check(next(r for r in led["sections"] if r["name"] == "history")["bytes"] > 0,
          "那一行的字节是真的（不是 0 占位）")
    check({"window", "dropped", "prefix_bytes", "bytes", "compress_at"} == set(led["history"]),
          f"窗口回执键齐（字段名刻意避开 budget，免得被当成断值）：{sorted(led['history'])}")
    check(led["history"]["dropped"] == 50 and led["history"]["window"] == 10,
          "回执里的数与装载时一致")
    # **断值表里没有它**：进了那张表就等于长对话在压缩来得及之前当场
    # `ContextBudgetError`（历史那一行是在压缩链上游记的账）——把「该压缩」
    # 写成「不许发」。这一条是 R10-5 的**偏离项**，记在指南 §9-Q136。
    from agent_framework.context import SECTION_BUDGET_BYTES
    check("history" not in SECTION_BUDGET_BYTES, "history 不在 ㉓ 的断值表里（只记账不判死）")
    check(led["over_budget"] == [], "长历史不因此把请求判死（这一轮 over_budget 为空）")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
