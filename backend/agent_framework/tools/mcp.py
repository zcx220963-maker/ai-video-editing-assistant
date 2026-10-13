"""MCP（Model Context Protocol）工具接入：把存量服务暴露为 Agent 可用工具。

对应设计文档「基于现有服务的扩展——MCP」三个关键认知：
  1. 本模块只提供 MCP **客户端**，需配合实现了 MCP 协议的服务端才能工作。
  2. 需事先把 MCP 配置写入文件（JSON / YAML 子集），供 Agent 读取。
  3. 配置只给出 MCP Server；Server 提供的功能（工具名 / description / inputSchema）
     要由客户端连上 Server 调 tools/list 才能拿到——本模块正是做这件事。

链路：  MCPConfig(文件) → MCPClient(transport) → tools/list → 逐个包成 MCPTool 注册进 Registry
                                                                 tools/call → 结果回填 LLM

可测试 / 可换后端：MCPClient 只依赖注入的 transport（**操作级**接缝：open / list_tools /
call_tool / reset / close），验证脚本用假会话即可跑通，不需要真实 Server；生产的三种
transport（stdio / streamableHttp / sse）都是官方 `mcp` 包的薄适配（R10-T44）。

本仓仍然自持的只剩**配置**那一侧：YAML 最小子集与 ${ENV} 展开（它为什么不换库、金样在哪，
见 §3.6 与 tests/test_handrolled_goldens.py）。**协议侧不再手写**——JSON-RPC 报文、
SSE 的 `data:` 行、mcp-session-id 的来回传递，全部交给官方客户端（R10-T44 之前它们都
是本仓的代码，这也正是 P30 记的那笔账）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Protocol, Sequence

# 官方 MCP 客户端（R10-T44）。**这三个传输入口就是「协议侧不再手写」的全部依据**：
# 报文、SSE 解析、会话头都在它们肚子里。
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.types import Implementation

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
# 传输层：官方 mcp 包的薄适配（R10-T44）
# --------------------------------------------------------------------------
#
# 迁移之前，这一节是本仓**手写**的 JSON-RPC：自己拼 initialize / tools/list /
# tools/call 的报文、自己扫 SSE 的 `data:` 行、自己把 mcp-session-id 传回去。
# 这类手写件会与官方实现各自演进（P30 记的就是这笔账），所以改成官方 `mcp` 包
# 客户端那一半的薄适配层。
#
# **换掉的是接缝底下的实现，不是接缝上面那三件。** 下面这个 Protocol 从「JSON-RPC
# 报文级」改成「操作级」——报文既然交给官方库去拼，接缝上就没法再谈报文了。而
# MCPTool 拿到的工具定义、execute 的身份覆写、enabled_tools 白名单，全都在上面那一层，
# 一行未动：适配层把 SDK 的 pydantic 对象 dump 回**与迁移前逐键相同**的 dict。
#
# 一条硬约束写在最前面（探针量出来的，不是设计偏好）：SDK 的 client 上下文管理器是
# anyio 取消作用域，**在哪个任务进就得在哪个任务出**——跨任务退出会当场
#   RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
# 而本仓的连接发生在**装配任务**、工具调用发生在 **agent run 任务**，两者不是同一个。
# 跨任务**调用** session.* 本身是安全的（探针：六问并发无串台；外层 wait_for 掐断一次
# 之后会话仍然可用）。于是形状只剩一种：
#
#     一个长驻任务自持 SDK 上下文（自己进、自己出），别的任务只调 session.*。
#
# 这不是为了好看——它是唯一能同时满足「用官方库」和「本仓连接/调用跨任务」的形状。

#: 建连 + 握手（``open``）的默认上限（秒）。
CONNECT_TIMEOUT = 30.0

#: 交给 SDK 的「流上多久没有新事件算断」阈值。刻意拉到远大于任何 ``tool_timeout``：
#: **超时的唯一裁判是本层的 ``asyncio.wait_for``**（它认得出超时、且认出来就不重发，
#: 见 ``_is_timeout``）。底下再挂一个看不见的定时器，只会多出一种措辞不同的超时错误。
STREAM_READ_TIMEOUT = 3600.0

#: 收尾时等长驻任务自己退出的上限；超了就取消它。
_CLOSE_TIMEOUT = 5.0

#: 握手时自报的客户端身份。原先挂在 ``MCPClient(client_name=...)`` 上，即「谁连的」
#: 记在客户端对象上；现在上线报文由 transport 里的 SDK 会话发，身份就跟着挪到 transport。
CLIENT_INFO_NAME = "creation-assistant"
CLIENT_INFO_VERSION = "0.1"


class MCPError(RuntimeError):
    pass


def _is_timeout(exc: BaseException) -> bool:
    """这个异常是不是「等超时了」而不是「连接断了」。

    两者必须区别对待：连接断了重建会话重发一次是对的；**超时**重发等于让服务端
    把同一段长活（asr / 画面理解）整段再跑一遍——时间与 API 费用翻倍，
    用户等更久还拿不到更早的结果。超时只该如实上报。
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


