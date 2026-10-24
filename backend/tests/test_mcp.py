"""MCP 工具接入验证：配置加载 + 客户端协议 + 工具适配 + 白名单注册 + 端到端。

第 1–6 节用一个内存假**会话**（``FakeMCPSession``）跑，不打网络、不起子进程；
第 7 节拉起 ``tests/mcp_stub_server.py``（官方 FastMCP 写的真服务端）走一遍真 stdio。

为什么两套都要有：假会话验的是「本仓这边的接线对没对」，它对我们自己的接缝说话，
证不了协议那一层；T44 换掉的恰恰是协议那一层，所以必须有一支**真服务端**的样本。
（旧版第 7 节是拿注入 fetcher 验手写 StreamableHttpTransport 的报文细节——那些细节
连同被测的代码一起删了，不留测试给不存在的实现。）

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
    MCPSessionGone,
    MCPTool,
    MCPToolsConfig,
    _is_session_gone,
    _refused,
    connect_server,
    extract_text,
    load_mcp_config,
    parse_simple_yaml,
    register_mcp_tools,
)
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeMCPSession:
    """内存版 MCP **会话**（操作级替身）。

    迁移前它替的是「一个 JSON-RPC 服务端」，断言读的是 `tr.requests` 里报文的
    `params.arguments`。T44 把接缝挪到操作级之后，它替的东西也跟着变了：答的是
    「列出来是哪些工具」「这次调用返回什么」，记的是 **(工具名, 参数)**。

    形状跟着接缝走是有意的——替身不该比接缝更懂协议。它现在对 JSON-RPC 一无所知，
    这正是「报文已经交给官方库」在测试这一侧的证据（旧替身那 30 行报文拼装，
    连同被它验的那些细节，一起没了）。
    """

    def __init__(self, tools: list[dict], handlers: dict[str, Any]) -> None:
        self.tools = tools
        self.handlers = handlers
        self.calls: list[tuple[str, dict]] = []
        self.opens = 0
        self.resets = 0
        self.closed = False

    async def open(self, timeout: float) -> dict:
        self.opens += 1
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-server"}}

    async def list_tools(self, timeout: float) -> list[dict]:
        return list(self.tools)

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        self.calls.append((name, arguments))
        handler = self.handlers.get(name)
        if handler is None:
            # 服务端明确回绝 → MCPError（与官方 SDK 的 McpError 同义）：不重试，如实上报
            raise MCPError(f"调用 {name} 被服务端回绝：no tool {name}")
        return handler(arguments) if callable(handler) else handler

    async def reset(self) -> None:
        self.resets += 1

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


async def part7_real_stdio() -> None:
    """T44 的收口判据：换库之后，客户端还连得上一个**真的** MCP 服务端吗。

    第 1–6 节用的是假会话，它只能证明「我们这边的接线没错」——它对我们自己的接缝说话，
    证不了协议那一层。这一节拉起 ``tests/mcp_stub_server.py``（官方 FastMCP 写的服务端，
    真子进程、真 stdio、真 JSON-RPC），把 ``tools/list`` 与 ``tools/call`` 走完。

    顺带在这里把 T44 点名「必须原样保留的三件」各钉一条**真链路**断言：白名单按名字过滤、
    身份覆写盖掉模型自报的值、说明附注与两处 schema 映射还在。第 4/5/6 节验的是同一批
    性质，但那是在假会话上验的；这一节是它们在真正的协议往返上的第二次读法。
    """
    stub = str(Path(__file__).resolve().parent / "mcp_stub_server.py")
    cfg = MCPServerConfig(
        name="stub", type="stdio", command=sys.executable, args=[stub], tool_timeout=30,
        enabled_tools=["search_repositories", "create_issue", "whoami"])
    client = await connect_server(cfg)
    try:
        defs = {d["name"]: d for d in await client.list_tools()}
        check({"search_repositories", "delete_repo", "echo_args"} <= set(defs),
              f"真 stdio 服务端 tools/list 回来：{sorted(defs)}")

        # ③ 映射：inputSchema → parameters、readOnlyHint → read_only（在真 schema 上）
        search = MCPTool(client, defs["search_repositories"], timeout=30, name_prefix="stub_")
        check(search.parameters.get("required") == ["query"],
              "inputSchema → parameters 原样透传（真服务端给的 schema）")
        props = search.parameters.get("properties") or {}
        check(props.get("query", {}).get("type") == "string"
              and props.get("max_results", {}).get("type") == "integer",
              "parameters.properties 里的入参形状完整（含默认值那个）")

        out = await search.execute(query="hello", max_results=2)
        check(out == "found:hello x2", f"真 stdio tools/call 往返：{out!r}")

        # ③b 「可以自己定的前置」附注：给一份 explicit_deps，说明里就得多出那一行。
        # 口径 2026-10-16 换过一次：原先写「必须显式调用、不会被自动补齐」（那时漏了
        # 它会被硬拦），现在那几步照样按默认参数补齐，附注只提醒**选择权还在你手里**。
        noted = MCPTool(client, defs["create_issue"], timeout=30,
                        explicit_deps={"create_issue": ["generate_script"]})
        check("[可以自己定的前置]" in noted.description
              and "generate_script" in noted.description,
              "「哪个前置的选择权还没定」附注仍然生成")

        # ② 身份覆写：桩的 whoami 声明了 user_id / conversation_id，模型自报的一律不算
        who = MCPTool(client, defs["whoami"], timeout=30)
        with use_identity("u_real", "c_real"):
            echoed = await who.execute(query="q", user_id="victim", conversation_id="victim_c")
        check(echoed == "identity:u_real/c_real",
              f"身份覆写：模型自报被会话身份盖掉（服务端实收 {echoed!r}）")

        # 没声明身份键的工具不被塞陌生参数（桩的 echo_args 只收 a / b）
        echoed2 = await MCPTool(client, defs["echo_args"], timeout=30).execute(a="1", b="2")
        check(echoed2 == "args:a=1,b=2", f"未声明身份键的工具不被塞陌生参数（实得 {echoed2!r}）")

        # ① 白名单：注册进真 Registry，只应有白名单里那三个
        registry = ToolRegistry()
        names = await register_mcp_tools(registry, client, cfg)
        check(sorted(names) == ["stub_create_issue", "stub_search_repositories", "stub_whoami"],
              f"enabled_tools 白名单按名字过滤：{sorted(names)}")
        check(not registry.has("stub_delete_repo") and not registry.has("stub_echo_args"),
              "白名单外的工具没有进注册表")
        r = await registry.execute("stub_search_repositories", {"query": "real"})
        check("found:real" in str(r), "真 stdio 上 Registry.execute 命中 MCP 工具")

        # 服务端回 isError（未知工具）→ 以 Error: 前缀交回，而不是把异常炸到 Agent 循环里
        bad = await MCPTool(client, {"name": "no_such_tool", "inputSchema": {}}, timeout=30).execute()
        check(str(bad).startswith("Error:"), f"服务端 isError → Error 前缀（实得 {str(bad)[:48]!r}）")
    finally:
        await client.close()


class _GoneOnceSession(FakeMCPSession):
    """第一次 `call_tool` 回「会话没了」，`reset()` 之后再调就正常。

    复刻的就是真机那一幕：编辑器容器被重建 → 旧 mcp-session-id 失效 → SDK 收到
    HTTP 404，发出 `ErrorData(code=32600, message="Session terminated")`。

    **抛的是 SDK 的原样异常、走的是真传输同一条翻译路（`_refused`）**，不自己搓一个
    `MCPSessionGone` 出来：替身站的位置就是 `_SDKTransport`，而「把 McpError 翻成本仓
    异常、并认出会话失效」正是那一层的活儿。自搓的话，`_refused` 哪天退化了这条端到端
    断言照样绿——那这个守卫就白加了（第一次写它时就踩了这个坑）。
    """

    def __init__(self, tools: list[dict], handlers: dict[str, Any]) -> None:
        super().__init__(tools, handlers)
        self.gone = True

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        if self.gone:
            self.calls.append((name, arguments))     # 记一笔：服务端其实**没收到**这次调用
            raise _refused(
                McpError(ErrorData(code=32600, message="Session terminated")),
                f"调用 {name} 被服务端回绝") from None
        return await super().call_tool(name, arguments, timeout)

    async def reset(self) -> None:
        await super().reset()
        self.gone = False


async def part8_session_gone_reconnect() -> None:
    """会话被终止 ≠ 这次调用错了：要重连重发；真·回绝则**不许**重连。

    红法（改前）：`_SDKTransport` 把 32600 按普通 `McpError` 翻成 `MCPError`，而
    `MCPClient.call_tool` 的 `except MCPError: raise` 一动就把它原样抛出去——**永远不重连**。
    真机 run 9272c7f59501 就是死在这儿：模型连拿三次
    `调用 X 被服务端回绝：code=32600 message='Session terminated'`，
    然后整条创作链停死，只有重启 app 才救得回来（前端刷新没有用——死的是 app↔editor
    那条会话，不是前端那条）。
    """
    print("\n[8] 会话被终止 → 重连重发一次")
    # ① 检测这一层：对着**真 SDK 的类型**读，不靠字符串假装（先把判据本身钉住）
    gone = McpError(ErrorData(code=32600, message="Session terminated"))
    other = McpError(ErrorData(code=-32602, message="Unknown tool: nope"))
    check(_is_session_gone(gone) and not _is_session_gone(other),
          "认得出 32600/Session terminated，且不把普通回绝误认成它")
    check(isinstance(_refused(gone, "x"), MCPSessionGone)
          and not isinstance(_refused(other, "x"), MCPSessionGone),
          "回绝翻译分两路：会话没了 → MCPSessionGone；其它 → 普通 MCPError")

    # ② 重连这一层：真 client 跑一遍，会话要先被重建才拿得到结果。
    #    这里**接住异常**、不往上抛：退化时该看到的是三条 ✗ 加一句原文，而不是一整个
    #    traceback 把后面两条反向断言也一起吞掉（第一次写它时就那样，红是红了，读不出信息）。
    tr = _GoneOnceSession(TOOLS, HANDLERS)
    client = MCPClient("github", tr)
    try:
        out = await client.call_tool("search_repositories", {"query": "hi"}, timeout=5)
        why = ""
    except MCPError as exc:
        out, why = "", f"｜改动前那一幕：{str(exc)[:90]}"
    check(tr.resets == 1, f"会话失效时重建了一次会话（resets={tr.resets}）")
    check(tr.opens == 2, f"重建＝重新 initialize 而不是复用死会话（opens={tr.opens}）")
    check("found:hi" in str(out),
          f"重发之后这次调用真的成了（实得 {str(out)[:48]!r}{why}）")

    # ③ 反向：**普通回绝不许重连**。重发一百次也是同样的回绝，白握一次手；
    #    更要紧的是「回绝」与「会话没了」在处置上必须继续分开，别为了修这一条把那条抹平。
    tr2 = FakeMCPSession(TOOLS, HANDLERS)
    try:
        await MCPClient("github", tr2).call_tool("no_such_tool", {}, timeout=5)
        kind = "没抛"
    except MCPSessionGone:
        kind = "被误判成会话失效"
    except MCPError:
        kind = "MCPError（对）"
    check(kind == "MCPError（对）", f"未知工具仍是普通回绝、不当会话失效：{kind}")
    check(tr2.resets == 0 and tr2.opens == 1,
          f"普通回绝不重连、也不重建会话（resets={tr2.resets}, opens={tr2.opens}）")


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

    # ---- 3. 客户端协议：建会话 + list + call ----
    tr = FakeMCPSession(TOOLS, HANDLERS)
    client = MCPClient("github", tr)
    info = await client.initialize()
    check(tr.opens == 1 and info.get("serverInfo", {}).get("name") == "fake-server",
          "initialize 建会话并取回 serverInfo")

    defs = await client.list_tools()
    check([d["name"] for d in defs] == ["search_repositories", "create_issue", "delete_repo"], "tools/list 取回工具清单")

    res = await client.call_tool("search_repositories", {"query": "agents", "max_results": 3}, 10)
    check(extract_text(res) == "found:agents x3", "tools/call 结果抽取为文本")

    # initialize 幂等：第二次不再建会话
    await client.list_tools()
    check(tr.opens == 1, "initialize 幂等、只建一次会话")

    # 服务端明确回绝 → MCPError
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
    tr2 = FakeMCPSession(TOOLS, HANDLERS)
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
    tr3 = FakeMCPSession(TOOLS, HANDLERS)
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
    tr4 = FakeMCPSession(
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
    sent_args = [args for _name, args in tr4.calls]
    check(sent_args and sent_args[0].get("user_id") == "u"
          and sent_args[0].get("conversation_id") == "mcp",
          "Agent 运行中 MCP 调用自动带上当前会话身份")

    # ---- 6b. 身份注入：模型自报的身份一律被覆写，未声明的 server 不受影响 ----
    tr_f = FakeMCPSession(
        [{"name": "search_repositories", "description": "s", "inputSchema": identity_schema}],
        {"search_repositories": lambda a: {"content": [{"type": "text", "text": "ok"}]}})
    forged = MCPTool(MCPClient("gh", tr_f),
                     {"name": "search_repositories", "inputSchema": identity_schema}, timeout=5)
    await forged.execute(query="q", user_id="victim", conversation_id="victim_c")
    got = tr_f.calls[0][1]
    check(got["user_id"] == "victim" and got["conversation_id"] == "victim_c",
          "无请求上下文时不伪造身份：调用方给什么就传什么")
    with use_identity("u_real", "c_real"):
        await forged.execute(query="q", user_id="victim")
    got2 = tr_f.calls[1][1]
    check(got2["user_id"] == "u_real" and got2["conversation_id"] == "c_real",
          "有请求上下文时模型自报的 user_id 被覆写（跨会话越权封死）")
    tr_n = FakeMCPSession(
        [{"name": "search_repositories", "description": "s",
          "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}],
        {"search_repositories": lambda a: {"content": [{"type": "text", "text": "ok"}]}})
    plain = MCPTool(MCPClient("gh", tr_n),
                    {"name": "search_repositories",
                     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
                    timeout=5)
    with use_identity("u_real", "c_real"):
        await plain.execute(query="q")
    got3 = tr_n.calls[0][1]
    check(set(got3) == {"query"}, "未声明身份键的 MCP server 不会被塞进陌生参数")
    check(current_identity() is None, "上下文外 current_identity() 为空")

    # ---- 7. 真·stdio 全链路（换库之后「还连得上真 MCP 服务端吗」）----
    await part7_real_stdio()

    # ---- 8. 会话被服务端终止 → 重连重发一次（真机 run 9272c7f59501 的判据）----
    await part8_session_gone_reconnect()

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
