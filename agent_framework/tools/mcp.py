"""MCP（Model Context Protocol）工具接入：把存量服务暴露为 Agent 可用工具。

对应设计文档「基于现有服务的扩展——MCP」三个关键认知：
  1. 本模块只提供 MCP **客户端**，需配合实现了 MCP 协议的服务端才能工作。
  2. 需事先把 MCP 配置写入文件（JSON / YAML 子集），供 Agent 读取。
  3. 配置只给出 MCP Server；Server 提供的功能（工具名 / description / inputSchema）
     要由客户端连上 Server 调 tools/list 才能拿到——本模块正是做这件事。

链路：  MCPConfig(文件) → MCPClient(transport) → tools/list → 逐个包成 MCPTool 注册进 Registry
                                                                 tools/call → 结果回填 LLM

可测试 / 可换后端：MCPClient 只依赖注入的 transport（JSON-RPC 一问一答），验证脚本
用 FakeTransport 即可跑通，不需要真实 Server；生产用 stdio / streamableHttp transport。
零第三方依赖：JSON-RPC 用标准库手写；YAML 只实现配置所需的最小子集并做 ${ENV} 展开。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..editing_contract import EditingContract, NodeContract
from ..identity import current_identity
from ..tool import Tool


# --------------------------------------------------------------------------
# 配置模型（对应文档代码块 1：MCPServerConfig）
# --------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """单个 MCP Server 的连接与筛选信息。字段含义见设计文档表格。"""

    name: str = ""
    type: str | None = None            # "stdio" | "sse" | "streamableHttp"
    command: str = ""                  # stdio：执行的命令，如 "npx"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""                      # HTTP/SSE：服务端 URL
    headers: dict[str, str] = field(default_factory=dict)
    tool_timeout: int = 30             # 工具调用超时（秒）
    enabled_tools: list[str] = field(default_factory=lambda: ["*"])

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "MCPServerConfig":
        # 兼容 camelCase（enabledTools）与 snake_case 两种写法。
        enabled = d.get("enabled_tools") or d.get("enabledTools") or ["*"]
        return cls(
            name=name,
            type=d.get("type"),
            command=d.get("command", ""),
            args=list(d.get("args", [])),
            env=dict(d.get("env", {})),
            url=d.get("url", ""),
            headers=dict(d.get("headers", {})),
            tool_timeout=int(d.get("tool_timeout", d.get("toolTimeout", 30))),
            enabled_tools=list(enabled),
        )


@dataclass
class MCPToolsConfig:
    """顶层配置：一组以名字索引的 MCP Server。对应 YAML 的 tools.mcp_servers。"""

    servers: dict[str, MCPServerConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPToolsConfig":
        servers_raw = _dig(data, "tools", "mcp_servers") or _dig(data, "mcp_servers") or data.get("servers") or {}
        servers = {n: MCPServerConfig.from_dict(n, cfg) for n, cfg in servers_raw.items()}
        return cls(servers=servers)

    def list_servers(self) -> list[MCPServerConfig]:
        return list(self.servers.values())


def _dig(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


# --------------------------------------------------------------------------
# 配置文件加载：JSON 或 YAML 最小子集 + ${ENV} 展开
# --------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def load_mcp_config(path: str | os.PathLike[str]) -> MCPToolsConfig:
    """读取 MCP 配置文件：按扩展名选 JSON / YAML 子集解析。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = parse_simple_yaml(text)
    return MCPToolsConfig.from_dict(data)


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """解析配置所需的 YAML 最小子集：嵌套映射、块列表、行内列表 [a, b]、标量、#注释。

    不支持锚点、多行块标量、复杂 key —— 只覆盖 MCP 配置写法。值里的 ${VAR} 会做环境变量展开。
    """
    lines = [
        (len(ln) - len(ln.lstrip(" ")), ln.strip())
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    pos = 0

    def parse_scalar(tok: str) -> Any:
        tok = tok.strip()
        if tok.startswith("[") and tok.endswith("]"):
            inner = tok[1:-1].strip()
            if not inner:
                return []
            return [parse_scalar(x) for x in _split_flow(inner)]
        if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
            return tok[1:-1]
        low = tok.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("null", "~", ""):
            return None
        if re.fullmatch(r"-?\d+", tok):
            return int(tok)
        if re.fullmatch(r"-?\d+\.\d+", tok):
            return float(tok)
        return _expand_env(tok)

    def parse_block(indent: int) -> Any:
        nonlocal pos
        # 判断这是映射还是列表（以该缩进下的第一行决定）
        if pos >= len(lines):
            return {}
        first = lines[pos][1]
        if first.startswith("- "):
            return parse_list(indent)
        return parse_map(indent)

    def parse_map(indent: int) -> dict[str, Any]:
        nonlocal pos
        result: dict[str, Any] = {}
        while pos < len(lines):
            cur_indent, content = lines[pos]
            if cur_indent < indent or content.startswith("- "):
                break
            key, _, rest = content.partition(":")
            key = key.strip().strip("\"'")
            pos += 1
            if rest.strip():
                result[key] = parse_scalar(rest)
            else:
                # 看下一行的缩进决定是否进入子块
                if pos < len(lines) and lines[pos][0] > cur_indent:
                    result[key] = parse_block(lines[pos][0])
                elif pos < len(lines) and lines[pos][0] == cur_indent and lines[pos][1].startswith("- "):
                    result[key] = parse_list(cur_indent)
                else:
                    result[key] = None
        return result

    def parse_list(indent: int) -> list[Any]:
        nonlocal pos
        result: list[Any] = []
        while pos < len(lines):
            cur_indent, content = lines[pos]
            if cur_indent != indent or not content.startswith("- "):
                break
            item = content[2:].strip()
            pos += 1
            if ":" in item and not item.startswith("["):
                # "- key: value" → 视作内联映射的起始，回退成一个 map（含后续更深缩进行）
                result.append(_parse_inline_or_reindented(item, cur_indent))
            else:
                result.append(parse_scalar(item))
        return result

    def _parse_inline_or_reindented(item: str, indent: int) -> Any:
        # 简化：把 "- a: b" 当成单键映射，随后若有更浅于父但同缩进的内容交给上层处理。
        key, _, rest = item.partition(":")
        d = {key.strip().strip("\"'"): (parse_scalar(rest) if rest.strip() else None)}
        if not rest.strip() and pos < len(lines) and lines[pos][0] > indent:
            d[key.strip()] = parse_block(lines[pos][0])
        return d

    if not lines:
        return {}
    return parse_map(lines[0][0]) if not lines[0][1].startswith("- ") else parse_list(lines[0][0])


def _split_flow(inner: str) -> list[str]:
    # 按逗号切分行内列表，尊重引号内的逗号。
    parts: list[str] = []
    buf, quote = "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return parts


# --------------------------------------------------------------------------
# 传输层：JSON-RPC 一问一答的抽象 + 具体实现
# --------------------------------------------------------------------------

class MCPTransport(Protocol):
    """把一条 JSON-RPC 请求发往 Server 并取回响应 dict。"""

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    async def notify(self, payload: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class StdioTransport:
    """stdio：拉起本地 Server 子进程，按行分隔的 JSON-RPC 经 stdin/stdout 通信。"""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None) -> None:
        self._argv = [command, *args]
        self._env = env
        self._proc: Any = None

    async def start(self) -> None:
        full_env = dict(os.environ)
        if self._env:
            full_env.update({k: _expand_env(v) for k, v in self._env.items()})
        self._proc = await asyncio_create_subprocess(
            self._argv, full_env
        )

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        import asyncio

        if self._proc is None:
            await self.start()
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        if not line:
            raise RuntimeError("MCP stdio Server 关闭了输出流")
        return json.loads(line.decode("utf-8"))

    async def notify(self, payload: dict[str, Any]) -> None:
        if self._proc is None:
            await self.start()
        self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self._proc.terminate()
            self._proc = None


async def asyncio_create_subprocess(argv: list[str], env: dict[str, str]) -> Any:
    import asyncio

    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )


