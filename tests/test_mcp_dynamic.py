"""动态 MCP Server 管理（McpManager + /mcp/servers 语义）。

对应「前端注册 tool」通道的 Tool 层：
  ① 连接注册：tools/list → 白名单 → 前缀名注册进主注册表，调用走通；
  ② 连接幂等：已连接再 connect 原样返回，transport 只建一次；
  ③ 撞名保护：与主注册表任何工具撞名则整体回绝、客户端关闭、不留半截注册；
  ④ 热断：disconnect 注销全部工具并关闭连接，配置行保留；
  ⑤ 启动接入：connect_enabled 只连 enabled 行，单个失败不阻断。

运行：  python tests/test_mcp_dynamic.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.mcp_manager import McpManager, McpManagerError
from agent_framework.storage import build_storage
from agent_framework.tool import ToolRegistry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


class FakeTransport:
    """假 MCP Server：initialize / tools/list / tools/call 三招，够跑通整条注册链。"""

    def __init__(self, *, broken: bool = False) -> None:
        self.closed = False
        self.broken = broken
        self.built = True

    async def request(self, payload: dict, timeout: float = None):  # noqa: ANN001
        method = payload.get("method")
        if self.broken and method == "tools/list":
            raise RuntimeError("连接被拒")
        rid = payload.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "ping", "description": "探活",
                 "inputSchema": {"type": "object", "properties": {}}},
                {"name": "echo", "description": "回声",
                 "inputSchema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]}},
            ]}}
        if method == "tools/call":
            text = (payload.get("params", {}).get("arguments", {}) or {}).get("text", "")
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": f"echo:{text}"}]}}
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    async def notify(self, msg: dict) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


def _make_manager(storage, registry, *, broken_names=()):
    built: list[FakeTransport] = []

    def factory(config):
        t = FakeTransport(broken=config.name in broken_names)
        built.append(t)
        return t

    return McpManager(storage, registry, transport_factory=factory), built


async def part1_connect_and_call() -> None:
    print("\n[1] 连接注册：tools/list → 前缀名注册 → 调用走通")
    storage = build_storage("memory")
    registry = ToolRegistry()
    mgr, built = _make_manager(storage, registry)
    await storage.mcp_servers.upsert("echo", {"type": "stdio", "command": "fake",
                                              "enabled_tools": ["*"]})
    names = await mgr.connect("echo")
    check(names == ["echo_ping", "echo_echo"], f"前缀注册名：{names}")
    check(all(registry.has(n) for n in names), "主注册表已含全部工具")

    out = await registry.execute("echo_echo", {"text": "hi"})
    check(not is_err(out) and "echo:hi" in str(out), f"调用走通：{str(out)[:40]}")

    names2 = await mgr.connect("echo")
    check(names2 == names and len(built) == 1, f"连接幂等：transport 只建一次（{len(built)}）")


async def part2_clash_guard() -> None:
    print("\n[2] 撞名保护：整体回绝、客户端关闭、不留半截注册")
    storage = build_storage("memory")
    registry = ToolRegistry()
    mgr, built = _make_manager(storage, registry)
    await storage.mcp_servers.upsert("echo", {"type": "stdio", "command": "fake"})

    async def manual(**kwargs):  # noqa: ANN001, ARG001
        return "占位"
    from agent_framework.team_tools import FunctionTool
    registry.register(FunctionTool("echo_ping", "占位",
                                   {"type": "object", "properties": {}},
                                   manual))
    try:
        await mgr.connect("echo")
        check(False, "撞名（竟未回绝）")
    except McpManagerError as e:
        check("撞名" in str(e) and "echo_ping" in str(e), f"整体回绝：{e}")
    check(built[0].closed, "回绝时客户端已关闭")
    check(not mgr.is_live("echo"), "不留下在线状态")
    check(registry.has("echo_ping") and not registry.has("echo_echo"),
          "既有工具未被覆盖，撞名工具也未注册进来")


async def part3_disable_and_reconnect() -> None:
    print("\n[3] 热断：注销工具、保留配置、可再连")
    storage = build_storage("memory")
    registry = ToolRegistry()
    mgr, _ = _make_manager(storage, registry)
    await storage.mcp_servers.upsert("echo", {"type": "stdio", "command": "fake"})
    names = await mgr.connect("echo")

    removed = await mgr.disconnect("echo")
    check(sorted(removed) == sorted(names), f"断开返回注销清单：{removed}")
    check(not any(registry.has(n) for n in names), "工具已从主注册表注销")
    check(await storage.mcp_servers.get("echo") is not None, "配置行保留")

    names2 = await mgr.connect("echo")
    check(names2 == names and all(registry.has(n) for n in names2), "可再次连接")


async def part4_connect_enabled() -> None:
    print("\n[4] 启动接入：只连 enabled 行，单个失败不阻断")
    storage = build_storage("memory")
    registry = ToolRegistry()
    broken = {"gamma"}
    def factory(config):  # noqa: ANN001
        return FakeTransport(broken=config.name in broken)
    mgr = McpManager(storage, registry, transport_factory=factory)

    await storage.mcp_servers.upsert("alpha", {"type": "stdio", "command": "f"}, enabled=True)
    await storage.mcp_servers.upsert("beta", {"type": "stdio", "command": "f"}, enabled=False)
    await storage.mcp_servers.upsert("gamma", {"type": "stdio", "command": "f"}, enabled=True)

    live = await mgr.connect_enabled()
    check("alpha" in live and mgr.is_live("alpha"), f"enabled 的连上了：{sorted(live)}")
    check("beta" not in live and not mgr.is_live("beta"), "未启用的跳过")
    check(not mgr.is_live("gamma"), "连接失败的跳过（不抛出）")


async def main() -> None:
    await part1_connect_and_call()
    await part2_clash_guard()
    await part3_disable_and_reconnect()
    await part4_connect_enabled()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


def is_err(x) -> bool:  # noqa: ANN001
    from agent_framework.tool import is_tool_error
    return is_tool_error(x)


if __name__ == "__main__":
    asyncio.run(main())
