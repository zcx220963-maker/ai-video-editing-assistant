"""视频剪辑编排验证（不联网）：DAG 拓扑 + Storyline TOML 解析 + 拦截器补齐整链 + 早停 + Skill 发现。

运行：  python tests/test_video_editing.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentOnceRun, AgentConfig
from agent_framework.llm import ScriptedLLM
from agent_framework.session import Session
from agent_framework.skill import SkillLoader
from agent_framework.storage import build_storage
from agent_framework.tool import conflicts, plan_batches
from agent_framework.video_editing import (
    ALL_NODE_CLASSES,
    build_agent_registry,
    build_node_registry,
    load_storyline_config,
    storyline_available_nodes,
    storyline_server_url,
    validate_dag,
)
from agent_framework.orchestration import NodeState

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


EXAMPLES = Path(__file__).parent.parent / "examples"


async def main() -> None:
    # ---- 1. DAG 拓扑：全量节点可排序、render_video 在链尾 ----
    reg = build_node_registry()
    order = validate_dag(reg)
    check(len(order) == len(ALL_NODE_CLASSES), f"DAG 全序包含 {len(ALL_NODE_CLASSES)} 个节点")
    check(order.index("load_media") < order.index("split_shots") < order.index("group_clips"),
          "前驱先于后继：load_media < split_shots < group_clips")
    check(order.index("plan_timeline") < order.index("render_video"), "plan_timeline 先于 render_video")
    check(order[-1] == "render_video", f"拓扑终点是 render_video（实为 {order[-1]}）")

    # ---- 1b. 流程图节点全集落地（含 plan_timeline_pro；select_BGM 来自文档文字版）----
    doc_nodes = {
        "load_media", "search_media", "split_shots", "asr", "correct_transcript",
        "speech_rough_cut",
        "generate_ai_transition", "understand_clips", "filter_clips", "group_clips",
        "generate_script", "script_template_rec", "generate_voiceover",
        "select_BGM", "transition_rec", "text_rec", "plan_timeline",
        "plan_timeline_pro", "plan_timeline_ai_transition", "render_video",
    }
    check(len(doc_nodes) == 20, "流程图白名单共 20 个节点")
    check(set(reg.names()) == doc_nodes, f"节点全集已注册（缺 {doc_nodes - set(reg.names())}）")

    # ---- 1c. 新节点也受拦截器 DAG 约束 ----
    state_ai = NodeState(session_id="u0:c1", artifact_id="art0", user_request="带 AI 转场")
    reg_ai, inter_ai = build_agent_registry(build_node_registry(), state_ai)
    await reg_ai.get("plan_timeline_ai_transition").execute()
    check("generate_ai_transition" in inter_ai.order_trace
          and inter_ai.order_trace.index("generate_ai_transition")
          < inter_ai.order_trace.index("plan_timeline_ai_transition"),
          "选 AI 时间线自动补齐 generate_ai_transition")
    await reg_ai.get("speech_rough_cut").execute()
    check("asr" in inter_ai.order_trace, "语音粗剪自动补齐前置 asr")
    await reg_ai.get("plan_timeline_pro").execute()
    tp = inter_ai.order_trace.index("plan_timeline_pro")
    check("transition_rec" in inter_ai.order_trace and "text_rec" in inter_ai.order_trace
          and inter_ai.order_trace.index("transition_rec") < tp
          and inter_ai.order_trace.index("text_rec") < tp,
          "专业版时间线自动补齐 transition_rec / text_rec")
    check("script_template_rec" in inter_ai.order_trace
          and inter_ai.order_trace.index("script_template_rec")
          < inter_ai.order_trace.index("generate_script"),
          "脚本推荐先于文案生成（script_template_rec → generate_script）")

    # ---- 2. Storyline TOML 解析（标准库 tomllib，不联网）----
    cfg = load_storyline_config(EXAMPLES / "storyline" / "config.toml")
    allowed = storyline_available_nodes(cfg)
    check("load_media" in allowed and "render_video" in allowed, "TOML 白名单含首尾节点")
    check(storyline_server_url(cfg) == "http://127.0.0.1:8001/mcp", f"服务地址拼接：{storyline_server_url(cfg)}")
    subset = build_node_registry(allowed=allowed)
    check(set(subset.names()) == set(allowed) - {"read_node_history", "render_status"},
          "按白名单实例化节点子集（read_node_history/render_status 为远程节点）")

    # ---- 3. 拦截器补齐整链：LLM 只选 render_video，上游全链被自动执行 ----
    state = NodeState(session_id="u1:c1", artifact_id="art", user_request="剪个旅行vlog")
    registry, interceptor = build_agent_registry(build_node_registry(), state)
    # 并发契约：节点工具声明状态键读写集，调度据此精确拆批，不再靠「写工具一律串行」。
    rv = registry.get("render_video")
    check(rv is not None, "render_video 已注册为工具")
    check(rv.writes == frozenset({"store:render_video"})
          and rv.reads == frozenset({"store:plan_timeline"}),
          f"render_video 声明读写集：{sorted(rv.reads)} → {sorted(rv.writes)}")
    check(rv.concurrency_safe, "声明了写集即可并发（冲突交给 conflicts 精确判定）")
    check(conflicts(registry.get("select_BGM"), registry.get("plan_timeline")),
          "plan_timeline 读 select_BGM 的写集 → 判为冲突")
    batches = plan_batches(["select_BGM", "plan_timeline", "generate_voiceover"], registry.get)
    check(batches == [["select_BGM", "generate_voiceover"], ["plan_timeline"]],
          f"撞键的调用退到下一批，互不影响的仍同批：{batches}")

    llm = ScriptedLLM([
        ("tool", "render_video", {}),
        ("answer", "已按流程渲染成片"),
    ])
    agent = AgentOnceRun(llm, registry, config=AgentConfig(max_iterations=5))
    result = await agent.run(Session(user_id="u1", conversation_id="c1"), "剪个旅行vlog")
    check("已按流程渲染成片" in result, "Agent 产出最终答复")
    # 拦截器应把 render_video 的整条上游链补齐后 finally 渲染。
    trace = interceptor.order_trace
    check("load_media" in trace and "plan_timeline" in trace and trace[-1] == "render_video",
          f"选 render_video 触发全链补齐：{trace}")
    check(await state.store.has("render_video"), "成片产物写入 Store")

    # ---- 4. 早停：只要 group_clips，绝不渲染 ----
    state2 = NodeState(session_id="u2:c1", artifact_id="art2", user_request="先分组看看")
    registry2, interceptor2 = build_agent_registry(build_node_registry(), state2)
    llm2 = ScriptedLLM([
        ("tool", "group_clips", {}),
        ("answer", "已分组"),
    ])
    agent2 = AgentOnceRun(llm2, registry2, config=AgentConfig(max_iterations=5))
    r2 = await agent2.run(Session(user_id="u2", conversation_id="c1"), "先分组看看")
    check("已分组" in r2, "早停场景产出分组答复")
    check("group_clips" in interceptor2.order_trace and "render_video" not in interceptor2.order_trace,
          f"仅到 group_clips 即止，未主动渲染：{interceptor2.order_trace}")
    check(await state2.store.has("group_clips") and not await state2.store.has("render_video"),
          "Store 有分组结果、无渲染结果")

    # ---- 5. Skill 发现：导入 examples/skills 后从库里读到 WORKFLOW + CAPABILITY 两类 ----
    st = build_storage("memory")
    await st.start()
    try:
        sloader = SkillLoader(st)
        await sloader.sync_from_dir(EXAMPLES / "skills")
        skills = {s.name: s for s in await sloader.discover()}
    finally:
        await st.close()
    check("default_editing_workflow_skill" in skills, "发现 WORKFLOW 技能")
    check("subtitle_imitation_skill" in skills, "发现 CAPABILITY 技能")
    wf = skills["default_editing_workflow_skill"]
    cp = skills["subtitle_imitation_skill"]
    check(wf.available and cp.available, "两个技能依赖均满足、可用")
    check("WORKFLOW SKILL" in wf.description and "CAPABILITY SKILL" in cp.description,
          "description 携带技能类型标记（触发关键）")
    check("render_video" in wf.body, "WORKFLOW 正文含完整剪辑流程步骤")
    check("generate_script" in cp.body, "CAPABILITY 正文指向 generate_script")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