# HTTP 传输：可注入一个 async/sync 的 fetcher，默认用标准库 urllib，避免强依赖。
HttpFetcher = Callable[[str, dict[str, str], dict[str, Any], float], Any]


class StreamableHttpTransport:
    """streamableHttp：向 url POST 一条 JSON-RPC，取回 JSON 响应。

    遵守 MCP Streamable HTTP 规范：Accept 同时声明 json 与 SSE；initialize
    响应头里的 mcp-session-id 会携带在后续请求/通知中；notification 以无 id
    POST 投递（202 空响应）。fetcher 可注入：返回 dict 即纯 JSON-RPC 响应
    （测试替身兼容），返回 (dict, headers) 元组则同时回传响应头。
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 fetcher: HttpFetcher | None = None) -> None:
        self._url = url
        self._headers = {"content-type": "application/json",
                         "accept": "application/json, text/event-stream",
                         **(headers or {})}
        self._fetcher = fetcher or _urllib_fetcher
        self._session_id = ""

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        import asyncio

        headers = {k: _expand_env(v) for k, v in self._headers.items()}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if asyncio.iscoroutinefunction(self._fetcher):
            result = await self._fetcher(self._url, headers, payload, timeout)
        else:
            result = await asyncio.to_thread(
                self._fetcher, self._url, headers, payload, timeout)
        if isinstance(result, tuple):
            body, resp_headers = result
            lowered = {str(k).lower(): v for k, v in (resp_headers or {}).items()}
            if payload.get("method") == "initialize" and lowered.get("mcp-session-id"):
                self._session_id = lowered["mcp-session-id"]
            return body or {}
        return result or {}

    async def notify(self, payload: dict[str, Any]) -> None:
        # 通知必须真正送达（initialized 通知完成握手）；服务端回 202 空体。
        await self.request(payload, timeout=15)

    async def close(self) -> None:
        return None


def _parse_rpc_body(raw: str, ctype: str) -> dict[str, Any]:
    """JSON / SSE(text/event-stream) 双模响应体 → JSON-RPC 响应 dict。"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "text/event-stream" in ctype or raw.startswith(("event:", "data:")):
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                        return obj
        return {}
    return json.loads(raw)


