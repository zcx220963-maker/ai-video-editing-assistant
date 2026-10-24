# -*- coding: utf-8 -*-
"""联网检索 / 抓取接 DeepSeek 官方 Search / Fetch（2026-10-18）。

证的是五件事：
① `deepseek_provider` 把官方返回解析成检索结果（长摘要给模型读、带日期），请求体合规
   （query ≤128 字、count 1~50、Bearer 鉴权）；
② 官方说明的重试规矩：408 / 429 / 5xx 等一下再试，402 余额不足、401 密钥无效直接报，
   其余 4xx 不重试；
③ 检索工具：沿用模型密钥那一路出岔子（限流、服务端错）退回免 key 回落链，402 / 401 照实报；
④ 抓取工具：官方 Fetch 成功就回 Markdown 正文并带计费行；没走成就退回直接抓；
⑤ 计费行能被主循环认出来记进预算。

跑法：``cd backend && PYTHONIOENCODING=utf-8 python tests/test_deepseek_web.py``
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework import agent as agent_mod                      # noqa: E402
from agent_framework.run_state import RunState                       # noqa: E402
from agent_framework.tools import web                                # noqa: E402

_fails = 0
_checks = 0


def check(cond: bool, label: str) -> None:
    global _fails, _checks
    _checks += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _fails += 1


class FakeHTTP:
    """按顺序回一串 (状态码, JSON 对象)；记下每次请求。"""

    def __init__(self, *replies: tuple[int, dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, url, max_bytes, timeout, *, headers=None, data=None):
        self.calls.append({"url": url, "headers": headers or {},
                           "body": json.loads(data.decode()) if data else None})
        status, obj = self.replies.pop(0)
        return web._Fetched(status=status, content_type="application/json",
                            body=json.dumps(obj, ensure_ascii=False).encode(), truncated=False)


def case_provider() -> None:
    print("① 解析官方 Search 返回、请求体合规")
    http = FakeHTTP((200, {"results": [
        {"title": "薄肌<b>理论</b>", "url": "https://a.example/1", "snippet": "短",
         "summary": "长摘要给模型读", "date": "2026-08-17T00:00:00+00:00"},
        {"title": "第二条", "url": "https://a.example/2", "snippet": "只有短摘要", "date": None}]}))
    rows = web.deepseek_provider("薄肌" * 100, 80, transport=http, key="sk-test")
    body, hdr = http.calls[0]["body"], http.calls[0]["headers"]
    check(http.calls[0]["url"] == "https://api.deepseek.com/v1/tools/search", "打的是官方 Search 地址")
    check(hdr.get("Authorization") == "Bearer sk-test", "Bearer 鉴权，与模型 API 同一把 key")
    check(len(body["query"]) == 128 and body["count"] == 50,
          "query 截到 128 字、count 钳在 50（官方上限）")
    check(rows[0].title == "薄肌理论" and rows[0].snippet == "（2026-08-17）长摘要给模型读",
          f"取长摘要、带日期、去标签：{rows[0].title} / {rows[0].snippet}")
    check(rows[1].snippet == "只有短摘要" and rows[0].backend == "DeepSeek",
          "没长摘要退回短摘要；后端名标 DeepSeek")


def case_retry() -> None:
    print("② 重试规矩：429/5xx 再试，402/401/其余 4xx 不重试")
    import time as _t
    real_sleep, _t.sleep = _t.sleep, (lambda s: None)
    try:
        http = FakeHTTP((429, {"error": {"message": "slow down"}}),
                        (503, {"error": {"message": "busy"}}), (200, {"results": []}))
        out = web._deepseek_call("/v1/tools/search", {"query": "x"}, "k", 5, http)
        check(out == {"results": []} and len(http.calls) == 3, "429、503 各等一下再试，第三次成了")
        for status, want in ((402, "余额不足"), (401, "无效"), (400, "bad count")):
            http = FakeHTTP((status, {"error": {"message": "bad count"}}))
            try:
                web._deepseek_call("/v1/tools/search", {"query": "x"}, "k", 5, http)
                got = None
            except web.DeepSeekToolError as e:
                got = e
            check(got is not None and got.status == status and want in str(got)
                  and len(http.calls) == 1, f"HTTP {status}：不重试，直接报「{want}」")
    finally:
        _t.sleep = real_sleep


def case_search_tool() -> None:
    print("③ 检索工具：官方出岔子退回免费链；402 照实报；成功带计费行")
    real_auto, real_key = web.auto_provider, web.deepseek_auto_key
    real_call = web._deepseek_call

    async def fake_key():
        return "sk-test"
    try:
        web.deepseek_auto_key = fake_key
        web.auto_provider = lambda q, n, t=20.0, tp=None: [
            web.SearchResult(title="维基", url="https://zh.wikipedia.org/x", snippet="s",
                             backend="wikipedia")]
        tool = web.SearchTool()

        def boom(status):
            def _f(*a, **k):
                raise web.DeepSeekToolError(status, "x")
            return _f
        web._deepseek_call = boom(503)
        out = asyncio.run(tool.execute("薄肌"))
        check("已改用免 key 回落链" in out and "维基" in out and "node_cost_yuan" not in out,
              "官方 503（重试用完）：退回免费链出结果，不计费")
        web._deepseek_call = boom(402)
        out = asyncio.run(tool.execute("薄肌"))
        check(out.startswith("Error:") and "维基" not in out, "402 余额不足：照实报，不悄悄换后端")
        web._deepseek_call = lambda *a, **k: {"results": [
            {"title": "t", "url": "https://a.example/1", "summary": "s"}]}
        out = asyncio.run(tool.execute("薄肌"))
        check("[检索后端：DeepSeek]" in out and "node_cost_yuan=0.005" in out,
              "成功：标出后端、带计费行（5 元 / 千次）")
    finally:
        web.auto_provider, web.deepseek_auto_key, web._deepseek_call = real_auto, real_key, real_call


def case_fetch_tool() -> None:
    print("④ 抓取工具：官方 Fetch 回 Markdown；没走成退回直接抓")
    real_key, real_call = web.deepseek_auto_key, web._deepseek_call

    async def fake_key():
        return "sk-test"
    try:
        web.deepseek_auto_key = fake_key
        web._deepseek_call = lambda path, body, key, timeout, tp=None: {
            "title": "薄肌理论", "content": "正文第一段\n\n正文第二段"}
        tool = web.FetchTool()
        out = asyncio.run(tool.execute("https://example.com/a"))
        check(out.startswith("[200·DeepSeek Fetch]") and "# 薄肌理论" in out
              and "正文第二段" in out and "node_cost_yuan=0.005" in out,
              "官方 Fetch：回标题 + Markdown 正文 + 计费行")

        def fail(*a, **k):
            raise web.DeepSeekToolError(404, "no content")
        web._deepseek_call = fail

        async def direct(self, url, max_bytes):
            return "[200] 直接抓到的"
        real_direct, web.FetchTool._direct_fetch = web.FetchTool._direct_fetch, direct
        try:
            out = asyncio.run(tool.execute("https://example.com/a"))
        finally:
            web.FetchTool._direct_fetch = real_direct
        check("DeepSeek 抓取没成：HTTP 404" in out and "直接抓到的" in out,
              "官方 404：说明一句，退回直接抓")
        injected = web.FetchTool(transport=lambda *a, **k: None)
        check(asyncio.run(injected._deepseek_fetch("https://example.com/a", 100)) == ("", ""),
              "注入了 transport（离线用例）时不打官方接口")
    finally:
        web.deepseek_auto_key, web._deepseek_call = real_key, real_call


def case_charge() -> None:
    print("⑤ 计费行记进这条 run 的预算")
    st = RunState(budget_yuan=3.0)
    msgs = [{"role": "tool", "content": "[检索后端：DeepSeek]\n- t\n[计费 node_cost_yuan=0.005]"},
            {"role": "tool", "content": "[200·DeepSeek Fetch] u\n正文\n[计费 node_cost_yuan=0.005]"},
            {"role": "tool", "content": "[检索后端：免 key 回落链]\n- t"}]
    agent_mod._charge_nodes(st, msgs)
    check(abs(st.spent_yuan - 0.010) < 1e-9, f"两次官方调用记 0.01 元，免费那次不记（实得 {st.spent_yuan}）")


def main() -> int:
    for case in (case_provider, case_retry, case_search_tool, case_fetch_tool, case_charge):
        case()
    print(f"\n{_checks - _fails}/{_checks} 通过")
    return 0 if _fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
