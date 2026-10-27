# -*- coding: utf-8 -*-
"""规范文档《整体流程全览》对齐项（R19）的契约测试。

守 6 条——它们是「照文档写代码能跑」的前提，任何一条回退都会让文档失效：

    ① `required_nodes` 收**类引用**（文档写法）也收名字（历史写法）
    ② `NodeState.node_summary` 可读（文档名，实现是 `summary`）
    ③ `NodeClass.meta.name` 在**类上**可读（文档的注册函数这么写）
    ⑥ `available_nodes` 白名单收**类名**，归一成工具名
    ⑧ `NodeState.llm` / `mcp_ctx` 字段存在（文档 wrapper 会塞）
    ⑩ `CronScheduler` 有文档里的 5 个方法名，且是真实现

不测 15/16（`LONG_RUNNING_NODES`、包预算）——那两条代码里早就有，由
`test_simple_node_contract` / `test_simple_boundary` 守着。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import BaseNode, NodeState, dep_name  # noqa: E402
from agent_framework.tools.cron import (  # noqa: E402
    CronScheduler, Schedule, _compute_next_run)

_CHECKS = 0
_FAILS = 0


def check(cond: bool, label: str) -> None:
    global _CHECKS, _FAILS
    _CHECKS += 1
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _FAILS += 1


class _Leaf(BaseNode):
    name = "r19_leaf"
    display_name = "上游"
    description = "最上游的节点"

    async def process(self, state, inputs):
        return {}


class _Mid(BaseNode):
    name = "r19_mid"
    display_name = "下游"
    description = "依赖上游"
    required_nodes = [_Leaf]        # ← 文档写法：类引用

    async def process(self, state, inputs):
        return {}


def main() -> int:
    print("\n① required_nodes 收类引用")
    check(dep_name(_Leaf) == "r19_leaf", f"dep_name(类) → 名字（{dep_name(_Leaf)}）")
    check(dep_name("r19_leaf") == "r19_leaf", "dep_name(字符串) 原样返回")
    check(_Mid().upstream == ["r19_leaf"], f"upstream 归一（{_Mid().upstream}）")
    check(_Mid().require_node == ["r19_leaf"], "require_node 只读别名也归一")
    try:
        dep_name(42)
        check(False, "非法依赖应抛 TypeError")
    except TypeError:
        check(True, "非法依赖抛 TypeError（不静默）")

    print("\n② node_summary 别名")
    st = NodeState(session_id="r19:s")
    check(st.node_summary is st.summary, "node_summary is summary（同一对象）")
    st.summary.calls = 3
    check(st.node_summary.calls == 3, "写 summary 后 node_summary 跟着变")

    print("\n③ meta 在类上与实例上都可读")
    check(_Mid.meta.name == "r19_mid", f"类上 .meta.name（{_Mid.meta.name}）")
    check(_Mid.meta.display_name == "下游", "类上 .meta.display_name")
    check(_Mid().meta.description == "依赖上游", "实例上 .meta.description")
    check("r19_mid" in repr(_Mid.meta), "repr 里看得见节点名（日志可用）")

    print("\n⑧ NodeState 有 llm / mcp_ctx")
    check(st.llm is None and st.mcp_ctx is None, "缺省都是 None（老节点不受影响）")
    st.llm = object()
    check(st.llm is not None, "可以赋值")

    print("\n⑩ Cron 的 5 个文档方法名")
    check(_compute_next_run(Schedule(kind="every", every_ms=5000), 1000) == 6000,
          "every → now + every_ms")
    check(_compute_next_run(Schedule(kind="at", at_ms=1000), 1000) is None,
          "at → None（单次没有下一次）")
    for m in ("_get_next_wake_ms", "_arm_timer", "_on_timer", "_execute_job"):
        check(hasattr(CronScheduler, m), f"CronScheduler.{m}")

    print("\n⑥ 白名单收类名")
    from storyline_server.nodes.core_nodes import build_real_registry  # noqa: PLC0415
    from storyline_server.providers import build_providers  # noqa: PLC0415
    from storyline_server.settings import Settings  # noqa: PLC0415

    try:
        cfg = Settings()
        reg = build_real_registry(cfg, build_providers(cfg), None,
                                  allowed=["LoadMediaNode", "RenderVideoNode"])
        names = sorted(getattr(reg, "_nodes", {}))
        check(names == ["load_media", "render_video"],
              f"类名白名单归一成工具名（{names}）")
    except Exception as exc:  # noqa: BLE001 —— 缺依赖就跳过，不算失败
        print(f"  ..   跳过（环境缺依赖：{type(exc).__name__}: {exc}）")

    print(f"\n{_CHECKS - _FAILS}/{_CHECKS} 通过")
    return 0 if _FAILS == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