def _urllib_fetcher(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> Any:
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read().decode("utf-8", "replace")
        ctype = (resp.headers.get("content-type") or "").lower()
        return _parse_rpc_body(raw, ctype), dict(resp.headers.items())


# --------------------------------------------------------------------------
# MCP 客户端：initialize / tools/list / tools/call
# --------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


def _is_timeout(exc: BaseException) -> bool:
    """这个异常是不是「等超时了」而不是「连接断了」。

    两者必须区别对待：连接断了重建会话重发一次是对的；**超时**重发等于让服务端
    把同一段长活（asr / 画面理解）整段再跑一遍——时间与 API 费用翻倍，
    用户等更久还拿不到结果。超时只该如实上报。
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text



class MCPClient:
    """对一个 MCP Server 的轻量客户端，只依赖注入的 transport。"""

    def __init__(self, server: str, transport: MCPTransport, *, client_name: str = "creation-assistant") -> None:
        self.server = server
        self._transport = transport
        self._client_name = client_name
        self._id = 0
        self._initialized = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        resp = await self._transport.request(payload, timeout=timeout)
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            raise MCPError(f"{method} 失败: {err.get('message', err)}")
        return resp.get("result") if isinstance(resp, dict) else None

    async def initialize(self, timeout: float = 30.0) -> dict[str, Any]:
        if self._initialized:
            return {}
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name, "version": "0.1"},
            },
            timeout,
        )
        await self._transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True
        return result or {}

    async def list_tools(self, timeout: float = 30.0) -> list[dict[str, Any]]:
        await self.initialize(timeout)
        result = await self._rpc("tools/list", {}, timeout)
        return (result or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        await self.initialize(timeout)
        try:
            return await self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout)
        except MCPError:
            raise
        except BaseException as exc:
            # 超时**不重试**：这不是「连接坏了」，而是「服务端还在算」。
            # 原先把它当断线处理——重新 initialize 再原样重发一次 tools/call，
            # 于是 asr / understand_clips 一旦超过 tool_timeout，就在服务端整段重跑一遍，
            # 时间和 API 费用直接翻倍，用户等更久还拿不到更早的结果。
            # 真超时该做的事是如实把错误交上去，由上层决定要不要换参数或降级。
            if _is_timeout(exc):
                raise MCPError(
                    f"{name} 调用超时（{timeout:g}s）：服务端仍在处理，"
                    f"不重发以免整段重跑。可改用更小的输入或稍后重试。") from exc
            # 真·传输异常：连接可能坏了，重建会话后重试一次。
            self._initialized = False
            if hasattr(self._transport, "_session_id"):
                self._transport._session_id = ""
            await self.initialize(timeout)
            return await self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout)

    async def close(self) -> None:
        await self._transport.close()


def extract_text(tool_result: Any) -> str:
    """把 MCP tools/call 的 content 块拼成文本；isError 时以 Error 前缀提示 Registry。"""
    if isinstance(tool_result, dict):
        blocks = tool_result.get("content")
        texts: list[str] = []
        if isinstance(blocks, list):
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(str(b.get("text", "")))
                elif isinstance(b, str):
                    texts.append(b)
        body = "\n".join(t for t in texts if t)
        if tool_result.get("isError"):
            return f"Error: MCP 工具返回错误: {body or '(无内容)'}"
        return body if body else json.dumps(tool_result, ensure_ascii=False)
    return tool_result if isinstance(tool_result, str) else str(tool_result)


# --------------------------------------------------------------------------
# Tool 适配：把一个 MCP 工具变成 Registry 可注册的 Tool
# --------------------------------------------------------------------------

class MCPTool(Tool):
    """包装某个 MCP Server 暴露的工具：schema 来自 tools/list，execute 走 tools/call。

    剪辑节点工具会带一份 ``contract``（服务端 ``dag_contract`` 的一个节点），
    读写集据此声明——Agent 循环因此知道哪两个节点调用可以同批并发。没有契约
    （非剪辑 server / 契约未取到）时读写集为空，按「证明不了独立就不同批」处理。

    中文名走 MCP 标准的 ``title`` 字段：Storyline 注册节点时把 ``BaseNode.display_name``
    填进去，这里读回来——服务端仍是节点元信息的唯一作者，主服务不再抄第二份词表。
    """

    def __init__(self, client: MCPClient, tool_def: dict[str, Any], *, timeout: float,
                 name_prefix: str | None = None,
                 contract: NodeContract | None = None,
                 explicit_deps: Mapping[str, Sequence[str]] | None = None) -> None:
        self._client = client
        self._raw_name = tool_def["name"]
        self._title = str(tool_def.get("title") or "")
        self._desc = tool_def.get("description", "")
        self._schema = tool_def.get("inputSchema") or {"type": "object", "properties": {}}
        self._timeout = timeout
        self._contract = contract
        # 契约里 require_explicit_call 的节点名 → 它**不允许被自动补齐**的下游集合。
        # 用来在工具说明里提前写清「哪些前置必须你自己显式调用」——见 description 的说明。
        self._explicit_deps = explicit_deps or {}
        annotations = tool_def.get("annotations") or {}
        self._read_only = bool(annotations.get("readOnlyHint", False))
        self._name = f"{name_prefix}{self._raw_name}" if name_prefix else self._raw_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._title

    @property
    def raw_name(self) -> str:
        return self._raw_name

    @property
    def description(self) -> str:
        """工具说明；剪辑节点额外附一行「必须先显式调用谁」。

        为什么要把这件事写进说明：`script_template_rec`／`group_clips`／`filter_clips`／
        `transition_rec`／`text_rec` 这几个节点的前置**不允许被自动补齐**（它们要的是
        创意决策参数，服务端替不了）。原先模型只有撞上去才知道——真机实测：
        模型直接调 `select_BGM`，服务端返回「脚本模板推荐 需要你直接调用…」，
        它才回头补，白烧一轮；重试一次还可能再撞另一个。
        把「哪些前置得你自己点名」提前写在说明里，模型第一次调用就能按对顺序来。
        """
        base = self._desc or f"MCP tool {self._raw_name} on {self._client.server}"
        blocking = self._explicit_deps.get(self._raw_name) or ()
        if not blocking:
            return base
        return (f"{base}\n"
                f"[必须显式调用] 这些前置节点不会被自动补齐，请在你真正调用本节点之前"
                f"先逐个调用它们（并传入创意决策参数）：{'、'.join(blocking)}。")

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def contract(self) -> NodeContract | None:
        return self._contract

    @property
    def reads(self) -> frozenset[str]:
        return self._contract.reads if self._contract else frozenset()

    @property
    def writes(self) -> frozenset[str]:
        return self._contract.writes if self._contract else frozenset()

    async def execute(self, **kwargs: Any) -> Any:
        ident = current_identity()
        if ident is not None:
            props = self._schema.get("properties") or {}
            # 只补服务器声明认识的键：别的 MCP server 收到未知参数会直接报错
            if "user_id" in props:
                kwargs["user_id"] = ident.user_id       # 覆写：模型自报的身份不作数
            if "conversation_id" in props:
                kwargs["conversation_id"] = ident.conversation_id
            # 分叉重跑带着新的产物作用域：不注入的话服务端仍写回 '_default'，
            # 上游复用与「不污染父版本」两条语义都会落空。
            if "artifact_id" in props and ident.artifact_id:
                kwargs["artifact_id"] = ident.artifact_id
        result = await self._client.call_tool(self._raw_name, kwargs, self._timeout)
        return extract_text(result)


def _matches(name: str, patterns: list[str]) -> bool:
    if "*" in patterns:
        return True
    return name in patterns


async def connect_server(config: MCPServerConfig, transport: MCPTransport | None = None) -> MCPClient:
    """按配置建立到某个 MCP Server 的客户端；transport 为空时据 type 选默认传输。"""
    if transport is None:
        transport = _default_transport(config)
    return MCPClient(config.name, transport)


def _default_transport(config: MCPServerConfig) -> MCPTransport:
    if config.type == "stdio" or (config.command and not config.url):
        return StdioTransport(config.command, config.args, config.env)
    return StreamableHttpTransport(config.url, config.headers)


async def register_mcp_tools(
    registry: Any,
    client: MCPClient,
    config: MCPServerConfig,
    *,
    name_prefix: bool = True,
    timeout: float | None = None,
    contract: EditingContract | None = None,
) -> list[str]:
    """连接 → tools/list → 按 enabled_tools 白名单筛选 → 逐个包成 MCPTool 注册。返回注册名。

    ``contract`` 是剪辑服务端 ``dag_contract`` 取回的那份（见 ``editing_contract``）：
    只影响 Agent 侧的并发分批与分叉作废，不改变工具对模型暴露的样子。
    """
    tool_timeout = float(timeout if timeout is not None else config.tool_timeout)
    defs = await client.list_tools(timeout=tool_timeout)
    prefix = f"{config.name}_" if name_prefix and config.name else ""
    blocking = _explicit_deps_map(contract)
    registered: list[str] = []
    for td in defs:
        if not isinstance(td, dict) or "name" not in td:
            continue
        if not _matches(td["name"], config.enabled_tools):
            continue
        tool = MCPTool(client, td, timeout=tool_timeout, name_prefix=prefix or None,
                       contract=(contract.get(td["name"]) if contract else None),
                       explicit_deps=blocking)
        registry.register(tool)
        registered.append(tool.name)
    return registered


def _explicit_deps_map(contract: EditingContract | None) -> dict[str, list[str]]:
    """节点 → 「它依赖的、且不允许被自动补齐」的那些前置节点名。

    只用于把这件事写进工具说明（见 ``MCPTool.description``），不改变任何执行语义：
    拦截器该拒绝还是拒绝，只是模型现在**提前**知道了。

    遍历每个节点的依赖闭包，因为 model 直接调的是下游（如 ``select_BGM``），
    而挡住它的是再上一层（``generate_script`` → ``script_template_rec``）。
    只写直接依赖会让模型仍然撞墙。
    """
    if contract is None:
        return {}
    nodes = getattr(contract, "nodes", None) or {}
    explicit = {n for n, c in nodes.items() if getattr(c, "explicit", False)}
    if not explicit:
        return {}
    out: dict[str, list[str]] = {}
    for name, c in nodes.items():
        found: list[str] = []
        seen: set[str] = set()
        stack = list(getattr(c, "requires", ()) or ())
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            if dep in explicit:
                found.append(dep)
                continue                     # 它本身就要显式调用，不必再往下挖
            dc = nodes.get(dep)
            if dc is not None:
                stack.extend(getattr(dc, "requires", ()) or ())
        if found:
            # 按依赖由浅入深排，读起来就是「先调谁、再调谁」
            out[name] = sorted(found, key=len)
    return out