class MCPTransport(Protocol):
    """一条到 MCP Server 的**操作级**接缝：建会话 / 列工具 / 调工具 / 丢弃 / 关闭。

    为什么是操作级而不是原来的报文级（``request(payload, timeout)``）：报文一旦由官方
    库来拼，接缝上就没法再谈报文了——留着它只会变成「假报文 → 真报文」的翻译层，比手写
    那一版还多一层。测试替身也随之从「假服务端」变成「假会话」：它答的是「列出来是哪些
    工具 / 这次调用返回什么」，而不是「第几条 JSON-RPC 长什么样」。
    """

    async def open(self, timeout: float) -> dict[str, Any]:
        """建立会话并握手；已建立则直接返回（幂等）。返回值是握手响应（可为空 dict）。"""
        ...

    async def list_tools(self, timeout: float) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    async def reset(self) -> None:
        """丢弃当前会话（下次 ``open`` 重建）。真·断线时由 ``MCPClient`` 调。"""
        ...

    async def close(self) -> None: ...


def _tool_def(tool: Any) -> dict[str, Any]:
    """SDK 的 ``Tool`` 对象 → 与迁移前**逐键相同**的 dict。

    为什么坚持 dump 回 dict，而不是让下游改读对象：``MCPTool`` 读的是
    ``tool_def.get("inputSchema")`` 与 ``annotations.readOnlyHint``，``register_mcp_tools``
    读的是 ``td["name"]``——那几行正是 T44 点名「必须原样保留」的三件。**下游一行不改**，
    这三件就不可能被这次迁移弄断（而不是「小心一点应该不会断」）。
    """
    if isinstance(tool, dict):
        return tool                      # 测试替身可以直接给 dict
    return tool.model_dump(by_alias=True, exclude_none=True)


def _result_dict(result: Any) -> dict[str, Any]:
    """``CallToolResult`` → dict：``content`` / ``isError`` 两个键与迁移前一致。"""
    if isinstance(result, dict):
        return result
    return result.model_dump(by_alias=True, exclude_none=True)


