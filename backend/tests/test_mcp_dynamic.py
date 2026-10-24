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
    """假 MCP **会话**：列工具 / 调工具两招，够跑通整条注册链。

    T44 把接缝从 JSON-RPC 报文级挪到操作级之后，这个替身也跟着变薄了：它不再拼
    ``{"jsonrpc": "2.0", ...}``，只回答「有哪些工具」「这次调用返回什么」。
    """

    _TOOLS = [
        {"name": "ping", "description": "探活",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "echo", "description": "回声",
         "inputSchema": {"type": "object",
                         "properties": {"text": {"type": "string"}},
                         "required": ["text"]}},
    ]

    def __init__(self, *, broken: bool = False) -> None:
        self.closed = False
        self.broken = broken
        self.built = True

    async def open(self, timeout: float) -> dict:
        return {"protocolVersion": "2024-11-05", "capabilities": {}}

    async def list_tools(self, timeout: float) -> list[dict]:
        if self.broken:
            raise RuntimeError("连接被拒")
        return [dict(t) for t in self._TOOLS]

    async def call_tool(self, name: str, arguments: dict, timeout: float) -> dict:
        text = (arguments or {}).get("text", "")
        return {"content": [{"type": "text", "text": f"echo:{text}"}]}

    async def reset(self) -> None:
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


async def part5_static_seed() -> None:
    """R10-T46：mcp.json 是**种子**，连接统一走 McpManager——不再有第二条腿。

    这一段复刻 `run_server.py` 启动期的三步（读 json → 逐项 upsert → connect_enabled），
    验的是那三行代码合起来的语义。为什么值得单测：T46 之前 json 那条腿绕开 McpManager
    直接 connect，于是它的 server 在 `live_servers()` 里**看不见也关不掉**（P31）。
    这里断言的第一件事就是「看得见」。
    """
    print("\n[5] 静态种子：mcp.json → mcp_servers → 动态通道连接")
    import json
    import tempfile
    from pathlib import Path

    from agent_framework.tools.mcp import load_mcp_config

    storage = build_storage("memory")
    registry = ToolRegistry()
    mgr, built = _make_manager(storage, registry)

    with tempfile.TemporaryDirectory() as td:
        seed = Path(td) / "mcp.json"
        seed.write_text(json.dumps({"tools": {"mcp_servers": {
            "seeded": {"type": "stdio", "command": "fake",
                       "enabled_tools": ["*"], "tool_timeout": 12},
        }}}), encoding="utf-8")

        async def seed_once() -> list[str]:
            """run_server 启动期做的三步，逐字复刻。"""
            cfg = load_mcp_config(seed)
            names = []
            for sc in cfg.list_servers():
                await storage.mcp_servers.upsert(sc.name, sc.to_dict(),
                                                 enabled=True, owner_user_id=None)
                names.append(sc.name)
            return names

        assert await seed_once() == ["seeded"], "种子读出一台"
        row = await storage.mcp_servers.get("seeded")
        check(row is not None and row["enabled"] is True and row["owner_user_id"] is None,
              "种子项入库：enabled=True、owner_user_id=NULL（系统级、全用户共享）")
        check(row["config"].get("tool_timeout") == 12 and row["config"].get("command") == "fake",
              "落地的 config 能被 from_dict 读回来（to_dict 与它是同一套字段）")

        live = await mgr.connect_enabled()
        check("seeded" in live and mgr.is_live("seeded"),
              f"连接由动态通道发起：{sorted(live)}")
        # P31 的正题：种子项必须**出现在 live_servers() 里**，也就是设置页看得见
        check("seeded" in mgr.live_servers(), "种子项出现在 live_servers()：设置页看得见它")
        check(registry.has("seeded_echo"), "它的工具进了主注册表")

        # 可关：关断走 McpManager.disconnect，工具随之从主注册表移除
        removed = await mgr.disconnect("seeded")
        check("seeded_echo" in removed and not registry.has("seeded_echo"),
              "关得掉：disconnect 注销工具并断开")
        check(await storage.mcp_servers.get("seeded") is not None, "关断不删配置行（下次还能启用）")

        # 幂等：重启一次（再跑一遍种子）不该多出第二台，也不该连不上
        assert await seed_once() == ["seeded"], "重复播种仍是同一台"
        check(len(await storage.mcp_servers.list()) == 1, "重复播种不产生重复行（upsert by name）")
        live2 = await mgr.connect_enabled()
        check("seeded" in live2, "重启后仍能连上（配置仍能从库里读回）")


async def main() -> None:
    await part1_connect_and_call()
    await part2_clash_guard()
    await part3_disable_and_reconnect()
    await part4_connect_enabled()
    await part5_static_seed()
    print(f"\n==== {_checks} 项检查，{_fails} 项失败 ====")
    if _fails:
        sys.exit(1)


def is_err(x) -> bool:  # noqa: ANN001
    from agent_framework.tool import is_tool_error
    return is_tool_error(x)


if __name__ == "__main__":
    asyncio.run(main())
