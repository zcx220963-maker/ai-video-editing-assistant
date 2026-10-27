# -*- coding: utf-8 -*-
"""creator_ops：存量创作者服务的 MCP mock（设计 §17 / R15-c）。

设计把「查询个人信息 / 创作周报 / 创作发布 / 回复粉丝」四类存量接口列为
需要经 MCP 接入的能力。真实端点不在本仓库，这里给一个 **stdio mock**：
工具名、入参、返回形状与设计一致，Agent 可以真的连上并调用；换成真实服务时
只改 ``creator_ops.json`` 的 command/args/url，工具面不变。

四个工具（与设计四类一一对应）：
  get_creator_profile        粉丝数 / 关注数 / 已创作内容
  get_creation_weekly_report 涨粉数 / 访问数 / 赞藏数
  publish_content            定时发布 / 选创作模板
  reply_fan_message          回复粉丝私信 / 评论

运行（stdio）：  python examples/mcp/creator_ops_server.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

#: mock 数据是**确定性**的：同一输入每次回同一份，测试与演示都不该靠随机数。
PROFILE = {
    "creator_id": "creator-mock-001",
    "nickname": "示例创作者",
    "fans": 12_860,
    "following": 231,
    "posts": 148,
    "platform": "xiaohongshu",
}

mcp = FastMCP("creator_ops")


@mcp.tool()
def get_creator_profile(platform: str = "xiaohongshu") -> dict:
    """查询创作者个人信息：粉丝数、关注数、已创作内容数。

    Args:
        platform: 平台标识，默认 xiaohongshu。
    """
    return {**PROFILE, "platform": platform}


@mcp.tool()
def get_creation_weekly_report(week: str = "current") -> dict:
    """查询创作周报：涨粉数、访问数、赞与收藏数。

    Args:
        week: 周标识；current=本周，或形如 2026-W43。
    """
    return {
        "week": week,
        "fans_gained": 326,
        "views": 48_200,
        "likes": 3_140,
        "collects": 1_275,
    }


@mcp.tool()
def publish_content(title: str, content: str, publish_at: str = "",
                    template_id: str = "") -> dict:
    """发布或定时发布一条内容。

    Args:
        title: 标题。
        content: 正文（含话题标签）。
        publish_at: ISO8601 定时发布时刻；空=立即发布。
        template_id: 创作模板 id；空=不使用模板。
    """
    when = publish_at or datetime.now(timezone.utc).isoformat()
    return {
        "status": "scheduled" if publish_at else "published",
        "post_id": "post-mock-0001",
        "publish_at": when,
        "template_id": template_id,
    }


@mcp.tool()
def reply_fan_message(message_id: str, content: str, kind: str = "comment") -> dict:
    """回复粉丝私信或评论。

    Args:
        message_id: 私信/评论 id。
        content: 回复内容。
        kind: comment=评论，dm=私信。
    """
    if kind not in ("comment", "dm"):
        raise ValueError("kind 只能是 comment 或 dm")
    return {
        "status": "replied",
        "reply_id": f"reply-{message_id}",
        "kind": kind,
        "replied_at": datetime.now(timezone.utc).isoformat(),
        "echo": content,
    }


if __name__ == "__main__":
    # 默认 stdio：MCP 客户端按配置拉起本进程，逐行 JSON-RPC。
    mcp.run()