class _SDKTransport:
    """三种 type 共用的骨架：一个长驻任务自持 SDK 会话，外面只调 ``session.*``。

    子类唯一要做的事是 ``_connector()``——把配置变成一个产出 SDK ``(read, write)`` 的
    ``@asynccontextmanager``。进和出都发生在 ``_owner`` 这一个任务里，因此天然满足
    anyio 的「谁进谁出」。这是整节存在的理由，不是包装癖：见模块上方那段硬约束。
    """

    #: 出错措辞里的可读标签，子类覆盖成 ``stdio:cmd`` / ``http:url`` 这类。
    label = "mcp"

    def __init__(self, *, client_name: str = CLIENT_INFO_NAME) -> None:
        self._client_name = client_name
        self._session: ClientSession | None = None
        self._info: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._error: BaseException | None = None

    # ---- 子类实现 ----

    def _connector(self) -> Any:
        raise NotImplementedError

    def _header_map(self) -> dict[str, str]:
        """配置里的请求头在**用时**才做 ``${ENV}`` 展开。

        为什么不能省：YAML 那条路 `parse_simple_yaml` 已经展开过一遍，但 **JSON 配置走
        `json.loads`，压根没有展开这一步**——迁移前的手写实现正是在这里补上的。
        """
        raw = getattr(self, "_headers", None) or {}
        return {str(k): _expand_env(str(v)) for k, v in raw.items()}

    # ---- 长驻任务：整个生命周期里唯一的进/出点 ----

    async def _owner(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(self._connector())
                session = await stack.enter_async_context(ClientSession(
                    read, write,
                    client_info=Implementation(name=self._client_name,
                                               version=CLIENT_INFO_VERSION),
                ))
                init = await session.initialize()
                self._info = init.model_dump(by_alias=True, exclude_none=True)
                self._session = session
                self._ready.set()
                await self._stop.wait()      # 会话在这里活着，直到 reset/close 叫停
        except asyncio.CancelledError:
            raise                            # 取消不是「连不上」，照实往上抛
        except BaseException as exc:         # noqa: BLE001 - 连不上/握手失败要交回调用方
            self._error = exc
        finally:
            self._session = None
            if self._ready is not None:
                self._ready.set()            # 别让 open() 一直在那儿等一个不会来的信号

    async def open(self, timeout: float = CONNECT_TIMEOUT) -> dict[str, Any]:
        if self._session is not None:
            return self._info
        if self._task is None or self._task.done():
            self._ready = asyncio.Event()
            self._stop = asyncio.Event()
            self._error = None
            self._info = {}
            self._task = asyncio.create_task(self._owner(), name=f"mcp-{self.label}")
        ready = self._ready
        if ready is None:                    # 只有上面那段被改坏才到得了这里
            raise MCPError(f"{self.label} 传输状态损坏：ready 事件缺失")
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            await self.reset()
            raise MCPError(f"连接 {self.label} 超时（{timeout:g}s）") from exc
        if self._error is not None:
            err = self._error
            await self.reset()
            raise MCPError(f"连接 {self.label} 失败：{err}") from err
        if self._session is None:
            raise MCPError(f"连接 {self.label} 失败：会话未建立就退出了")
        return self._info

    async def reset(self) -> None:
        """丢掉会话。**必须由长驻任务自己收尾**，所以这里是「叫停 + 等它回来」。"""
        task, stop = self._task, self._stop
        self._task = None
        self._stop = None
        self._ready = None
        self._session = None
        self._error = None
        self._info = {}
        if stop is not None:
            stop.set()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=_CLOSE_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                # wait_for 超时会顺手取消它；这里把取消收干净，别把异常漏给收尾方
                with contextlib.suppress(BaseException):
                    await task
            except BaseException:            # noqa: BLE001 - 收尾失败不该盖住原本的错误
                pass

    async def close(self) -> None:
        await self.reset()

    # ---- 操作 ----

    async def _use(self, timeout: float) -> ClientSession:
        if self._session is None:
            await self.open(timeout)
        session = self._session
        if session is None:
            raise MCPError(f"{self.label} 会话不可用")
        return session

    async def list_tools(self, timeout: float = CONNECT_TIMEOUT) -> list[dict[str, Any]]:
        session = await self._use(timeout)
        try:
            result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
        except McpError as exc:
            raise MCPError(f"{self.label} 列工具被服务端回绝：{exc.error}") from exc
        return [_tool_def(t) for t in result.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
        session = await self._use(timeout)
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, arguments), timeout=timeout)
        except McpError as exc:
            # 服务端以 JSON-RPC error 明确回绝（不是工具自己报的 isError）→ 如实上报，
            # 别当断线去重连重发。``McpError`` 是 SDK 的协议级异常类型。
            raise MCPError(f"调用 {name} 被服务端回绝：{exc.error}") from exc
        # 超时（TimeoutError）与其它异常**原样放行**：MCPClient 靠这个区别决定
        # 「重连重试一次」还是「如实上报」。在这里提前翻译会把那个区别抹掉。
        return _result_dict(result)


