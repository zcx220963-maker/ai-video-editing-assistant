"""DAG 编排引擎验证（不联网）：Store 总线 + BaseNode 链路 + 拦截器递归补齐 + 环检测 + 产物落 artifacts 表。

运行：  python tests/test_orchestration.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.orchestration import (
    ArtifactStore,
    BaseNode,
    Interceptor,
    MissingNode,
    NodeRegistry,
    NodeState,
    Store,
    StoreManager,
    detect_cycle,
    topological_order,
)
from agent_framework.storage import build_storage

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


# ---- 一组剪辑 DAG 节点（mock process）----

class LoadMediaNode(BaseNode):
    name = "load_media"
    description = "加载素材，得到 clips 基础信息"

    async def process(self, state, inputs):
        return {"clips": [{"id": "c1"}, {"id": "c2"}]}


class UnderstandClipsNode(BaseNode):
    name = "understand_clips"
    required_nodes = ["load_media"]
    description = "为每个片段生成描述"

    async def process(self, state, inputs):
        clips = inputs["load_media"]["clips"]
        return {"captions": [f"cap:{c['id']}" for c in clips]}


class GroupClipsNode(BaseNode):
    name = "group_clips"
    required_nodes = ["understand_clips"]

    async def process(self, state, inputs):
        caps = inputs["understand_clips"]["captions"]
        return {"groups": [{"group_id": "g1", "caps": caps}]}


class RenderVideoNode(BaseNode):
    name = "render_video"
    required_nodes = ["group_clips"]

    async def process(self, state, inputs):
        groups = inputs["group_clips"]["groups"]
        return {"video": f"rendered:{len(groups)}group"}


class BadCycleA(BaseNode):
    name = "a"
    required_nodes = ["b"]

    async def process(self, state, inputs):
        return {}


class BadCycleB(BaseNode):
    name = "b"
    required_nodes = ["a"]

    async def process(self, state, inputs):
        return {}


def _build_registry() -> NodeRegistry:
    reg = NodeRegistry()
    LoadMediaNode(registry=reg)
    UnderstandClipsNode(registry=reg)
    GroupClipsNode(registry=reg)
    RenderVideoNode(registry=reg)
    return reg


async def main() -> None:
    # ---- Store 总线 ----
    store = Store()
    await store.put("load_media", {"clips": [1, 2]})
    check(await store.has("load_media")
          and (await store.get("load_media"))["clips"] == [1, 2],
          "Store 按节点名存取 payload")
    mgr = StoreManager()
    check(mgr.get("x") is not mgr.get("y"), "不同 session 的 Store 相互隔离")
    check(mgr.get("x") is mgr.get("x"), "同一 session 复用同一 Store")

    # ---- BaseNode 单次执行链路与入库 ----
    reg = _build_registry()
    state = NodeState(session_id="u:c", artifact_id="art1", user_request="剪个旅行vlog")
    loaded = await reg.get("load_media")(state)
    check(loaded["node"] == "load_media" and loaded["artifact_id"] == "art1",
          "BaseNode.__call__ 打包输出返回客户端格式")
    check(await state.store.has("load_media") and (await state.store.get("load_media"))["clips"],
          "节点结果自动写入 Store 供下游读取")
    check(state.summary.calls == 1, "执行计数随调用累加")

    # ---- DAG 工具：拓扑序 + 环检测 ----
    order = topological_order(reg)
    check(order.index("load_media") < order.index("understand_clips")
          < order.index("group_clips") < order.index("render_video"),
          f"拓扑序满足依赖: {order}")
    cyc = NodeRegistry()
    BadCycleA(registry=cyc)
    BadCycleB(registry=cyc)
    check(sorted(detect_cycle(cyc) or []) == ["a", "b"], "detect_cycle 找出成环节点")
    try:
        topological_order(cyc)
        check(False, "存在环时拓扑排序应报错")
    except ValueError:
        check(True, "存在环时拓扑排序抛 ValueError")

    # ---- 拦截器：直接选 render_video，递归补齐整条依赖链 ----
    state2 = NodeState(session_id="u:c2")
    itc = Interceptor(reg)
    result = await itc.invoke("render_video", state2)
    check(itc.order_trace == ["load_media", "understand_clips", "group_clips", "render_video"],
          f"拦截器按 DAG 递归补齐并保序: {itc.order_trace}")
    check(result["output"]["video"] == "rendered:1group", "最终节点产出正确")
    check(await state2.store.executed() == ["load_media", "understand_clips",
                                            "group_clips", "render_video"],
          "全链路输出都进了 Store")

    # ---- 终止点不固定：只走到 group_clips，不渲染 ----
    state3 = NodeState(session_id="u:c3")
    itc3 = Interceptor(reg)
    await itc3.invoke("group_clips", state3)
    check("render_video" not in await state3.store.snapshot(),
          "未选 render_video 时绝不主动渲染")
    check(itc3.order_trace == ["load_media", "understand_clips", "group_clips"],
          "补齐只到被选节点为止")

    # ---- 依赖已满足时不重复执行 ----
    state4 = NodeState(session_id="u:c4")
    await reg.get("load_media")(state4)
    itc4 = Interceptor(reg)
    await itc4.invoke("understand_clips", state4)
    check(itc4.order_trace == ["understand_clips"], f"已执行过的依赖不重放: {itc4.order_trace}")

    # ---- 未注册节点 / 缺失依赖 ----
    try:
        await Interceptor(reg).invoke("nope", NodeState(session_id="z"))
        check(False, "未注册节点应报错")
    except MissingNode:
        check(True, "invoke 未注册节点抛 MissingNode")

    empty_reg = NodeRegistry()
    lonely = GroupClipsNode(registry=empty_reg)  # 依赖未注册
    try:
        topological_order(empty_reg)
        check(False, "依赖未注册应报错")
    except ValueError:
        check(True, "依赖未注册时建图报错")

    # ---- ArtifactStore：产物落 artifacts 表（取代 FileStore 目录树） ----
    st = build_storage("memory")
    repo = st.artifacts("u:cf", "art9")
    fs = await ArtifactStore.open(repo, "u:cf", "art9")
    st5 = NodeState(session_id="u:cf", artifact_id="art9", store=fs)
    itc5 = Interceptor(_build_registry())
    await itc5.invoke("render_video", st5)
    row = await st.db.get_by_pk("artifacts", {"session_id": "u:cf", "node": "render_video",
                                              "artifact_id": "art9"})
    check(row is not None and row["payload"]["video"] == "rendered:1group",
          "产物按 (session_id, node, artifact_id) 落表，payload 原样往返")
    check((await fs.meta("group_clips"))["artifact_id"] == "art9"
          and await fs.meta("nope") is None,
          "meta() 暴露上游 artifact_meta（存在才可见）")
    fs2 = await ArtifactStore.open(repo, "u:cf", "art9")
    check(await fs2.has("load_media") and await fs2.get("render_video") == row["payload"],
          "重启后的 ArtifactStore 从表里扫回全部产物")
    check(await repo.executed() == ["group_clips", "load_media", "render_video",
                                    "understand_clips"],
          "响应拦截器把补齐链上的每个节点产物都持久化")
    other = await ArtifactStore.open(st.artifacts("u:cf", "other"), "u:cf", "other")
    check(not await other.has("render_video"), "artifact_id 不同则产物互不可见")
    await st.close()

    # ---- 请求拦截器：注入上游 payload + 合并 artifact_id/lang + require_prior_kind ----
    class NeedsPriorKind(BaseNode):
        name = "needs_prior"
        require_prior_kind = ["understand_clips"]  # 不走 required_nodes，也要校验上游产物

        async def process(self, state, inputs):
            return {
                "got_upstream": "understand_clips" in inputs,
                "artifact_id": inputs.get("artifact_id"),
                "lang": inputs.get("lang"),
            }

    reg6 = _build_registry()
    NeedsPriorKind(registry=reg6)
    st6 = NodeState(session_id="u:c6", artifact_id="a6", lang="en")
    itc6 = Interceptor(reg6)
    r6 = await itc6.invoke("needs_prior", st6)
    check(r6["output"]["got_upstream"] is True
          and r6["output"]["artifact_id"] == "a6" and r6["output"]["lang"] == "en",
          "inject_media_content_before 注入上游 payload 并合并 artifact_id/lang")
    check(itc6.order_trace == ["load_media", "understand_clips", "needs_prior"],
          f"require_prior_kind 缺失时递归补齐上游: {itc6.order_trace}")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
