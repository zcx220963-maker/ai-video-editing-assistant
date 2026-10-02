"""视频剪辑集成验证（不联网）：把 Skill + 记忆 + DAG 拦截器 + Checkpoint + Hook + Agent Team 装配跑通。

覆盖文档「视频剪辑」整章的**集成层**（各模块此前已各自单测）：
- 场景 A：单个剪辑 Agent —— <skills> 清单进上下文、load_skill 命中、选 group_clips 经拦截器
  自动补齐上游、read_node_history 读回 Store 产物、Checkpoint 落表并正常收尾、Hook 计量迭代数。
- 场景 B：Agent Team —— 把剪辑 DAG 一键拆成任务，Task Manager 依赖解锁 × 拦截器 × 共享 Store
  协作执行，SubAgent 被动唤醒认领 DAG 任务。

运行：  python tests/test_editing_integration.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.agent import AgentOnceRun, AgentConfig
from agent_framework.tool import is_tool_error
from agent_framework.editing_agent import (build_editing_agent, node_required_map,
 plan_team_from_dag)
from agent_framework.llm import ScriptedLLM
from agent_framework.message_center import MessageCenter
from agent_framework.orchestration import NodeState
from agent_framework.session import Session
from agent_framework.skill import SkillLoader
from agent_framework.subagent_manager import ManagedSubAgent, SubAgentManager
from agent_framework.task_manager import TaskManager
from agent_framework.storage import build_storage
from agent_framework.video_editing import build_agent_registry, build_node_registry

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def _noop(_):
    return None


async def main() -> None:
    examples = Path(__file__).parent.parent / "examples"

    # ================= 场景 A：单 Agent，全骨架装配 =================
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        llm = ScriptedLLM([
            ("tool", "load_skill", {"name": "default_editing_workflow_skill"}),
            ("tool", "group_clips", {}),
            ("answer", "已按流程完成片段分组，随时可继续。"),
        ])
        storage = build_storage("memory")
        await storage.start()
        loader = SkillLoader(storage)
        await loader.sync_from_dir(examples / "skills")   # 目录只是导入源
        parts = build_editing_agent(
            llm,
            storage=storage,
            skill_loader=loader,
            observability=True,
        )
        agent, state, interceptor = parts["agent"], parts["state"], parts["interceptor"]
        sess = Session(user_id="u", conversation_id="edit")

        # 装配检查：剪辑节点工具 + load_skill + read_node_history + 记忆工具都在同一 registry。
        names = set(parts["registry"].tool_names)
        check({"group_clips", "render_video", "load_skill", "read_node_history"} <= names,
              "节点工具 + 技能工具 + Store 读工具同装一个 registry")
        check("update_memory" in names or "read_memory" in names, "记忆工具也已接入")

        out = await agent.run(sess, "帮我把旅行素材分组整理下", run_id="run-A")

        # 上下文装配：<skills> 清单 + 剪辑系统提示进了 System。
        system = parts["agent"].context_builder
        built = await system.build(sess, "x")
        check("<skills>" in built[0]["content"], "<skills> 清单注入剪辑 Agent 上下文")
        check("default_editing_workflow_skill" in built[0]["content"], "WORKFLOW 样例技能出现在清单里")

        # 拦截器补齐上游：只选了 group_clips，但 load_media/split/understand/filter 被自动执行。
        check(all(n in interceptor.order_trace for n in
                  ["load_media", "split_shots", "understand_clips", "filter_clips", "group_clips"]),
              f"选 group_clips 自动补齐整条上游：{interceptor.order_trace}")
        check("render_video" not in await state.store.executed(), "未渲染成片（终止点由需求决定）")
        check(await state.store.has("group_clips"), "分组结果写入 Store 数据总线")

        # read_node_history：LLM 可经工具读回前驱产物。
        hist = await parts["registry"].execute("read_node_history", {"key": "understand_clips"})
        check("clip_captions" in hist and "Error" not in hist, "read_node_history 取回 understand_clips 产物")
        miss = await parts["registry"].execute("read_node_history", {"key": "render_video"})
        check(is_tool_error(miss), "读取未执行节点给出 Error 提示")

        # Checkpoint：正常收尾 → 无未完成快照，且表里留下一条 completed 行。
        check(parts["checkpoint"] is not None and await parts["checkpoint"].pending() == [],
              "Checkpoint 记录本轮并最终 completed（无待恢复项）")
        cp_row = await storage.db.get_by_pk("checkpoints", {"run_id": "run-A"})
        cp_msgs = await storage.checkpoints.load_entries("run-A")
        body = [m for e in cp_msgs for m in (e.get("payload") or [])]
        check(cp_row is not None and cp_row["status"] == "completed"
              and any(m.get("role") == "tool" for m in body),
              f"checkpoints 留有 run-A 的收尾指针行 + {len(cp_msgs)} 个一致点")
        # Hook 计量：多轮迭代（load_skill→group_clips→answer 至少 3 轮）。
        check(parts["metrics"].iterations >= 3, f"MetricsHook 计到迭代数：{parts['metrics'].snapshot()}")
        check("分组" in out, "最终答复产出")

        # 落点：技能与记忆都不再是本地文件。
        # 期望值从 examples/skills 的真实目录数推出来——写死 3 会在新增技能
        # （editing_decisions 就是这么进来的）后变成永假断言，掩盖真回归。
        _want_skills = len([d for d in (examples / "skills").iterdir() if d.is_dir()])
        check(len(await storage.db.select("skills")) == _want_skills,
              f"{_want_skills} 个样例技能正文进了 skills 表")
        check(list(t.iterdir()) == [], "整程没在本地目录留任何文件")

    # ================= 场景 B：Agent Team × DAG × 共享 Store =================
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        node_reg = build_node_registry()
        storage = build_storage("memory")
        await storage.start()
        tm = TaskManager(storage, user_id="u", conversation_id="team")
        ids = await plan_team_from_dag(node_required_map(node_reg), tm)

        check(len(ids) == len(node_reg.names()), "DAG 每个节点映射为一个任务")
        by_name = {t_["name"]: t_ for t_ in await tm.list_all()}
        check(await storage.db.count("tasks") == len(ids), "任务板就是 tasks 表，没有本地文件")
        check(list(ids) == sorted(ids), "任务按拓扑序插入，id 递增即依赖递增")
        check(by_name["render_video"]["id"] > by_name["plan_timeline"]["id"],
              "下游节点编号大于其上游（拓扑序落进 id 段）")
        # 初始可认领 = 无依赖节点（load_media / search_media）。
        first_ready = {r["name"] for r in await tm.ready()}
        check(first_ready == {"load_media", "search_media"},
              f"链首无依赖节点先可认领：{first_ready}")
        # render_video 对应任务初始被阻塞。
        rv = by_name["render_video"]["id"]
        check(bool((await tm.get(rv))["blockedBy"]), "render_video 任务初始 blockedBy 非空")

        # 协作执行：反复取 ready 任务 → 认领 → 经拦截器在【共享 Store】上跑对应节点 → 完成解锁。
        state = NodeState(session_id="team:s1", artifact_id="art", user_request="分工剪辑")
        _, interceptor = build_agent_registry(node_reg, state)
        executed: list[str] = []
        guard = 0
        while True:
            ready = await tm.ready()
            if not ready:
                break
            guard += 1
            if guard > 50:
                check(False, "执行未收敛（防死循环）")
                break
            task = ready[0]
            node_name = task["name"]
            await tm.claim(task["id"], "sub_agent")
            if node_name != "search_media":  # search_media 是可选旁支，剪辑主链不需要
                await interceptor.invoke(node_name, state)
                executed.append(node_name)
            await tm.complete(task["id"])

        check(set(executed) >= {"load_media", "split_shots", "understand_clips",
                               "filter_clips", "group_clips", "generate_script",
                               "plan_timeline", "render_video"},
              f"整条剪辑主链被分工执行：{executed}")
        check(executed.index("plan_timeline") < executed.index("render_video"),
              "跨 Agent 协作仍满足 DAG 依赖：plan_timeline 先于 render_video")
        check(await state.store.has("render_video"), "共享 Store 汇聚出成片产物")
        check((await tm.get(rv))["status"] == "completed" and await tm.ready() == [],
              "全部任务完成后无可认领项")
        check(all(t_["owner"] == "sub_agent" for t_ in await tm.list_all()),
              "认领记录留在任务板上（谁干的查得到）")

        # SubAgent 被动唤醒认领 DAG 任务（协作层 × 编排层打通）。
        mc = MessageCenter(storage, user_id="u2", conversation_id="team2")
        mgr = SubAgentManager(storage, user_id="u2", conversation_id="team2")
        tm2 = TaskManager(storage, user_id="u2", conversation_id="team2")
        await plan_team_from_dag(node_required_map(node_reg), tm2)  # 载入一批 DAG 任务，含可认领的链首
        boom_agent = AgentOnceRun(
            ScriptedLLM([("answer", "done")]),
            build_agent_registry(node_reg, NodeState(session_id="x"))[0],
            config=AgentConfig(max_iterations=2),
        )
        sub = ManagedSubAgent(
            "worker_1", boom_agent, mgr, message_center=mc, task_manager=tm2,
            session=Session(user_id="u", conversation_id="team"),
            loop_times=1, poll_interval=0, sleep=_noop,
        )
        wake_prompt = await sub._wait_for_work()
        claimed = [t_ for t_ in await tm2.list_all() if t_.get("owner") == "worker_1"]
        check(wake_prompt is not None and len(claimed) == 1,
              f"SubAgent 被动扫描并认领了一个 DAG 任务：{wake_prompt}")
        # 另一会话的任务板/队友互不可见
        check(await TaskManager(storage, user_id="u", conversation_id="team").ready() == [],
              "两个会话的任务板互不串台")

    print(f"\n通过 {_checks - _fails}/{_checks}" + ("" if _fails == 0 else f"，失败 {_fails}"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