class StdioTransport(_SDKTransport):
    """stdio：由官方 ``stdio_client`` 负责拉起子进程与逐行 JSON-RPC。"""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None,
                 *, client_name: str = CLIENT_INFO_NAME) -> None:
        super().__init__(client_name=client_name)
        self._command = command
        self._args = list(args)
        self._env = dict(env or {})
        self.label = f"stdio:{command}"

    @contextlib.asynccontextmanager
    async def _connector(self) -> AsyncIterator[Any]:
        # env **显式给全量**（父进程环境 + 配置里展开后的覆盖项），不吃 SDK 的默认值：
        # ``StdioServerParameters(env=None)`` 会让 SDK 换成 ``get_default_environment()``
        # ——那是刻意收窄过的一份安全子集（POSIX 上只有 HOME/LOGNAME/PATH/SHELL/TERM/USER），
        # 而迁移前的手写实现传的是 ``dict(os.environ)``。换库不该顺手改这件事：收窄是有
        # 价值的动作，但它会让依赖父环境变量的 server 直接连不上，得单独定价、单独上线。
        env = dict(os.environ)
        env.update({k: _expand_env(v) for k, v in self._env.items()})
        params = StdioServerParameters(command=self._command, args=list(self._args), env=env)
        async with stdio_client(params) as (read, write):
            yield read, write


class StreamableHttpTransport(_SDKTransport):
    """streamableHttp：由官方 ``streamablehttp_client`` 管双模响应与会话头。"""

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 *, timeout: float = CONNECT_TIMEOUT,
                 client_name: str = CLIENT_INFO_NAME) -> None:
        super().__init__(client_name=client_name)
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self.label = f"http:{url}"

    @contextlib.asynccontextmanager
    async def _connector(self) -> AsyncIterator[Any]:
        async with streamablehttp_client(
            self._url, headers=self._header_map(),
            timeout=self._timeout, sse_read_timeout=STREAM_READ_TIMEOUT,
        ) as (read, write, _get_session_id):
            # 第三个回值才是取 mcp-session-id 的函数。迁移前我们自己存这个 id、每个请求
            # 带回去，还额外留了一句 `transport._session_id = ""` 的越权清空；现在它由
            # SDK 维护，我们不再需要那个字段（`MCPClient` 也不再伸手去改传输的内部状态）。
            yield read, write


