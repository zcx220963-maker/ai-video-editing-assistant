"""动态 MCP Server 管理器：配置在 PG，运行时热连/热断。

对应「像 coding agent 一样在前端注册 tool」的 Tool 层通道。与启动期 mcp.json
静态接入的关系：json 是发版才能改的部署清单，这里走运行期——配置经
``POST /mcp/servers`` 写库后 enable 即连、disable 即断，主注册表随之增删。

三条纪律：
* **撞名不上线**：注册前先在一次性临时注册表里试装，任何一个工具名与主注册表
  撞上就整体回绝（宁可不上线也不静默覆盖既有工具）；
* **连接幂等**：对已连接的 server 再 connect 直接返回现有工具名，不重复注册；
* **失败即回滚**：tools/list 或注册中途抛错，关闭客户端、不留下半截状态，
  真正落库的只有 config/enabled 行。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .tool import ToolRegistry
from .tools.mcp import MCPClient, MCPServerConfig, connect_server, register_mcp_tools

logger = logging.getLogger("agent_framework")


class McpManagerError(RuntimeError):
    pass


class McpManager:
    """一组 (PG 配置行 → 运行中连接 → 注册表工具) 的运行期协调者。"""

    def __init__(self, storage: Any, registry: ToolRegistry, *,
                 default_timeout: float | None = None,
                 transport_factory: Callable[[MCPServerConfig], Any] | None = None) -> None:
        self._storage = storage
        self._registry = registry
        self._default_timeout = default_timeout
        # 测试注入位：给 FakeTransport 用；生产为 None（connect_server 按 type 自选）
        self._transport_factory = transport_factory
        # name -> (client, registered_tool_names)
        self._live: dict[str, tuple[MCPClient, list[str]]] = {}

    # ---- 查询 ----

    def is_live(self, name: str) -> bool:
        return name in self._live

    def tool_names(self, name: str) -> list[str]:
        return list(self._live.get(name, (None, []))[1])

    def live_servers(self) -> dict[str, list[str]]:
        return {name: list(names) for name, (_c, names) in self._live.items()}

    # ---- 连接 / 断开 ----

    async def connect(self, name: str) -> list[str]:
        """按库里的配置连接一个 server 并注册其工具（幂等：已连接则原样返回）。"""
        if name in self._live:
            return list(self._live[name][1])
        row = await self._storage.mcp_servers.get(name)
        if row is None:
            raise McpManagerError(f"MCP server {name!r} 未注册")
        config = MCPServerConfig.from_dict(name, row.get("config") or {})
        transport = self._transport_factory(config) if self._transport_factory else None
        client = await connect_server(config, transport)
        try:
            # 先在一次性临时注册表里试装：撞名/白名单语义与正式注册完全一致
            scratch = ToolRegistry()
            names = await register_mcp_tools(scratch, client, config,
                                             timeout=self._default_timeout)
            clashes = sorted(n for n in names if self._registry.has(n))
            if clashes:
                raise McpManagerError(
                    f"MCP server {name!r} 的工具与现有工具撞名：{clashes}——"
                    f"请改名或调整 enabled_tools 后重试")
            for n in names:
                self._registry.register(scratch.get(n))
        except Exception:
            await client.close()
            raise
        self._live[name] = (client, names)
        return list(names)

    async def disconnect(self, name: str) -> list[str]:
        """断开并注销一个 server 的全部工具；本来就没连着则空操作。"""
        client, names = self._live.pop(name, (None, []))
        if client is None:
            return []
        for n in names:
            self._registry.unregister(n)
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - 关连接失败不阻断注销
            logger.warning("MCP server %s 关闭连接失败：%s", name, exc)
        return names

    async def connect_enabled(self) -> dict[str, list[str]]:
        """启动期入口：把库里所有 enabled 的 server 连起来。

        单个失败只告警跳过（与 mcp.json 接入同一口径），返回 {name: 工具名}。
        """
        connected: dict[str, list[str]] = {}
        for row in await self._storage.mcp_servers.list():
            if not row.get("enabled"):
                continue
            name = row["name"]
            try:
                connected[name] = await self.connect(name)
                logger.info("MCP server「%s」已接入 %d 个工具（动态注册）",
                            name, len(connected[name]))
            except Exception as exc:  # noqa: BLE001 - 失败不阻断启动
                logger.warning("动态 MCP server「%s」连接失败，已跳过：%s", name, exc)
        return connected

    async def close_all(self) -> None:
        """进程收尾：断开全部动态连接。"""
        for name in list(self._live):
            await self.disconnect(name)
