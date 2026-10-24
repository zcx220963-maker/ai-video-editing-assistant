"""一支**真**的 stdio MCP 服务端桩（R10-T44）：给客户端迁移验「全链路仍然连得上」。

为什么要有它，而不是继续用内存替身：T44 把客户端从手写 JSON-RPC 换成官方 `mcp` 包的
薄适配层，换掉的正是「进程/HTTP 那一层怎么说话」。内存替身说话的对象是我们自己的
transport 接口——它**证不了**迁移后的东西真能连上一个 MCP 服务端。这支桩用的是官方
**服务端**那一半（`FastMCP`，`storyline_server` 也在用它），于是「客户端换库之后
协议还对不对」这件事才有真样本。

它同时是三件保留项的载体：白名单要按名字过滤（得有多于白名单的工具）、身份覆写要有
一份声明了 `user_id`/`conversation_id` 的入参 schema、显式调用附注要有契约。

**这不是测试**，是一个被测试拉起的子进程服务端：
    python tests/mcp_stub_server.py          # 直接跑会一直等 stdin（正常的 stdio 行为）

工具集刻意与 `tests/test_mcp.py` 的内存替身同名同形（`search_repositories` /
`create_issue` / `delete_repo`），这样「换传输前后同样能连上」这句判据可以被
**同一组断言**读出来。
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-stub")


@mcp.tool(title="搜索仓库")
def search_repositories(query: str, max_results: int = 10) -> str:
    """Search GitHub repositories."""
    return f"found:{query} x{max_results}"


@mcp.tool(title="开 issue")
def create_issue(title: str) -> str:
    """Open an issue."""
    return f"opened:{title}"


@mcp.tool(title="删仓库")
def delete_repo(name: str) -> str:
    """Delete a repo (danger)."""
    return f"deleted:{name}"


@mcp.tool(title="回显身份")
def whoami(query: str, user_id: str = "", conversation_id: str = "") -> str:
    """回显服务端收到的身份两栏（身份覆写断言读它）。

    它声明了 ``user_id`` / ``conversation_id``，正是 ``MCPTool.execute`` 允许
    覆写的那两个键；没声明的 server 不会被塞陌生参数。
    """
    return f"identity:{user_id}/{conversation_id}"


@mcp.tool(title="回显任意参数")
def echo_args(a: str = "", b: str = "") -> str:
    """把收到的两个参数回显（并发串台断言用）。

    刻意收两个具名参数而不是 ``**kwargs``：FastMCP 对 ``**kwargs`` 生成的是
    「必填的 kwargs 字段」schema，调用方传 ``{"a": 1}`` 会直接校验失败——
    那不是 MCP 客户端的问题，是桩自己写歪了。
    """
    return f"args:a={a},b={b}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
