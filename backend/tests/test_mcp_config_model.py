# -*- coding: utf-8 -*-
"""R15：MCP 配置模型（Pydantic）+ camelCase 别名 + ${ENV} 展开（后两条是防回退项）。"""

from __future__ import annotations

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


def main() -> None:
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

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
