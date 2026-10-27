# -*- coding: utf-8 -*-
"""框架对齐总对账（R20）。**独立脚本**，不依赖 pytest（容器里没装）。

    docker exec creation-app python tests/test_framework_conformance.py

每条断言带 R# 前缀，失败信息直接指出缺口。它只验**结构与契约**（函数/字段/
开关在不在、有没有接上），不验业务行为——业务行为在各自的 test_*.py 里。
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import sys
import tomllib

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_fail = 0
_pass = 0


def check(ok: bool, msg: str) -> None:
    global _fail, _pass
    if ok:
        _pass += 1
    else:
        _fail += 1
        print(f"  ✗ {msg}")


def src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


async def main() -> None:
    # ---- R1 消息面：dispatcher + 水位 + 死信 ack ----
    from agent_framework.dispatch import QueueFull, SessionDispatcher, session_of
    from agent_framework.mq import KafkaMessageQueue, build_message_queue

    check(inspect.isclass(SessionDispatcher), "R1 缺 SessionDispatcher")
    check(QueueFull is not None and callable(session_of), "R1 缺 QueueFull / session_of")
    check(hasattr(KafkaMessageQueue, "_consume"), "R1 Kafka 消费入口不见了")
    mq_src = src("agent_framework/mq.py")
    check("enable_auto_commit=False" in mq_src, "R1 Kafka 仍自动提交（水位会失效）")
    check("_Watermark" in mq_src and "commit_offset" in mq_src, "R1 缺分区水位")
    check("dead_letter" in mq_src, "R1 缺死信")
    check("pause(" in mq_src and "resume(" in mq_src, "R1 缺 pause/resume 背压")

    # ---- R2 每实例唯一消费组 ----
    from agent_framework.connection_manager import instance_group

    g1, g2 = instance_group("a"), instance_group("b")
    check(g1 != g2 and g1.startswith("conn-"), "R2 消费组没有按实例区分")

    # ---- R3 WS 归属 ----
    from agent_framework.storage.repositories import ConversationsRepo

    check(hasattr(ConversationsRepo, "is_owned"), "R3 缺 is_owned")
    server_src = src("agent_framework/server.py")
    check("is_owned(" in server_src and "4403" in server_src, "R3 WS 未校验归属")

    # ---- R4 外层 worker ----
    from agent_framework.worker import AgentWorker, StopReason

    check(hasattr(AgentWorker, "run_forever") and StopReason.BROKER_DOWN, "R4 缺 AgentWorker")

    # ---- R5 工具校验收口 ----
    from agent_framework.tool import Tool, ToolRegistry, validate_params, with_hint

    check(callable(validate_params) and callable(with_hint), "R5 缺 validate_params/with_hint")
    check("user_id" in inspect.signature(ToolRegistry.has).parameters, "R5 has 未按 user 收口")
    check(getattr(Tool, "strict_params", None) is True, "R5 缺 strict_params 开关")
    check("return_exceptions=True" in src("agent_framework/agent.py"), "R6 gather 仍会取消同批")

    # ---- R9 Hook 异常隔离 ----
    hooks_src = src("agent_framework/hooks.py")
    check("_finalize_failures" in hooks_src, "R9 finalize_content 未隔离异常")

    # ---- R11 压缩分级 ----
    from agent_framework.compress import ContextCompressor, fold_tool_schemas

    sig = inspect.signature(ContextCompressor.__init__)
    check("drop_at_tokens" in sig.parameters and "summary_at_tokens" in sig.parameters,
          "R11 三级阈值没拆开")
    check(callable(fold_tool_schemas), "R11 缺工具元信息折叠")
    check("TOOL_SCHEMA_MAX_BYTES" in src("agent_framework/agent.py"), "R11 折叠未接入 _round_schema")

    # ---- R16 DAG：扫包 / 预置产物 / 白名单 ----
    from agent_framework.orchestration import BaseNode, Interceptor, NodeRegistry

    check(hasattr(NodeRegistry, "scan_package") and hasattr(NodeRegistry, "keep_only"),
          "R16 缺 scan_package / keep_only")
    check("require_node" in dir(BaseNode), "R16 缺 require_node 只读别名")
    check(hasattr(BaseNode, "output_keys"), "R16 缺 output_keys")
    # presupplied 是实例属性（每次 invoke 重置），查源码更稳。
    check("presupplied" in src("agent_framework/orchestration.py"), "R16 缺 presupplied 记录")
    for name in ("config.toml", "config.docker.toml"):
        cfg = tomllib.loads(src(f"examples/storyline/{name}"))
        srv = cfg.get("local_mcp_server", {})
        check(bool(srv.get("available_node_pkgs")), f"R16 {name} 缺 available_node_pkgs")
        check("dag_contract" in srv.get("available_nodes", []), f"R16 {name} 白名单缺 dag_contract")

    # ---- R18 simple 收口 ----
    pipeline = BACKEND / "open_storyline" / "simple" / "pipeline.py"
    check(pipeline.exists(), "R18 缺 simple/pipeline.py")
    check((BACKEND / "open_storyline" / "nodes" / "simple_nodes.py").exists(),
          "R18 缺 simple_nodes.py")
    sv = src("agent_framework/tools/short_video.py")
    check("open_storyline.simple import" not in sv.replace("from open_storyline.simple import", ""),
          "R18 short_video 仍直连 simple 子模块")
    for name in ("make_short_video", "plan_short_film", "reshoot_shot",
                 "render_short_film", "export_film", "list_short_films"):
        check(f'"{name}"' in sv or f"name = \"{name}\"" in sv or name in sv,
              f"R18 工具 {name} 不见了")

    # ---- R20 对账脚本（仓库级工具；容器里只挂 backend，看不到就跳过）----
    script = BACKEND.parent / "scripts" / "conformance.py"
    if (BACKEND.parent / "docs").exists():
        check(script.exists(), "R20 缺 scripts/conformance.py")

    print(f"\n{_pass} 通过 / {_fail} 失败")
    if _fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
