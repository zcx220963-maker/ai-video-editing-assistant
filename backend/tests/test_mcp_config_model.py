# -*- coding: utf-8 -*-
"""R15：MCP 配置模型（Pydantic）+ 样例配置 + mock 四工具 + camelCase 别名 + ${ENV} 展开。"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_framework.tools.mcp import MCPServerConfig  # noqa: E402

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


async def mock_tools() -> None:
    """R15-c：stdio mock 的 tools/list 返回设计点名的四类工具，且真能被调用。"""
    from agent_framework.tools.mcp import (MCPClient, StdioTransport,
                                           load_mcp_config)

    cfg_path = BACKEND / "examples" / "mcp" / "creator_ops.json"
    cfg = load_mcp_config(cfg_path)
    server_cfg = cfg.servers.get("creator_ops")
    check(server_cfg is not None and server_cfg.type == "stdio",
          f"样例配置可读且指向 stdio：{server_cfg and server_cfg.type}")
    check(server_cfg.args == ["examples/mcp/creator_ops_server.py"],
          f"配置 args 指向 mock 脚本：{server_cfg.args if server_cfg else None}")

    script = BACKEND / "examples" / "mcp" / "creator_ops_server.py"
    client = MCPClient("creator_ops", StdioTransport(
        sys.executable, [str(script)], env={"PYTHONIOENCODING": "utf-8"}))
    try:
        tools = {t["name"]: t for t in await client.list_tools(timeout=30)}
        want = {"get_creator_profile", "get_creation_weekly_report",
                "publish_content", "reply_fan_message"}
        check(set(tools) == want, f"tools/list 四个工具（实际 {sorted(tools)}）")
        check(all(t.get("inputSchema", {}).get("type") == "object" for t in tools.values()),
              "每个工具都带 object inputSchema")
        check({"title", "content"} <= set(
                  tools.get("publish_content", {}).get("inputSchema", {})
                  .get("properties", {})),
              "publish_content 暴露 title/content 入参")

        res = await client.call_tool("get_creator_profile", {"platform": "douyin"},
                                     timeout=30)
        text = "".join(c.get("text", "") for c in res.get("content", []))
        body = json.loads(text) if text else {}
        check(not res.get("isError") and body.get("platform") == "douyin"
              and body.get("fans", 0) > 0,
              f"get_creator_profile 真的被调通：{text[:90]}")

        res2 = await client.call_tool("publish_content",
                                      {"title": "t", "content": "c",
                                       "publish_at": "2026-11-01T09:00:00+08:00"},
                                      timeout=30)
        text2 = "".join(c.get("text", "") for c in res2.get("content", []))
        body2 = json.loads(text2) if text2 else {}
        check(body2.get("status") == "scheduled", f"定时发布走 publish_content：{text2[:90]}")
    finally:
        await client.close()


async def main() -> None:
    # ---- 非法 type 被拒（模型期就报，不带病到连接阶段）----
    try:
        MCPServerConfig(name="x", type="websocket")
        check(False, "非法 type 被拒")
    except Exception:
        check(True, "非法 type 被拒")

    # ---- 三种合法传输 + 历史别名归一 ----
    for raw, want in (("stdio", "stdio"), ("sse", "sse"),
                      ("streamableHttp", "streamableHttp"),
                      ("streamable_http", "streamableHttp"), ("http", "streamableHttp")):
        cfg = MCPServerConfig(name="x", type=raw)
        check(cfg.type == want, f"type {raw} → {want}（实际 {cfg.type}）")

    # ---- enabledTools / toolTimeout 别名（防回退）----
    cfg = MCPServerConfig.from_dict("srv", {
        "type": "stdio", "command": "npx",
        "enabledTools": ["a", "b"], "toolTimeout": 9,
    })
    check(cfg.enabled_tools == ["a", "b"], "enabledTools 别名生效")
    check(cfg.tool_timeout == 9, "toolTimeout 别名生效")
    cfg2 = MCPServerConfig.from_dict("srv", {"enabled_tools": ["*"], "tool_timeout": 3})
    check(cfg2.enabled_tools == ["*"] and cfg2.tool_timeout == 3, "snake_case 仍生效")

    # ---- 往返 ----
    again = MCPServerConfig.from_dict("srv", cfg.to_dict())
    check(again.enabled_tools == cfg.enabled_tools and again.tool_timeout == cfg.tool_timeout,
          "to_dict → from_dict 往返一致")

    # ---- 额外字段被忽略而不是炸 ----
    cfg3 = MCPServerConfig.from_dict("srv", {"type": "sse", "url": "http://x", "zzz": 1})
    check(cfg3.url == "http://x", "未知字段被忽略，合法字段保留")

    # ---- ${ENV} 展开（防回退）：模块里有 _expand_env 且在用时展开 ----
    from agent_framework.tools.mcp import _expand_env

    os.environ["WB_TEST_TOKEN"] = "secret"
    check(_expand_env("Bearer ${WB_TEST_TOKEN}") == "Bearer secret", "${ENV} 展开")
    check(_expand_env("no-var") == "no-var", "无变量时原样")

    await mock_tools()

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