class SseTransport(_SDKTransport):
    """sse：由官方 ``sse_client`` 负责。

    迁移前 ``MCPServerConfig.type`` 里写着 ``sse`` 这个取值，但 ``_default_transport``
    会把它当 streamableHttp 用——一个静默的谎。现在三种取值都是**真的**。
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 *, timeout: float = CONNECT_TIMEOUT,
                 client_name: str = CLIENT_INFO_NAME) -> None:
        super().__init__(client_name=client_name)
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self.label = f"sse:{url}"

    @contextlib.asynccontextmanager
    async def _connector(self) -> AsyncIterator[Any]:
        async with sse_client(self._url, headers=self._header_map(),
                              timeout=self._timeout,
                              sse_read_timeout=STREAM_READ_TIMEOUT) as (read, write):
            yield read, write


# --------------------------------------------------------------------------
# MCP 客户端：initialize / tools/list / tools/call
# --------------------------------------------------------------------------

class MCPClient:
    """对一个 MCP Server 的轻量客户端，只依赖注入的 transport。

    T44 之后这一层**更薄**了：握手、会话头、SSE 解析都沉到了 transport 底下的官方客户端。
    留在这儿的只剩两条**本仓的业务语义**，它们与用哪个库无关：
      1. **超时不重发**（``_is_timeout``）——见 ``call_tool`` 里的长注释；
      2. 只有真·断线才重建会话重试一次。
    """

    def __init__(self, server: str, transport: MCPTransport) -> None:
        self.server = server
        self._transport = transport
        self._initialized = False

    async def initialize(self, timeout: float = CONNECT_TIMEOUT) -> dict[str, Any]:
        if self._initialized:
            return {}
        info = await self._transport.open(timeout)
        self._initialized = True
        return info or {}

    async def list_tools(self, timeout: float = CONNECT_TIMEOUT) -> list[dict[str, Any]]:
        await self.initialize(timeout)
        return await self._transport.list_tools(timeout)

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        await self.initialize(timeout)
        try:
            return await self._transport.call_tool(name, arguments, timeout)
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
            # 迁移前这里伸手改传输的内部状态（`transport._session_id = ""`）；现在
            # `reset()` 是协议上的一个动作，不再需要谁去偷看别人的私有字段。
            await self._transport.reset()
            self._initialized = False
            await self.initialize(timeout)
            return await self._transport.call_tool(name, arguments, timeout)

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

# MCP 侧每份工具描述的字节上限（§5.7 第⑥条；T41 落地）。
# 为什么是「字节」而不是字数：与 §5.4.1 的整张预算表同一把尺（``section_bytes``），
# 账面要能被逐字复核。600 这个数本身是条款里定的**内容长度约束**，不是标定出来的
# 阈值——改它必须同批改对应断言（同 §11 那句「改一个数必须同批改断言」）。
DESC_MAX_BYTES = 600

# 被截掉的正文去哪了，要**写在截断处**：模型看不见「少了一句」，只会照着剩下的做。
# 指向技能正文而不是指向某个 HTTP 端点：那是模型这一轮真拿得到的东西（计划里用到本节点时
# 服务端预注入它的技能；单轮内也能 load_skill 取）。
_DESC_POINTER = "……（本节点说明过长，此处截断；完整约束与指针形状在本节点随身的技能正文里，load_skill 可取）"


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut(text: str, limit: int) -> str:
    """按 UTF-8 字节切到 ``limit`` 以内，且**不切碎字符**。

    ``decode(errors="ignore")`` 会丢掉最后那个被切一半的多字节字符——这是刻意的：
    JSON 里带半个汉字能编码成功，但读出来是乱码，而且它会让「描述 ≤600 B」这条判据
    变成随机数（同一句话在两台机器上可能不同长）。
    """
    if limit <= 0:
        return ""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def compose_description(body: str, tail: str = "") -> tuple[str, bool]:
    """排一份描述并截到 ``DESC_MAX_BYTES``，返回 ``(文本, 是否截过)``。

    优先丢正文的**尾部**，两样东西必须留下：
    1. ``tail``（「必须先显式调用谁」）——少一句就是一次白跑的调用；
    2. ``_DESC_POINTER``——被截掉的那半句去哪了，只有这里说得出来。
    指针一行自己都放不下时（正文长到 600 B 装不下任何东西）先丢指针、只保 ``tail``：
    宁可少一句「去哪找」，也不能少一句「必须先调谁」。
    """
    if _bytes(body) + _bytes(tail) <= DESC_MAX_BYTES:
        return body + tail, False
    room = DESC_MAX_BYTES - _bytes(tail) - _bytes(_DESC_POINTER)
    if room > 0:
        return _cut(body, room) + _DESC_POINTER + tail, True
    return _cut(tail, DESC_MAX_BYTES), True


class MCPTool(Tool):
    """包装某个 MCP Server 暴露的工具：schema 来自 tools/list，execute 走 tools/call。

    剪辑节点工具会带一份 ``contract``（服务端 ``dag_contract`` 的一个节点），
    读写集据此声明——Agent 循环因此知道哪两个节点调用可以同批并发。没有契约
    （非剪辑 server / 契约未取到）时读写集为空，按「证明不了独立就不同批」处理。

    中文名走 MCP 标准的 ``title`` 字段：Storyline 注册节点时把 ``BaseNode.display_name``
    填进去，这里读回来——服务端仍是节点元信息的唯一作者，主服务不再抄第二份词表。

    描述在这一层截到 ``DESC_MAX_BYTES``（T41，§5.7 第⑥条）：剪辑节点的描述由 Storyline
    侧写，最长的几份近 2 KB，而它们的约束在技能正文里**本来就有第二份**（真机对过账：
    ``plan_motion``/``patch_motion_code``/``render_motion_code`` → ``motion_explainer_skill``，
    ``patch_video`` → ``default_editing_workflow_skill``）。截断而不是删：被截的那份说明
    末尾留一行指向技能正文，且 ``plan.round.unbacked_truncations`` 会问「这段溢出真有载体吗」
    ——**移出即须有闸读到**（同一句判据的另一半）。
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
    def _desc_body(self) -> str:
        """会被截的那一段（服务端写的那份说明）。"""
        return self._desc or f"MCP tool {self._raw_name} on {self._client.server}"

    @property
    def _desc_tail(self) -> str:
        """截不动的那一段——「必须先显式调用谁」。

        为什么它比正文末尾那句话更该留下：这几个前置**不会被服务端自动补齐**，
        模型不知道就会先撞一次错、再回头补（真机实测白烧一轮）。正文末尾多半是
        渲染耗时这类提醒，少一句不致命；这条少一句就是一次白跑的调用。
        """
        blocking = self._explicit_deps.get(self._raw_name) or ()
        if not blocking:
            return ""
        return (f"[必须显式调用] 这些前置节点不会被自动补齐，请在你真正调用本节点之前"
                f"先逐个调用它们（并传入创意决策参数）：{'、'.join(blocking)}。")

    @property
    def description(self) -> str:
        """工具说明（**已按 ``DESC_MAX_BYTES`` 截过**）；剪辑节点额外附一行「必须先显式调用谁」。

        为什么非截不可：执行轮整批 schema 真机实测 54,140 B，其中最长四份描述合计
        6.1 KB，而它们说的每一句在各自技能正文里都有更完整的一份——描述是**另一次**
        抄写（第二份真相），瘦它就等于删掉一次抄写。截断点与指向都写在 ``compose_description``。
        """
        return compose_description(self._desc_body, self._desc_tail)[0]

    @property
    def desc_truncated(self) -> bool:
        """这份说明是不是被截过——承载闸（``plan.round.unbacked_truncations``）读它。"""
        return compose_description(self._desc_body, self._desc_tail)[1]

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
    """按 ``type`` 选传输。三种取值（stdio / streamableHttp / sse）现在**都是真的**。

    认不出的 ``type`` 从「静默当 streamableHttp 用」改成**明确报错**：原先写错一个
    取值不会有任何提示，只会在连接时表现成「这台 server 怎么连不上」。报错之后
    ``McpManager.connect_enabled`` 会记一条告警跳过它，那才是能查的样子。
    """
    kind = (config.type or "").strip().lower()
    if kind == "stdio" or (not kind and config.command and not config.url):
        return StdioTransport(config.command, config.args, config.env)
    if not config.url:
        raise MCPError(
            f"MCP server「{config.name}」的 type={config.type!r} 缺少 url"
            f"（stdio 要 command，streamableHttp / sse 要 url）")
    timeout = float(config.tool_timeout)
    if kind == "sse":
        return SseTransport(config.url, config.headers, timeout=timeout)
    if kind in ("", "streamablehttp", "http"):
        return StreamableHttpTransport(config.url, config.headers, timeout=timeout)
    raise MCPError(
        f"MCP server「{config.name}」的 type={config.type!r} 不认识："
        f"只支持 stdio / streamableHttp / sse")


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
