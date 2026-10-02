"""MCP 工具接入验证（不联网）：配置加载 + 客户端协议 + 工具适配 + 白名单注册 + 端到端。

用一个内存 FakeTransport 模拟实现了 MCP 协议的服务端，验证脚本不打真实网络。

运行：  python tests/test_mcp.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentConfig, AgentOnceRun
from agent_framework.context import ContextBuilder
from agent_framework.identity import current_identity, use_identity
from agent_framework.llm import ScriptedLLM
from agent_framework.messages import ToolCall
from agent_framework.session import Session
from agent_framework.tool import ToolRegistry
from agent_framework.tools.mcp import (
    MCPClient,
    MCPError,
    MCPServerConfig,
    MCPTool,
    MCPToolsConfig,
    extract_text,
    load_mcp_config,
    parse_simple_yaml,
    register_mcp_tools,
)

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeMCPServerTransport:
    """内存版 MCP 服务端：按 JSON-RPC 约定应答 initialize / tools/list / tools/call。"""

    def __init__(self, tools: list[dict], handlers: dict[str, Any]) -> None:
        self.tools = tools
        self.handlers = handlers
        self.requests: list[dict] = []
        self.notifications: list[dict] = []
        self.closed = False

    async def request(self, payload: dict, timeout: float) -> dict:
        self.requests.append(payload)
        method = payload.get("method")
        params = payload.get("params", {})
        rid = payload.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-server"},
            }}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": self.tools}}
        if method == "tools/call":
            name = params["name"]
            handler = self.handlers.get(name)
            if handler is None:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {name}"}}
            out = handler(params["arguments"]) if callable(handler) else handler
            return {"jsonrpc": "2.0", "id": rid, "result": out}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown {method}"}}

    async def notify(self, payload: dict) -> None:
        self.notifications.append(payload)

    async def close(self) -> None:
        self.closed = True


TOOLS = [
    {
        "name": "search_repositories",
        "description": "Search GitHub repositories",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_issue",
        "description": "Open an issue",
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
    },
    {
        "name": "delete_repo",
        "description": "Delete a repo (danger)",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
]

HANDLERS = {
    "search_repositories": lambda a: {"content": [{"type": "text", "text": f"found:{a['query']} x{a.get('max_results',10)}"}]},
    "create_issue": lambda a: {"content": [{"type": "text", "text": f"opened:{a['title']}"}]},
    "delete_repo": lambda a: {"content": [{"type": "text", "text": f"deleted:{a['name']}"}], "isError": True},
}


async def main() -> None:
    # ---- 1. 配置加载：JSON ----
    with tempfile.TemporaryDirectory() as td:
        jf = Path(td) / "mcp.json"
        jf.write_text(json.dumps({
            "tools": {"mcp_servers": {
                "internal_api": {
                    "type": "streamableHttp",
                    "url": "http://mcp-internal/mcp",
                    "tool_timeout": 60,
                    "enabledTools": ["query_employee", "search_docs"],
                }
            }}
        }), encoding="utf-8")
        cfg = load_mcp_config(jf)
        s = cfg.servers["internal_api"]
        check(s.type == "streamableHttp" and s.tool_timeout == 60 and s.enabled_tools == ["query_employee", "search_docs"],
              "JSON 配置：server / tool_timeout / enabledTools 解析")

    # ---- 2. 配置加载：YAML 子集 ----
    ycfg = MCPServerConfig.from_dict("github", parse_simple_yaml(
        'type: stdio\ncommand: npx\nargs: ["-y", "server-github"]\n'
        'env:\n  GITHUB_TOKEN: "${GITHUB_TOKEN}"\nenabledTools: ["*"]\n'
    ))
    check(ycfg.command == "npx" and ycfg.args == ["-y", "server-github"] and ycfg.enabled_tools == ["*"],
          "YAML 子集：命令 / 行内列表 / 白名单解析")

    # ---- 3. 客户端协议：initialize 握手 + list + call ----
    tr = FakeMCPServerTransport(TOOLS, HANDLERS)
    client = MCPClient("github", tr)
    info = await client.initialize()
    methods = [p["method"] for p in tr.requests]
    check(methods == ["initialize"] and info["serverInfo"]["name"] == "fake-server", "initialize 先握手并取回 serverInfo")
    check(tr.notifications and tr.notifications[0]["method"] == "notifications/initialized",
          "握手后发送 initialized 通知")

    defs = await client.list_tools()
    check([d["name"] for d in defs] == ["search_repositories", "create_issue", "delete_repo"], "tools/list 取回工具清单")

    res = await client.call_tool("search_repositories", {"query": "agents", "max_results": 3}, 10)
    check(extract_text(res) == "found:agents x3", "tools/call 结果抽取为文本")

    # initialize 幂等：第二次不再重发握手
    await client.list_tools()
    check(sum(1 for p in tr.requests if p["method"] == "initialize") == 1, "initialize 幂等、只握一次手")

    # 错误响应 → MCPError
    try:
        await client.call_tool("nope", {}, 10)
        check(False, "未知工具应抛 MCPError")
    except MCPError:
        check(True, "未知工具抛 MCPError")

    # ---- 4. MCPTool 适配：schema 透传 + read_only + isError ----
    mtool = MCPTool(client, TOOLS[0], timeout=10, name_prefix="github_")
    check(mtool.name == "github_search_repositories" and mtool.raw_name == "search_repositories",
          "注册名带前缀、调用仍用原始名")
    check(mtool.parameters["required"] == ["query"] and mtool.description.startswith("Search GitHub"),
          "inputSchema / description 透传给 LLM")
    check(mtool.read_only is True and mtool.concurrency_safe is True, "readOnlyHint → 可并发")
    out = await mtool.execute(query="x", max_results=5)
    check(out == "found:x x5", "MCPTool.execute 走 tools/call")

    err_tool = MCPTool(client, TOOLS[2], timeout=10)
    e_out = await err_tool.execute(name="r")
    check(e_out.startswith("Error:"), "isError 结果转为 Error 前缀供 Registry 提示")

    await client.close()
    check(tr.closed, "close 关闭底层 transport")

    # ---- 5. 白名单注册进 ToolRegistry ----
    cfg_github = MCPServerConfig(name="github", type="stdio", command="npx", enabled_tools=["search_repositories", "create_issue"])
    tr2 = FakeMCPServerTransport(TOOLS, HANDLERS)
    client2 = MCPClient("github", tr2)
    registry = ToolRegistry()
    names = await register_mcp_tools(registry, client2, cfg_github)
    check(sorted(names) == ["github_create_issue", "github_search_repositories"], f"只注册白名单工具: {names}")
    check(not registry.has("github_delete_repo"), "白名单外工具未注册")
    # Registry 端到端按 inputSchema 校验并执行
    r = await registry.execute("github_search_repositories", {"query": "hello"})
    check("found:hello" in str(r), "Registry.execute 命中 MCP 工具")
    miss = await registry.execute("github_create_issue", {})
    check(str(miss).startswith("Error: missing required"), "缺必填参数被 Registry 拦截")

    # "*" 全量注册
    tr3 = FakeMCPServerTransport(TOOLS, HANDLERS)
    all_reg = ToolRegistry()
    await register_mcp_tools(all_reg, MCPClient("gh", tr3), MCPServerConfig(name="gh", enabled_tools=["*"]))
    check(len(all_reg) == 3, "enabled_tools=['*'] 注册全部工具")

    # ---- 6. 端到端：Agent ReAct 经 MCP 工具 Search→Answer ----
    llm = ScriptedLLM([
        ("tool", "gh_search_repositories", {"query": "video editing"}),
        ("answer", "已找到相关仓库"),
    ])
    identity_schema = {"type": "object", "properties": {
        "query": {"type": "string"},
        "user_id": {"type": "string"},
        "conversation_id": {"type": "string"},
    }, "required": ["query"]}
    tr4 = FakeMCPServerTransport(
        [{"name": "search_repositories", "description": "s", "inputSchema": identity_schema}],
        {"search_repositories": lambda a: {"content": [
            {"type": "text", "text": "hit:" + a["query"]}]}})
    reg4 = ToolRegistry()
    await register_mcp_tools(reg4, MCPClient("gh", tr4), MCPServerConfig(name="gh", enabled_tools=["*"]))
    runner = AgentOnceRun(llm, reg4, ContextBuilder("你是创作助手"), config=AgentConfig(max_iterations=5))
    sess = Session(user_id="u", conversation_id="mcp")
    ans = await runner.run(sess, "帮我找视频剪辑相关仓库")
    check(ans == "已找到相关仓库", "Agent 端到端经 MCP 工具完成 ReAct")
    fed = llm.calls[1][-1]  # 第二次调用前，回填的工具结果
    check(fed["role"] == "tool" and "hit:video editing" in fed["content"], "MCP 工具结果回填进上下文")
    sent_args = [p["params"]["arguments"] for p in tr4.requests if p.get("method") == "tools/call"]
    check(sent_args and sent_args[0].get("user_id") == "u"
          and sent_args[0].get("conversation_id") == "mcp",
          "Agent 运行中 MCP 调用自动带上当前会话身份")

    # ---- 6b. 身份注入：模型自报的身份一律被覆写，未声明的 server 不受影响 ----
    tr_f = FakeMCPServerTransport(
        [{"name": "search_repositories", "description": "s", "inputSchema": identity_schema}],
        {"search_repositories": lambda a: {"content": [{"type": "text", "text": "ok"}]}})
    forged = MCPTool(MCPClient("gh", tr_f),
                     {"name": "search_repositories", "inputSchema": identity_schema}, timeout=5)
    await forged.execute(query="q", user_id="victim", conversation_id="victim_c")
    got = [p["params"]["arguments"] for p in tr_f.requests if p.get("method") == "tools/call"][0]
    check(got["user_id"] == "victim" and got["conversation_id"] == "victim_c",
          "无请求上下文时不伪造身份：调用方给什么就传什么")
    with use_identity("u_real", "c_real"):
        await forged.execute(query="q", user_id="victim")
    got2 = [p["params"]["arguments"] for p in tr_f.requests if p.get("method") == "tools/call"][1]
    check(got2["user_id"] == "u_real" and got2["conversation_id"] == "c_real",
          "有请求上下文时模型自报的 user_id 被覆写（跨会话越权封死）")
    tr_n = FakeMCPServerTransport(
        [{"name": "search_repositories", "description": "s",
          "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}],
        {"search_repositories": lambda a: {"content": [{"type": "text", "text": "ok"}]}})
    plain = MCPTool(MCPClient("gh", tr_n),
                    {"name": "search_repositories",
                     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
                    timeout=5)
    with use_identity("u_real", "c_real"):
        await plain.execute(query="q")
    got3 = [p["params"]["arguments"] for p in tr_n.requests if p.get("method") == "tools/call"][0]
    check(set(got3) == {"query"}, "未声明身份键的 MCP server 不会被塞进陌生参数")
    check(current_identity() is None, "上下文外 current_identity() 为空")

    # ---- 7. StreamableHttpTransport 规范细节（注入 fetcher，不联网）----
    from agent_framework.tools.mcp import StreamableHttpTransport, _parse_rpc_body

    sent: list[tuple[dict, dict]] = []

    def sse_fetcher(url: str, headers: dict, payload: dict, timeout: float):
        sent.append((payload, dict(headers)))
        rid = payload.get("id")
        if payload.get("method") == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "x", "capabilities": {}}}, \
                {"mcp-session-id": "sess-42", "content-type": "application/json"}
        if payload.get("method") == "tools/list":
            # 服务端以 SSE 事件流回 JSON-RPC 响应；按 _urllib_fetcher 同样的方式解析
            raw = 'event: message\ndata: {"jsonrpc":"2.0","id":%s,"result":{"tools":[]}}\n\n' % rid
            return _parse_rpc_body(raw, "text/event-stream"), {"content-type": "text/event-stream"}
        return "", {"content-type": "application/json"}  # 202 空响应

    http_tr = StreamableHttpTransport("http://srv/mcp", fetcher=sse_fetcher)
    hclient = MCPClient("srv", http_tr)
    await hclient.list_tools()
    init_payload, init_headers = sent[0]
    check("text/event-stream" in init_headers.get("accept", "") and "application/json" in init_headers.get("accept", ""),
          "HTTP 请求声明 json + event-stream 双 Accept")
    notif = sent[1]
    check("id" not in notif[0] and notif[0]["method"] == "notifications/initialized",
          "initialized 通知以无 id POST 真正投递")
    check(notif[1].get("mcp-session-id") == "sess-42", "initialize 后携带 mcp-session-id 头")
    list_headers = sent[2][1]
    check(list_headers.get("mcp-session-id") == "sess-42" and http_tr._session_id == "sess-42",
          "后续请求继续携带会话头")
    check(any(p["method"] == "tools/list" for p, _ in sent), "SSE data: 行解析成 JSON-RPC 响应并走通 tools/list")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
